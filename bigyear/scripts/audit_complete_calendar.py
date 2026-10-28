"""Independently save/audit an exact full-model calendar, without another search."""

import argparse
from contextlib import ExitStack
from pathlib import Path
import shutil
import time

from bigyear_planner.calendar_study import verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.util import ROOT, dump, read, sha256
from evaluate_lower48 import OUT, DATA as NATIONAL, guarded, router, prepare_audit


STUDY = ROOT/'data/calendar-strategy-study-2024-v1'
HOLDS = OUT/'search-calendar-controls-v2/rules-access/exact-access-holds.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--parent', required=True, type=Path)
    parser.add_argument('--cache-source', required=True, type=Path)
    parser.add_argument('--expected', required=True, type=int)
    args = parser.parse_args()
    if Path(args.tag).name != args.tag:
        raise ValueError('tag must be a single directory name')
    out = OUT/args.tag
    out.mkdir(exist_ok=True)
    candidate = read(args.candidate)
    ids = list(map(int, candidate['ids']))
    model_path = Path(read(args.parent/'problem.json')['compact_source']['path'])
    model = load(model_path)
    started = time.monotonic()
    try:
        if candidate['source_route_sha256'] != sha256(args.parent/'route.json'):
            raise ValueError('candidate parent calendar binding differs')
        binding = {'candidate_path': str(args.candidate.resolve()), 'candidate_sha256': sha256(args.candidate),
                   'parent_path': str(args.parent.resolve()), 'parent_sha256': sha256(args.parent/'route.json'),
                   'model_sha256': sha256(model.path), 'expected_species': args.expected,
                   'purpose': 'independent validation only; selected visits must remain exact'}
        previous = out/'audit-input.json'
        if previous.exists() and read(previous) != binding:
            raise ValueError('audit inputs differ from this output directory')
        dump(previous, binding)
        if (out/'route.json').exists():
            audit = read(out/'validation.json')
            if audit['valid'] and audit['route_sha256'] == sha256(out/'route.json'):
                print(read(out/'result.json'), flush=True)
                return
            raise ValueError('existing saved route has not passed audit; preserve it for diagnosis')
        with guarded(args.tag), ExitStack() as stack:
            process = stack.enter_context(router(args.tag, 120))
            flight = FlightModel(model, NATIONAL/'flights-2024')
            work = STUDY/(args.tag+'-cache')
            if not work.exists():
                shutil.copytree(args.cache_source, work)
            roads = ResolvedJointRoads(flight, NATIONAL/'roads/release.json', work,
                query_timeout_seconds=135, access_holds_path=HOLDS,
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            stack.callback(roads.close)
            _, inherited = seed_verified_joint(flight, roads, args.parent)
            dump(out/'baseline-binding.json', inherited)
            score = verify_fresh_calendar(flight, roads, out/'fresh-preparation', ids, timeout=300)
            if score['pending'] or not score['feasible'] or score['species'] != args.expected:
                raise ValueError('fresh measured evaluator did not reproduce the candidate')
            dump(out/'seed.json', {'ids': ids, 'mode': 'complete', 'model_sha256': sha256(model.path),
                'source_file': str(args.candidate.resolve()), 'source_sha256': sha256(args.candidate)})
            roads.prepare_audit = lambda: prepare_audit(stack, process, roads, args.tag)
            route = run(model.path, roads, out, seconds=.01, seed_file=out/'seed.json')
            if route['search']['selected_model_opportunities'] != ids or route['species_total'] != args.expected:
                raise ValueError('serialization altered the selected calendar')
            audit = read(out/'validation.json')
            if not audit['valid'] or audit['route_sha256'] != sha256(out/'route.json'):
                raise ValueError('independent source and transport validation did not pass')
            parent = read(args.parent/'route.json')
            before = {s for d in parent['days'] for stop in d['stops'] for s in stop['species']}
            after = {s for d in route['days'] for stop in d['stops'] for s in stop['species']}
            result = {'valid': True, 'species': route['species_total'], **route['scoring_tiers'],
                'route_sha256': sha256(out/'route.json'), 'candidate_sha256': sha256(args.candidate),
                'added_species': sorted(after-before), 'lost_species': sorted(before-after),
                'gap_to_759': 759-route['species_total'], 'full_calendar_days': audit['days_checked'],
                'publisher_records_checked': audit['joint_transport']['publisher_sources']['records_checked'],
                'wall_seconds': time.monotonic()-started, 'optimization_performed': False,
                'all_observations_retained': True, 'exact_coordinates': True, 'walking_kmh': 2.2,
                'rest_days': sum(d['kind'] == 'rest' for d in route['days']),
                'countability_and_sensitive_species_policy_unchanged': True}
            dump(out/'result.json', result)
            print(result, flush=True)
    finally:
        model.close()


if __name__ == '__main__':
    main()
