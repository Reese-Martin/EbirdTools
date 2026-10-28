"""Rerun complete national exploration with frozen countability scoring."""

from contextlib import ExitStack
import json
import os
import shutil

from bigyear_planner.compact_model import load
from bigyear_planner.mobility import FlightModel, MobilityRoads
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator, RegionalExplorer
from bigyear_planner.util import ROOT, dump, read, sha256
from audit_countability import BASE, DATA, POLICY
from evaluate_lower48 import DATA as ORIGINAL, OUT, guarded, prepare_audit, router

TAG=os.environ.get('BIGYEAR_COUNTABILITY_TAG','countability-flights-900s-600s')


def main():
    folder=OUT/TAG
    if (folder/'route.json').exists():
        raise ValueError('completed result exists')
    folder.mkdir(parents=True,exist_ok=True)
    policy=read(DATA/'policy.json')
    if policy['id']!=POLICY or sha256(BASE/'route.json')!=policy['baseline_route_sha256']:
        raise ValueError('countability source route changed')
    if not read(BASE/'validation.json')['valid']:
        raise ValueError('source route is not independently validated')
    original_archive=sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json')
    cache=DATA/(TAG+'-cache')
    if not cache.exists():
        shutil.copytree(ORIGINAL/(BASE.name+'-cache'),cache)
    model=load(DATA/'model/model.json')
    try:
        with guarded(TAG),ExitStack() as stack:
            overlay=FlightModel(model,ORIGINAL/'flights-2024')
            process=stack.enter_context(router(TAG,8))
            roads=MobilityRoads(overlay,ORIGINAL/'roads/release.json',cache,query_timeout_seconds=23)
            try:
                roads.seed_evidence(BASE/'route.json');roads.seed_incumbent(BASE/'route.json')
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,TAG)
                dump(folder/'benchmark-design.json',{'baseline':str(BASE/'route.json'),
                     'baseline_sha256':sha256(BASE/'route.json'),'scoring_policy':POLICY,
                     'scoring_policy_sha256':sha256(DATA/'policy.json'),
                     'regional_seconds':900,'native_seconds':600,'threads':2,
                     'all_original_exact_locations_and_opportunities_retained':True,
                     'walking_speed_kmh':2.2,'flight_policy':'Same dated operated 2024 service and full flight/ground days.'})
                dump(OUT/'run-status.json',{'stage':'search','run':TAG,'label':'Countability-scored national search',
                     'message':'Audited scoring rerun in progress; unresolved/excluded records receive no credit. Previous verified route retained.'})
                with NativeEvaluator(overlay,roads,folder).open() as evaluator:
                    initial=evaluator.evaluate(read(BASE/'checkpoint.json'),timeout=30)
                    if initial['pending'] or not initial['feasible'] or initial['species']!=policy['baseline_decisions']['supported']:
                        raise ValueError('native baseline score disagrees with countability audit')
                    dump(folder/'baseline-rescored.json',initial)
                    print(json.dumps({'stage':'baseline-rescored',**initial}),flush=True)
                    explorer=RegionalExplorer(overlay,evaluator,folder)
                    construction=explorer.explore(read(BASE/'checkpoint.json'),seconds=900)
                seed=folder/'audited-seed.json'
                dump(seed,{'ids':construction['ids'],'mode':'complete','model_sha256':sha256(model.path),
                     'source_file':str(folder/'regional-exploration.json'),
                     'source_sha256':sha256(folder/'regional-exploration.json'),
                     'method':'Same complete canonical opportunities, audited species credit.'})
                dump(OUT/'run-status.json',{'stage':'search','run':TAG,'label':'Countability-scored national search',
                     'message':f"Regional construction reached {construction['score']['species']} supported modeled species; native polish and independent validation pending."})
                route=run(model.path,roads,folder,seconds=600,seed_file=seed,blocks_path=folder/'regional-blocks.txt')
                if sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json')!=original_archive:
                    raise ValueError('original observation archive changed')
                summary={'valid':read(folder/'validation.json')['valid'],'run':TAG,
                     'policy':POLICY,'species':route['species_total'],
                     'baseline_unfiltered_species':626,'baseline_supported_species':initial['species'],
                     'baseline_unresolved':policy['baseline_decisions'].get('unresolved',0),
                     'baseline_excluded':policy['baseline_decisions'].get('excluded',0),
                     'birding_states':sorted({route['locations'][s['location']]['state'] for d in route['days'] for s in d['stops']}),
                     'flight_days':sum(d['kind']=='flight' for d in route['days']),
                     'road_miles':sum(d['road_miles'] for d in route['days']),
                     'original_source_unchanged':True,
                     'original_locations':model.meta['locations'],'original_opportunities':model.meta['opportunities'],
                     'source_observations_retained':model.meta['original_retained_observations'],
                     'scoring_eligible_observations':model.meta['represented_observations'],
                     'all_observer_supported_species':len(model.meta['first_seen']),
                     'individual_wild_status_certified':False}
                dump(OUT/'countability-benchmark-result.json',summary)
                print(json.dumps(summary),flush=True)
            finally:
                roads.close()
    finally:
        model.close()
    dump(OUT/'run-status.json',{'stage':'completed','run':TAG,'best_run':TAG,'best_species':route['species_total'],
         'message':'Countability-scored national rerun independently validated. Score uses the documented publisher-review proxy; unresolved evidence excluded.'})


if __name__=='__main__':
    try:
        main()
    except BaseException as error:
        dump(OUT/'run-status.json',{'stage':'failed','run':TAG,
             'message':'Countability rerun needs attention; previous validated result retained.','error':repr(error)})
        raise
