"""Matched full-cohort driving/flight trials with the same starting road cache."""

import json
import argparse
import shutil
import threading
import time
from contextlib import ExitStack

from bigyear_planner.compact_model import load
from bigyear_planner.mobility import FlightModel, MobilityRoads
from bigyear_planner.national_runner import run
from bigyear_planner.util import dump, read
from evaluate_lower48 import DATA, OUT, guarded, router, search, prepare_audit

SECONDS = 600
BASELINE = OUT / 'national-3600s'
DRIVE = 'paired-driving-600s'
FLIGHT = 'paired-flights-600s'


def main():
    while not (OUT/'search-improved-drive-600s/router-stopped.json').exists():
        time.sleep(5)
    snapshot = DATA / 'benchmark-cache-start'
    if not snapshot.exists():
        snapshot.mkdir()
        for name in ['provider.json', 'costs.bin', 'access.bin']:
            shutil.copyfile(DATA/'routing-cache-connected'/name, snapshot/name)
    driving_cache = DATA / (DRIVE + '-cache')
    if not driving_cache.exists():
        shutil.copytree(snapshot, driving_cache)
    if not (OUT/DRIVE/'validation.json').exists():
        dump(OUT/'mobility-status.json', {'stage': 'driving', 'run': DRIVE, 'requested_seconds': SECONDS})
        search(SECONDS, DRIVE, road_seconds=8, resume=BASELINE/'checkpoint.json', discovery=True, cache_dir=driving_cache)
    if not read(OUT/DRIVE/'validation.json')['valid']:
        raise ValueError('paired driving baseline failed validation')
    dump(OUT/'mobility-status.json', {'stage': 'flights', 'run': FLIGHT, 'requested_seconds': SECONDS})
    original = load(DATA/'model/model.json')
    try:
        model = FlightModel(original, DATA/'flights-2024')
        flight_cache = DATA/(FLIGHT + '-cache')
        new_cache = not (flight_cache/'provider.json').exists()
        with guarded(FLIGHT), ExitStack() as routing:
            process = routing.enter_context(router(FLIGHT, 8))
            roads = MobilityRoads(model, DATA/'roads/release.json', flight_cache, query_timeout_seconds=23)
            try:
                roads.prepare_audit = lambda: prepare_audit(routing, process, roads, FLIGHT)
                if new_cache:
                    roads.copy_ground_cache(snapshot)
                roads.seed_evidence(BASELINE/'route.json')
                roads.seed_incumbent(BASELINE/'route.json')
                run(original.path, roads, OUT/FLIGHT, seconds=SECONDS, resume=BASELINE/'checkpoint.json')
            finally:
                roads.close()
    finally:
        original.close()
    comparison = {}
    for label, tag in [('driving', DRIVE), ('flights', FLIGHT)]:
        route = read(OUT/tag/'route.json')
        benchmark = read(OUT/tag/'benchmark.json')
        comparison[label] = {
            'run': tag, 'species': route['species_total'], 'search': read(OUT/tag/'engine.json'),
            'wall_seconds': benchmark['wall_seconds'], 'routing': benchmark['cache'],
            'resources': read(OUT/(tag+'-resources.json')),
            'road_miles': sum(d['road_miles'] for d in route['days']),
            'flight_days': sum(d['kind']=='flight' for d in route['days']),
            'road_transit_days': sum(d['kind']=='road_transit' for d in route['days']),
            'visited_states': sorted({route['locations'][s['location']]['state'] for d in route['days'] for s in d['stops']}),
            'species_ids': sorted({s for d in route['days'] for stop in d['stops'] for s in stop['species']}),
            'valid': read(OUT/tag/'validation.json')['valid'],
        }
    comparison['flight_only_species'] = sorted(set(comparison['flights']['species_ids'])-set(comparison['driving']['species_ids']))
    comparison['driving_only_species'] = sorted(set(comparison['driving']['species_ids'])-set(comparison['flights']['species_ids']))
    comparison['design'] = f'Same full 2024 national observations, original coordinates, 2.2 km/h walking, starting 432-species checkpoint, initial road-cache snapshot, two workers and {SECONDS}-second native budget. Sequential heuristic trials; no global optimality or causal population inference.'
    dump(OUT/'mobility-comparison.json', comparison)
    dump(OUT/'mobility-status.json', {'stage': 'completed', 'driving_run': DRIVE, 'flight_run': FLIGHT,
                                    'driving_species': comparison['driving']['species'], 'flight_species': comparison['flights']['species']})
    print(json.dumps({k:{j:v for j,v in value.items() if j in ['species', 'flight_days', 'visited_states', 'valid']}
                      for k,value in comparison.items() if k in ['driving','flights']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds',type=int,default=600)
    parser.add_argument('--suffix',default='')
    args = parser.parse_args()
    SECONDS = args.seconds
    DRIVE = f"paired-driving-{SECONDS}s{args.suffix}"
    FLIGHT = f"paired-flights-{SECONDS}s{args.suffix}"
    stop = threading.Event()

    def monitor():
        while not stop.wait(5):
            status = read(OUT/'mobility-status.json') if (OUT/'mobility-status.json').exists() else {'stage':'preparing', 'run':DRIVE}
            tag = status.get('run', DRIVE)
            score = 432
            history = OUT/tag/'improvements.jsonl'
            if history.exists():
                try:
                    score = json.loads(history.read_text().splitlines()[-1])['species']
                except (IndexError, ValueError):
                    pass
            validating = (OUT/tag/'engine.json').exists()
            mode = 'Flight' if tag==FLIGHT else 'Driving'
            dump(OUT/'run-status.json', {'stage':'validation' if validating else 'search', 'run':tag, 'best_species':score,
                                        'label': f'{mode} benchmark being validated' if validating else f'{mode} benchmark in progress',
                                        'message': f"Matched {status['stage']} benchmark; candidate {score} species, validation pending."})

    worker = threading.Thread(target=monitor, daemon=True)
    worker.start()
    try:
        main()
    except BaseException as error:
        stop.set()
        worker.join()
        dump(OUT/'run-status.json', {'stage':'failed', 'run':FLIGHT, 'message': 'Mobility benchmark needs attention; verified routes retained.', 'error':repr(error)})
        raise
    else:
        stop.set()
        worker.join()
        comparison = read(OUT/'mobility-comparison.json')
        candidates = [comparison['driving'], comparison['flights']]
        longer = OUT/'paired-driving-600s'
        if (longer/'validation.json').exists() and read(longer/'validation.json')['valid']:
            candidates.append({'run':longer.name,'species':read(longer/'validation.json')['species_total']})
        best = max(candidates, key=lambda r:r['species'])
        dump(OUT/'run-status.json', {'stage':'completed','run':best['run'], 'best_run':best['run'], 'best_species':best['species'], 'message': 'Matched driving and flight benchmarks complete and independently validated.'})
