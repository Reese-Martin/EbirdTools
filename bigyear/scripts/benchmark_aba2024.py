"""Bounded national rerun after a complete ABA 2024 source reconciliation."""

from contextlib import ExitStack
import json
import os
import sqlite3
import subprocess

import numpy as np

from bigyear_planner.compact_model import build,load
from bigyear_planner.config import Config
from bigyear_planner.mobility import FlightModel,MobilityRoads
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator,RegionalExplorer
from bigyear_planner.road_matching import gap_m,verify_evidence
from bigyear_planner.util import ROOT,dump,read,sha256
from align_aba2024 import DATA,ORIGINAL,POLICY
from evaluate_lower48 import OUT,guarded,prepare_audit,router

TAG='aba2024-aligned-flights-900s-600s'
PREVIOUS='countability-flights-awake-600s'


def remap_seed(model,roads,previous,old_cache,allow_access_holds=False):
    """Reuse only selected measured costs, rekeyed by exact source identity.

    Full old caches and endpoint proofs are NOT relabeled to the new model.
    Final endpoint geometry is independently queried under the new binding.
    """
    route=read(previous/'route.json');problem=read(previous/'problem.json')
    if not read(previous/'validation.json')['valid']:
        raise ValueError('seed is not independently validated')
    for path,key in [(previous/'problem.json','problem_sha256'),(previous/'roads.sqlite','roads_sha256')]:
        if sha256(path)!=route['provenance'][key]:
            raise ValueError('seed input changed')
    old_provider=read(old_cache/'provider.json')
    if old_provider['network_sha256']!=route['provenance']['road_network_sha256']:
        raise ValueError('seed provider identity differs')
    for key in ['road_release_sha256','matching_policy','access_policy','additional_locations']:
        actual=roads.manifest.get(key)
        if key=='access_policy' and allow_access_holds:
            actual={k:v for k,v in actual.items() if k!='surface_access_holds'}
        if old_provider.get(key)!=actual:
            raise ValueError('seed physical road/access/airport policy changed')
    for key in ['year','activity_minutes','birding_minutes','drive_minutes','walking_speed_kmh','walking_threshold_m','local_miles']:
        if problem['config'][key]!=model.meta['config'][key]:
            raise ValueError('seed travel rules changed')
    saved={l['id']:l for l in problem['locations']}
    mapping_by_id={}
    # SQLite scans the small metadata catalog once; no nationwide dict in RAM.
    placeholders=','.join('?' for _ in saved)
    rows=model.original.locations.db.execute(
        f"SELECT idx,metadata FROM locations WHERE json_extract(metadata,'$.id') IN ({placeholders})",list(saved))
    for idx,payload in rows:
        loc=json.loads(payload);mapping_by_id[loc['id']]=idx
    for idx,airport in enumerate(model.airports):
        if airport['id'] in saved:
            mapping_by_id[airport['id']]=model.meta['locations']+idx
    if set(saved)!=set(mapping_by_id):
        raise ValueError('new cohort does not contain every source seed location')
    mapping={}
    for local,old in enumerate(problem['routing_locations']):
        loc=problem['locations'][local];idx=mapping_by_id[loc['id']]
        new=model.locations[idx]
        if any(loc[k]!=new[k] for k in ['id','name','lat','lon','state']):
            raise ValueError('seed exact source location differs')
        mapping[old]=idx
        anchor=loc['road_access'];cfg=model.meta['config']
        gap=gap_m([loc['lon'],loc['lat']],[anchor['lon'],anchor['lat']])
        if abs(gap-anchor['offset_meters'])>1e-6:
            raise ValueError('seed walking gap differs')
        walk=2*gap/(cfg['walking_speed_kmh']*1000)*60 if gap>cfg['walking_threshold_m'] else 0
        roads.anchors[idx]=[anchor['lat'],anchor['lon'],cfg['birding_minutes']+walk]
    with sqlite3.connect('file:'+str(previous/'roads.sqlite')+'?mode=ro&immutable=1',uri=True) as db:
        db.row_factory=sqlite3.Row
        costs=list(db.execute('SELECT * FROM roads'))
        held_costs_skipped=0
        for r in costs:
            proof=json.loads(db.execute('SELECT evidence FROM road_endpoints WHERE origin=? AND destination=?',
                [r['origin'],r['destination']]).fetchone()[0])
            if proof['network_sha256']!=old_provider['network_sha256']:
                raise ValueError('old proof network differs')
            a,b=mapping_by_id[r['origin']],mapping_by_id[r['destination']]
            if allow_access_holds and any(tuple(map(float,model.coords[i])) in roads.access_holds for i in [a,b]):
                held_costs_skipped+=1
                continue
            anchors=[[roads.access(i)[1],roads.access(i)[0]] for i in [a,b]]
            verify_evidence(proof,anchors,r)
            roads.store.put(a,b,(r['miles'],r['minutes']))
    ids=[];records=model.original.records
    for day in route['days']:
        for stop in day['stops']:
            old_location=route['locations'][stop['location']]['id']
            loc=mapping_by_id[old_location]
            daynum=(__import__('datetime').date.fromisoformat(day['date'])-Config(**model.original.meta['config']).first_date).days
            lo,hi=np.searchsorted(records['day'],[daynum,daynum+1])
            matches=np.flatnonzero(records['location'][lo:hi]==loc)
            if len(matches)!=1:
                raise ValueError('seed dated opportunity cannot be mapped uniquely')
            ids.append(int(lo+matches[0]))
    roads.seed_incumbent(previous/'route.json',location_mapping=mapping)
    binding={'source_route':str(previous/'route.json'),'source_route_sha256':sha256(previous/'route.json'),
        'source_problem_sha256':sha256(previous/'problem.json'),'source_roads_sha256':sha256(previous/'roads.sqlite'),
        'source_provider_sha256':sha256(old_cache/'provider.json'),'new_model_sha256':sha256(model.path),
        'old_network':old_provider['network_sha256'],'new_network':roads.manifest['network_sha256'],
        'locations_remapped':len(mapping),'opportunities_remapped':len(ids),'selected_directed_costs_reused':len(costs)-held_costs_skipped,
        'surface_held_directed_costs_skipped':held_costs_skipped,
        'full_old_cache_reused':False,'old_endpoint_proofs_relabelled':False,
        'additional_surface_access_holds':roads.manifest['access_policy'].get('surface_access_holds'),
        'final_endpoint_proofs':'Independently queried and frozen under new network binding.'}
    return sorted(ids),binding


def main():
    folder=OUT/TAG;folder.mkdir(exist_ok=True)
    if (folder/'route.json').exists():
        raise ValueError('completed run exists')
    awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    model=None
    try:
        dump(OUT/'run-status.json',{'stage':'preparing','run':TAG,'label':'ABA 2024 aligned nationwide rerun',
            'message':'Building the complete 770-species eligible model after reconciling all 858 source taxon concepts. Previous verified routes remain available.'})
        if not (DATA/'model/model.json').exists():
            with guarded(TAG+'-model'):
                model=build(DATA/'dataset',DATA/'model',Config(**read(ORIGINAL/'config.json')))
        else:
            model=load(DATA/'model/model.json')
        with guarded(TAG),ExitStack() as stack:
            overlay=FlightModel(model,ORIGINAL/'flights-2024')
            process=stack.enter_context(router(TAG,8))
            roads=MobilityRoads(overlay,ORIGINAL/'roads/release.json',DATA/(TAG+'-cache'),query_timeout_seconds=23)
            try:
                initial,binding=remap_seed(overlay,roads,OUT/PREVIOUS,
                    ROOT/'data/lower48-countability-2024'/(PREVIOUS+'-cache'))
                dump(folder/'remapped-incumbent.json',binding)
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,TAG)
                dump(folder/'benchmark-design.json',{'policy':POLICY,'policy_sha256':sha256(DATA/'policy.json'),
                    'regional_seconds':900,'native_active_seconds':600,'threads':2,'walking_speed_kmh':2.2,
                    'all_source_observations_retained':True,'exact_source_coordinates':True,
                    'complete_2024_approved_source_crosswalk':True,'retained_original_archive_sha256':sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json')})
                dump(OUT/'run-status.json',{'stage':'search','run':TAG,'label':'ABA 2024 aligned nationwide rerun',
                    'message':'Complete 2024 approved-set crosswalk and population envelopes applied. Re-evaluating remapped cross-country incumbent, then regional exploration and 600 active seconds of native search.'})
                with NativeEvaluator(overlay,roads,folder).open() as evaluator:
                    score=evaluator.evaluate(initial,timeout=60)
                    if score['pending'] or not score['feasible']:
                        raise ValueError('remapped seed is not fully feasible with measured current costs')
                    dump(folder/'baseline-rescored.json',score)
                    print(json.dumps({'stage':'baseline-rescored',**score}),flush=True)
                    construction=RegionalExplorer(overlay,evaluator,folder).explore(initial,seconds=900)
                dump(folder/'audited-seed.json',{'ids':construction['ids'],'mode':'complete','model_sha256':sha256(model.path),
                    'source_file':str(folder/'regional-exploration.json'),'source_sha256':sha256(folder/'regional-exploration.json')})
                route=run(model.path,roads,folder,seconds=600,seed_file=folder/'audited-seed.json',blocks_path=folder/'regional-blocks.txt')
                result={'valid':read(folder/'validation.json')['valid'],'run':TAG,'policy':POLICY,
                    'species':route['species_total'],'annual_scoring_pool':len(model.meta['species']),
                    'birding_states':sorted({route['locations'][s['location']]['state'] for d in route['days'] for s in d['stops']}),
                    'flight_days':sum(d['kind']=='flight' for d in route['days']),
                    'road_miles':sum(d['road_miles'] for d in route['days']),
                    'source_observations_retained':True,'exact_coordinates_retained':True,'walking_speed_kmh':2.2,
                    'publisher_source_postcheck':'pending','individual_wild_status_certified':False}
                dump(OUT/'aba2024-aligned-benchmark-result.json',result)
                print(json.dumps(result),flush=True)
            finally:
                roads.close()
    finally:
        if model is not None:model.close()
        if awake.poll() is None:awake.terminate();awake.wait(timeout=5)
        dump(folder/'idle-sleep-assertion-released.json',{'released':True,'process_scoped':True})
    dump(OUT/'run-status.json',{'stage':'completed','run':TAG,'best_run':TAG,'best_species':result['species'],
        'message':'ABA 2024 aligned search independently validated. Publisher record postcheck pending.'})


if __name__=='__main__':
    try:
        main()
    except BaseException as error:
        dump(OUT/'run-status.json',{'stage':'failed','run':TAG,'message':'ABA 2024 aligned rerun needs attention; previous validated runs retained.','error':repr(error)})
        raise
