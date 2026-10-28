"""Save an existing measured study checkpoint without repeating its search."""
import argparse
from contextlib import ExitStack

from bigyear_planner.calendar_study import verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.util import ROOT,dump,read,sha256
from evaluate_lower48 import OUT,DATA as NATIONAL,guarded,router,prepare_audit


def main():
    parser=argparse.ArgumentParser();parser.add_argument('tag');args=parser.parse_args()
    out=OUT/args.tag;search=read(out/'study-search.json');baseline=read(out/'baseline-binding.json')
    seed=read(out/'seed.json');model=load(ROOT/('data/calendar-strategy-study-2024-v1/island-model/model.json'
        if search['arm'] in {'island','combined'} else 'data/lower48-joint-mobility-2024-v1/model/model.json'))
    if sha256(model.path)!=baseline['complete_model_sha256'] or seed['source_sha256']!=sha256(out/'study-search.json'):
        raise ValueError('saved search binding changed')
    flight=FlightModel(model,NATIONAL/'flights-2024');tag=args.tag+'-resume'
    try:
        with guarded(tag),ExitStack() as stack:
            process=stack.enter_context(router(tag,120))
            roads=ResolvedJointRoads(flight,NATIONAL/'roads/release.json',ROOT/'data/calendar-strategy-study-2024-v1'/(args.tag+'-cache'),
                query_timeout_seconds=135,access_holds_path=OUT/'search-calendar-controls-v2/rules-access/exact-access-holds.json',
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            try:
                if search['arm'] in {'island','combined'}:raise ValueError('diagnostic resume currently expects the unchanged terrestrial model')
                seed_verified_joint(flight,roads,OUT/'resolved-routing-2024-v3')
                score=verify_fresh_calendar(flight,roads,out/'fresh-preparation-resume',search['ids'],timeout=300)
                if score['species']!=search['score']['species']:raise ValueError('checkpoint species changed')
                roads.study_context={'arm':search['arm'],'allocated_seconds':search['allocated_seconds'],
                    'elapsed_seconds':search['elapsed_seconds'],'path':str((out/'study-search.json').resolve()),
                    'sha256':sha256(out/'study-search.json'),'required_final_visits':search['required_final_visits'],
                    'candidate_evaluations':sum(s.get('candidate_evaluations',0) for s in search['stages']),
                    'diagnostic_only':True,'resume_search_seconds':0}
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,tag)
                route=run(model.path,roads,out,seconds=.01,seed_file=out/'seed.json')
                dump(out/'result.json',{'valid':True,'species':route['species_total'],**route['scoring_tiers'],
                    'route_sha256':sha256(out/'route.json'),'diagnostic_only':True,'search_repeated':False,
                    'reason':'Pilot retained outside the main comparison; finalization resumed after cold-client cost preparation was fixed.'})
                print({'valid':True,'species':route['species_total'],'search_repeated':False},flush=True)
            finally:roads.close()
    finally:model.close()


if __name__=='__main__':main()
