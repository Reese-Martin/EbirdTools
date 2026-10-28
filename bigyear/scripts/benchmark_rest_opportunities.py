"""Central Texas, Midwest and national rest-date opportunity trials for 2024."""

import argparse
from contextlib import ExitStack
import gc
import json
from pathlib import Path
import shutil
import time

from bigyear_planner.calendar_study import superior, verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator
from bigyear_planner.rest_exploration import RestExplorer
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.targeted_exploration import TargetedExplorer
from bigyear_planner.util import ROOT, dump, read, sha256
from evaluate_lower48 import OUT, DATA as NATIONAL, guarded, router, prepare_audit


STUDY = ROOT/'data/calendar-strategy-study-2024-v1'
BASE = OUT/'rest-study-baseline-696-2024-v1'
HOLDS = OUT/'search-calendar-controls-v2/rules-access/exact-access-holds.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='rest-opportunities-2024-v1')
    parser.add_argument('--incumbent', type=Path, default=BASE, help='independently validated starting route directory')
    parser.add_argument('--regional-seconds', type=float, default=300)
    parser.add_argument('--rest-seconds', type=float, default=600)
    args = parser.parse_args()
    base = args.incumbent.resolve()
    if Path(args.tag).name != args.tag or min(args.regional_seconds, args.rest_seconds) <= 0:
        raise ValueError('require a single-directory tag and positive search budgets')
    audit = read(base/'validation.json')
    base_route = read(base/'route.json')
    baseline_count = base_route['species_total']
    if not audit['valid'] or audit['route_sha256'] != sha256(base/'route.json'):
        raise ValueError('regional trials require an independently verified calendar')
    out = OUT/args.tag
    if out.exists():
        raise ValueError('trial output already exists; preserve it and choose a new tag')
    out.mkdir()
    work = STUDY/(args.tag+'-cache')
    if work.exists():
        raise ValueError('trial cache already exists')
    shutil.copytree(STUDY/(base.name+'-cache'), work)
    model = load(read(base/'problem.json')['compact_source']['path'])
    wall = time.monotonic()
    try:
        with guarded(args.tag), ExitStack() as stack:
            process = stack.enter_context(router(args.tag, 120))
            flight = FlightModel(model, NATIONAL/'flights-2024')
            roads = ResolvedJointRoads(flight, NATIONAL/'roads/release.json', work,
                query_timeout_seconds=135, access_holds_path=HOLDS,
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            stack.callback(roads.close)
            initial, binding = seed_verified_joint(flight, roads, base)
            dump(out/'baseline-binding.json', binding)
            print(json.dumps({'stage': 'baseline-verified', 'species': baseline_count}), flush=True)
            with NativeEvaluator(flight, roads, out/'evaluator').open() as evaluator:
                baseline = evaluator.evaluate(initial, timeout=180)
                if baseline['pending'] or not baseline['feasible'] or baseline['species'] != baseline_count:
                    raise ValueError('trials did not reproduce the verified baseline')
                phases = []
                best_ids, best = initial, baseline
                focuses = [
                    ('central-texas', ['aba2024:Setophaga_chrysoparia', 'aba2024:Vireo_atricapilla'], ['US-TX']),
                    ('midwest', ['aba2024:Passer_montanus', 'aba2024:Centronyx_henslowii',
                                 'aba2024:Setophaga_kirtlandii'],
                     ['US-MO','US-IL','US-IA','US-IN','US-WI','US-MI','US-MN','US-OH'])]
                arms = []
                for label, targets, states in focuses:
                    roads.begin_phase(label)
                    helper = RestExplorer(flight, evaluator, out/label, base_route)
                    start = time.monotonic()
                    result = helper.explore(initial, seconds=args.regional_seconds*.75,
                                            targets=targets, per_species=32, proposal_states=states)
                    current, score = result['ids'], result['score']
                    # If a pure insertion cannot collect the target, try an
                    # actual regional reconstruction around dated target heads.
                    missing = [s for s in targets if not helper.seen(current)[
                        next(i for i,v in enumerate(model.meta['species']) if v['id'] == s)]]
                    regional = []
                    remaining = args.regional_seconds-(time.monotonic()-start)
                    if missing and remaining > 1:
                        selected_targets = [{'species_id': s, 'species': next(v['name'] for v in model.meta['species'] if v['id'] == s)} for s in missing]
                        campaign = TargetedExplorer(flight, evaluator, out/label, selected_targets)
                        deadline = start+args.regional_seconds
                        plans = []
                        for target, species in zip(selected_targets, campaign.target_indices):
                            heads = campaign.heads(initial, species, limit=24)
                            heads = [h for h in heads if model.locations[int(model.records[h]['location'])]['state'] in states]
                            for rank, head in enumerate(heads[:8]):
                                for radius in [3,7]: plans.append((rank,radius,target,head))
                        plans.sort(key=lambda p: (p[0],p[1],p[3]))
                        for rank, radius, target, head in plans:
                            if time.monotonic() >= deadline: break
                            proposal = campaign.campaign(current, head, radius)
                            candidate_score = evaluator.evaluate(proposal['ids'], timeout=min(5,max(.01,deadline-time.monotonic())))
                            entry = {'target': target['species_id'], 'head': head, 'radius': radius, **candidate_score}
                            regional.append(entry)
                            if superior(candidate_score, score): current, score = proposal['ids'], candidate_score
                        del campaign
                    arm = {'label': label, 'ids': current, 'score': score, 'insertion_result': result,
                           'regional_attempts': regional, 'seconds': time.monotonic()-start,
                           'same_verified_baseline_species': baseline_count, 'proposal_states_only': states,
                           'all_source_observations_and_full_model_retained': True}
                    dump(out/label/'phase-result.json', arm)
                    arms.append(arm)
                    phases.append({k: arm[k] for k in ['label','score','seconds','regional_attempts']})
                    if superior(score, best): best_ids, best = current, score
                    print(json.dumps({'stage': label+'-completed', 'score': score, 'seconds': arm['seconds']}), flush=True)
                    del helper
                    gc.collect()
                roads.begin_phase('combine-regions')
                combined = sorted(set(initial).union(*(a['ids'] for a in arms)), key=lambda i:(int(model.records[i]['day']),i))
                combined_score = evaluator.evaluate(combined, timeout=120)
                if superior(combined_score, best): best_ids, best = combined, combined_score
                phases.append({'label': 'combined-regions', 'score': combined_score})
                roads.begin_phase('all-rest-dates-all-regions')
                helper = RestExplorer(flight, evaluator, out/'national-rest', base_route)
                result = helper.explore(best_ids, seconds=args.rest_seconds, per_species=32)
                if superior(result['score'], best): best_ids, best = result['ids'], result['score']
                phases.append({'label': 'national-rest', 'score': result['score'], 'seconds': result['seconds'],
                    'rest_dates_indexed': result['inventory']['opportunity_dates'],
                    'rest_dates_attempted': result['rest_dates_attempted'],
                    'target_species_attempted': result['target_species_attempted'],
                    'pending_attempts': result['pending_attempts']})
                measured = evaluator.evaluate(best_ids, timeout=180)
                if measured['pending'] or not measured['feasible'] or measured['species'] != best['species']:
                    raise ValueError('final rest-study calendar was not measured feasible')
                search = {'ids': best_ids, 'score': measured, 'phases': phases,
                    'source_route_sha256': sha256(base/'route.json'), 'model_sha256': sha256(model.path),
                    'all_observations_retained': True, 'exact_coordinates': True, 'walking_kmh': 2.2,
                    'countability_and_sensitive_species_policy_unchanged': True}
                dump(out/'search-result.json', search)
                del helper
                gc.collect()
            print(json.dumps({'stage':'independent-validation', 'candidate_species': best['species']}), flush=True)
            roads.begin_phase('finalize-rest-calendar')
            prepared = verify_fresh_calendar(flight, roads, out/'fresh-preparation', best_ids, timeout=300)
            if prepared['species'] != best['species']: raise ValueError('fresh-client candidate score changed')
            dump(out/'seed.json', {'ids': best_ids, 'mode': 'complete', 'model_sha256': sha256(model.path),
                'source_file': str((out/'search-result.json').resolve()), 'source_sha256': sha256(out/'search-result.json')})
            roads.prepare_audit = lambda: prepare_audit(stack, process, roads, args.tag)
            route = run(model.path, roads, out, seconds=.01, seed_file=out/'seed.json')
            if route['search']['selected_model_opportunities'] != best_ids:
                raise ValueError('serialization altered the timed trial calendar')
            final_audit = read(out/'validation.json')
            before = {s for d in base_route['days'] for stop in d['stops'] for s in stop['species']}
            after = {s for d in route['days'] for stop in d['stops'] for s in stop['species']}
            result = {'valid': final_audit['valid'], 'species': route['species_total'], **route['scoring_tiers'],
                'route_sha256': sha256(out/'route.json'), 'baseline_species': baseline_count,
                'added_species': sorted(after-before), 'lost_species': sorted(before-after),
                'gap_to_759': 759-route['species_total'], 'phases': phases,
                'wall_seconds': time.monotonic()-wall, 'all_observations_retained': True,
                'exact_coordinates': True, 'walking_kmh': 2.2,
                'rest_days': sum(d['kind']=='rest' for d in route['days']),
                'publisher_records_checked': final_audit['joint_transport']['publisher_sources']['records_checked'],
                'countability_and_sensitive_species_policy_unchanged': True,
                'scope': 'Finite species-balanced source-dated rest opportunities plus regional reconstructions; optimality and exhaustive evaluation are not claimed.'}
            dump(out/'result.json', result)
            print(json.dumps({'stage':'completed', **result}), flush=True)
    finally:
        model.close()


if __name__ == '__main__':
    main()
