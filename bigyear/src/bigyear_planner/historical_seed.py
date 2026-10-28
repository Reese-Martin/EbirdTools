"""Use another year's geography as proposals, never as observation evidence."""
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import numpy as np

from .calendar_study import superior
from .road_matching import gap_m
from .util import dump, read, sha256


def seasonal_hints(model, reference, output):
    """Nearby actual records around the old route's month/day, including boats."""
    reference = Path(reference)
    route = read(reference/'route.json')
    audit = read(reference/'validation.json')
    if not audit['valid'] or audit['route_sha256'] != sha256(reference/'route.json'):
        raise ValueError('historical reference requires a route-bound audit')
    source = model.original if hasattr(model,'original') else model
    records, coordinates = source.records, source.coords
    proposals = []
    for day in route['days']:
        if not day['stops']:
            continue
        olddate = date.fromisoformat(day['date'])
        # Feb 29, if ever selected, proposes Feb 28; support must still be a
        # real recorded date. No offset-after-February leap-year arithmetic.
        target = date(model.meta['config']['year'],olddate.month,
            min(olddate.day,28) if olddate.month==2 else olddate.day)
        ordinal = (target-date(target.year,1,1)).days
        lo,hi = np.searchsorted(records['day'],[max(0,ordinal-14),ordinal+15])
        block = records[lo:hi]
        pins = coordinates[block['location']]
        for stop in day['stops']:
            loc = route['locations'][stop['location']]
            # Boat records are stored at their departure port in the native
            # catalog. Match the actual old port for boat-date hints.
            if day['kind'] in ['pelagic','island']:
                catalog = read(source.meta['joint_marine']['catalog_path'])
                matching = [t for t in catalog['trips'] if
                    abs((date.fromisoformat(t['date'])-target).days)<=14 and
                    any(abs(o['lat']-loc['lat'])<.15 and abs(o['lon']-loc['lon'])<.15
                        for o in t['observations']) and
                    (t.get('kind')=='island_landing')==(day['kind']=='island')]
                ids = [source.meta['joint_marine']['trip_opportunities'][t['id']] for t in matching]
            else:
                distance = np.hypot((pins[:,0]-loc['lat'])*69.1,
                    (pins[:,1]-loc['lon'])*69.1*np.cos(np.radians(loc['lat'])))
                ids = (int(lo)+np.flatnonzero((distance<=8)&(block['reserved']==0))).tolist()
            wanted = set(stop['species'])
            def rank(i):
                row = records[i]
                off,count = int(row['offset']),int(row['count'])
                species = {source.meta['species'][int(s)]['id'] for s in source.species[off:off+count]}
                return (len(species&wanted),len(species),-abs(int(row['day'])-ordinal),-i)
            best = sorted(ids,key=rank,reverse=True)[:3]
            proposals.extend(best)
    sets = {i:set(model.opportunity(i)['species']) for i in set(proposals)}
    remaining = set(sets)
    order = []
    seen = set()
    while remaining:
        item = max(remaining,key=lambda i:(len(sets[i]-seen),len(sets[i]),-i))
        order.append(item)
        seen |= sets[item]
        remaining.remove(item)
    result = {'ids':order,'potential_species':len(seen),'year':model.meta['config']['year'],
        'model_sha256':sha256(model.path),'reference_route_sha256':sha256(reference/'route.json'),
        'method':'At most three actual dated 2023 opportunities near each 2024 route stop, within 14 calendar days. Geography guides initialization only; every date, species and exact pin belongs to 2023. All national candidates remain available.'}
    dump(output,result)
    return order


def seed_physical_evidence(model, roads, reference, output):
    """Rekey only independently verified identical physical locations/roads."""
    reference = Path(reference)
    route = read(reference/'route.json')
    problem = read(reference/'problem.json')
    audit = read(reference/'validation.json')
    if not audit['valid'] or audit['route_sha256'] != sha256(reference/'route.json'):
        raise ValueError('physical reference lacks route-bound audit')
    provider = read(reference/'search-binding.json')['road_provider']
    binding = {k:provider[k] for k in ['model_sha256','road_release_sha256','matching_policy','access_policy']}
    old_digest = hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()
    if old_digest!=route['provenance']['road_network_sha256']:
        raise ValueError('historical provider does not reproduce audited binding')
    if any(provider[k]!=roads.manifest[k] for k in ['road_release_sha256','matching_policy','access_policy']):
        raise ValueError('historical physical road release/matching/access rules differ')
    if route['provenance']['access_policy'] != roads.manifest['access_policy']:
        raise ValueError('historical physical access rules differ')
    for name,key in [('roads.sqlite','roads_sha256'),('problem.json','problem_sha256')]:
        if sha256(reference/name) != route['provenance'][key]:
            raise ValueError('physical reference artifact changed')
    source = model.original
    wanted = {loc['id']:loc for loc in problem['locations'] if loc.get('road_access')}
    placeholders = ','.join('?' for _ in wanted)
    mapping = {json.loads(payload)['id']:idx for idx,payload in source.locations.db.execute(
        f"SELECT idx,metadata FROM locations WHERE json_extract(metadata,'$.id') IN ({placeholders})",sorted(wanted))}
    mapping.update({a['id']:model.meta['locations']+i for i,a in enumerate(model.airports)})
    exact = {}
    for key,loc in wanted.items():
        idx = mapping.get(key)
        if idx is None:
            continue
        current = model.locations[idx]
        if any(current[k]!=loc[k] for k in ['id','lat','lon','state']):
            continue
        anchor = loc['road_access']
        gap = gap_m([loc['lon'],loc['lat']],[anchor['lon'],anchor['lat']])
        if abs(gap-anchor['offset_meters'])>1e-6:
            raise ValueError('physical reference anchor differs')
        walk = 2*gap/2200*60 if gap>100 else 0
        roads.anchors[idx] = [anchor['lat'],anchor['lon'],120+walk]
        exact[key] = idx
    count = 0
    with sqlite3.connect(f'file:{reference/"roads.sqlite"}?mode=ro&immutable=1',uri=True) as db:
        for a,b,miles,minutes in db.execute('SELECT origin,destination,miles,minutes FROM roads'):
            if a in exact and b in exact:
                roads.store.put(exact[a],exact[b],(miles,minutes))
                count += 1
    same_binding = route['provenance']['road_network_sha256']==roads.manifest['network_sha256']
    if same_binding:
        roads.seed_evidence(reference/'route.json')
    result = {'reference_route_sha256':sha256(reference/'route.json'),
        'anchors_reused':len(exact),'directed_costs_reused':count,
        'model_indices_reused':False,'calendar_plans_reused':False,
        'old_provider_binding_sha256':old_digest,
        'new_provider_binding_sha256':roads.manifest['network_sha256'],
        'shared_physical_release_sha256':provider['road_release_sha256'],
        'old_endpoint_proofs_relabelled':False,
        'final_endpoint_proofs_must_be_remeasured':not same_binding,
        'rule':'Only identical stable location IDs, exact coordinates, road network and access policy; previous-year observations/flights/feasibility are never reused.'}
    dump(output,result)
    return result


def build_feasible_seed(model,evaluator,hints,output,seconds=600):
    deadline = time.monotonic()+seconds
    selected = []
    best = evaluator.evaluate([],timeout=5)
    attempts = []
    pending = list(hints)
    for round_ in range(3):
        retry = []
        for item in pending:
            if time.monotonic()>=deadline:
                break
            candidate = sorted([*selected,item],key=lambda i:(int(model.original.records[i]['day']),i))
            score = evaluator.evaluate(candidate,timeout=min(3+round_*3,deadline-time.monotonic()))
            attempts.append({'round':round_,'opportunity':item,**score})
            if superior(score,best):
                selected,best = candidate,score
                print(json.dumps({'stage':'historical-seed-improved','species':best['species'],
                    'visits':len(selected)}),flush=True)
            elif score['pending']:
                retry.append(item)
        pending = retry
        if not pending or time.monotonic()>=deadline:
            break
    dump(output,{'ids':selected,'score':best,'attempts':attempts,'unresolved':pending,
        'preparation_budget_seconds':seconds,'optimization_proven':False})
    return selected,best
