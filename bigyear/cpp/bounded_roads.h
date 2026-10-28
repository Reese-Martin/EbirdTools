#pragma once
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/tcp.h>
#include <unistd.h>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <list>
#include <chrono>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>

struct DeferredRoad:std::runtime_error {using std::runtime_error::runtime_error;};
struct RoadCost { double miles,minutes; };
struct TransitCost {int days;double minutes,miles;};
class BoundedRoadClient {
    int port;
    int timeout_seconds;
    std::string token;
    size_t capacity;
    mutable std::mutex mutex;
    mutable std::list<uint64_t> order;
    struct Entry {RoadCost cost; std::list<uint64_t>::iterator position;};
    mutable std::unordered_map<uint64_t,Entry> hot;
    mutable std::unordered_map<std::string,TransitCost> transit_hot;
    mutable std::unordered_map<std::string,std::chrono::steady_clock::time_point> retry_after;
    std::string exchange(const std::string& message) const {
        struct Connection {int fd=-1,port=-1; std::string token; ~Connection(){if(fd>=0)close(fd);}};
        static thread_local Connection connection;
        auto send_all=[](int fd,const std::string& text){
            size_t sent=0;
            while(sent<text.size()) {auto n=send(fd,text.data()+sent,text.size()-sent,0);if(n<=0)throw std::runtime_error("road bridge send failed");sent+=n;}
        };
        if(connection.fd<0 || connection.port!=port || connection.token!=token) {
            if(connection.fd>=0)close(connection.fd);
            connection.fd=socket(AF_INET,SOCK_STREAM,0); connection.port=port; connection.token=token;
            if(connection.fd<0)throw std::runtime_error("cannot create road bridge socket");
            timeval timeout{timeout_seconds,0};
            setsockopt(connection.fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
            setsockopt(connection.fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
            int no_delay=1;setsockopt(connection.fd,IPPROTO_TCP,TCP_NODELAY,&no_delay,sizeof(no_delay));
#ifdef SO_NOSIGPIPE
            int yes=1;setsockopt(connection.fd,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes));
#endif
            sockaddr_in address{};address.sin_family=AF_INET;address.sin_port=htons(port);address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
            if(connect(connection.fd,reinterpret_cast<sockaddr*>(&address),sizeof(address))!=0)throw std::runtime_error("cannot connect to measured road provider");
            send_all(connection.fd,token+"\n");
        }
        send_all(connection.fd,message+"\n");
        std::string line;char c;
        while(line.size()<256){auto n=recv(connection.fd,&c,1,0);if(n!=1)throw std::runtime_error("measured road provider unavailable");if(c=='\n')break;line+=c;}
        ++requests;
        if(line=="DEFER")throw DeferredRoad("road query unresolved; candidate deferred");
        return line;
    }
    RoadCost request(int a,int b) const {
        auto line=exchange(std::to_string(a)+" "+std::to_string(b));
        if(line=="NO")return {INFINITY,INFINITY};
        RoadCost result;std::istringstream input(line);input>>result.miles>>result.minutes;
        if(!input || !std::isfinite(result.miles) || !std::isfinite(result.minutes) || result.miles<0 || result.minutes<0)
            throw std::runtime_error("measured road query failed; no substitute road cost used");
        return result;
    }
public:
    mutable std::atomic<uint64_t> hits{0},requests{0},evictions{0},pending_hits{0};
    BoundedRoadClient(int port_,std::string token_,size_t capacity_,int timeout_seconds_=55):port(port_),timeout_seconds(timeout_seconds_),token(std::move(token_)),capacity(capacity_){
        if(port<1 || port>65535 || !capacity || capacity>1000000)throw std::runtime_error("invalid bounded road provider");
    }
    RoadCost get(int a,int b) const {
        if(a==b && a>=0)return {0,0};
        const uint64_t key=(uint64_t(uint32_t(a))<<32)|uint32_t(b);
        {std::lock_guard<std::mutex> guard(mutex);auto found=hot.find(key);if(found!=hot.end()){
            order.splice(order.begin(),order,found->second.position);++hits;return found->second.cost;}}
        auto pending_key=std::to_string(a)+" "+std::to_string(b);
        {std::lock_guard<std::mutex> guard(mutex);auto found=retry_after.find(pending_key);if(found!=retry_after.end()&&found->second>std::chrono::steady_clock::now()){++pending_hits;throw DeferredRoad("discovery pending; retry later");}}
        RoadCost result;
        try{result=request(a,b);}catch(const DeferredRoad&){std::lock_guard<std::mutex> guard(mutex);if(retry_after.size()>=capacity)retry_after.clear();retry_after[pending_key]=std::chrono::steady_clock::now()+std::chrono::milliseconds(100);throw;}
        {std::lock_guard<std::mutex> guard(mutex);
            auto found=hot.find(key);if(found!=hot.end())return found->second.cost;
            if(hot.size()>=capacity){hot.erase(order.back());order.pop_back();++evictions;}
            order.push_front(key);hot.emplace(key,Entry{result,order.begin()});}
        return result;
    }
    double stop(int location) const {return get(-1,location).minutes;}
    TransitCost transition(int a,int b,int previous,int target) const {
        std::string key=std::to_string(a)+" "+std::to_string(b)+" "+std::to_string(previous)+" "+std::to_string(target);
        {std::lock_guard<std::mutex> guard(mutex);auto found=transit_hot.find(key);if(found!=transit_hot.end())return found->second;}
        {std::lock_guard<std::mutex> guard(mutex);auto found=retry_after.find("T "+key);if(found!=retry_after.end()&&found->second>std::chrono::steady_clock::now()){++pending_hits;throw DeferredRoad("airport discovery pending; retry later");}}
        std::string line;try{line=exchange("T "+key);}catch(const DeferredRoad&){std::lock_guard<std::mutex> guard(mutex);if(retry_after.size()>=capacity)retry_after.clear();retry_after["T "+key]=std::chrono::steady_clock::now()+std::chrono::milliseconds(100);throw;}
        TransitCost result;std::istringstream in(line);in>>result.days>>result.minutes>>result.miles;
        if(!in||result.days<0||!std::isfinite(result.minutes)||!std::isfinite(result.miles)||result.minutes<0||result.miles<0)throw std::runtime_error("invalid measured airport transition");
        {std::lock_guard<std::mutex> guard(mutex);if(transit_hot.size()>=8192)transit_hot.clear();transit_hot[key]=result;}
        return result;
    }
    size_t size() const {std::lock_guard<std::mutex> guard(mutex);return hot.size();}
    size_t limit() const {return capacity;}
};
