"""Full-cohort 2024 shared-12-hour calendar, regional and rest-date benchmark."""

import argparse
from collections import Counter
from contextlib import ExitStack
import csv
import gc
import json
import math
from pathlib import Path
import shutil
import time

from bigyear_planner.calendar_study import native_session, superior, verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel, WindowTransitions
from bigyear_planner.national_runner import freeze_selected, run, selected_problem
from bigyear_planner.regional_exploration import NativeEvaluator, RegionalExplorer
from bigyear_planner.rest_exploration import RestExplorer
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.runner import materialize
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from evaluate_lower48 import OUT, DATA as NATIONAL, guarded, router, prepare_audit


STUDY = ROOT/'data/calendar-strategy-study-2024-v1'


def verified(folder):
    route, audit, problem = (read(folder/name) for name in ['route.json','validation.json','problem.json'])
    if not audit['valid'] or audit['route_sha256'] != sha256(folder/'route.json'):
        raise ValueError('require a route-bound positive independent audit: '+str(folder))
    for name, key in [('problem.json','problem_sha256'), ('roads.sqlite','roads_sha256')]:
        if sha256(folder/name) != route['provenance'][key]:
            raise ValueError('verified comparison artifact changed: '+str(folder/name))
    return route, problem


def metrics(route):
    days = route['days']
    return {'species': route['species_total'], **route['scoring_tiers'],
        'road_miles': sum(d['road_miles'] for d in days),
        'travel_hours': sum(d['travel_minutes'] for d in days)/60,
        'birding_hours': sum(s.get('birding_minutes',route['config']['birding_minutes'])
                             for d in days for s in d['stops'])/60,
        'walking_hours': sum(d.get('walking_minutes',0) for d in days)/60,
        'day_counts': dict(Counter(d['kind'] for d in days)),
        'birding_days_over_30_road_miles': sum(d['kind']=='birding' and d['road_miles']>30 for d in days),
        'birding_days_over_8_driving_hours': sum(d['kind']=='birding' and
            sum(x['minutes'] for x in d['legs'] if x['mode']=='road')>480+1e-5 for d in days)}


def reference_calendar(model, roads, ids):
    problem = selected_problem(model, roads, ids)
    adapter = freeze_selected(problem, roads, None)
    return materialize(problem, adapter, WindowTransitions(problem),
        list(range(len(ids))), {}, calendar_only=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='shared-12h-full-2024-v1')
    parser.add_argument('--calendar-seconds', type=float, default=900)
    parser.add_argument('--regional-seconds', type=float, default=600)
    parser.add_argument('--rest-seconds', type=float, default=600)
    parser.add_argument('--polish-seconds', type=float, default=900)
    parser.add_argument('--incumbent', type=Path, default=OUT/'rest-opportunities-2024-v1')
    parser.add_argument('--control', type=Path, default=OUT/'mixed-activity-2024-v1')
    parser.add_argument('--model', type=Path, default=ROOT/'data/shared-12h-2024-v1/model/model.json')
    parser.add_argument('--cache-source',type=Path,help='reuse a compatible bounded physical cost cache')
    args = parser.parse_args()
    budgets = {k: getattr(args,k+'_seconds') for k in ['calendar','regional','rest','polish']}
    if Path(args.tag).name != args.tag or any(not math.isfinite(v) or v<=0 for v in budgets.values()):
        raise ValueError('require a single-directory tag and positive finite phase budgets')
    base, control = args.incumbent.resolve(), args.control.resolve()
    original, original_problem = verified(base)
    prior, prior_problem = verified(control)
    model = load(args.model)
    out = OUT/args.tag
    work = STUDY/(args.tag+'-cache')
    if out.exists() or work.exists():
        model.close()
        raise ValueError('output/cache exists; preserve it and choose another tag')
    out.mkdir()
    started = time.monotonic()
    phases = []
    best = None
    best_ids = []

    def event(stage, **fields):
        record = {'stage': stage, 'wall_seconds': time.monotonic()-started,
                  'candidate_verified': False, **fields}
        if best is not None:
            record['best_candidate_species'] = best['species']
        dump(out/'run-status.json', record)
        print(json.dumps(record), flush=True)

    try:
        cfg = model.meta['config']
        if (cfg['activity_minutes'],cfg['drive_minutes'],cfg['birding_minutes'],
            cfg['walking_speed_kmh'],cfg.get('mixed_road_birding')) != (720,720,120,2.2,True):
            raise ValueError('require the clarified shared-12h configuration')
        for folder, problem in [(base,original_problem),(control,prior_problem)]:
            source = problem['compact_source']
            if sha256(source['path']) != source['sha256']:
                raise ValueError('comparison model changed: '+str(folder))
            meta = read(source['path'])
            if (meta['components'] != model.meta['components'] or any(meta[k]!=model.meta[k]
                for k in ['locations','opportunities','species','species_entries','dataset','manifest_sha256'])):
                raise ValueError('comparison must retain the identical complete observation model')
        taxonomy = {r['species_id']: r for r in csv.DictReader((Path(model.meta['dataset'])/'taxonomy.csv').open())}
        pool = [dict(id=s['id'],name=s['name'],tier=taxonomy[s['id']]['score_tier'])
                for s in model.meta['species'] if taxonomy[s['id']]['score_tier'].startswith('provisional')]
        cache_source = args.cache_source.resolve() if args.cache_source else STUDY/(control.name+'-cache')
        plan = {'model_path': str(model.path), 'model_sha256': sha256(model.path),
            'components': model.meta['components'], 'baseline_route_sha256': sha256(base/'route.json'),
            'control_route_sha256': sha256(control/'route.json'), 'budgets_seconds': budgets,
            'provisional_pool': pool, 'provisional_scoring_authorized': True,
            'taxonomy_policy': original['taxonomy_policy'], 'baseline_metrics': metrics(original),
            'eight_hour_control_metrics': metrics(prior), 'configuration': cfg,
            'all_observations_retained': True, 'exact_coordinates': True,
            'cache_start': str(cache_source),
            'scope': 'Full-year 2024 lower 48/DC, complete EOD land and existing partial marine/island catalog; all scoring tiers compete in the unchanged unique-species objective.'}
        dump(out/'plan.json', plan)
        shutil.copytree(cache_source, work)
        with guarded(args.tag), ExitStack() as stack:
            process = stack.enter_context(router(args.tag,120))
            flight = FlightModel(model,NATIONAL/'flights-2024')
            roads = ResolvedJointRoads(flight,NATIONAL/'roads/release.json',work,
                query_timeout_seconds=135,
                access_holds_path=OUT/'search-calendar-controls-v2/rules-access/exact-access-holds.json',
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            stack.callback(roads.close)
            initial, binding = seed_verified_joint(flight,roads,base)
            # Physical costs and anchors remain valid; calendar transitions
            # must be scheduled again with twelve-hour driving days.
            roads.plan_cache.clear()
            roads.flight_plans.clear()
            binding['old_calendar_plans_discarded_for_new_driving_budget'] = True
            dump(out/'baseline-binding.json',binding)
            event('baseline-replay',species=original['species_total'])
            with NativeEvaluator(flight,roads,out/'initial-evaluator').open() as evaluator:
                baseline = evaluator.evaluate(initial,timeout=600)
                if baseline['pending'] or not baseline['feasible'] or baseline['species']!=original['species_total']:
                    raise ValueError('new rules did not reproduce the verified 699-species baseline')
                best_ids,best = initial,baseline
                candidates = [('eight-hour-control',prior['search']['selected_model_opportunities'])]
                union = sorted(set(initial)|set(candidates[0][1]),key=lambda i:(int(model.records[i]['day']),i))
                candidates.append(('combined-verified-visits',union))
                seeds = []
                for label, ids in candidates:
                    score = evaluator.evaluate(ids,timeout=180)
                    seeds.append({'label':label,'ids':ids,'score':score})
                    if superior(score,best): best_ids,best = list(ids),score
                dump(out/'seed-comparisons.json',{'baseline':baseline,'candidates':seeds})
                phases.append({'label':'baseline-replay','score':baseline})
                phases.append({'label':'seed-combinations','score':best})
            event('initial-seeds-ready',score=best)

            def native_phase(label, seconds, blocks=()):
                nonlocal best,best_ids
                roads.begin_phase(label)
                event(label+'-running',seconds=seconds,regional_blocks=len(blocks))
                with roads.bridge():
                    search = native_session(flight,roads,out/label,best_ids,seconds,blocks=blocks)
                score = {'pending':False,'feasible':True,'species':search['species'],
                         'minutes':search['travel_minutes'],'miles':search['road_miles']}
                if superior(score,best):best_ids,best = search['selected'],score
                phases.append({'label':label,'score':score,'seconds':search['elapsed_seconds'],
                    'evaluations':search['candidate_evaluations'],'deferred':search['deferred_candidates'],
                    'date_moves':search['date_move_changed'],'regional_block_proposals':search['regional_block_proposals']})
                dump(out/'phase-checkpoint.json',{'ids':best_ids,'score':best,'phases':phases})
                event(label+'-completed',score=best)

            native_phase('calendar-search',budgets['calendar'])
            roads.begin_phase('regional-and-rest')
            blocks = []
            with NativeEvaluator(flight,roads,out/'regional-rest-evaluator').open() as evaluator:
                folder = out/'regional-reconstruction'
                folder.mkdir()
                event('regional-reconstruction-running',seconds=budgets['regional'])
                helper = RegionalExplorer(flight,evaluator,folder)
                regional = helper.explore(best_ids,seconds=budgets['regional'])
                blocks = list(helper.library.values())
                if superior(regional['score'],best):best_ids,best = regional['ids'],regional['score']
                phases.append({'label':'regional-reconstruction','score':regional['score'],
                    'seconds':regional['elapsed_seconds'],'attempts':regional['attempts'],
                    'accepted_blocks':regional['accepted_blocks'],'proposal_library_size':len(blocks)})
                del helper
                gc.collect()
                event('regional-reconstruction-completed',score=best)
                roads.begin_phase('all-current-rest-dates')
                reference = reference_calendar(flight,roads,best_ids)
                dump(out/'rest-reference-calendar.json',reference)
                event('rest-opportunities-running',seconds=budgets['rest'],
                      actual_rest_days=sum(d['kind']=='rest' for d in reference['days']))
                helper = RestExplorer(flight,evaluator,out/'national-rest',reference)
                rest = helper.explore(best_ids,seconds=budgets['rest'],per_species=48)
                if superior(rest['score'],best):best_ids,best = rest['ids'],rest['score']
                phases.append({'label':'national-rest','score':rest['score'],'seconds':rest['seconds'],
                    'attempts':rest['attempts'],'pending_attempts':rest['pending_attempts'],
                    'target_species_attempted':rest['target_species_attempted'],
                    'rest_dates_attempted':rest['rest_dates_attempted'],'inventory':rest['inventory']})
                del helper
                gc.collect()
                event('rest-opportunities-completed',score=best)
            native_phase('joint-calendar-polish',budgets['polish'],blocks)
            search = {'ids':best_ids,'score':best,'phases':phases,
                'model_sha256':sha256(model.path),'source_route_sha256':sha256(base/'route.json')}
            dump(out/'search-result.json',search)
            event('independent-validation',score=best)
            roads.begin_phase('full-year-audit')
            prepared = verify_fresh_calendar(flight,roads,out/'fresh-preparation',best_ids,timeout=600)
            if prepared['species']!=best['species']:
                raise ValueError('fresh evaluator changed the selected score')
            dump(out/'seed.json',{'ids':best_ids,'mode':'complete','model_sha256':sha256(model.path),
                'source_file':str(out/'search-result.json'),'source_sha256':sha256(out/'search-result.json')})
            roads.prepare_audit = lambda:prepare_audit(stack,process,roads,args.tag)
            route = run(model.path,roads,out,seconds=.01,seed_file=out/'seed.json')
            audit = read(out/'validation.json')
            if (not audit['valid'] or audit['route_sha256']!=sha256(out/'route.json') or
                route['search']['selected_model_opportunities']!=best_ids):
                raise ValueError('final saved calendar did not pass independent validation')
            before = {s for d in original['days'] for stop in d['stops'] for s in stop['species']}
            after = {s for d in route['days'] for stop in d['stops'] for s in stop['species']}
            result = {'valid':True,**metrics(route),'baseline_species':original['species_total'],
                'gain_over_699':route['species_total']-original['species_total'],
                'added_species':sorted(after-before),'lost_species':sorted(before-after),
                'route_sha256':sha256(out/'route.json'),'baseline_sha256':sha256(base/'route.json'),
                'model_sha256':sha256(model.path),'baseline_metrics':metrics(original),
                'eight_hour_control_metrics':metrics(prior),'full_calendar_days':audit['days_checked'],
                'publisher_records_checked':audit['joint_transport']['publisher_sources']['records_checked'],
                'all_observations_retained':True,'exact_coordinates':True,'walking_kmh':2.2,
                'provisional_pool':pool,'countability_and_sensitive_species_policy_unchanged':True,
                'phases':phases,'wall_seconds':time.monotonic()-started,
                'scope':plan['scope'],'optimization_proven':False}
            dump(out/'result.json',result)
            rows = [{'phase':p['label'],'species':p['score'].get('species'),
                     'seconds':p.get('seconds'),'pending':p['score']['pending']} for p in phases]
            write_csv(out/'phase-comparison.csv',rows,list(rows[0]))
            dump(out/'run-status.json',{'stage':'completed','candidate_verified':True,
                'species':route['species_total'],'route_sha256':sha256(out/'route.json')})
            print(json.dumps({'stage':'completed',**{k:v for k,v in result.items()
                if k not in ['phases','provisional_pool']}}),flush=True)
    except BaseException as error:
        event('interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',error=str(error))
        raise
    finally:
        model.close()


if __name__=='__main__':
    main()
