"""Warm, full-cohort 2023 target/rest study with independently audited output."""
import argparse
from contextlib import ExitStack
import csv
import gc
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

from bigyear_planner.calendar_study import native_session, superior, verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator
from bigyear_planner.rest_exploration import RestExplorer
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.targeted_exploration import TargetedExplorer
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from benchmark_shared_year import metrics, reference_calendar, verified
from finalize_historical_year import complete_exports
import evaluate_lower48 as resources

DATA = ROOT/'data/lower48-eod-2023'
OUT = ROOT/'runs/lower48-eod-2023'
BASE = OUT/'shared-12h-full-2023-v5'
DIFF = ROOT/'runs/year-comparison-2023-2024/species-differences.csv'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='target-rest-2023-v1')
    parser.add_argument('--incumbent', type=Path, default=BASE)
    parser.add_argument('--target-source', type=Path, default=DIFF)
    parser.add_argument('--rest-seconds', type=float, default=300)
    parser.add_argument('--target-seconds', type=float, default=600)
    parser.add_argument('--calendar-seconds', type=float, default=600)
    parser.add_argument('--refresh-seconds', type=float, default=300)
    parser.add_argument('--polish-seconds', type=float, default=600)
    args = parser.parse_args()
    budgets = {k: getattr(args, k+'_seconds') for k in ['rest','target','calendar','refresh','polish']}
    if Path(args.tag).name != args.tag or any(not math.isfinite(v) or v <= 0 for v in budgets.values()):
        raise ValueError('require a single-directory tag and positive finite budgets')
    base = args.incumbent.resolve()
    before, problem = verified(base)
    model = load(problem['compact_source']['path'])
    cfg = model.meta['config']
    if (cfg['year'],cfg['activity_minutes'],cfg['drive_minutes'],cfg['birding_minutes'],
        cfg['walking_speed_kmh'],cfg['mixed_road_birding']) != (2023,720,720,120,2.2,True):
        raise ValueError('scenario differs from the approved shared-12h 2023 benchmark')
    targets = [{'species_id':r['species_id'],'species':r['common_name']} for r in
        csv.DictReader(args.target_source.open()) if r['collected_in']=='2024'
        and r['available_in_both_cohorts']=='True']
    original_species = {s for d in before['days'] for v in d['stops'] for s in v['species']}
    target_ids = {t['species_id'] for t in targets}
    if len(targets) != 107 or target_ids & original_species:
        raise ValueError('expected the frozen 107 supported species missing from the verified 619 route')
    out, work = OUT/args.tag, DATA/(args.tag+'-cache')
    if out.exists() or work.exists():
        raise ValueError('preserve existing results; choose a new tag')
    out.mkdir(parents=True)
    resources.OUT = OUT
    started = time.monotonic()
    phases, blocks, best_ids, best = [], [], [], None
    awake = subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    plan = {'year':2023,'configuration':cfg,'budgets_seconds':budgets,
        'model_path':str(model.path),'model_sha256':sha256(model.path),
        'baseline_path':str(base),'baseline_route_sha256':sha256(base/'route.json'),
        'baseline_metrics':metrics(before),'target_source':str(args.target_source.resolve()),
        'target_source_sha256':sha256(args.target_source),'targets':targets,
        'all_observations_retained':True,'exact_coordinates':True,'walking_kmh':2.2,
        'countability_policy_unchanged':True,'calendar_rearrangement':True,
        'rest_policy':'No mandatory rest. Reindex the current calendar after regional/calendar changes.',
        'objective':'Unique eligible species, then travel minutes and road miles; no target or rest bonus.',
        'scope':'Actual 2023 lower-48/DC source dates, actual BTS operated flight dates with full flight days; existing modeled charter and island catalog.'}
    dump(out/'plan.json',plan)
    dump(out/'targets.json',targets)

    def event(stage, **fields):
        item = {'stage':stage,'wall_seconds':time.monotonic()-started,
            'candidate_verified':False,**fields}
        if best is not None:
            item['best_candidate_species'] = best['species']
        dump(out/'run-status.json',item)
        print(json.dumps(item),flush=True)

    def checkpoint(label, score, seconds, **fields):
        phases.append({'label':label,'score':score,'seconds':seconds,**fields})
        dump(out/'phase-checkpoint.json',{'ids':best_ids,'score':best,
            'phases':phases,'model_sha256':sha256(model.path)})
        event(label+'-completed',score=best,**fields)

    try:
        shutil.copytree(DATA/(base.name+'-cache'),work)
        with resources.guarded(args.tag), ExitStack() as stack:
            process = stack.enter_context(resources.router(args.tag,120))
            flight = FlightModel(model,DATA/'flights-2023')
            roads = ResolvedJointRoads(flight,resources.DATA/'roads/release.json',work,
                query_timeout_seconds=135,
                access_holds_path=ROOT/'runs/lower48-eod-2024/search-calendar-controls-v2/rules-access/exact-access-holds.json',
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            stack.callback(roads.close)
            initial,binding = seed_verified_joint(flight,roads,base)
            dump(out/'baseline-binding.json',binding)
            # Same model and time budget: retain verified transitions as well
            # as physical costs, rather than rediscover the incumbent's trips.
            event('baseline-replay')
            with NativeEvaluator(flight,roads,out/'guided-evaluator').open() as evaluator:
                best = evaluator.evaluate(initial,timeout=300)
                best_ids = initial
                if best['pending'] or not best['feasible'] or best['species'] != before['species_total']:
                    raise ValueError('could not reproduce the independently audited starting calendar')
                checkpoint('baseline-replay',best,0)
                roads.begin_phase('target-rest-insertion')
                event('target-rest-insertion-running',seconds=budgets['rest'])
                helper = RestExplorer(flight,evaluator,out/'target-rest',before)
                rest = helper.explore(best_ids,seconds=budgets['rest'],targets=target_ids,per_species=48)
                if superior(rest['score'],best):
                    best_ids,best = rest['ids'],rest['score']
                checkpoint('target-rest-insertion',rest['score'],rest['seconds'],
                    attempts=rest['attempts'],pending_attempts=rest['pending_attempts'],
                    target_species_attempted=rest['target_species_attempted'],
                    rest_dates_attempted=rest['rest_dates_attempted'])
                del helper
                gc.collect()
                # Do not cancel the preceding unresolved queries: they can
                # serve the same targets in the regional replacement sweep.
                event('target-regional-reconstruction-running',seconds=budgets['target'])
                (out/'target-regional').mkdir()
                helper = TargetedExplorer(flight,evaluator,out/'target-regional',targets)
                result = helper.explore(best_ids,seconds=budgets['target'],first_probe_seconds=2)
                blocks = list(helper.library.values())
                if superior(result['score'],best):
                    best_ids,best = result['ids'],result['score']
                checkpoint('target-regional-reconstruction',result['score'],result['elapsed_seconds'],
                    target_species_attempted=sum(t['attempts']>0 for t in result['targets']),
                    regional_blocks=len(blocks))
                del helper
                gc.collect()

            def native(label, seconds):
                nonlocal best_ids,best
                roads.begin_phase(label)
                event(label+'-running',seconds=seconds,regional_blocks=len(blocks))
                with roads.bridge():
                    result = native_session(flight,roads,out/label,best_ids,seconds,blocks=blocks)
                score = {'pending':False,'feasible':True,'species':result['species'],
                    'minutes':result['travel_minutes'],'miles':result['road_miles']}
                if superior(score,best):
                    best_ids,best = result['selected'],score
                checkpoint(label,score,result['elapsed_seconds'],
                    evaluations=result['candidate_evaluations'],deferred=result['deferred_candidates'],
                    date_moves=result['date_move_changed'],regional_block_proposals=result['regional_block_proposals'])

            native('joint-calendar-search',budgets['calendar'])
            roads.begin_phase('refreshed-rest-opportunities')
            with NativeEvaluator(flight,roads,out/'refresh-evaluator').open() as evaluator:
                reference = reference_calendar(flight,roads,best_ids)
                dump(out/'refreshed-reference-calendar.json',reference)
                event('refreshed-rest-running',seconds=budgets['refresh'],
                    rest_days=sum(d['kind']=='rest' for d in reference['days']))
                helper = RestExplorer(flight,evaluator,out/'refreshed-rest',reference)
                rest = helper.explore(best_ids,seconds=budgets['refresh'],per_species=48)
                if superior(rest['score'],best):
                    best_ids,best = rest['ids'],rest['score']
                checkpoint('refreshed-rest',rest['score'],rest['seconds'],
                    attempts=rest['attempts'],pending_attempts=rest['pending_attempts'],
                    target_species_attempted=rest['target_species_attempted'],
                    rest_dates_attempted=rest['rest_dates_attempted'])
                del helper
                gc.collect()
            native('joint-calendar-polish',budgets['polish'])
            search = {'ids':best_ids,'score':best,'phases':phases,
                'model_sha256':sha256(model.path),'source_route_sha256':sha256(base/'route.json')}
            dump(out/'search-result.json',search)
            event('independent-validation')
            roads.begin_phase('full-year-audit')
            prepared = verify_fresh_calendar(flight,roads,out/'fresh-preparation',best_ids,timeout=600)
            if prepared['species'] != best['species']:
                raise ValueError('fresh independent client changed the candidate score')
            dump(out/'seed.json',{'ids':best_ids,'mode':'complete','model_sha256':sha256(model.path),
                'source_file':str(out/'search-result.json'),'source_sha256':sha256(out/'search-result.json')})
            roads.prepare_audit = lambda:resources.prepare_audit(stack,process,roads,args.tag)
            route = run(model.path,roads,out,seconds=.01,seed_file=out/'seed.json')
            audit = read(out/'validation.json')
            if (not audit['valid'] or audit['route_sha256'] != sha256(out/'route.json')
                or route['search']['selected_model_opportunities'] != best_ids):
                raise ValueError('saved full-year route did not pass independent audit')
            if route['config'] != before['config'] or route['taxonomy_policy'] != before['taxonomy_policy']:
                raise ValueError('travel or countability rules changed')
            after = {s for d in route['days'] for v in d['stops'] for s in v['species']}
            result = {'valid':True,**metrics(route),'baseline_species':before['species_total'],
                'baseline_metrics':metrics(before),'gain_over_baseline':len(after)-len(original_species),
                'added_species':sorted(after-original_species),'lost_species':sorted(original_species-after),
                'requested_targets':len(targets),'targets_collected':sorted(after&target_ids),
                'targets_remaining':sorted(target_ids-after),'route_sha256':sha256(out/'route.json'),
                'baseline_sha256':sha256(base/'route.json'),'model_sha256':sha256(model.path),
                'full_calendar_days':audit['days_checked'],
                'publisher_records_checked':audit['joint_transport']['publisher_sources']['records_checked'],
                'all_observations_retained':True,'exact_coordinates':True,'walking_kmh':2.2,
                'countability_and_sensitive_species_policy_unchanged':True,
                'phases':phases,'wall_seconds':time.monotonic()-started,'optimization_proven':False}
            dump(out/'result.json',result)
            write_csv(out/'target-outcomes.csv',[{**t,'collected':t['species_id'] in after} for t in targets],
                ['species_id','species','collected'])
            complete_exports(out)
            dump(out/'run-status.json',{'stage':'completed','candidate_verified':True,
                'species':route['species_total'],'route_sha256':sha256(out/'route.json')})
            print(json.dumps({'stage':'completed','species':route['species_total'],
                'targets_collected':len(after&target_ids),'rest_days':result['day_counts'].get('rest',0)}),flush=True)
    except BaseException as error:
        event('interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',error=str(error))
        raise
    finally:
        model.close()
        awake.terminate()
        awake.wait()


if __name__ == '__main__':
    main()
