"""Reconstruct a complete warm start from a verified multimodal calendar."""
import json
from pathlib import Path
import sqlite3

import numpy as np

from .road_matching import verify_evidence,gap_m
from .util import read,sha256


def seed_verified_joint(model, roads, previous):
    """Reuse a route only when its complete model and physical bindings match."""
    previous = Path(previous)
    route = read(previous/'route.json'); problem = read(previous/'problem.json')
    audit = read(previous/'validation.json'); digest = sha256(previous/'route.json')
    if not audit['valid'] or audit.get('route_sha256') != digest:
        raise ValueError('joint warm start requires a route-bound positive audit')
    if problem['compact_source']['sha256'] != sha256(model.path):
        control=getattr(model,'meta',{}).get('calendar_source',{})
        if control.get('sha256')!=problem['compact_source']['sha256']:
            raise ValueError('joint warm start complete model changed')
        old=read(control['path'])
        if (old['components']!=model.meta['components'] or
            any(old[k]!=model.meta[k] for k in ['locations','opportunities','species','species_entries'])):
            raise ValueError('joint calendar warm start observation identities changed')
    for name, key in [('problem.json','problem_sha256'),('roads.sqlite','roads_sha256')]:
        if sha256(previous/name) != route['provenance'][key]:
            raise ValueError('joint warm start artifact changed: '+name)
    migrated=route['provenance']['road_network_sha256'] != roads.manifest['network_sha256']
    if migrated:
        migration=getattr(roads,'access_migration',None)
        if (not migration or migration['old_network_sha256']!=route['provenance']['road_network_sha256']
            or migration['new_network_sha256']!=roads.manifest['network_sha256']
            or migration['old_provider']['access_policy']!=route['provenance']['access_policy']):
            raise ValueError('joint warm start physical road network changed')
    mapping = {}
    config = model.meta['config']
    for local, global_ in enumerate(problem['routing_locations']):
        saved = problem['locations'][local]; current = model.locations[global_]
        if any(saved[k] != current[k] for k in ['id','lat','lon','state']):
            raise ValueError('joint warm start routing identity changed')
        anchor = saved['road_access']
        gap = gap_m([saved['lon'],saved['lat']], [anchor['lon'],anchor['lat']])
        if abs(gap-anchor['offset_meters']) > 1e-6:
            raise ValueError('joint warm start anchor distance changed')
        walking = 2*gap/(config['walking_speed_kmh']*1000)*60 if gap>config['walking_threshold_m'] else 0
        roads.anchors[global_] = [anchor['lat'],anchor['lon'],config['birding_minutes']+walking]
        mapping[saved['id']] = global_
    selected = route['search']['selected_model_opportunities']
    for i in selected:
        if not np.isfinite(roads.access(int(model.original.records[i]['location']))[2]):
            raise ValueError('joint warm start contains a held visit')
    # Cost values retain their original, independently checked physical network.
    with sqlite3.connect(f'file:{previous/"roads.sqlite"}?mode=ro&immutable=1',uri=True) as db:
        for origin,destination,miles,minutes in db.execute('SELECT origin,destination,miles,minutes FROM roads'):
            roads.store.put(mapping[origin],mapping[destination],(miles,minutes))
    if not migrated:roads.seed_evidence(previous/'route.json')
    roads.seed_incumbent(previous/'route.json')
    return list(selected), {'source_route':str(previous/'route.json'),'route_sha256':digest,
        'validation_sha256':sha256(previous/'validation.json'),'model_sha256':sha256(model.path),
        'road_network_sha256':roads.manifest['network_sha256'],'species':route['species_total'],
        'same_model_indices_verified_by_exact_identity':True,'physical_proofs_relabelled':False,
        'access_discovery_migrated':migrated,'selected_road_proofs_must_be_remeasured':migrated}


def seed_joint(model,roads,previous,marine_cache,baseline_cache):
    previous=Path(previous);route=read(previous/'route.json');p=read(previous/'problem.json')
    check=read(previous/'validation.json')
    if not check['valid'] or check.get('route_sha256')!=sha256(previous/'route.json'):
        raise ValueError('joint seed requires route-bound verified marine incumbent')
    for name,key in [('problem.json','problem_sha256'),('roads.sqlite','roads_sha256')]:
        if sha256(previous/name)!=route['provenance'][key]:raise ValueError('joint seed input changed')
    joint=model.meta['joint_marine'];nland=joint['land_locations']
    # Bind the unchanged physical releases; old endpoint proofs are never
    # relabeled. They support carrying costs only; final proofs are re-queried.
    old_port=read(Path(marine_cache)/'provider.json');old_base=read(Path(baseline_cache)/'provider.json')
    for provider in [old_port,old_base]:
        for key in ['road_release_sha256','matching_policy']:
            if provider[key]!=roads.manifest[key]:raise ValueError('joint seed car network/policy changed')
    old_policy=old_base['access_policy'];new_policy=dict(roads.manifest['access_policy']);new_policy.pop('extension',None)
    if new_policy!=old_policy:raise ValueError('joint seed terrestrial access policy changed')
    if old_port['access_policy'].get('extension')!=roads.manifest['access_policy'].get('extension'):
        raise ValueError('joint seed port matching policy changed')
    if old_base['additional_locations']!=roads.manifest['additional_locations']:
        raise ValueError('joint seed airport coordinates changed')
    ids={l['id'] for l in route['locations'] if l.get('kind')!='pelagic_observation'}
    placeholders=','.join('?' for _ in ids)
    mapping={json.loads(payload)['id']:idx for idx,payload in model.original.locations.db.execute(
        f"SELECT idx,metadata FROM locations WHERE json_extract(metadata,'$.id') IN ({placeholders})",sorted(ids))}
    for i,airport in enumerate(model.airports):mapping[airport['id']]=model.meta['locations']+i
    local={}
    for i,loc in enumerate(route['locations']):
        if loc['id'] not in mapping:continue
        idx=mapping[loc['id']];new=model.locations[idx]
        keys=['id','lat','lon','state']
        if loc.get('kind')!='marine_port':keys.append('name')
        elif loc['name']!=new['name']+' (modeled boarding)':raise ValueError('unexpected port label migration')
        if any(loc[k]!=new[k] for k in keys):raise ValueError('joint seed exact location changed')
        local[i]=idx
        if 'road_access' in loc:
            anchor=loc['road_access'];gap=gap_m([loc['lon'],loc['lat']],[anchor['lon'],anchor['lat']])
            if abs(gap-anchor['offset_meters'])>1e-6:raise ValueError('joint seed anchor gap differs')
            roads.anchors[idx]=[anchor['lat'],anchor['lon'],120+(2*gap/2200*60 if gap>100 else 0)]
    nbase=len(read(p['base_route'])['locations'])
    if (Path(marine_cache)/'access.bin').stat().st_size!=(nbase+len(joint['port_indices']))*24:
        raise ValueError('marine seed port cache size changed')
    if old_port['model_sha256']!=joint['catalog_sha256']:raise ValueError('marine seed catalog changed')
    old_anchors=np.memmap(Path(marine_cache)/'access.bin',mode='r',dtype='<f8',shape=(nbase+len(joint['port_indices']),3))
    for i,port in enumerate(read(joint['catalog_path'])['ports']):
        if np.isfinite(old_anchors[nbase+i,2]):roads.anchors[joint['port_indices'][port['id']]]=old_anchors[nbase+i]
    old_anchors._mmap.close()
    carried=0
    with sqlite3.connect(f'file:{previous/"roads.sqlite"}?mode=ro&immutable=1',uri=True) as db:
        db.row_factory=sqlite3.Row
        for row in db.execute('SELECT * FROM roads'):
            a,b=mapping[row['origin']],mapping[row['destination']]
            proof=json.loads(db.execute('SELECT evidence FROM road_endpoints WHERE origin=? AND destination=?',
                [row['origin'],row['destination']]).fetchone()[0])
            expected=(old_port['network_sha256'] if row['origin'].startswith('marine-port:') or
                      row['destination'].startswith('marine-port:') else old_base['network_sha256'])
            if proof['network_sha256']!=expected:raise ValueError('joint old road proof network changed')
            try:
                verify_evidence(proof,[[float(roads.anchors[i,1]),float(roads.anchors[i,0])] for i in [a,b]],row)
            except ValueError as error:
                raise ValueError(f"joint seed road {row['origin']} -> {row['destination']}: {roads.anchors[[a,b]].tolist()}") from error
            roads.store.put(a,b,(row['miles'],row['minutes']));carried+=1
    records=model.original.records;selected=[];groups={}
    trips={t['id']:t for t in read(joint['catalog_path'])['trips']}
    for d,day in enumerate(route['days']):
        if day['kind']=='pelagic':
            t=trips[day['marine_trip']]
            idx=joint['trip_opportunities'][t['id']];selected.append(idx);groups[d]=[int(records[idx]['location'])]
        elif day['stops']:
            values=[]
            for stop in day['stops']:
                loc=local[stop['location']];lo,hi=np.searchsorted(records['day'],[d,d+1])
                options=np.flatnonzero((records['location'][lo:hi]==loc)&(records['reserved'][lo:hi]==0))
                if len(options)!=1:raise ValueError('land warm-start opportunity not unique')
                idx=int(lo+options[0]);selected.append(idx);values.append(loc)
            groups[d]=values
    here=None;previous_day=-1;windows=0
    for target,locations in sorted(groups.items()):
        first=locations[0]
        if here is None:here=first
        segments=[];minutes=miles=0.;flight_at=None;cursor=previous_day+1
        while cursor<target:
            day=route['days'][cursor]
            if day['kind']=='rest':cursor+=1;continue
            if day['kind'] not in ['road_transit','flight']:raise ValueError('warm-start omitted a birding day')
            leg=day['legs'][0];a,b=local[leg['from']],local[leg['to']]
            if leg['mode']=='flight':
                if flight_at is not None:raise ValueError('multiple flights in a source window')
                flight_at=(cursor,sum(s['days'] for s in segments))
                segments.append({'mode':'flight','from':a,'to':b,'days':1,'minutes':leg['minutes'],'service_day':cursor})
                minutes+=leg['minutes'];cursor+=1
            else:
                if leg['start_fraction']!=0:raise ValueError('warm-start split road begins mid-leg')
                count=0
                while cursor<target:
                    ld=route['days'][cursor]
                    if ld['kind']!='road_transit' or ld['legs'][0]['from']!=leg['from'] or ld['legs'][0]['to']!=leg['to']:break
                    v=ld['legs'][0];minutes+=v['minutes'];miles+=v['miles'];count+=1;cursor+=1
                    if v['end_fraction']==1:break
                if v['end_fraction']!=1:raise ValueError('warm-start split road ends mid-leg')
                segments.append({'mode':'road','from':a,'to':b,'days':count})
        if segments:
            days=sum(s['days'] for s in segments)
            start=flight_at[0]-flight_at[1] if flight_at else target-days
            if start<previous_day+1 or start+days>target:raise ValueError('compressed source transit does not fit')
            if segments[0]['from']!=here or segments[-1]['to']!=first:raise ValueError('joint source transit endpoints differ')
            roads._remember((here,first,previous_day,target),{'days':days,'minutes':minutes,'miles':miles,
                'start_day':start,'segments':segments});windows+=1
        elif here==first:
            roads._remember((here,first,previous_day,target),{'days':0,'minutes':0.,'miles':0.,'segments':[]})
        here=locations[-1];previous_day=target
    return sorted(selected),{'source_route':str(previous/'route.json'),'source_route_sha256':sha256(previous/'route.json'),
        'species':route['species_total'],'selected':len(selected),'directed_costs_reused':carried,'transit_windows':windows,
        'full_old_cache_reused':False,'old_endpoint_proofs_relabelled':False,'land_locations_retained':nland}
