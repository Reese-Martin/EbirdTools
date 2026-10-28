"""Bounded full-calendar insertion probes; never replaces the verified route."""

from contextlib import ExitStack
import csv
import json
import shutil
import time

from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.regional_exploration import NativeEvaluator
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.util import ROOT, dump, read, sha256
from evaluate_lower48 import router
from analyze_remaining_2024 import BASE, OUTPUT


TAG = 'remaining-opportunities-2024-v1'
NATIONAL = ROOT/'data/lower48-eod-2024'
STUDY = ROOT/'data/calendar-strategy-study-2024-v1'


def main():
    output = OUTPUT/'insertion-probes'
    if output.exists():
        raise ValueError('insertion probe output exists; preserve its diagnostic evidence')
    output.mkdir()
    work = STUDY/(TAG+'-cache')
    if work.exists():
        raise ValueError('probe cache already exists')
    previous = STUDY/(BASE.name+'-cache')
    shutil.copytree(previous, work)
    model = load(read(BASE/'problem.json')['compact_source']['path'])
    before = sha256(BASE/'route.json')
    try:
        with ExitStack() as stack:
            stack.enter_context(router(TAG, 45))
            flight = FlightModel(model, NATIONAL/'flights-2024')
            roads = ResolvedJointRoads(flight, NATIONAL/'roads/release.json', work,
                query_timeout_seconds=55,
                access_holds_path=ROOT/'runs/lower48-eod-2024/search-calendar-controls-v2/rules-access/exact-access-holds.json',
                source_access_holds_path=ROOT/'data/lower48-joint-mobility-2024-v1/source-visit-holds.json')
            stack.callback(roads.close)
            initial, binding = seed_verified_joint(flight, roads, BASE)
            dump(output/'baseline-binding.json', binding)
            evaluator = stack.enter_context(NativeEvaluator(flight, roads, output/'evaluator').open())
            baseline = evaluator.evaluate(initial, timeout=90)
            if baseline['pending'] or not baseline['feasible'] or baseline['species'] != 691:
                raise ValueError('probe did not reproduce verified 691 baseline')
            current, best = initial, baseline
            candidates = list(csv.DictReader((OUTPUT/'candidate-visits.csv').open()))
            priority = ['Mississippi Kite', 'Blue-crowned Parakeet', "Kirtland's Warbler",
                        'Aplomado Falcon', 'Gray Partridge', 'Ferruginous Pygmy-Owl',
                        'Black Swift', 'Black-backed Woodpecker', 'Little Gull',
                        'Brambling', 'Smooth-billed Ani', 'Eurasian Tree Sparrow']
            plans = []
            for rank in range(5):
                for species in priority:
                    options = [r for r in candidates if r['species'] == species and not int(r['boat_variant'])]
                    options.sort(key=lambda r: (r['kind'] != 'same_birding_date',
                        float(r['air_miles_to_calendar_anchor_or_corridor']), int(r['opportunity'])))
                    unique = {int(r['opportunity']): r for r in reversed(options)}
                    options = list(reversed(list(unique.values())))
                    if rank < len(options): plans.append(options[rank])
            started = time.monotonic()
            deadline = started+240
            entries, accepted, pending = [], [], []

            def probe(row, timeout):
                nonlocal current, best
                head = int(row['opportunity'])
                candidate = sorted(set([*current, head]), key=lambda i: (int(model.records[i]['day']), i))
                score = evaluator.evaluate(candidate, timeout=timeout)
                entry = {**row, **score, 'incumbent_species': best['species']}
                entries.append(entry)
                if not score['pending'] and score['feasible'] and score['species'] > best['species']:
                    current, best = candidate, score
                    accepted.append(entry)
                    dump(output/'candidate-calendar.json', {'ids': current, 'score': best,
                         'not_independently_validated': True, 'source_route_sha256': before})
                log.write(json.dumps(entry)+'\n'); log.flush()
                print(json.dumps({k: entry[k] for k in ['species_id','date','pending','feasible','species','failure_day']}), flush=True)
                return score

            with (output/'attempts.jsonl').open('w') as log:
                for row in plans:
                    if time.monotonic() >= deadline: break
                    score = probe(row, min(3, max(.01, deadline-time.monotonic())))
                    if score['pending']: pending.append(row)
                for row in pending:
                    if time.monotonic() >= deadline: break
                    probe(row, min(8, max(.01, deadline-time.monotonic())))
            result = {'baseline_species': 691, 'candidate_score': best, 'accepted': accepted,
                      'attempts': len(entries), 'pending_attempts': sum(e['pending'] for e in entries),
                      'seconds': time.monotonic()-started,
                      'route_unchanged': before == sha256(BASE/'route.json'),
                      'baseline_sha256': before, 'model_sha256': sha256(model.path),
                      'policy': 'Full native drive/flight/boat calendar and measured roads; no source/coordinate/score/access changes.',
                      'status': 'Diagnostic evaluator results only. A new credited route requires independent source and transport validation.'}
            dump(output/'result.json', result)
            dump(output/'cache-metrics.json', roads.metrics())
            print(json.dumps({'candidate_species': best['species'], 'accepted': len(accepted), 'attempts': len(entries)}), flush=True)
    finally:
        model.close()


if __name__ == '__main__':
    main()
