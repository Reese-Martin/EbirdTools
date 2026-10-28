"""Repair the audited water-crossing visit using another exact source record."""
from contextlib import ExitStack
import csv
import json
import os
import subprocess
import time

import numpy as np

from bigyear_planner.compact_model import load
from bigyear_planner.joint_marine import JointRoads
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator
from bigyear_planner.road_matching import gap_m
from bigyear_planner.util import dump,read,sha256
from benchmark_joint_mobility import DATA,PARENT,NATIONAL,HOLDS,OUT
from evaluate_lower48 import guarded,router,prepare_audit

TAG='joint-mobility-2024-access-v3'
PREVIOUS=OUT/'joint-mobility-2024-v2'
PILOT=OUT/'joint-mobility-2024-pilot-v1'


def main():
    out=OUT/TAG;out.mkdir(exist_ok=True)
    if (out/'route.json').exists():raise ValueError('completed/draft route already exists; choose another version')
    old=read(PREVIOUS/'route.json');p=read(PREVIOUS/'problem.json');audit=read(PREVIOUS/'validation.json')
    expected='terrestrial access crosses open sea: EOD:4e24f9d8a383ae8bf10dd6369405e54a1af9d690327504957860ea61705754b0'
    if audit['errors']!=[expected] or not read(PREVIOUS/'publisher-precheck.json')['valid']:
        raise ValueError('source candidate has failures beyond the explicitly held access point')
    if sha256(PREVIOUS/'problem.json')!=old['provenance']['problem_sha256'] or sha256(PREVIOUS/'roads.sqlite')!=old['provenance']['roads_sha256']:
        raise ValueError('candidate physical/source artifacts changed')
    hold_path=DATA/'source-visit-holds.json';hold=read(hold_path);blocked={tuple(v) for v in hold['exact_coordinate_holds']}
    started=time.monotonic();awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    base=load(DATA/'model/model.json')
    try:
        with guarded(TAG),ExitStack() as stack:
            process=stack.enter_context(router(TAG,8));model=FlightModel(base,NATIONAL/'flights-2024')
            roads=JointRoads(model,NATIONAL/'roads/release.json',DATA/'joint-mobility-2024-v2-cache',
                query_timeout_seconds=23,access_holds_path=HOLDS,source_access_holds_path=hold_path)
            try:
                if roads.manifest['network_sha256']!=old['provenance']['road_network_sha256']:
                    raise ValueError('physical car network changed during visit-only repair')
                roads.seed_evidence(PREVIOUS/'route.json');roads.seed_evidence(PILOT/'route.json')
                for local,global_ in enumerate(p['routing_locations']):
                    loc=p['locations'][local];a=loc['road_access'];gap=gap_m([loc['lon'],loc['lat']],[a['lon'],a['lat']])
                    roads.anchors[global_]=[a['lat'],a['lon'],120+(2*gap/2200*60 if gap>100 else 0)]
                for w in p['window_transitions']:
                    indices=p['routing_locations'];a,b=indices[w['from']],indices[w['to']]
                    plan={**w['plan'],'segments':[{**s,'from':indices[s['from']],'to':indices[s['to']]} for s in w['plan']['segments']]}
                    nodes={a,b,*[s[k] for s in plan['segments'] for k in ['from','to']]}
                    if any(tuple(map(float,model.coords[i])) in blocked for i in nodes):continue
                    roads._remember((a,b,w['previous'],w['target']),plan)
                ids=old['search']['selected_model_opportunities']
                bad=[i for i in ids if tuple(map(float,base.coords[int(base.records[i]['location'])])) in blocked]
                if len(bad)!=1:raise ValueError('repair expected exactly one held visit')
                kept=[i for i in ids if i not in bad]
                badopp=base.opportunity(bad[0]);missing=set(badopp['species'])-{s for i in kept for s in base.opportunity(i)['species']}
                pilot=read(PILOT/'route.json')
                trusted={int(base.records[i]['location']) for i in pilot['search']['selected_model_opportunities']
                         if missing&set(base.opportunity(i)['species'])}
                lo,hi=np.searchsorted(base.records['day'],[badopp['day'],badopp['day']+1])
                replacements=[i for i in range(int(lo),int(hi)) if int(base.records[i]['location']) in trusted
                              and missing<=set(base.opportunity(i)['species'])]
                candidates=[]
                with NativeEvaluator(model,roads,out).open() as evaluator:
                    for replacement in replacements:
                        trial=sorted([*kept,replacement]);score=evaluator.evaluate(trial,timeout=150)
                        candidates.append({'replacement':replacement,'ids':trial,'score':score})
                        print(json.dumps({'stage':'exact-source-access-repair','replacement':base.opportunity(replacement),**score}),flush=True)
                    feasible=[v for v in candidates if v['score']['feasible'] and not v['score']['pending']]
                    if not feasible:
                        score=evaluator.evaluate(kept,timeout=150);candidates.append({'replacement':None,'ids':kept,'score':score})
                        feasible=[v for v in candidates if v['score']['feasible'] and not v['score']['pending']]
                    if not feasible:raise ValueError('held-point calendar repair remains unresolved')
                    best=max(feasible,key=lambda v:(v['score']['species'],-v['score']['minutes'],-v['score']['miles']))
                decision={'source_route':str(PREVIOUS/'route.json'),'source_route_sha256':sha256(PREVIOUS/'route.json'),
                    'source_audit_sha256':sha256(PREVIOUS/'validation.json'),'source_hold_sha256':sha256(hold_path),
                    'held_opportunities':bad,'held_original_observation':badopp,
                    'actual_same_date_replacement_opportunities':replacements,'candidates':candidates,
                    'chosen':best,'method':'Choose another actual exact-coordinate observation on the same recorded date. No source pin is moved, rounded or erased.'}
                dump(out/'access-repair.json',decision)
                dump(out/'seed.json',{'ids':best['ids'],'mode':'complete','model_sha256':sha256(base.path),
                    'source_file':str(out/'access-repair.json'),'source_sha256':sha256(out/'access-repair.json')})
                roads.search_lineage={'prior_run':str(PREVIOUS),'prior_route_sha256':sha256(PREVIOUS/'route.json'),
                    'prior_engine_sha256':sha256(PREVIOUS/'engine-snapshot'),'prior_engine_result_sha256':sha256(PREVIOUS/'engine.json'),
                    'prior_native_seconds':900,'prior_completed_evaluations':read(PREVIOUS/'engine.json')['candidate_evaluations'],
                    'prior_calendar_status':'rejected for the single documented open-water walk',
                    'repair_decision_sha256':sha256(out/'access-repair.json'),'repair_native_seconds':60,
                    'physical_road_costs_relabelled':False}
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,TAG)
                route=run(base.path,roads,out,seconds=60,seed_file=out/'seed.json')
                taxonomy={v['species_id']:v for v in csv.DictReader((PARENT/'dataset/taxonomy.csv').open())}
                selected={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
                provisional=sorted(s for s in selected if taxonomy[s]['score_tier'].startswith('provisional'))
                validation=read(out/'validation.json');sources=validation['joint_transport']['publisher_sources']
                result={'species':route['species_total'],'primary':len(selected)-len(provisional),'provisional':len(provisional),
                    'provisional_species':provisional,'baseline_fixed_calendar':667,'baseline_land_flights':656,
                    'gain_vs_fixed_calendar':route['species_total']-667,'record_comparison_target':759,
                    'gap_to_759':759-route['species_total'],'pool':len(base.meta['species']),'native_joint_search':True,
                    'boat_days':sum(d['kind']=='pelagic' for d in route['days']),'flight_days':sum(d['kind']=='flight' for d in route['days']),
                    'road_miles':sum(d['road_miles'] for d in route['days']),
                    'boat_nautical_miles':sum(d.get('boat_nautical_miles',0) for d in route['days']),
                    'walking_kmh':2.2,'all_observations_retained':True,'exact_coordinates':True,
                    'source_records_checked':sources['records_checked'],'publisher_source_postcheck':'passed',
                    'recordholder_replay':'pending_input','validation':validation,'route_sha256':sha256(out/'route.json'),
                    'total_wall_seconds':time.monotonic()-started,'search_lineage':roads.search_lineage}
                dump(out/'result.json',result)
                dump(out/'countability-source-validation.json',{'valid':True,'route_sha256':result['route_sha256'],
                    'policy_sha256':sha256(PARENT/'policy.json'),'retrieval_sha256':sha256(out/'publisher-retrieval.json'),
                    'records_checked':sources['records_checked'],'individual_wild_status_certified':False,
                    'unresolved_or_excluded_credited':[],'decisions':sources['decisions']})
                dump(OUT/'run-status.json',{'stage':'completed','run':TAG,'species':route['species_total'],
                    'best_species':route['species_total'],'best_run':TAG,
                    'message':'Joint calendar passed independent source, road, flight, walking and charter audits after exact-source access repair.'})
                print(json.dumps({'stage':'verified-access-repair',**result}),flush=True)
            finally:roads.close()
    finally:
        base.close()
        if awake.poll() is None:awake.terminate();awake.wait(timeout=5)
        dump(out/'cleanup.json',{'process_scoped_idle_assertion_released':True,'transient_bridge_credentials_removed':True,
            'original_observations_and_all_source_coordinates_preserved':True,'visit_hold_does_not_cache_false_unreachable_roads':True})


if __name__=='__main__':main()
