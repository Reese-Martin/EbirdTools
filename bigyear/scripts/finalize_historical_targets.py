"""Resume fixed-visit finalization of a completed target search, without searching."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

from bigyear_planner.calendar_study import verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from benchmark_shared_year import metrics, verified
from finalize_historical_year import complete_exports
import evaluate_lower48 as resources

DATA = ROOT/'data/lower48-eod-2023'
OUT = ROOT/'runs/lower48-eod-2023'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=OUT/'target-rest-2023-v1')
    parser.add_argument('--tag', default='target-rest-2023-v2')
    args = parser.parse_args()
    source = args.source.resolve()
    if Path(args.tag).name != args.tag:
        raise ValueError('require a single-directory tag')
    plan = read(source/'plan.json')
    search = read(source/'search-result.json')
    baseline = Path(plan['baseline_path'])
    before,_ = verified(baseline)
    model = load(plan['model_path'])
    if sha256(model.path) != search['model_sha256'] or sha256(baseline/'route.json') != plan['baseline_route_sha256']:
        raise ValueError('source search model or verified baseline changed')
    previous_problem = read(source/'problem.json')
    if previous_problem['compact_source']['sha256'] != sha256(model.path):
        raise ValueError('source measured windows belong to a different full model')
    out,work = OUT/args.tag,DATA/(args.tag+'-cache')
    if out.exists() or work.exists():
        raise ValueError('preserve existing artifacts; choose a new tag')
    out.mkdir()
    for name in ['targets.json','target-source-inventory.json','publisher-prefetch-checkpoint.json',
        'phase-checkpoint.json','refreshed-reference-calendar.json']:
        if (source/name).exists():
            os.link(source/name,out/name)
    for name in ['publisher-verbatim','target-rest','target-regional','joint-calendar-search',
        'refreshed-rest','joint-calendar-polish']:
        shutil.copytree(source/name,out/name,copy_function=os.link)
    plan = {**plan,'finalization_recovery':{'source':str(source),
        'source_search_sha256':sha256(source/'search-result.json'),
        'source_problem_sha256':sha256(source/'problem.json'),
        'source_roads_sha256':sha256(source/'roads.sqlite'),
        'source_status':read(source/'run-status.json'),
        'reason':'A newly completed measured road changed the preferred transfer after the fixed native replay.',
        'additional_search_seconds':0,'selected_visits_unchanged':True}}
    dump(out/'plan.json',plan)
    started = time.monotonic()
    awake = subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    resources.OUT = OUT
    try:
        shutil.copytree(DATA/(source.name+'-cache'),work)
        with resources.guarded(args.tag), ExitStack() as stack:
            process = stack.enter_context(resources.router(args.tag,120))
            flight = FlightModel(model,DATA/'flights-2023')
            roads = ResolvedJointRoads(flight,resources.DATA/'roads/release.json',work,
                query_timeout_seconds=135,
                access_holds_path=ROOT/'runs/lower48-eod-2024/search-calendar-controls-v2/rules-access/exact-access-holds.json',
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            stack.callback(roads.close)
            _,binding = seed_verified_joint(flight,roads,baseline)
            expected = read(source/'search-binding.json')['road_provider']['network_sha256']
            if roads.manifest['network_sha256'] != expected:
                raise ValueError('physical/model routing binding changed')
            routing = previous_problem['routing_locations']
            for i,global_id in enumerate(routing):
                current,old = flight.locations[global_id],previous_problem['locations'][i]
                if any(current[k] != old[k] for k in ['id','lat','lon','state']):
                    raise ValueError('measured source window endpoint identity changed')
            for item in previous_problem['window_transitions']:
                key = (routing[item['from']],routing[item['to']],item['previous'],item['target'])
                value = {**item['plan'],'segments':[{**s,'from':routing[s['from']],'to':routing[s['to']]}
                    for s in item['plan']['segments']]}
                roads._remember(key,value)
            roads.evidence_databases.append(sqlite3.connect(f'file:{source/"roads.sqlite"}?mode=ro',uri=True))
            dump(out/'baseline-binding.json',{**binding,'source_windows_are_measured_hints':True,
                'source_problem_sha256':sha256(source/'problem.json'),
                'independent_full_audit_still_required':True})
            ids = search['ids']
            dump(out/'run-status.json',{'stage':'fresh-fixed-calendar-replay','candidate_verified':False})
            score = verify_fresh_calendar(flight,roads,out/'fresh-preparation',ids,timeout=600)
            if score['species'] != search['score']['species']:
                raise ValueError('same-visit replay changed species count')
            search = {**search,'timed_search_score_before_late_measurement':search['score'],
                'score':score,'finalization_recovery':plan['finalization_recovery']}
            dump(out/'search-result.json',search)
            dump(out/'seed.json',{'ids':ids,'mode':'complete','model_sha256':sha256(model.path),
                'source_file':str(out/'search-result.json'),'source_sha256':sha256(out/'search-result.json')})
            roads.prepare_audit = lambda:resources.prepare_audit(stack,process,roads,args.tag)
            dump(out/'run-status.json',{'stage':'independent-validation','candidate_verified':False,'species':score['species']})
            print(json.dumps({'stage':'independent-validation','fixed_calendar_score':score['species']}),flush=True)
            route = run(model.path,roads,out,seconds=.01,seed_file=out/'seed.json')
            audit = read(out/'validation.json')
            if (not audit['valid'] or audit['route_sha256'] != sha256(out/'route.json')
                or route['search']['selected_model_opportunities'] != ids):
                raise ValueError('saved fixed calendar did not pass independent audit')
            if route['config'] != before['config'] or route['taxonomy_policy'] != before['taxonomy_policy']:
                raise ValueError('travel or countability rules changed')
            old = {s for d in before['days'] for v in d['stops'] for s in v['species']}
            new = {s for d in route['days'] for v in d['stops'] for s in v['species']}
            target_ids = {t['species_id'] for t in read(out/'targets.json')}
            result = {'valid':True,**metrics(route),'baseline_species':before['species_total'],
                'baseline_metrics':metrics(before),'gain_over_baseline':len(new)-len(old),
                'added_species':sorted(new-old),'lost_species':sorted(old-new),
                'requested_targets':len(target_ids),'targets_collected':sorted(new&target_ids),
                'targets_remaining':sorted(target_ids-new),'route_sha256':sha256(out/'route.json'),
                'baseline_sha256':sha256(baseline/'route.json'),'model_sha256':sha256(model.path),
                'full_calendar_days':audit['days_checked'],
                'publisher_records_checked':audit['joint_transport']['publisher_sources']['records_checked'],
                'all_observations_retained':True,'exact_coordinates':True,'walking_kmh':2.2,
                'countability_and_sensitive_species_policy_unchanged':True,
                'phases':search['phases'],'wall_seconds':plan['finalization_recovery']['source_status']['wall_seconds']+time.monotonic()-started,
                'recovery_wall_seconds':time.monotonic()-started,'additional_search_seconds':0,
                'optimization_proven':False}
            dump(out/'result.json',result)
            write_csv(out/'target-outcomes.csv',[{**t,'collected':t['species_id'] in new}
                for t in read(out/'targets.json')],['species_id','species','collected'])
            complete_exports(out)
            dump(out/'run-status.json',{'stage':'completed','candidate_verified':True,
                'species':route['species_total'],'route_sha256':sha256(out/'route.json')})
            print(json.dumps({'stage':'completed','species':route['species_total'],
                'targets_collected':len(new&target_ids),'rest_days':result['day_counts'].get('rest',0)}),flush=True)
    except BaseException as error:
        dump(out/'run-status.json',{'stage':'failed','candidate_verified':False,'error':str(error)})
        raise
    finally:
        model.close()
        awake.terminate()
        awake.wait()


if __name__ == '__main__':
    main()
