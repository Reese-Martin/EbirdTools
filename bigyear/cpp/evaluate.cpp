#include "model.h"
#include <iostream>
#include <sstream>

// Evaluate complete source-derived regional moves with the same native
// feasibility rules as the national search. Unknown costs stay deferred.
int main(int argc,char**argv) {
    try {
        if(argc!=2)throw std::runtime_error("usage: evaluate compact_problem");
        Problem p(argv[1]);std::string line;
        while(std::getline(std::cin,line)) {
            if(line=="QUIT")break;
            for(char& c:line)if(c=='['||c==']'||c==',')c=' ';
            std::istringstream input(line);std::vector<int> route;int id;
            while(input>>id){if(id<0||id>=p.nopp)throw std::runtime_error("invalid source opportunity");route.push_back(id);}
            p.canonical(route);int failed=-1;
            try {
                // Match the full search's conservative pre-routing calendar
                // screen. In particular, a nonlocal visit with zero transit
                // days must not enqueue a doomed cross-country road query.
                bool screened=!p.screen(route);
                Score s=screened?Score{}:p.evaluate(route,&failed);
                std::cout<<std::setprecision(17)<<"{\"pending\":false,\"feasible\":"<<(s.feasible?"true":"false")
                         <<",\"species\":"<<s.species<<",\"minutes\":"<<s.minutes<<",\"miles\":"<<s.miles
                         <<",\"failure_day\":"<<failed<<",\"screened\":"<<(screened?"true":"false")<<"}"<<std::endl;
            } catch(const DeferredRoad&) {
                std::cout<<"{\"pending\":true,\"feasible\":false,\"failure_day\":"<<failed<<"}"<<std::endl;
            }
        }
        return 0;
    } catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}
}
