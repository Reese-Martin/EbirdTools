"""Upgrade a verified national itinerary with dated flights, then search briefly.

This follow-up starts from the longer driving trial and is deliberately separate
from the equal-budget comparison. All source candidates remain mapped.
"""

from contextlib import ExitStack
import argparse
import json
import time

from bigyear_planner.compact_model import load
from bigyear_planner.mobility import FlightModel, MobilityRoads
from bigyear_planner.national_runner import run
from bigyear_planner.regional_roads import DeferredRoad
from bigyear_planner.util import dump, read
from evaluate_lower48 import DATA, OUT, guarded, prepare_audit, router


def main(wait=False):
    if wait:
        deadline = time.monotonic()+900
        while read(OUT/'mobility-status.json')['stage'] != 'completed':
            if time.monotonic()>deadline or read(OUT/'run-status.json')['stage']=='failed':
                raise ValueError('matched trials did not complete successfully')
            time.sleep(3)
    if read(OUT/'mobility-status.json')['stage'] != 'completed':
        raise ValueError('finish the matched trials before this follow-up')
    baseline = OUT/'paired-driving-600s'
    if not read(baseline/'validation.json')['valid']:
        raise ValueError('the driving incumbent must pass its independent audit')
    tag = 'national-flights-60s'
    model = load(DATA/'model/model.json')
    try:
        overlay = FlightModel(model,DATA/'flights-2024')
        with guarded(tag), ExitStack() as routing:
            preflight = routing.enter_context(router(tag+'-preflight',120))
            roads = MobilityRoads(overlay,DATA/'roads/release.json',DATA/(tag+'-cache'),query_timeout_seconds=135)
            try:
                roads.copy_ground_cache(DATA/'benchmark-cache-driving')
                roads.seed_evidence(baseline/'route.json')
                roads.seed_incumbent(baseline/'route.json')
                windows = sorted([(plan['minutes'],key) for key,plan in roads.plan_cache.items()
                                  if key[3]-key[2]-1>=3 and key[0]!=key[1]],reverse=True)[:3]
                attempted = []
                for minutes,key in windows:
                    old = roads.plan_cache[key]
                    try:
                        upgraded = roads._build_transition(*key)
                        attempted.append({'window':key,'old':old,'new':upgraded})
                    except DeferredRoad as error:
                        attempted.append({'window':key,'old':old,'deferred':str(error)})
                dump(OUT/'flight-upgrade-windows.json',attempted)
                if not any(any(s['mode']=='flight' for s in v.get('new',{}).get('segments',[])) for v in attempted):
                    raise ValueError('none of the three longest incumbent gaps has a verified flight improvement')
                preflight.terminate();preflight.wait(timeout=60)
                process = routing.enter_context(router(tag,8))
                roads.query_timeout_seconds = 23
                roads.prepare_audit = lambda: prepare_audit(routing,process,roads,tag)
                dump(OUT/'run-status.json',{'stage':'search','run':tag,'best_species':440,
                     'label':'National flight integration check',
                     'message':'Upgrading the verified 440-species driving incumbent; separate from the matched comparison.'})
                route = run(model.path,roads,OUT/tag,seconds=60,resume=baseline/'checkpoint.json')
                if not any(leg['mode']=='flight' for day in route['days'] for leg in day['legs']):
                    raise ValueError('the audited integration follow-up has no flight')
                first = json.loads((OUT/tag/'improvements.jsonl').read_text().splitlines()[0])
                if first['selected'] != read(baseline/'checkpoint.json'):
                    raise ValueError('the initial flight comparison changed the source observations')
                old = read(baseline/'engine.json')
                summary = {'run':tag,'valid':read(OUT/tag/'validation.json')['valid'],
                           'species':route['species_total'],
                           'flight_days':sum(d['kind']=='flight' for d in route['days']),
                           'road_miles':sum(d['road_miles'] for d in route['days']),
                           'travel_minutes':sum(d['travel_minutes'] for d in route['days']),
                           'starting_driving_species':read(baseline/'validation.json')['species_total'],
                           'fixed_checkpoint':{'species':first['species'],
                               'road_miles_before':old['road_miles'],'road_miles_after':first['road_miles'],
                               'travel_minutes_before':old['travel_minutes'],'travel_minutes_after':first['travel_minutes'],
                               'selected_observations_unchanged':True},
                           'purpose':'Flight integration follow-up from the longer verified driving result; excluded from matched performance comparison.'}
                dump(OUT/'flight-integration-result.json',summary)
                dump(OUT/'run-status.json',{'stage':'completed','run':tag,'best_run':tag,
                     'best_species':route['species_total'], 'message':'Matched comparison and national flight integration independently validated.'})
                print(json.dumps(summary),flush=True)
            finally:
                roads.close()
    finally:
        model.close()


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--wait-for-pair',action='store_true')
    args = parser.parse_args()
    main(args.wait_for_pair)
