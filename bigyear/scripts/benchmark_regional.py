"""Resource-bounded, full-cohort regional construction plus native flight search."""

import argparse
from contextlib import ExitStack
import json
import shutil
import threading

from bigyear_planner.compact_model import load
from bigyear_planner.mobility import FlightModel, MobilityRoads
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator, RegionalExplorer
from bigyear_planner.util import dump, read, sha256
from evaluate_lower48 import DATA, OUT, guarded, prepare_audit, router


def main(args):
    tag = args.tag
    folder = OUT/tag
    if (folder/'route.json').exists():
        raise ValueError('finished result exists; choose another tag')
    folder.mkdir(parents=True,exist_ok=True)
    baseline = OUT/'national-flights-60s'
    if not read(baseline/'validation.json')['valid']:
        raise ValueError('starting national flight itinerary is not audited')
    cache = DATA/(tag+'-cache')
    if not cache.exists():
        shutil.copytree(DATA/'national-flights-60s-cache',cache)
    before = sha256(DATA.parent/'eod-us-since-2000-zstd3/manifest.json')
    dump(folder/'benchmark-design.json',{'baseline':baseline.name,'exploration_seconds':args.explore,
        'native_seconds':args.seconds,'threads':2,'first_species_milestone':500,'working_species_target':705,
        'observation_policy':'Complete eligible 2024 lower-48/DC cohort; exact source coordinates; 2.2 km/h round-trip walking above 100 m.',
        'flight_policy':'Observed 2024 nonstop service; full flight days and separately measured airport driving.',
        'source_archive_manifest_sha256':before})
    stop = threading.Event()
    phase = ['preparation']

    def monitor():
        while not stop.wait(5):
            score = 440
            if (folder/'regional-progress.json').exists():
                score = read(folder/'regional-progress.json')['species']
            history = folder/'improvements.jsonl'
            if history.exists():
                try:
                    score = json.loads(history.read_text().splitlines()[-1])['species']
                except (IndexError,ValueError):
                    pass
            validating = (folder/'engine.json').exists()
            dump(OUT/'run-status.json',{'stage':'validation' if validating else 'search','run':tag,
                 'best_species':score,'label':'Regional route being validated' if validating else 'Regional exploration and flights',
                 'message':f'{phase[0]}; {score}-species candidate. Final source/calendar/road/flight validation pending.'})

    monitor_thread = threading.Thread(target=monitor,daemon=True);monitor_thread.start()
    model = load(DATA/'model/model.json')
    try:
        overlay = FlightModel(model,DATA/'flights-2024')
        with guarded(tag), ExitStack() as routing:
            process = routing.enter_context(router(tag,8))
            roads = MobilityRoads(overlay,DATA/'roads/release.json',cache,query_timeout_seconds=23)
            try:
                roads.seed_evidence(baseline/'route.json')
                roads.seed_incumbent(baseline/'route.json')
                roads.prepare_audit = lambda: prepare_audit(routing,process,roads,tag)
                phase[0] = 'Building and joining complete regional trips'
                with NativeEvaluator(overlay,roads,folder).open() as evaluator:
                    explorer = RegionalExplorer(overlay,evaluator,folder)
                    result = explorer.explore(read(baseline/'checkpoint.json'),seconds=args.explore)
                seed_file = folder/'regional-seed.json'
                source = folder/'regional-exploration.json'
                dump(seed_file,{'ids':result['ids'],'mode':'complete','model_sha256':sha256(model.path),
                     'source_file':str(source),'source_sha256':sha256(source),
                     'method':'Complete regional candidate reevaluated by native full-model calendar before search; no greedy rebuild.'})
                phase[0] = 'Full-cohort native search with regional block proposals'
                route = run(model.path,roads,folder,seconds=args.seconds,seed_file=seed_file,
                            blocks_path=folder/'regional-blocks.txt')
                if sha256(DATA.parent/'eod-us-since-2000-zstd3/manifest.json')!=before:
                    raise ValueError('original observation archive manifest changed')
                locations = [route['locations'][s['location']] for d in route['days'] for s in d['stops']]
                summary = {'run':tag,'valid':read(folder/'validation.json')['valid'],'species':route['species_total'],
                     'visited_states':sorted({v['state'] for v in locations}),
                     'westernmost_birding_longitude':min(v['lon'] for v in locations),
                     'easternmost_birding_longitude':max(v['lon'] for v in locations),
                     'road_miles':sum(d['road_miles'] for d in route['days']),
                     'travel_minutes':sum(d['travel_minutes'] for d in route['days']),
                     'flight_days':sum(d['kind']=='flight' for d in route['days']),
                     'regional_construction':result,'all_observations_retained':True,
                     'source_archive_unchanged':True,'species_milestone_500':route['species_total']>=500}
                dump(OUT/'regional-benchmark-result.json',summary)
            finally:
                roads.close()
    finally:
        stop.set();monitor_thread.join();model.close()
    dump(OUT/'run-status.json',{'stage':'completed','run':tag,'best_run':tag,'best_species':route['species_total'],
         'message':'Regional exploration benchmark complete and independently validated.'})
    print(json.dumps({k:v for k,v in summary.items() if k!='regional_construction'}),flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--explore',type=int,default=900)
    parser.add_argument('--seconds',type=int,default=600)
    parser.add_argument('--tag',default='regional-flights-900s-600s')
    args = parser.parse_args()
    try:
        main(args)
    except BaseException as error:
        dump(OUT/'run-status.json',{'stage':'failed','run':args.tag,
             'message':'Regional benchmark needs attention; previous independently verified routes retained.','error':repr(error)})
        raise
