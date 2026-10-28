"""Full 2023 search with the shared-12h multimodal 2024 scenario and audit."""
import argparse
from contextlib import ExitStack
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import shutil

from bigyear_planner.calendar_study import native_session, superior, verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.historical_seed import seasonal_hints, seed_physical_evidence, build_feasible_seed
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator, RegionalExplorer
from bigyear_planner.rest_exploration import RestExplorer
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.util import ROOT, dump, read, sha256
from benchmark_shared_year import metrics, reference_calendar, verified
import evaluate_lower48 as resources

DATA = ROOT/'data/lower48-eod-2023'
OUT = ROOT/'runs/lower48-eod-2023'
BASE = ROOT/'runs/lower48-eod-2024/shared-12h-full-2024-v2'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag',default='shared-12h-full-2023-v1')
    parser.add_argument('--seed-seconds',type=float,default=600)
    parser.add_argument('--calendar-seconds',type=float,default=900)
    parser.add_argument('--regional-seconds',type=float,default=600)
    parser.add_argument('--rest-seconds',type=float,default=600)
    parser.add_argument('--polish-seconds',type=float,default=900)
    parser.add_argument('--resume-search',type=Path)
    parser.add_argument('--model',type=Path,default=DATA/'population-audited-v2/joint-model/model.json')
    parser.add_argument('--cache-source',type=Path)
    parser.add_argument('--publisher-source',type=Path)
    parser.add_argument('--prior-search',type=Path)
    args = parser.parse_args()
    if Path(args.tag).name!=args.tag:
        raise ValueError('tag must be a single directory name')
    OUT.mkdir(exist_ok=True)
    resources.OUT = OUT
    out = OUT/args.tag
    if out.exists():
        raise ValueError('output exists; preserve it and choose another tag')
    out.mkdir()
    baseline,_ = verified(BASE)
    baseline_digest = sha256(BASE/'route.json')
    model = load(args.model)
    cfg = model.meta['config']
    if (cfg['year'],cfg['activity_minutes'],cfg['drive_minutes'],cfg['walking_speed_kmh'],cfg['mixed_road_birding']) != (2023,720,720,2.2,True):
        raise ValueError('historical run scenario differs')
    awake = subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    started = time.monotonic()
    phases = []
    best_ids = []
    best = None
    budgets = {p:getattr(args,p+'_seconds') for p in ['calendar','regional','rest','polish']}
    plan = {'year':2023,'configuration':cfg,'budgets_seconds':budgets,
        'seed_preparation_seconds':args.seed_seconds,'model_path':str(model.path),
        'model_sha256':sha256(model.path),'reference_route_sha256':baseline_digest,
        'reference_metrics':metrics(baseline),'all_observations_retained':True,
        'exact_coordinates':True,'countability_policy':read(Path(model.meta['dataset'])/'countability-policy.json'),
        'road_release':str(resources.DATA/'roads/release.json'),
        'flight_network':str(DATA/'flights-2023'),
        'comparison':'Same activity, walking, drive/flight/boat access and scoring concepts. Actual observations and operated flight dates differ. 2024 has a much longer accumulated search history; this is not an equal-convergence experiment.'}
    dump(out/'plan.json',plan)
    if args.prior_search:
        prior = args.prior_search.resolve()
        plan['prior_search'] = {'path':str(prior),
            'model_sha256':read(prior/'plan.json')['model_sha256'],
            'phase_artifacts':{str(p.relative_to(prior)):sha256(p) for p in
                [prior/'plan.json',prior/'calendar-search/engine.json',prior/'regional-reconstruction/regional-exploration.json',prior/'national-rest/rest-result.json'] if p.exists()},
            'reason':'Continue the same dated candidate indices after introduced-population scoring review; all complete native search stages and budgets remain documented.'}
        dump(out/'plan.json',plan)
    if args.resume_search:
        plan['resume_input'] = {'path':str(args.resume_search.resolve()),
            'sha256':sha256(args.resume_search)}
        dump(out/'plan.json',plan)
    if args.publisher_source:
        dest = out/'publisher-verbatim'
        dest.mkdir()
        for path in (args.publisher_source/'publisher-verbatim').glob('*.json'):
            os.link(path,dest/path.name)
    def event(stage,**fields):
        item = {'stage':stage,'wall_seconds':time.monotonic()-started,
            'candidate_verified':False,**fields}
        if best is not None:
            item['best_candidate_species'] = best['species']
        dump(out/'run-status.json',item)
        print(json.dumps(item),flush=True)
    try:
        with resources.guarded(args.tag), ExitStack() as stack:
            process = stack.enter_context(resources.router(args.tag,120))
            flight = FlightModel(model,DATA/'flights-2023')
            work = DATA/(args.tag+'-cache')
            if args.cache_source:
                shutil.copytree(args.cache_source,work)
            roads = ResolvedJointRoads(flight,resources.DATA/'roads/release.json',work,
                query_timeout_seconds=135,
                access_holds_path=ROOT/'runs/lower48-eod-2024/search-calendar-controls-v2/rules-access/exact-access-holds.json',
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            stack.callback(roads.close)
            event('building-actual-2023-seed')
            seed_physical_evidence(flight,roads,BASE,out/'physical-evidence-reuse.json')
            hints = seasonal_hints(flight,BASE,out/'seasonal-proposals.json')
            if args.resume_search:
                checkpoint = read(args.resume_search)
                if checkpoint['model_sha256']!=sha256(model.path):
                    raise ValueError('resume search model differs')
                best_ids = checkpoint['ids']
                with NativeEvaluator(flight,roads,out/'resume-evaluator').open() as evaluator:
                    best = evaluator.evaluate(best_ids,timeout=600)
                if best['pending'] or not best['feasible']:
                    raise ValueError('resume calendar not measured feasible')
            else:
                with NativeEvaluator(flight,roads,out/'seed-evaluator').open() as evaluator:
                    best_ids,best = build_feasible_seed(flight,evaluator,hints,out/'seed-preparation.json',args.seed_seconds)
            phases.append({'label':'actual-2023-initialization','score':best})
            event('initial-seed-ready',score=best)
            def native_phase(label,seconds,blocks=()):
                nonlocal best,best_ids
                roads.begin_phase(label)
                event(label+'-running',seconds=seconds)
                with roads.bridge():
                    result = native_session(flight,roads,out/label,best_ids,seconds,blocks=blocks)
                score = {'pending':False,'feasible':True,'species':result['species'],
                    'minutes':result['travel_minutes'],'miles':result['road_miles']}
                if superior(score,best):
                    best_ids,best = result['selected'],score
                phases.append({'label':label,'score':best,'seconds':result['elapsed_seconds'],
                    'evaluations':result['candidate_evaluations'],'deferred':result['deferred_candidates']})
                dump(out/'phase-checkpoint.json',{'ids':best_ids,'score':best,'phases':phases,'model_sha256':sha256(model.path)})
                event(label+'-completed',score=best)
            native_phase('calendar-search',budgets['calendar'])
            prefetch_log = stack.enter_context((out/'publisher-prefetch.log').open('w'))
            prefetch = subprocess.Popen([sys.executable,str(ROOT/'scripts/prefetch_historical_sources.py'),
                '--model',str(model.path),'--checkpoint',str(out/'phase-checkpoint.json'),
                '--output',str(out)],stdout=prefetch_log,stderr=subprocess.STDOUT)
            def stop_prefetch():
                if prefetch.poll() is None:
                    prefetch.terminate()
                    prefetch.wait(timeout=10)
                for partial in (out/'publisher-verbatim').glob('*.prefetch.part'):
                    partial.unlink()
            stack.callback(stop_prefetch)
            roads.begin_phase('regional-and-rest')
            with NativeEvaluator(flight,roads,out/'regional-rest-evaluator').open() as evaluator:
                folder = out/'regional-reconstruction'
                folder.mkdir()
                event('regional-reconstruction-running',seconds=budgets['regional'])
                helper = RegionalExplorer(flight,evaluator,folder)
                regional = helper.explore(best_ids,seconds=budgets['regional'])
                blocks = list(helper.library.values())
                if args.prior_search:
                    prior_model = read(args.prior_search/'plan.json')['model_sha256']
                    if prior_model!=sha256(model.path):
                        resume = read(args.resume_search)
                        proof_path = resume['canonical_identity_proof']
                        if sha256(proof_path)!=resume['canonical_identity_proof_sha256']:
                            raise ValueError('continued regional identity proof changed')
                        proof = read(proof_path)
                        if (not proof['valid'] or proof['original_model_sha256']!=prior_model or
                            proof['audited_model_sha256']!=sha256(model.path) or not proof['coordinates_and_candidate_ids_identical']):
                            raise ValueError('continued regional candidates changed identity')
                    block_source = args.prior_search/'regional-reconstruction/regional-blocks.txt'
                    for line in block_source.read_text().splitlines():
                        begin,end,count,*ids = map(int,line.split())
                        if count!=len(ids):
                            raise ValueError('inherited regional block is truncated')
                        blocks.append({'begin':begin,'end':end,'ids':ids})
                    dump(out/'inherited-regional-blocks.json',{'path':str(block_source.resolve()),
                        'sha256':sha256(block_source),'blocks':blocks,
                        'rule':'Proposal library only; all visits and current species credit are reevaluated.'})
                if superior(regional['score'],best):
                    best_ids,best = regional['ids'],regional['score']
                phases.append({'label':'regional-reconstruction','score':best,
                    'seconds':regional['elapsed_seconds'],'attempts':regional['attempts'],
                    'accepted_blocks':regional['accepted_blocks']})
                del helper
                gc.collect()
                event('regional-reconstruction-completed',score=best)
                roads.begin_phase('all-current-rest-dates')
                reference = reference_calendar(flight,roads,best_ids)
                dump(out/'rest-reference-calendar.json',reference)
                event('rest-opportunities-running',seconds=budgets['rest'])
                helper = RestExplorer(flight,evaluator,out/'national-rest',reference)
                rest = helper.explore(best_ids,seconds=budgets['rest'],per_species=48)
                if superior(rest['score'],best):
                    best_ids,best = rest['ids'],rest['score']
                phases.append({'label':'national-rest','score':best,'seconds':rest['seconds'],
                    'attempts':rest['attempts'],'inventory':rest['inventory']})
                del helper
                gc.collect()
                event('rest-opportunities-completed',score=best)
            native_phase('joint-calendar-polish',budgets['polish'],blocks)
            dump(out/'search-result.json',{'ids':best_ids,'score':best,'phases':phases,'model_sha256':sha256(model.path)})
            event('independent-validation',score=best)
            roads.begin_phase('full-year-audit')
            prepared = verify_fresh_calendar(flight,roads,out/'fresh-preparation',best_ids,timeout=600)
            if prepared['species']!=best['species']:
                raise ValueError('fresh evaluator changed score')
            dump(out/'seed.json',{'ids':best_ids,'mode':'complete','model_sha256':sha256(model.path),
                'source_file':str(out/'search-result.json'),'source_sha256':sha256(out/'search-result.json')})
            roads.prepare_audit = lambda:resources.prepare_audit(stack,process,roads,args.tag)
            route = run(model.path,roads,out,seconds=.01,seed_file=out/'seed.json')
            audit = read(out/'validation.json')
            if not audit['valid'] or audit['route_sha256']!=sha256(out/'route.json'):
                raise ValueError('saved 2023 calendar failed audit')
            if sha256(BASE/'route.json')!=baseline_digest:
                raise ValueError('2024 reference changed')
            result = {'valid':True,**metrics(route),'year':2023,'phases':phases,
                'route_sha256':sha256(out/'route.json'),'model_sha256':sha256(model.path),
                'full_calendar_days':audit['days_checked'],
                'publisher_records_checked':audit['joint_transport']['publisher_sources']['records_checked'],
                'all_observations_retained':True,'exact_coordinates':True,'walking_kmh':2.2,
                'reference_metrics':metrics(baseline),'reference_route_sha256':baseline_digest,
                'wall_seconds':time.monotonic()-started,'optimization_proven':False}
            dump(out/'result.json',result)
            dump(out/'run-status.json',{'stage':'completed','candidate_verified':True,
                'species':route['species_total'],'route_sha256':sha256(out/'route.json')})
            print(json.dumps({'stage':'completed',**{k:v for k,v in result.items() if k!='phases'}}),flush=True)
    except BaseException as error:
        event('interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',error=str(error))
        raise
    finally:
        model.close()
        if awake.poll() is None:
            awake.terminate()
            awake.wait(timeout=10)


if __name__=='__main__':
    main()
