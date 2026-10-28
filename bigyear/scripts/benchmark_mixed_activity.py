"""Compare a verified incumbent under the relaxed shared activity budget."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import time

from bigyear_planner.calendar_study import native_session, verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.util import ROOT, dump, read, sha256
from evaluate_lower48 import OUT, DATA as NATIONAL, guarded, router, prepare_audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='shared-12h-2024-v1')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--incumbent', type=Path, default=OUT/'rest-opportunities-2024-v1')
    parser.add_argument('--model', type=Path, default=ROOT/'data/shared-12h-2024-v1/model/model.json')
    args = parser.parse_args()
    if Path(args.tag).name != args.tag or args.seconds <= 0:
        raise ValueError('require a single-directory tag and a positive search budget')
    base = args.incumbent.resolve()
    original = read(base/'route.json')
    audit = read(base/'validation.json')
    if not audit['valid'] or audit['route_sha256'] != sha256(base/'route.json'):
        raise ValueError('daily policy comparison requires a verified incumbent')
    out = OUT/args.tag
    work = ROOT/'data/calendar-strategy-study-2024-v1'/(args.tag+'-cache')
    if out.exists() or work.exists():
        raise ValueError('output or cache exists; preserve it and choose a new tag')
    out.mkdir()
    shutil.copytree(work.parent/(base.name+'-cache'), work)
    model = load(args.model)
    started = time.monotonic()
    try:
        if not model.meta['config'].get('mixed_road_birding'):
            raise ValueError('this comparison requires the relaxed daily policy')
        with guarded(args.tag), ExitStack() as stack:
            process = stack.enter_context(router(args.tag, 120))
            flight = FlightModel(model, NATIONAL/'flights-2024')
            roads = ResolvedJointRoads(flight, NATIONAL/'roads/release.json', work,
                query_timeout_seconds=135,
                access_holds_path=OUT/'search-calendar-controls-v2/rules-access/exact-access-holds.json',
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            stack.callback(roads.close)
            initial, binding = seed_verified_joint(flight, roads, base)
            dump(out/'baseline-binding.json', binding)
            with NativeEvaluator(flight, roads, out/'baseline-evaluator').open() as evaluator:
                baseline = evaluator.evaluate(initial, timeout=120)
            if not baseline['feasible'] or baseline['species'] != original['species_total']:
                raise ValueError('relaxed policy failed to preserve the verified baseline')
            print(json.dumps({'stage': 'baseline-replayed', 'score': baseline}), flush=True)
            roads.begin_phase('mixed-activity-calendar-search')
            with roads.bridge():
                search = native_session(flight, roads, out/'calendar-search', initial, args.seconds)
            ids = search['selected']
            dump(out/'search-result.json', {'ids': ids, 'score': search,
                'scope': 'All observations and exact coordinates; unanchored recorded-date and calendar-window replacement search under the shared daily budget.'})
            print(json.dumps({'stage': 'search-completed', 'species': search['species'],
                              'date_moves': search['date_move_changed']}), flush=True)
            roads.begin_phase('mixed-activity-audit')
            prepared = verify_fresh_calendar(flight, roads, out/'fresh-preparation', ids, timeout=300)
            if prepared['species'] != search['species']:
                raise ValueError('fresh evaluator changed the selected score')
            dump(out/'seed.json', {'ids': ids, 'mode': 'complete', 'model_sha256': sha256(model.path),
                'source_file': str(out/'search-result.json'), 'source_sha256': sha256(out/'search-result.json')})
            roads.prepare_audit = lambda: prepare_audit(stack, process, roads, args.tag)
            route = run(model.path, roads, out, seconds=.01, seed_file=out/'seed.json')
            audit = read(out/'validation.json')
            if (not audit['valid'] or audit['route_sha256'] != sha256(out/'route.json') or
                    route['search']['selected_model_opportunities'] != ids):
                raise ValueError('saved calendar failed independent validation')
            before = {s for day in original['days'] for stop in day['stops'] for s in stop['species']}
            after = {s for day in route['days'] for stop in day['stops'] for s in stop['species']}
            result = {'valid': True, 'species': route['species_total'], **route['scoring_tiers'],
                'baseline_species': original['species_total'], 'added_species': sorted(after-before),
                'lost_species': sorted(before-after), 'route_sha256': sha256(out/'route.json'),
                'all_observations_retained': True, 'exact_coordinates': True, 'walking_kmh': 2.2,
                'full_calendar_days': audit['days_checked'],
                'config_change': {'mixed_road_birding': True, 'drive_minutes': model.meta['config']['drive_minutes']},
                'birding_days_over_30_road_miles': sum(day['kind']=='birding' and day['road_miles']>30
                                                     for day in route['days']),
                'wall_seconds': time.monotonic()-started,
                'scope': 'Lower 48 and DC, 2024; finite short regression search, no full-ABA-area record comparison or optimality claim.'}
            dump(out/'result.json', result)
            print(json.dumps({'stage': 'completed', **result}), flush=True)
    finally:
        model.close()


if __name__ == '__main__':
    main()
