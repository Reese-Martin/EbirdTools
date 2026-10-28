"""Matched 2024 search/calendar controls, then a bounded improved search.

All arms begin from the same verified 648-species itinerary and byte-identical
warm road caches. A 150-mile local-driving cap is a sensitivity experiment,
not a changed ABA rule. Flight days, 2h visits and 2.2 km/h walks stay fixed.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
import shutil
import subprocess
import time

from bigyear_planner.compact_model import load
from bigyear_planner.mobility import FlightModel,MobilityRoads
from bigyear_planner.national_runner import export_native,run
from bigyear_planner.regional_exploration import NativeEvaluator,RegionalExplorer
from bigyear_planner.runner import compile_engine
from bigyear_planner.util import ROOT,dump,read,sha256
from evaluate_lower48 import OUT,DATA as ORIGINAL,guarded,prepare_audit,router

DATA=ROOT/'data/lower48-aba2024-aligned'
PREVIOUS=OUT/'aba2024-aligned-flights-900s-600s'
TAG='search-calendar-controls-v2'
FOLDER=OUT/TAG
OLD_CACHE=DATA/(PREVIOUS.name+'-cache')


def calendar_model(miles):
    source=DATA/'model/model.json'
    if miles==30:return source
    target=DATA/f'calendar-local-{miles}'
    target.mkdir(exist_ok=True)
    meta=deepcopy(read(source));meta['config']['local_miles']=miles
    meta['calendar_source']={'path':str(source),'sha256':sha256(source),
       'purpose':'Only local driving cap changed; all observation components shared read-only.'}
    for name in meta['components']:
        link=target/name
        if not link.exists():os.link(source.parent/name,link)
    dump(target/'model.json',meta)
    return target/'model.json'


def roads_for(model,work,separate=True):
    if not work.exists():
        work.mkdir(parents=True)
        for name in ['provider.json','costs.bin','access.bin']:
            shutil.copyfile(OLD_CACHE/name,work/name)
    roads=MobilityRoads(model,ORIGINAL/'roads/release.json',work,
                        query_timeout_seconds=23,separate_discovery=separate)
    roads.seed_incumbent(PREVIOUS/'route.json')
    roads.seed_evidence(PREVIOUS/'route.json')
    return roads


def native_control(model,roads,binary,out,initial,seconds=180):
    out.mkdir(parents=True,exist_ok=True)
    dump(out/'initial.json',initial)
    snapshot=out/'engine-snapshot';shutil.copyfile(binary,snapshot);snapshot.chmod(0o755)
    # A wider same-day cap can ask direct roads the old full-day itinerary
    # never needed. Resolve the shared incumbent before starting either timer.
    with NativeEvaluator(model,roads,out/'seed-check').open() as evaluator:
        seed_score=evaluator.evaluate(initial,timeout=90)
    if seed_score['pending'] or not seed_score['feasible']:
        raise ValueError('controlled incumbent is not fully measured under this calendar')
    dump(out/'seed-score.json',seed_score)
    initial_cache={n:sha256(roads.work/n) for n in ['provider.json','costs.bin','access.bin']}
    started=time.monotonic()
    native=out/'problem.by'
    try:
        with roads.bridge():
            export_native(model,roads,native)
            cmd=[str(snapshot),str(native),str(out),str(seconds),'2','20240101','1','2','0',str(out/'initial.json')]
            with (out/'engine.log').open('w') as log:
                subprocess.run(cmd,check=True,stdout=log,stderr=subprocess.STDOUT)
        result=read(out/'engine.json')
        with NativeEvaluator(model,roads,out).open() as evaluator:
            checked=evaluator.evaluate(result['selected'],timeout=30)
        if checked['pending'] or not checked['feasible'] or checked['species']!=result['species']:
            raise ValueError('controlled candidate failed re-evaluation')
        dump(out/'result.json',{'engine':result,'re_evaluated':checked,'cache':roads.metrics(),
            'wall_seconds':time.monotonic()-started,'model_sha256':sha256(model.path),
            'binary_sha256':sha256(snapshot),'initial_sha256':sha256(out/'initial.json'),
            'initial_cache_after_seed_measurement':initial_cache,
            'complete_endpoint_and_source_audit':'Only final published winner is independently frozen/audited.'})
        return result
    finally:
        if native.exists():native.unlink()


def main():
    FOLDER.mkdir(exist_ok=True)
    if (FOLDER/'summary.json').exists():raise ValueError('completed comparison exists')
    route=read(PREVIOUS/'route.json')
    if not read(PREVIOUS/'validation.json')['valid']:raise ValueError('seed invalid')
    initial=route['search']['selected_model_opportunities']
    old=PREVIOUS/'engine-snapshot';new=compile_engine()
    frozen=FOLDER/'improved-engine';shutil.copyfile(new,frozen);frozen.chmod(0o755)
    dump(FOLDER/'design.json',{'baseline_route_sha256':sha256(PREVIOUS/'route.json'),
        'observations':'All 129,187,831 represented observations, all exact source locations and dates.',
        'matrix':'old shared scheduling/global date proposals vs new reserved workers/indexed dates/backpressure; local cap 30 vs 150 miles',
        'native_active_seconds_per_arm':180,'threads':2,'seed':20240101,
        'regional_followup_seconds':600,'native_followup_seconds':300,
        'calendar_constants':{'walking_kmh':2.2,'activity_hours':12,'drive_hours':8,'birding_hours_per_stop':2,'full_flight_days':True},
        'recordholder_replay':'pending verified dated itinerary; no surrogate used'})
    awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    results=[]
    try:
        with guarded(TAG),ExitStack() as stack:
            process=stack.enter_context(router(TAG,8))
            for miles in [30,150]:
                for version,binary in [('old',old),('new',frozen)]:
                    label=f'{version}-local{miles}'
                    out=FOLDER/label
                    model=load(calendar_model(miles))
                    roads=None
                    try:
                        overlay=FlightModel(model,ORIGINAL/'flights-2024')
                        roads=roads_for(overlay,DATA/(TAG+'-'+label+'-cache'),version=='new')
                        if (out/'result.json').exists():result=read(out/'result.json')['engine']
                        else:result=native_control(overlay,roads,binary,out,initial)
                        results.append({'arm':label,'species':result['species'],
                            'evaluations':result['candidate_evaluations'],'deferred':result['deferred_candidates'],
                            'local_miles':miles,'result':str(out/'result.json')})
                        print(json.dumps({'stage':'control',**results[-1]}),flush=True)
                        dump(FOLDER/'progress.json',results)
                    finally:
                        if roads is not None:roads.close()
                        model.close()
            # Follow up under the ORIGINAL calendar, isolating search changes.
            model=load(calendar_model(30));overlay=FlightModel(model,ORIGINAL/'flights-2024')
            roads=roads_for(overlay,DATA/(TAG+'-new-local30-cache'))
            try:
                start=read(FOLDER/'new-local30/engine.json')['selected']
                with NativeEvaluator(overlay,roads,FOLDER/'regional').open() as evaluator:
                    construction=RegionalExplorer(overlay,evaluator,FOLDER/'regional').explore(start,seconds=600)
                dump(FOLDER/'seed.json',{'ids':construction['ids'],'mode':'complete','model_sha256':sha256(model.path),
                    'source_file':str(FOLDER/'regional/regional-exploration.json'),
                    'source_sha256':sha256(FOLDER/'regional/regional-exploration.json')})
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,TAG)
                final=run(model.path,roads,FOLDER/'final',seconds=300,seed_file=FOLDER/'seed.json',blocks_path=FOLDER/'regional/regional-blocks.txt')
                dump(FOLDER/'summary.json',{'controls':results,'final_species':final['species_total'],
                    'validation':read(FOLDER/'final/validation.json'),'recordholder_replay':'pending_input',
                    'final_route':str(FOLDER/'final/route.json'),'final_route_sha256':sha256(FOLDER/'final/route.json')})
                print(json.dumps({'stage':'completed','species':final['species_total']}),flush=True)
            finally:roads.close();model.close()
    finally:
        if awake.poll() is None:awake.terminate();awake.wait(timeout=5)
        dump(FOLDER/'cleanup.json',{'idle_sleep_assertion_released':True,'routers_owned_by_context':True})


if __name__=='__main__':main()
