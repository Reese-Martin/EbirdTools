"""End-to-end single-visit island diagnostic before the comparison arms."""
from contextlib import ExitStack
import shutil

from bigyear_planner.compact_model import load
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.util import ROOT,dump,read,sha256
from evaluate_lower48 import OUT,DATA as NATIONAL,guarded,router,prepare_audit


def main():
    tag='island-landing-diagnostic-2024-v1';out=OUT/tag;out.mkdir(exist_ok=True)
    data=ROOT/'data/calendar-strategy-study-2024-v1';joint=ROOT/'data/lower48-joint-mobility-2024-v1'
    model=load(data/'island-model/model.json');flight=FlightModel(model,NATIONAL/'flights-2024')
    if (out/'route.json').exists():
        if not read(out/'validation.json')['valid']:raise ValueError('previous island diagnostic failed')
        print({'reused_verified_diagnostic':str(out)});model.close();return
    try:
        with guarded(tag),ExitStack() as stack:
            process=stack.enter_context(router(tag,120));cache=data/(tag+'-cache')
            if not cache.exists():shutil.copytree(joint/'resolved-routing-2024-v3-cache',cache)
            roads=ResolvedJointRoads(flight,NATIONAL/'roads/release.json',cache,query_timeout_seconds=135,
                access_holds_path=OUT/'search-calendar-controls-v2/rules-access/exact-access-holds.json',
                source_access_holds_path=joint/'source-visit-holds.json')
            try:
                candidates=[i for i in model.meta['joint_marine']['trip_opportunities'].values()
                    if int(model.records[i]['reserved'])>2001]
                target=next(s for s,v in enumerate(model.meta['species']) if v['name']=='Island Scrub-Jay')
                chosen=next(i for i in candidates if target in model.opportunity(i)['species'])
                with NativeEvaluator(flight,roads,out/'evaluator').open() as evaluator:
                    score=evaluator.evaluate([chosen],timeout=180)
                    if score['pending'] or not score['feasible']:raise ValueError('island native diagnostic not feasible')
                    dump(out/'diagnostic-seed.json',{'ids':[chosen],'score':score,'island_scrub_jay_index':int(target),
                        'exact_source_date':model.opportunity(chosen)['date']})
                dump(out/'seed.json',{'ids':[chosen],'mode':'complete','model_sha256':sha256(model.path),
                    'source_file':str((out/'diagnostic-seed.json').resolve()),'source_sha256':sha256(out/'diagnostic-seed.json')})
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,tag)
                route=run(model.path,roads,out,seconds=.01,seed_file=out/'seed.json')
                day=next(d for d in route['days'] if d['kind']=='island')
                print({'valid':read(out/'validation.json')['valid'],'species':route['species_total'],
                    'date':day['date'],'island_walk_minutes':day['walking_minutes'],'activity_minutes':day['activity_minutes']})
            finally:roads.close()
    finally:model.close()


if __name__=='__main__':main()
