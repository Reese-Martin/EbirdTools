"""Separate scoring/access follow-up after the matched primary-only controls."""
from contextlib import ExitStack
import csv
import json
import os
import subprocess
import time

from bigyear_planner.compact_model import load
from bigyear_planner.mobility import FlightModel,MobilityRoads
from bigyear_planner.national_runner import run
from bigyear_planner.util import ROOT,dump,read,sha256
from evaluate_lower48 import OUT,DATA as ORIGINAL,guarded,prepare_audit,router
from reconcile_2024_rules import DATA,POLICY

TAG='reconciled-2024-provisional-access-v3'
CONTROLS=OUT/'search-calendar-controls-v2'


def main():
    folder=OUT/TAG;folder.mkdir(exist_ok=True)
    # Dependencies run in this same authorized evaluation; no scheduled job.
    deadline=time.monotonic()+3600
    while not (CONTROLS/'summary.json').exists() or not (DATA/'model/model.json').exists():
        if time.monotonic()>deadline:raise RuntimeError('comparison/model dependency did not finish')
        time.sleep(5)
    from bigyear_planner.regional_exploration import NativeEvaluator,RegionalExplorer
    from benchmark_aba2024 import remap_seed
    previous=CONTROLS/'final'
    holds=CONTROLS/'rules-access/exact-access-holds.json'
    dump(holds,{'reason':'Known straight-line walk crosses >100m of open sea. Foot-path or dated boat access remains unresolved.',
        'audit_sha256':sha256(CONTROLS/'rules-access/marine-access-audit.json'),
        'exact_coordinate_holds':[[41.310337,-71.87439]],
        'no_source_observations_deleted':True})
    awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    model=load(DATA/'model/model.json')
    try:
        with guarded(TAG),ExitStack() as stack:
            process=stack.enter_context(router(TAG,8))
            overlay=FlightModel(model,ORIGINAL/'flights-2024')
            roads=MobilityRoads(overlay,ORIGINAL/'roads/release.json',DATA/(TAG+'-cache'),
                query_timeout_seconds=23,access_holds_path=holds)
            try:
                initial,binding=remap_seed(overlay,roads,previous,
                    ROOT/'data/lower48-aba2024-aligned/search-calendar-controls-v2-new-local30-cache',
                    allow_access_holds=True)
                safe=[i for i in initial if tuple(map(float,model.coords[int(model.records[i]['location'])])) not in roads.access_holds]
                binding['surface_held_stops_removed_from_seed']=len(initial)-len(safe)
                dump(folder/'remapped-incumbent.json',binding)
                with NativeEvaluator(overlay,roads,folder).open() as evaluator:
                    score=evaluator.evaluate(safe,timeout=120)
                    if score['pending'] or not score['feasible']:raise ValueError('held-stop seed repair remains unresolved')
                    dump(folder/'baseline-rescored.json',score)
                    print(json.dumps({'stage':'rescored',**score}),flush=True)
                    construction=RegionalExplorer(overlay,evaluator,folder).explore(safe,seconds=300)
                dump(folder/'seed.json',{'ids':construction['ids'],'mode':'complete','model_sha256':sha256(model.path),
                    'source_file':str(folder/'regional-exploration.json'),'source_sha256':sha256(folder/'regional-exploration.json')})
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,TAG)
                route=run(model.path,roads,folder,seconds=300,seed_file=folder/'seed.json',blocks_path=folder/'regional-blocks.txt')
                taxonomy={t['species_id']:t['score_tier'] for t in csv.DictReader((DATA/'taxonomy.csv').open())}
                selected={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
                provisional=sorted(s for s in selected if taxonomy[s].startswith('provisional'))
                result={'policy':POLICY,'species':route['species_total'],'primary':len(selected)-len(provisional),
                    'provisional':len(provisional),'provisional_species':provisional,
                    'annual_modeled_pool':len(model.meta['species']),'walking_kmh':2.2,
                    'exact_coordinates':True,'all_observations_retained':True,'open_water_walk_hold_enforced':True,
                    'validation':read(folder/'validation.json'),'route_sha256':sha256(folder/'route.json'),
                    'publisher_source_postcheck':'pending','recordholder_replay':'pending_input',
                    'boats':'Held unless dated access can be validated; native vessel search not yet implemented.'}
                dump(folder/'result.json',result);print(json.dumps(result),flush=True)
            finally:roads.close()
    finally:
        model.close()
        if awake.poll() is None:awake.terminate();awake.wait(timeout=5)
        dump(folder/'cleanup.json',{'process_scoped_idle_assertion_released':True})


if __name__=='__main__':main()
