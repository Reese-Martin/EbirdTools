#include "exact.h"
#include "ortools/sat/cp_model.h"
#include "ortools/sat/cp_model_solver.h"
#include "ortools/sat/sat_parameters.pb.h"
#include <chrono>

using namespace operations_research;
using namespace operations_research::sat;
ExactResult solve_exact(const Problem&p,double seconds) {
    if(p.nopp>250) throw std::runtime_error("reduce to at most 250 opportunities for the CP-SAT benchmark");
    auto start=std::chrono::steady_clock::now();
    CpModelBuilder model;
    std::vector<BoolVar> visits;
    std::vector<std::vector<BoolVar>> incoming(p.nopp+2), outgoing(p.nopp+2);
    const int source=p.nopp,sink=p.nopp+1;
    LinearExpr travel;
    auto arc=[&](int a,int b,double minutes){auto x=model.NewBoolVar();outgoing[a].push_back(x);incoming[b].push_back(x);travel+=x*int64_t(std::llround(minutes*60));};
    auto cost=[&](int loc,int dest,int gap)->double {
        if(p.stop_minutes[dest]>p.activity+1e-7)return std::numeric_limits<double>::infinity();
        if(loc<0)return 0;
        double best=std::numeric_limits<double>::infinity();
        if((p.mixed_road_birding||p.road_miles(loc,dest)<=p.local+1e-7) &&
           (!p.mixed_road_birding||p.road_minutes(loc,dest)<=p.drive+1e-7) &&
           p.road_minutes(loc,dest)+p.stop_minutes[dest]<=p.activity+1e-7)best=p.road_minutes(loc,dest);
        double best_m=std::numeric_limits<double>::infinity();
        p.best_transition(loc,dest,gap,best,best_m);
        return best;
    };
    arc(source,sink,0);
    for(int i=0;i<p.nopp;++i){
        visits.push_back(model.NewBoolVar());
        double initial=cost(p.start,p.opp[i].location,p.opp[i].day);
        if(std::isfinite(initial))arc(source,i,initial);
        arc(i,sink,0);
        for(int j=0;j<p.nopp;++j)if(p.opp[j].day>p.opp[i].day){
            double t=cost(p.opp[i].location,p.opp[j].location,p.opp[j].day-p.opp[i].day-1);
            if(std::isfinite(t))arc(i,j,t);
        }
    }
    for(int i=0;i<p.nopp;++i){model.AddEquality(LinearExpr::Sum(incoming[i]),visits[i]);model.AddEquality(LinearExpr::Sum(outgoing[i]),visits[i]);}
    model.AddEquality(LinearExpr::Sum(outgoing[source]),1);
    model.AddEquality(LinearExpr::Sum(incoming[sink]),1);
    LinearExpr total;
    for(int s=0;s<p.nspp;++s){
        auto seen=model.NewBoolVar();std::vector<BoolVar> supporting;
        for(int i=0;i<p.nopp;++i)if(std::find(p.opp[i].species.begin(),p.opp[i].species.end(),s)!=p.opp[i].species.end())supporting.push_back(visits[i]);
        model.AddLessOrEqual(seen,LinearExpr::Sum(supporting));total+=seen;
    }
    model.Maximize(total);
    auto solve=[&](double limit){Model solver;SatParameters settings;settings.set_max_time_in_seconds(std::max(0.001,limit));settings.set_num_search_workers(1);solver.Add(NewSatParameters(settings));return SolveCpModel(model.Build(),&solver);};
    auto response=solve(seconds);
    if(response.status()!=CpSolverStatus::OPTIMAL && response.status()!=CpSolverStatus::FEASIBLE)throw std::runtime_error("CP-SAT did not return a feasible route");
    bool optimal=response.status()==CpSolverStatus::OPTIMAL;
    if(optimal){
        int64_t count=std::llround(response.objective_value());
        model.AddEquality(total,count);model.Minimize(travel);
        double elapsed=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
        if(seconds-elapsed>0.01){auto refined=solve(seconds-elapsed);if(refined.status()==CpSolverStatus::FEASIBLE || refined.status()==CpSolverStatus::OPTIMAL)response=refined;}
    }
    std::vector<int> route;
    for(int i=0;i<p.nopp;++i)if(SolutionBooleanValue(response,visits[i]))route.push_back(i);
    p.canonical(route);
    return {route,optimal}; // Proven primary objective, within the single-stop/day candidate model only.
}
