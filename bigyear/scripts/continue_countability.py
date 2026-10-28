"""Complete an awake-time native budget from a validated audited incumbent."""

from contextlib import ExitStack
import json
import os
import shutil
import subprocess

from bigyear_planner.compact_model import load
from bigyear_planner.mobility import FlightModel,MobilityRoads
from bigyear_planner.national_runner import run
from bigyear_planner.util import dump,read,sha256
from audit_countability import DATA,OUT,POLICY
from evaluate_lower48 import DATA as ORIGINAL,guarded,prepare_audit,router

PREVIOUS='countability-flights-900s-600s'
TAG='countability-flights-awake-600s'


def main():
    previous=OUT/PREVIOUS;folder=OUT/TAG;folder.mkdir(exist_ok=True)
    if (folder/'route.json').exists():
        raise ValueError('completed awake-time result exists')
    if not read(previous/'validation.json')['valid'] or not read(previous/'countability-source-validation.json')['valid']:
        raise ValueError('continuation requires both independent and publisher-source audits')
    old=read(previous/'route.json')
    cache=DATA/(TAG+'-cache')
    if not cache.exists():
        shutil.copytree(DATA/(PREVIOUS+'-cache'),cache)
    # This assertion lasts only for this owned process. It does not change
    # persistent power settings, display sleep, or user-requested suspension.
    awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    model=load(DATA/'model/model.json')
    try:
        with guarded(TAG),ExitStack() as stack:
            overlay=FlightModel(model,ORIGINAL/'flights-2024')
            process=stack.enter_context(router(TAG,8))
            roads=MobilityRoads(overlay,ORIGINAL/'roads/release.json',cache,query_timeout_seconds=23)
            try:
                roads.seed_evidence(previous/'route.json');roads.seed_incumbent(previous/'route.json')
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,TAG)
                dump(folder/'continuation-binding.json',{'source_route':str(previous/'route.json'),
                     'source_route_sha256':sha256(previous/'route.json'),'requested_active_seconds':600,
                     'reason':'A documented 903-second idle sleep exhausted libc++ steady_clock time in the previous attempt. macOS search now uses mach_absolute_time, matching Python monotonic awake-time accounting.',
                     'clock':'macOS mach_absolute_time; excludes suspension',
                     'idle_sleep_assertion':'Scoped caffeinate -i -w for this process only.'})
                shutil.copyfile(previous/'regional-exploration.json',folder/'regional-exploration.json')
                dump(OUT/'run-status.json',{'stage':'search','run':TAG,'best_species':old['species_total'],
                     'label':'Countability search: full active-time continuation',
                     'message':'Completing 600 active seconds from the independently validated 610-species audited route.'})
                route=run(model.path,roads,folder,seconds=600,resume=previous/'checkpoint.json',blocks_path=previous/'regional-blocks.txt')
                summary=read(OUT/'countability-benchmark-result.json')
                summary.update(run=TAG,species=route['species_total'],valid=read(folder/'validation.json')['valid'],
                     birding_states=sorted({route['locations'][s['location']]['state'] for d in route['days'] for s in d['stops']}),
                     flight_days=sum(d['kind']=='flight' for d in route['days']),
                     road_miles=sum(d['road_miles'] for d in route['days']),
                     resumed_from=PREVIOUS,active_time_clock_corrected=True)
                if route['taxonomy_policy']!=POLICY or route['species_total']<old['species_total']:
                    raise ValueError('continuation lost the audited incumbent or changed policy')
                dump(OUT/'countability-benchmark-result.json',summary)
                print(json.dumps(summary),flush=True)
            finally:
                roads.close()
    finally:
        model.close()
        if awake.poll() is None:
            awake.terminate();awake.wait(timeout=5)
        dump(folder/'idle-sleep-assertion-released.json',{'released':True,'process_scoped':True})
    dump(OUT/'run-status.json',{'stage':'completed','run':TAG,'best_run':TAG,'best_species':route['species_total'],
         'message':'Full active-time audited search completed and independently validated. Final publisher-source checks pending.'})


if __name__=='__main__':
    try:
        main()
    except BaseException as error:
        dump(OUT/'run-status.json',{'stage':'failed','run':TAG,
             'message':'Active-time continuation needs attention; previous audited route retained.','error':repr(error)})
        raise
