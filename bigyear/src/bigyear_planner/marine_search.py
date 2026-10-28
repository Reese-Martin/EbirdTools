"""Add timed, same-port marine expeditions to a verified terrestrial calendar.

The incumbent is locked. Search consumes only its free days, including complete
measured driving days to/from ports. This gives a controlled marine lower bound
without silently changing terrestrial scoring, flight days or walking rules.
"""
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import math

import numpy as np
from shapely.geometry import LineString

from .road_matching import gap_m
from .surface_access import open_water_meters
from .travel import road_transit_days


def free_windows(route, reserved=()):
    reserved=set(reserved);here=route['initial_location'];start=None;position=None
    result=[]
    for i,day in enumerate([*route['days'],{'kind':'sentinel','stops':[],'legs':[]}]):
        free=day['kind']=='rest' and i not in reserved
        if free and start is None:start,position=i,here
        if not free and start is not None:
            result.append((start,i,position));start=None
        if day['stops']:here=day['stops'][-1]['location']
        elif day['legs']:here=day['legs'][-1]['to']
    return result


def road_model(base, ports, binding_path):
    locations=deepcopy(base['locations'])
    for p in ports:
        lon,lat=p['boarding_point']
        locations.append({'id':'marine-port:'+p['id'],'name':p['name']+' (modeled boarding)',
            'lat':lat,'lon':lon,'state':p['state'],'kind':'marine_port',
            'port_reference':p['reference'],'modeled_boarding_point':True})
    return SimpleNamespace(locations=locations,coords=np.asarray([[l['lat'],l['lon']] for l in locations]),
        path=Path(binding_path),meta={'config':base['config']})


def audit_port_walk(port,location,land,config):
    anchor=location['road_access'];pin=[location['lon'],location['lat']]
    road=[anchor['lon'],anchor['lat']]
    water=open_water_meters(LineString([road,pin]),land)
    gap=gap_m(road,pin)
    if water>100:raise ValueError('port walk crosses more than 100 m of open water')
    distance=2*gap if gap>config['walking_threshold_m'] else 0.
    return {'port':port['id'],'gap_meters':gap,'water_meters':water,
            'walking_meters':distance,'walking_minutes':distance/(config['walking_speed_kmh']*1000)*60,
            'foot_access':'modeled dock/shore connection; public access assumed, no island landing'}


def choose_expeditions(base,catalog,roads,port_land,maximum_rounds=24,progress=None,
                       species_holds=(),evidence_seed=None,deferred_seed=None):
    """Greedy exact-date port portfolios; all costs used for acceptance measured."""
    ports={p['id']:p for p in catalog['ports']};nbase=len(base['locations'])
    species_holds=set(species_holds)
    port_indices={p['id']:nbase+i for i,p in enumerate(catalog['ports'])}
    origin=date(base['year'],1,1)
    trips={key:[] for key in ports}
    for trip in catalog['trips']:
        trips[trip['port']].append({**trip,'day':(date.fromisoformat(trip['date'])-origin).days,
            'species_set':set(s for o in trip['observations'] for s in o['species'])-species_holds})
    seen={s for d in base['days'] for t in d['stops'] for s in t['species']}
    initial_seen=set(seen);reserved=set();selected=[];road_evidence={};walks={};deferred=list(deferred_seed or [])
    seeds=evidence_seed or {}
    deferred_pairs={(v['from'],port_indices[v['port']]) for v in deferred if v['port'] in port_indices}
    scored_portfolios=0;scored_charters=0
    for _ in range(maximum_rounds):
        choices=[]
        for begin,end,here in free_windows(base,reserved):
            if end-begin<3:continue
            # Nearby ports guide proposals; this is a search heuristic, not an
            # assertion that distant charters or alternative flights cannot fit.
            candidates=[p for p in ports.values() if any(begin+1<=t['day']<end-1 and t['species_set']-seen for t in trips[p['id']])]
            candidates.sort(key=lambda p:gap_m([base['locations'][here]['lon'],base['locations'][here]['lat']],p['boarding_point']))
            for port in candidates[:4]:
                idx=port_indices[port['id']]
                if (here,idx) in deferred_pairs:continue
                cfg=base['config'];home=base['locations'][here]
                distance=gap_m([home['lon'],home['lat']],port['boarding_point'])
                home_gap=home['road_access']['offset_meters']
                maximum_port_gap=(cfg['activity_minutes']-cfg['birding_minutes'])*cfg['walking_speed_kmh']*1000/120
                # The frozen car profile caps speed at vmax. Subtract every
                # possible access gap before screening a geodesic lower bound.
                maximum_speed=float(roads.release['parameters']['vmax'])
                lower_minutes=max(0,distance-home_gap-maximum_port_gap)/1000/maximum_speed*60
                if 2*math.ceil(lower_minutes/cfg['drive_minutes'])+1>end-begin:continue
                try:
                    loc=roads.location(idx)
                    walks[port['id']]=audit_port_walk(port,loc,port_land[port['id']],base['config'])
                    a,b=roads[here,idx],roads[idx,here]
                except (ValueError,RuntimeError,KeyError) as error:
                    deferred.append({'port':port['id'],'from':here,'reason':str(error)[:250]})
                    # Unknown queries are held for this bounded comparison,
                    # never persisted as unreachable or retried on every round.
                    if len(deferred_pairs)<1024:deferred_pairs.add((here,idx))
                    continue
                da=road_transit_days(a,base['config']['drive_minutes'])
                db=road_transit_days(b,base['config']['drive_minutes'])
                # Driving occupies separate days. No borrowing activity hours
                # from adjoining days and no 14-hour pelagic inside a 12h cap.
                eligible=[t for t in trips[port['id']] if begin+da<=t['day']<end-db
                          and t['activity_minutes']+walks[port['id']]['walking_minutes']<=base['config']['activity_minutes']+1e-7]
                if not eligible:continue
                by_day={}
                for t in eligible:by_day.setdefault(t['day'],[]).append(t)
                for reverse in [False,True]:
                    scored_portfolios+=1
                    collected=set(seen);picked=[]
                    for d,options in sorted(by_day.items(),reverse=reverse):
                        scored_charters+=len(options)
                        best=max(options,key=lambda t:(len(t['species_set']-collected),-t['activity_minutes']))
                        if best['species_set']-collected:
                            picked.append(best);collected.update(best['species_set'])
                    if not picked:continue
                    picked.sort(key=lambda t:t['day'])
                    start,finish=picked[0]['day']-da,picked[-1]['day']+db+1
                    choices.append({'new':len(collected-seen),'begin':start,'end':finish,'here':here,
                        'port':port['id'],'port_index':idx,'trips':picked,'outbound':a,'inbound':b,
                        'outbound_days':da,'inbound_days':db,'species':collected-seen})
        if not choices:break
        best=max(choices,key=lambda p:(p['new'],-(p['end']-p['begin']),
                                      -sum(t['activity_minutes'] for t in p['trips'])))
        for pair,cost in [((best['here'],best['port_index']),best['outbound']),
                          ((best['port_index'],best['here']),best['inbound'])]:
            if pair not in road_evidence:
                from .road_matching import verify_evidence
                anchors=[[roads.location(i)['road_access']['lon'],roads.location(i)['road_access']['lat']] for i in pair]
                saved=seeds.get(pair)
                if saved and saved['proof']['network_sha256']==roads.manifest['network_sha256']:
                    verify_evidence(saved['proof'],anchors,cost);proof=saved['proof']
                else:proof=roads.endpoint_evidence(*pair,cost)
                # The final selected route must use the exact measured costs
                # represented by its geometry, not a stale preliminary query.
                r=proof['routes'][0]
                if abs(r['distance']/1609.344-cost['miles'])>1e-6 or abs(r['duration']/60-cost['minutes'])>1e-6:
                    raise ValueError('port access cost changed during final endpoint audit')
                road_evidence[pair]={'cost':cost,'proof':proof}
        seen.update(best['species']);reserved.update(range(best['begin'],best['end']));selected.append(best)
        if progress is not None:progress({'stage':'marine-expedition','port':best['port'],
            'new_species_so_far':len(seen-initial_seen),'expeditions':len(selected),
            'boat_days':sum(len(e['trips']) for e in selected),'deferred_port_pairs':len(deferred_pairs)})
    return {'expeditions':selected,'seen':seen,'new_species':seen-initial_seen,
            'road_evidence':road_evidence,'port_walks':walks,'deferred':deferred,
            'scored_portfolios':scored_portfolios,'scored_charters':scored_charters,
            'proposal_rule':'four nearest ports with novel exact-date options per free window; forward/reverse chronological portfolios; locked terrestrial incumbent'}


def extend_calendar(base,search,locations,catalog,species_holds=()):
    result=deepcopy(base);result['locations']=deepcopy(locations)
    held=set(species_holds);pin_locations={}
    trips=[]
    for expedition in search['expeditions']:
        port=expedition['port_index'];here=expedition['here']
        def drive(start,a,b,count):
            proof=search['road_evidence'][a,b]['proof'];cost=search['road_evidence'][a,b]['cost']
            for k in range(count):
                d=result['days'][start+k];d.update(kind='road_transit',stops=[],
                    legs=[{'from':a,'to':b,'mode':'road','start_fraction':k/count,'end_fraction':(k+1)/count,
                        'miles':cost['miles']/count,'minutes':cost['minutes']/count,
                        'geometry':proof['routes'][0]['geometry'],'schematic':False,
                        'marine_access_expedition':expedition['port']}],
                    road_miles=cost['miles']/count,travel_minutes=cost['minutes']/count,
                    activity_minutes=cost['minutes']/count,walking_minutes=0.,walking_meters=0.)
        drive(expedition['begin'],here,port,expedition['outbound_days'])
        drive(expedition['end']-expedition['inbound_days'],port,here,expedition['inbound_days'])
        for trip in expedition['trips']:
            frozen={k:v for k,v in trip.items() if k not in ['species_set','day']};trips.append(frozen)
            day=result['days'][trip['day']];day.update(kind='pelagic',stops=[],legs=[],
                road_miles=0.,boat_miles=trip['nautical_miles']*1852/1609.344,
                boat_nautical_miles=trip['nautical_miles'],travel_minutes=trip['sailing_minutes'],
                activity_minutes=trip['activity_minutes']+search['port_walks'][trip['port']]['walking_minutes'],
                walking_minutes=search['port_walks'][trip['port']]['walking_minutes'],
                walking_meters=search['port_walks'][trip['port']]['walking_meters'],
                marine_trip=trip['id'],boarding_minutes=trip['boarding_minutes'])
            prev=port
            for j,obs in enumerate(trip['observations']):
                key=(obs['latitude'],obs['longitude'])
                if key not in pin_locations:
                    pin_locations[key]=len(result['locations'])
                    result['locations'].append({'id':f'marine:{obs["latitude"]},{obs["longitude"]}',
                        'name':f'Offshore {obs["latitude"]},{obs["longitude"]}',
                        'lat':obs['lat'],'lon':obs['lon'],'kind':'pelagic_observation',
                        'state':locations[port]['state'],'latitude_text':obs['latitude'],'longitude_text':obs['longitude']})
                idx=pin_locations[key]
                leg=trip['legs'][j]
                day['legs'].append({'from':prev,'to':idx,'mode':'boat','start_fraction':0,'end_fraction':1,
                    'miles':leg['meters']/1609.344,'minutes':leg['minutes'],
                    'geometry':leg['geometry'],'schematic':False,'modeled_charter':True})
                day['stops'].append({'opportunity':trip['id']+f':{j}','location':idx,
                    'name':result['locations'][idx]['name'],'lat':obs['lat'],'lon':obs['lon'],
                    'species':sorted(set(obs['species'])-held),'new_species':[],'evidence':{},
                    'birding_minutes':result['config']['birding_minutes'],
                    'walking_minutes':0.,'walking_meters':0.,'marine_trip':trip['id']})
                prev=idx
            leg=trip['legs'][-1]
            day['legs'].append({'from':prev,'to':port,'mode':'boat','start_fraction':0,'end_fraction':1,
                'miles':leg['meters']/1609.344,'minutes':leg['minutes'],
                'geometry':leg['geometry'],'schematic':False,'modeled_charter':True})
    # Recompute first-credit dates across the complete combined calendar.
    seen=set()
    for day in result['days']:
        fresh=[]
        for stop in day['stops']:
            stop['new_species']=sorted(set(stop['species'])-seen)
            fresh.extend(stop['new_species']);seen.update(stop['species'])
        day.update(new_species=fresh,cumulative_species=len(seen))
    result.update(species_total=len(seen),status='best marine extension found; terrestrial itinerary fixed',
        marine_extension={'scenario':'modeled_same_port_charters_v1','boat_days':len(trips),
            'new_species':sorted(search['new_species']),'baseline_species':base['species_total'],
            'ports':sorted({t['port'] for t in trips}),'native_joint_search':False},
        assumptions=[*base['assumptions'],
            'Marine extension assumes a 12-knot charter available on the selected date; weather, seats, tides, depths and restricted-water navigation are not reconstructed.',
            'Every marine stop uses the source date and exact coordinate. No island landing is assumed. Boat paths clear the frozen OSM land polygon; paths are modeled, not recorded vessel tracks.',
            'Charters return to their departure port. Two hours of birding per exact pin, one hour boarding, full sailing and 2.2 km/h access walking fit the existing 12-hour cap.',
            'Added driving to/from ports occupies separate days within the existing 8-hour driving limit. Existing terrestrial birding and observed-flight days are fixed.'])
    return result,trips
