#include "model.h"
#include <atomic>
#include <chrono>
#include <filesystem>
#include <functional>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <random>
#include <set>
#include <deque>
#include <array>
#include <cstdlib>
#include <sstream>
#include <thread>
#ifdef __APPLE__
#include <mach/mach_time.h>
#endif
#ifdef BIGYEAR_ORTOOLS
#include "exact.h"
#endif

using Clock=std::chrono::steady_clock;
// Match Python's monotonic budget on macOS: suspended time is not search time.
// libc++ steady_clock advances during suspend on this platform.
inline double active_search_seconds() {
#ifdef __APPLE__
    static const double scale=[](){mach_timebase_info_data_t info;mach_timebase_info(&info);return double(info.numer)/info.denom*1e-9;}();
    return double(mach_absolute_time())*scale;
#else
    return std::chrono::duration<double>(Clock::now().time_since_epoch()).count();
#endif
}
struct ArchiveEntry {Score score; std::vector<int> route;};
int main(int argc,char**argv) {
 try {
    if(argc<9) throw std::runtime_error("usage: engine problem output seconds threads seed alternatives near exact[0/1/2] [resume]");
    Problem p(argv[1]); const std::string output=argv[2];
    double seconds=std::stod(argv[3]); int threads=std::stoi(argv[4]), seed=std::stoi(argv[5]);
    int alternatives=std::stoi(argv[6]),near=std::stoi(argv[7]), exact=std::stoi(argv[8]);
    std::filesystem::create_directories(output);
    auto started=active_search_seconds();
    auto elapsed=[&](){return active_search_seconds()-started;};
    std::mutex lock; Score best; std::vector<int> winning;
    std::vector<ArchiveEntry> archive;
    std::atomic<uint64_t> evaluations{0},starts{0},deferred{0},screened{0},proposals{0},unchanged{0};
    std::atomic<uint64_t> block_proposals{0},block_feasible{0};
    std::atomic<uint64_t> date_moves{0},date_moves_changed{0},nearby_date_moves{0},pending_pauses{0};
    struct TripBlock {int begin,end;std::vector<int> route;};
    std::vector<TripBlock> blocks;
    std::vector<int> required;
    if(const char* path=std::getenv("BIGYEAR_REQUIRED_OPPORTUNITIES")) {
        std::ifstream source(path);int id;
        if(!source)throw std::runtime_error("required visit file unavailable");
        while(source>>id){if(id<0||id>=p.nopp)throw std::runtime_error("invalid required visit");required.push_back(id);}
        if(!source.eof())throw std::runtime_error("malformed required visit file");
        p.canonical(required);
    }
    auto anchored=[&](const std::vector<int>& route){for(int id:required)if(std::find(route.begin(),route.end(),id)==route.end())return false;return true;};
    if(const char* path=std::getenv("BIGYEAR_BLOCK_PROPOSALS")) {
        std::ifstream source(path);if(!source)throw std::runtime_error("regional proposal library unavailable");
        std::string line;
        while(std::getline(source,line)) {
            std::istringstream row(line);TripBlock block;int count,id;
            if(!(row>>block.begin>>block.end>>count)||block.begin<0||block.end>p.days||block.begin>=block.end||count<1||count>32||blocks.size()>=512)
                throw std::runtime_error("invalid bounded regional trip proposal");
            for(int i=0;i<count;++i){if(!(row>>id)||id<0||id>=p.nopp||p.opp[id].day<block.begin||p.opp[id].day>=block.end)throw std::runtime_error("regional trip differs from source calendar");block.route.push_back(id);}
            p.canonical(block.route);blocks.push_back(std::move(block));
        }
    }
    std::array<std::atomic<uint64_t>,8> region_proposals{},region_starts{};
    uint64_t support_index_bytes=0,date_index_bytes=0;
    std::ofstream history(output+"/improvements.jsonl",std::ios::app);
    double first_feasible=-1; bool proven=false;
    auto ids=[](std::ostream& out,const std::vector<int>& route){out<<'[';for(size_t j=0;j<route.size();++j){if(j)out<<',';out<<route[j];}out<<']';};
    auto offer=[&](const std::vector<int>& r,const Score& s){
        if(!s.feasible || !anchored(r)) return;
        std::lock_guard<std::mutex> guard(lock);
        if(first_feasible<0 && !r.empty()) first_feasible=elapsed();
        if(better(s,best)) {
            best=s; winning=r;
            history<<"{\"elapsed_seconds\":"<<elapsed()<<",\"species\":"<<s.species<<",\"travel_minutes\":"<<s.minutes<<",\"road_miles\":"<<s.miles<<",\"evaluations\":"<<evaluations.load()<<",\"selected\":"; ids(history,r); history<<"}\n"; history.flush();
            std::ofstream ck(output+"/checkpoint.tmp"); ids(ck,r); ck<<'\n'; ck.close();
            std::filesystem::rename(output+"/checkpoint.tmp",output+"/checkpoint.json");
        }
        bool exists=false;for(const auto& a:archive) if(a.route==r)exists=true;
        if(!exists && s.species>=best.species-near) archive.push_back({s,r});
        std::sort(archive.begin(),archive.end(),[](const auto&a,const auto&b){return better(a.score,b.score);});
        archive.erase(std::remove_if(archive.begin(),archive.end(),[&](const auto&a){return a.score.species<best.species-near;}),archive.end());
        if(int(archive.size())>alternatives) archive.resize(alternatives);
    };
    std::vector<int> resume;
    uint64_t warm_attempted=0,warm_accepted=0;double warm_seconds=0;
    if(argc>9){
        std::ifstream r(argv[9]);char c;int id;r>>c;
        while(r>>id){if(id<0||id>=p.nopp)throw std::runtime_error("invalid resume ID");resume.push_back(id);r>>c;if(c==']')break;}
        if(argc>10 && std::string(argv[10])=="greedy") {
            auto seed=resume;resume.clear();double before=elapsed();
            Score seed_score=p.evaluate(resume);
            // Re-evaluate a source-derived seed under the current full model
            // and road provider. No old road costs or feasibility are trusted.
            for(int value:seed) {
                if(elapsed()>=seconds*.75)break;
                auto trial=resume;trial.push_back(value);p.canonical(trial);++warm_attempted;
                try {auto score=p.evaluate(trial);++evaluations;
                    if(score.feasible && better(score,seed_score)){resume=std::move(trial);seed_score=score;++warm_accepted;offer(resume,score);}
                } catch(const DeferredRoad&) {++deferred;}
            }
            warm_seconds=elapsed()-before;
        } else {double before=elapsed();p.canonical(resume);offer(resume,p.evaluate(resume));warm_seconds=elapsed()-before;}
    }
    if(exact==1) {
        if(p.nopp>24) throw std::runtime_error("exhaustive exact mode is limited to 24 opportunities");
        std::vector<int> route;
        std::function<void(int)> dfs=[&](int k){
            if(elapsed()>=seconds)return;
            if(k==p.nopp){auto r=route;p.canonical(r);auto s=p.evaluate(r);++evaluations;offer(r,s);return;}
            dfs(k+1);route.push_back(k);dfs(k+1);route.pop_back();
        }; dfs(0); proven=elapsed()<seconds;
    } else if(exact==2) {
#ifdef BIGYEAR_ORTOOLS
        auto result=solve_exact(p,seconds);proven=result.optimal;offer(result.route,p.evaluate(result.route));
#else
        throw std::runtime_error("engine was built without OR-Tools; use CMake with BIGYEAR_ORTOOLS=ON");
#endif
    } else {
        if(!anchored(resume))throw std::runtime_error("complete warm start must contain every required visit");
        // Every actual recorded date at each exact source pin, in a compact
        // CSR index. Global random resampling almost never rediscovers a pin.
        std::vector<uint32_t> location_offsets(size_t(p.nloc)+1,0);
        for(const auto&o:p.opp)++location_offsets[size_t(o.location)+1];
        for(size_t i=1;i<location_offsets.size();++i)location_offsets[i]+=location_offsets[i-1];
        std::vector<int> location_dates(p.nopp);
        {auto cursor=location_offsets;for(int id=0;id<p.nopp;++id)location_dates[cursor[p.opp[id].location]++]=id;}
        auto date_order=[&](int a,int b){return p.opp[a].day==p.opp[b].day?a<b:p.opp[a].day<p.opp[b].day;};
        for(int loc=0;loc<p.nloc;++loc){
            auto first=location_dates.begin()+location_offsets[loc],last=location_dates.begin()+location_offsets[loc+1];
            if(!std::is_sorted(first,last,date_order))std::sort(first,last,date_order);
        }
        date_index_bytes=location_offsets.size()*sizeof(uint32_t)+location_dates.size()*sizeof(int);
        std::vector<double> rarity(p.nspp,0);
        for(const auto&o:p.opp)for(int s:o.species)rarity[s]+=1;
        std::vector<std::vector<int>> supporting;
        uint64_t support_entries=0;
        for(double count:rarity)support_entries+=uint64_t(count);
        // A capped proposal index, containing every observed support, avoids
        // burying a rare species among millions of common opportunities.
        // Larger inputs fall back to global proposals without dropping data.
        if(p.coordinates && support_entries*sizeof(int)<=512ULL*1024*1024) {
            supporting.resize(p.nspp);
            for(int s=0;s<p.nspp;++s)supporting[s].reserve(size_t(rarity[s]));
            for(int id=0;id<p.nopp;++id)for(int s:p.opp[id].species)supporting[s].push_back(id);
            support_index_bytes=support_entries*sizeof(int);
        }
        for(auto& n:rarity)n=1.0/std::max(1.0,n);
        // BY4 keeps a complete spatial proposal index. Global proposal modes
        // still reach every opportunity; this is an index, not cohort thinning.
        std::vector<std::vector<int>> geographic;
        std::array<std::vector<int>,8> regional;
        if(p.coordinates) {
            geographic.resize(72*144);
            for(int id=0;id<p.nopp;++id) {
                int loc=p.opp[id].location;
                int row=int((p.coordinates[2*loc]+90)/2.5),col=int((p.coordinates[2*loc+1]+180)/2.5);
                geographic.at(row*144+col).push_back(id);
                regional[p.region(loc)].push_back(id);
            }
        }
        std::vector<std::thread> workers;
        std::exception_ptr worker_error;
        std::atomic<bool> failed{false};
        for(int tid=0;tid<threads;++tid) workers.emplace_back([&,tid](){
          try {
            std::mt19937 rng(seed+tid*7919); uint64_t iteration=0;
            std::vector<int> current=resume; Score current_score=p.evaluate(current);
            struct Pending {std::vector<int> route;double retry;bool macro;};
            std::deque<Pending> pending;
            int previous_phase=-1;
            std::vector<int> coverage(p.nspp,0);
            auto recount=[&](){std::fill(coverage.begin(),coverage.end(),0);for(int id:current)for(int s:p.opp[id].species)++coverage[s];};
            recount(); offer(current,current_score); ++starts;
            while(!failed && elapsed()<seconds) {
                // Reserve time for measured discovery when speculation fills
                // the bounded retry queue. No source opportunity is removed.
                if(pending.size()>=64 && pending.front().retry>elapsed()){
                    ++pending_pauses;std::this_thread::sleep_for(std::chrono::milliseconds(2));continue;
                }
                int phase=int(elapsed()/std::max(.01,seconds/8));
                int focus=(tid+phase)%8;
                // One worker keeps improving the incumbent. Other workers get
                // equal time in every geographic proposal region, starting
                // independently so a distant insertion need not first break
                // the incumbent's calendar. Global proposals remain enabled.
                if(p.coordinates && tid>0 && phase!=previous_phase) {
                    current=required.empty()?std::vector<int>{}:resume;current_score=p.evaluate(current);recount();++starts;
                    ++region_starts[focus];previous_phase=phase;pending.clear();
                }
                if(!p.coordinates && iteration && iteration%3000==0){current.clear();current_score=p.evaluate(current);recount();++starts;}
                auto candidate=current; int mode=iteration++%(blocks.empty()?7:8);int proposed=-1;
                bool macro=false;
                bool retry=!pending.empty()&&pending.front().retry<=elapsed()&&iteration%3==0;
                if(retry){macro=pending.front().macro;candidate=std::move(pending.front().route);pending.pop_front();}
                else {
                if(mode<=2 || candidate.empty()) {
                    // Rarity-first, unweighted novelty and geographic/time clustering starts.
                    int chosen=-1;double rank=-1;
                    int anchor=(mode==2 && !candidate.empty())?candidate[rng()%candidate.size()]:-1;
                    for(int j=0;j<48;++j){
                        int id=rng()%p.nopp;double value=0;
                        if(p.coordinates&&tid>0&&j%4!=0&&!regional[focus].empty())id=regional[focus][rng()%regional[focus].size()];
                        if(mode==0 && !supporting.empty()&&(tid==0||j%4==0)) {
                            int species=rng()%p.nspp;
                            const auto& support=supporting[species];
                            if(!coverage[species] && !support.empty())id=support[rng()%support.size()];
                        }
                        if(anchor>=0 && !geographic.empty()) {
                            int loc=p.opp[anchor].location;
                            int row=int((p.coordinates[2*loc]+90)/2.5)+int(rng()%3)-1;
                            int col=int((p.coordinates[2*loc+1]+180)/2.5)+int(rng()%3)-1;
                            if(row>=0&&row<72&&col>=0&&col<144) {
                                const auto& bucket=geographic[row*144+col];
                                if(!bucket.empty())id=bucket[rng()%bucket.size()];
                            }
                        }
                        for(int s:p.opp[id].species)if(!coverage[s]) value+=mode==0?rarity[s]:1.0;
                        if(p.coordinates && candidate.empty())value=p.opp[id].species.count;
                        if(mode==2 && !candidate.empty()) {
                            const auto& a=p.opp[anchor];const auto& b=p.opp[id];
                            double t=p.ranking_minutes(a.location,b.location);
                            value/=1+std::abs(a.day-b.day)/30.0+std::min(t,10000.0)/480.0;
                        }
                        value*=0.8+double(rng()%1000)/2500;
                        if(value>rank){chosen=id;rank=value;}
                    }
                    candidate.push_back(chosen);proposed=chosen;
                } else if(mode==3){auto at=rng()%candidate.size();proposed=candidate[at];candidate.erase(candidate.begin()+at);}
                else if(mode==4){ // Move a trip to another reported day at its exact location.
                    int at=rng()%candidate.size(),loc=p.opp[candidate[at]].location;
                    ++date_moves;
                    uint32_t begin=location_offsets[loc],count=location_offsets[loc+1]-begin;
                    // Most date repairs should preserve the surrounding trip.
                    // Keep global recorded-date moves as well for season shifts.
                    if(count>1&&rng()%5!=0){
                        int day=p.opp[candidate[at]].day;
                        auto first=location_dates.begin()+begin,last=first+count;
                        auto left=std::lower_bound(first,last,day-14,[&](int id,int d){return int(p.opp[id].day)<d;});
                        auto right=std::upper_bound(left,last,day+14,[&](int d,int id){return d<int(p.opp[id].day);});
                        if(right-left>1){begin=uint32_t(left-location_dates.begin());count=uint32_t(right-left);++nearby_date_moves;}
                    }
                    if(count>1){
                        int old=candidate[at],draw=rng()%(count-1);
                        auto pos=std::lower_bound(location_dates.begin()+begin,location_dates.begin()+begin+count,old,date_order)-location_dates.begin()-begin;
                        if(draw>=pos)++draw;
                        candidate[at]=location_dates[begin+draw];++date_moves_changed;
                    }
                    proposed=candidate[at];
                } else if(mode==5){proposed=rng()%p.nopp;candidate[rng()%candidate.size()]=proposed;}
                else if(mode==7 && !blocks.empty()) {
                    const auto& block=blocks[rng()%blocks.size()];
                    candidate.erase(std::remove_if(candidate.begin(),candidate.end(),[&](int id){return p.opp[id].day>=block.begin&&p.opp[id].day<block.end;}),candidate.end());
                    candidate.insert(candidate.end(),block.route.begin(),block.route.end());
                    proposed=block.route.front();macro=true;++block_proposals;
                } else { // Replace a time section; later insertions rebuild that portion.
                    int day=rng()%p.days,window=1+rng()%30;
                    candidate.erase(std::remove_if(candidate.begin(),candidate.end(),[&](int id){return p.opp[id].day>=day&&p.opp[id].day<day+window;}),candidate.end());
                    proposed=rng()%p.nopp;candidate.push_back(proposed);
                }
                candidate.insert(candidate.end(),required.begin(),required.end());
                p.canonical(candidate);
                }
                ++proposals;
                if(candidate==current){++unchanged;continue;}
                if(proposed>=0)++region_proposals[p.region(p.opp[proposed].location)];
                if(!p.screen(candidate)){++screened;continue;}
                // Incremental coverage delta scores proposals without Python callbacks.
                auto delta=coverage;
                for(int id:current)if(!std::binary_search(candidate.begin(),candidate.end(),id,[&](int a,int b){return p.opp[a].day==p.opp[b].day?a<b:p.opp[a].day<p.opp[b].day;}))for(int s:p.opp[id].species)--delta[s];
                for(int id:candidate)if(std::find(current.begin(),current.end(),id)==current.end())for(int s:p.opp[id].species)++delta[s];
                Score score;try{score=p.evaluate(candidate);}catch(const DeferredRoad&){++deferred;if(pending.size()<256)pending.push_back({std::move(candidate),elapsed()+.25,macro});continue;}++evaluations;
                if(score.feasible){
                    if(macro)++block_feasible;
                    offer(candidate,score);
                    if(better(score,current_score) || rng()%500==0){current=std::move(candidate);current_score=score;coverage=std::move(delta);}
                }
                if(iteration%5000==0 && (!p.coordinates||tid==0)){std::lock_guard<std::mutex> guard(lock);current=winning;current_score=best;recount();}
            }
          } catch(...) {
            std::lock_guard<std::mutex> guard(lock);
            if(!worker_error)worker_error=std::current_exception();
            failed=true;
          }
        });
        for(auto& w:workers)w.join();
        if(worker_error)std::rethrow_exception(worker_error);
    }
    std::ofstream result(output+"/engine.json"); result<<std::setprecision(12);
    result<<"{\"species\":"<<best.species<<",\"travel_minutes\":"<<best.minutes<<",\"road_miles\":"<<best.miles
          <<",\"elapsed_seconds\":"<<elapsed()<<",\"candidate_evaluations\":"<<evaluations.load()<<",\"starts\":"<<starts.load()
          <<",\"deferred_candidates\":"<<deferred.load()<<",\"time_to_first_feasible_seconds\":"<<first_feasible<<",\"optimal\":"<<(proven?"true":"false")
          <<",\"date_index_bytes\":"<<date_index_bytes<<",\"date_move_attempts\":"<<date_moves.load()<<",\"date_move_changed\":"<<date_moves_changed.load()<<",\"pending_pauses\":"<<pending_pauses.load()
          <<",\"nearby_date_moves\":"<<nearby_date_moves.load()
          <<",\"model\":\""<<(exact==2?"single_stop_per_day":"canonical_intra_day_order")<<"\",\"selected\":";ids(result,winning);
    result<<",\"alternatives\":[";for(size_t i=0;i<archive.size();++i){if(i)result<<',';result<<"{\"species\":"<<archive[i].score.species<<",\"selected\":";ids(result,archive[i].route);result<<'}';}result<<"]}\n";
    result.seekp(-2,std::ios_base::cur);
    result<<",\"required_visits\":"<<required.size()<<",\"warm_start\":{\"attempted\":"<<warm_attempted<<",\"accepted\":"<<warm_accepted
          <<",\"seconds\":"<<warm_seconds<<"},\"rare_proposal_index_bytes\":"<<support_index_bytes<<"}\n";
    result.seekp(-2,std::ios_base::cur);
    result<<",\"regional_block_library\":"<<blocks.size()<<",\"regional_block_proposals\":"<<block_proposals<<",\"regional_block_feasible\":"<<block_feasible<<",\"proposals\":"<<proposals<<",\"unchanged_proposals_skipped\":"<<unchanged<<",\"screened_before_routing\":"<<screened<<",\"regional_proposals\":[";
    for(int i=0;i<8;++i){if(i)result<<',';result<<region_proposals[i];}result<<"],\"regional_starts\":[";
    for(int i=0;i<8;++i){if(i)result<<',';result<<region_starts[i];}result<<"]}\n";
    if(p.bounded) {
        result.seekp(-2,std::ios_base::cur);
        result<<",\"road_cache\":{\"bridge_requests\":"<<p.bounded->requests.load()
              <<",\"hot_hits\":"<<p.bounded->hits.load()<<",\"hot_evictions\":"<<p.bounded->evictions.load()
              <<",\"hot_entries\":"<<p.bounded->size()<<",\"hot_capacity\":"<<p.bounded->limit()<<",\"pending_backoff_hits\":"<<p.bounded->pending_hits<<"}}\n";
    }
    return 0;
 } catch(const std::exception&e){std::cerr<<e.what()<<'\n';return 1;}
}
