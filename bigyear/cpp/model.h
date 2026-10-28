#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <memory>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>
#include "bounded_roads.h"
#include "compact_catalog.h"


struct Plan { int days; double minutes, miles; };
struct Score { bool feasible=false; int species=0; double minutes=0, miles=0; };
inline bool better(const Score& a,const Score& b) {
    if(a.feasible!=b.feasible) return a.feasible;
    if(a.species!=b.species) return a.species>b.species;
    if(std::abs(a.minutes-b.minutes)>1e-7) return a.minutes<b.minutes;
    return a.miles+1e-7<b.miles;
}
struct Problem {
    int days,nloc,nspp,nopp,start; double local,activity,drive,bird;
    OpportunityCatalog opp;
    std::shared_ptr<void> coordinate_owner;const double* coordinates=nullptr;bool lazy_walking=false;
    double maximum_anchor_gap_miles=INFINITY;
    bool flights=false;
    bool mixed_road_birding=false;
    double minimum_flight_minutes=0;
    std::vector<double> miles,minutes;
    std::vector<std::vector<Plan>> plans;
    std::shared_ptr<void> matrix_owner;
    const double* matrix=nullptr;
    int matrix_n=0;
    std::vector<int> matrix_nodes;
    std::shared_ptr<BoundedRoadClient> bounded;
    std::vector<double> latitude,longitude;
    std::vector<double> stop_minutes;
    // Variant zero is an ordinary land visit. A charter starts/ends at its
    // port; its exact observation track is separately frozen and audited.
    std::vector<double> variant_activity{0},variant_sailing{0};
    double road_miles(int a,int b) const {
        if(bounded)return bounded->get(a,b).miles;
        return matrix ? matrix[(size_t(matrix_nodes[a])*matrix_n+matrix_nodes[b])*2] : miles[size_t(a)*nloc+b];
    }
    double road_minutes(int a,int b) const {
        if(bounded)return bounded->get(a,b).minutes;
        return matrix ? matrix[(size_t(matrix_nodes[a])*matrix_n+matrix_nodes[b])*2+1] : minutes[size_t(a)*nloc+b];
    }
    void best_transition(int a,int b,int gap,double& best_t,double& best_m,int previous=-1,int target=-1) const {
        if(flights) {
            auto plan=bounded->transition(a,b,previous,target);
            if(plan.days<=gap&&(plan.minutes<best_t-1e-7||(std::abs(plan.minutes-best_t)<1e-7&&plan.miles<best_m))){best_t=plan.minutes;best_m=plan.miles;}
            return;
        }
        if(matrix || bounded) {
            double t=road_minutes(a,b),m=road_miles(a,b);
            double needed=std::max(m>0?1.0:0.0,std::ceil(t/drive));
            if(std::isfinite(t) && needed<=gap &&
               (t<best_t-1e-7 || (std::abs(t-best_t)<1e-7 && m<best_m))) {best_t=t;best_m=m;}
        } else for(const auto& p:plans[size_t(a)*nloc+b]) {
            if(p.days<=gap && (p.minutes<best_t-1e-7 || (std::abs(p.minutes-best_t)<1e-7 && p.miles<best_m))) {best_t=p.minutes;best_m=p.miles;}
        }
    }
    double ranking_minutes(int a,int b) const {
        if(!bounded)return road_minutes(a,b);
        // Proposal ranking only. Feasibility always uses measured road costs.
        const double radians=3.14159265358979323846/180;
        double alat=coordinates?coordinates[2*a]:latitude[a], blat=coordinates?coordinates[2*b]:latitude[b];
        double alon=coordinates?coordinates[2*a+1]:longitude[a], blon=coordinates?coordinates[2*b+1]:longitude[b];
        double x=(blat-alat)*radians/2,y=(blon-alon)*radians/2;
        double h=std::sin(x)*std::sin(x)+std::cos(alat*radians)*std::cos(blat*radians)*std::sin(y)*std::sin(y);
        return 3958.7613*2*std::asin(std::sqrt(std::min(1.0,h)))*60/50;
    }
    explicit Problem(const std::string& path) {
        std::ifstream in(path); std::string magic;
        in>>magic>>days>>nloc>>nspp>>nopp>>start>>local>>activity>>drive>>bird;
        mixed_road_birding=!magic.empty() && magic.back()=='M';
        if(mixed_road_birding)magic.pop_back();
        bool walking=!magic.empty() && magic.back()=='W';
        if(walking)magic.pop_back();
        if((magic!="BY1" && magic!="BY2" && magic!="BY3" && magic!="BY4") || nloc<1 || nspp<1 || nopp<1 || days<1) throw std::runtime_error("invalid problem header");
        if(mixed_road_birding && drive>activity)throw std::runtime_error("driving exceeds the shared activity budget");
        if(magic=="BY4") {
            std::string records,species_file,coords,token;size_t entries,capacity;int port;
            in>>std::quoted(records)>>std::quoted(species_file)>>entries>>std::quoted(coords)>>port>>std::quoted(token)>>capacity;
            if(!in)throw std::runtime_error("truncated compact header");
            opp.mapped(records,species_file,nopp,entries,days,nloc,nspp);
            coordinate_owner=map_readonly(coords,size_t(nloc)*16);coordinates=static_cast<const double*>(coordinate_owner.get());
            for(int i=0;i<nloc*2;++i)if(!std::isfinite(coordinates[i]))throw std::runtime_error("nonfinite compact coordinate");
            bounded=std::make_shared<BoundedRoadClient>(port,token,capacity,200);lazy_walking=true;
            std::string extension;
            if(in>>extension){
                int enabled=0;
                if(extension!="SCREEN"||!(in>>maximum_anchor_gap_miles>>enabled>>minimum_flight_minutes)||!std::isfinite(maximum_anchor_gap_miles)||maximum_anchor_gap_miles<0||!std::isfinite(minimum_flight_minutes)||minimum_flight_minutes<0)throw std::runtime_error("invalid conservative screen bound");
                flights=enabled!=0;
                if(in>>extension){
                    std::string file;if(extension!="BOATS"||!(in>>std::quoted(file)))throw std::runtime_error("invalid boat extension");
                    std::ifstream visits(file);size_t count;if(!(visits>>count)||count>size_t(nopp))throw std::runtime_error("invalid boat visit count");
                    for(size_t j=1;j<=count;++j){size_t id;double extra,sail;
                        if(!(visits>>id>>extra>>sail)||id!=j||!std::isfinite(extra)||!std::isfinite(sail)||extra<0||sail<0||sail>extra||bird+extra>activity+1e-7)throw std::runtime_error("invalid boat visit duration");
                        variant_activity.push_back(extra);variant_sailing.push_back(sail);
                    }
                    if(visits>>extension)throw std::runtime_error("unexpected boat visit data");
                    if(in>>extension)throw std::runtime_error("unexpected compact extension");
                }
            }
            for(const auto& o:opp)if(o.variant>=variant_activity.size())throw std::runtime_error("unbound boat variant");
            return;
        }
        opp.owned_records.reserve(nopp);
        for(int i=0;i<nopp;++i) {
            int day,loc,k;in>>day>>loc>>k;
            if(day<0||day>=days||loc<0||loc>=nloc||k<0)throw std::runtime_error("invalid opportunity");
            opp.owned_records.push_back({uint32_t(day),uint32_t(loc),opp.owned_species.size(),uint32_t(k),0});
            for(int j=0;j<k;++j){int sp;in>>sp;if(sp<0||sp>=nspp)throw std::runtime_error("invalid species");opp.owned_species.push_back(sp);}
        }
        opp.owned();
        stop_minutes.assign(nloc,bird);
        if(walking)for(double& duration:stop_minutes) {
            in>>duration;
            if(!in || !std::isfinite(duration) || duration<bird)
                throw std::runtime_error("invalid walking-adjusted stop duration");
        }
        if(magic=="BY3") {
            int port;size_t capacity;std::string token;in>>port>>std::quoted(token)>>capacity;
            bounded=std::make_shared<BoundedRoadClient>(port,token,capacity);
            latitude.resize(nloc);longitude.resize(nloc);
            for(int i=0;i<nloc;++i)in>>latitude[i]>>longitude[i];
            if(!in)throw std::runtime_error("truncated bounded search problem");
            return;
        }
        if(magic=="BY2") {
            std::string file; in>>std::quoted(file)>>matrix_n;
            if(matrix_n<1) throw std::runtime_error("invalid matrix size");
            matrix_nodes.resize(nloc);
            for(int& node:matrix_nodes) {in>>node;if(node<0 || node>=matrix_n) throw std::runtime_error("invalid matrix node");}
            const uint16_t endian=1;
            if(*reinterpret_cast<const uint8_t*>(&endian)!=1 || sizeof(double)!=8) throw std::runtime_error("BY2 requires little-endian float64");
            int fd=open(file.c_str(),O_RDONLY); struct stat st;
            size_t bytes=size_t(matrix_n)*matrix_n*16;
            if(fd<0 || fstat(fd,&st)!=0 || size_t(st.st_size)!=bytes) {if(fd>=0)close(fd);throw std::runtime_error("missing or truncated road matrix");}
            void* data=mmap(nullptr,bytes,PROT_READ,MAP_SHARED,fd,0);close(fd);
            if(data==MAP_FAILED) throw std::runtime_error("cannot map road matrix");
            matrix_owner=std::shared_ptr<void>(data,[bytes](void* p){munmap(p,bytes);});
            matrix=static_cast<const double*>(data);
            if(!in) throw std::runtime_error("truncated search problem");
            return;
        }
        const size_t pairs=size_t(nloc)*nloc;
        miles.assign(pairs,std::numeric_limits<double>::infinity()); minutes=miles; plans.resize(pairs);
        for(int i=0;i<nloc;++i) miles[size_t(i)*nloc+i]=minutes[size_t(i)*nloc+i]=0;
        int n; in>>n;
        for(int k=0;k<n;++k){ int a,b; double m,t; in>>a>>b>>m>>t; miles.at(size_t(a)*nloc+b)=m; minutes.at(size_t(a)*nloc+b)=t; }
        in>>n;
        for(int k=0;k<n;++k){ int a,b,d; double t,m; in>>a>>b>>d>>t>>m; plans.at(size_t(a)*nloc+b).push_back({d,t,m}); }
        if(!in) throw std::runtime_error("truncated search problem");
    }
    void canonical(std::vector<int>& route) const {
        std::sort(route.begin(),route.end(),[&](int a,int b){return opp[a].day==opp[b].day?a<b:opp[a].day<opp[b].day;});
        route.erase(std::unique(route.begin(),route.end()),route.end());
    }
    int region(int location) const {
        if(!coordinates)return 0;
        double lon=coordinates[2*location+1],lat=coordinates[2*location];
        return (lon < -115 ? 0 : lon < -100 ? 1 : lon < -85 ? 2 : 3)*2+(lat>=37);
    }
    double optimistic_miles(int a,int b) const {
        // Reported pins can walk to different anchors. Subtract the largest
        // possible access gap at BOTH endpoints before using a distance bound.
        if(!coordinates)return 0;
        return std::max(0.0,ranking_minutes(a,b)*50/60-2*maximum_anchor_gap_miles);
    }
    bool screen(const std::vector<int>& route,bool airborne=false) const {
        if(!coordinates)return true;
        int here=start,previous=-1;
        for(size_t i=0;i<route.size();) {
            size_t end=i+1;auto first=opp[route[i]];
            while(end<route.size()&&opp[route[end]].day==first.day)++end;
            if(end-i>1)for(size_t j=i;j<end;++j)if(opp[route[j]].variant)return false;
            if(double(end-i)*bird>activity+1e-7)return false;
            double local_bound=0;
            for(size_t j=i+1;j<end;++j)local_bound+=optimistic_miles(opp[route[j-1]].location,opp[route[j]].location);
            bool mixed=mixed_road_birding && first.variant==0;
            // 200 km/h exceeds the frozen car profile's 130-km/h maximum.
            // This is a conservative screen only; measured roads decide.
            double mile_limit=mixed?std::min(drive,activity-double(end-i)*bird)/60*200/1.609344:local;
            if(local_bound>mile_limit+1e-7)return false;
            if(here>=0&&first.day-previous-1==0&&local_bound+optimistic_miles(here,first.location)>mile_limit+1e-7)return false;
            (void)airborne;
            here=opp[route[end-1]].location;previous=first.day;i=end;
        }
        return true;
    }
    Score evaluate(const std::vector<int>& route,int* failure_day=nullptr) const {
        Score result; result.feasible=true;
        int here=start, previous=-1; std::vector<uint64_t> seen((nspp+63)/64,0);
        for(size_t i=0;i<route.size();) {
            size_t end=i+1; const auto& first=opp[route[i]];
            if(failure_day)*failure_day=first.day;
            while(end<route.size() && opp[route[end]].day==first.day) ++end;
            if(end-i>1)for(size_t j=i;j<end;++j)if(opp[route[j]].variant)return {};
            if(first.day<=previous) return {};
            if(here<0) here=first.location; // free location on the experiment's first day
            bool mixed=mixed_road_birding && first.variant==0;
            double internal_m=0, internal_t=0, visit_t=0;
            for(size_t j=i;j<end;++j)visit_t+=(lazy_walking?bounded->stop(opp[route[j]].location):stop_minutes[opp[route[j]].location])+variant_activity[opp[route[j]].variant];
            if(visit_t>activity+1e-7)return {};
            for(size_t j=i+1;j<end;++j){
                int a=opp[route[j-1]].location,b=opp[route[j]].location;
                internal_m+=road_miles(a,b); internal_t+=road_minutes(a,b);
            }
            if((!mixed&&internal_m>local+1e-7) || (mixed&&internal_t>drive+1e-7) || internal_t+visit_t>activity+1e-7) return {};
            double best_t=std::numeric_limits<double>::infinity(),best_m=best_t;
            bool transition_pending=false;
            double mile_limit=mixed?std::min(drive-internal_t,activity-internal_t-visit_t)/60*200/1.609344:local-internal_m;
            if(!flights||optimistic_miles(here,first.location)<=mile_limit+1e-7) {
                try {
                    double rm=road_miles(here,first.location),rt=road_minutes(here,first.location);
                    if((mixed||internal_m+rm<=local+1e-7) && (!mixed||internal_t+rt<=drive+1e-7) && internal_t+rt+visit_t<=activity+1e-7){best_t=rt;best_m=rm;}
                } catch(const DeferredRoad&) {transition_pending=true;}
            }
            int gap=first.day-previous-1;
            if((gap>0 || (!flights&&!std::isfinite(best_t)))&&(!flights||best_t+1e-7>=minimum_flight_minutes)) {
                try {best_transition(here,first.location,gap,best_t,best_m,previous,first.day);}
                catch(const DeferredRoad&) {transition_pending=true;}
            }
            if(!std::isfinite(best_t)) {
                if(transition_pending)throw DeferredRoad("connection alternatives remain unresolved");
                return {};
            }
            result.minutes+=best_t+internal_t; result.miles+=best_m+internal_m;
            for(size_t j=i;j<end;++j)result.minutes+=variant_sailing[opp[route[j]].variant];
            for(size_t j=i;j<end;++j)for(int s:opp[route[j]].species)seen[s/64]|=uint64_t(1)<<(s%64);
            here=opp[route[end-1]].location; previous=first.day; i=end;
        }
        for(auto b:seen) result.species+=__builtin_popcountll(b);
        if(failure_day)*failure_day=-1;
        return result;
    }
};
