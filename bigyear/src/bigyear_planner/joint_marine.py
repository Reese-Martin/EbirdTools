"""Joint road/dated-flight/charter catalog; observation pins are never ports.

The terrestrial catalog is copied losslessly. Marine alternatives share physical
port nodes, and carry per-opportunity activity/sailing costs in a bound sidecar.
"""
from copy import deepcopy
import csv
from datetime import date
import json
from pathlib import Path
import shutil
import sqlite3

import numpy as np

from .compact_model import load
from .config import Config
from .marine import observer_coverage
from .marine_roads import MarineRoads
from .mobility import MobilityRoads
from .util import dump, read, sha256
from .walking import access_walk


def build_joint(parent_path, catalog_path, holds_path, output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    target=output/'model.json'
    if target.exists():return load(target)
    if any(output.iterdir()):raise ValueError('incomplete joint model exists; inspect before replacing')
    parent=load(parent_path);catalog=read(catalog_path);holds=read(holds_path)
    try:
        held={v['species_id'] for v in holds['species']}
        trips=catalog['trips'];ports=catalog['ports'];nland=parent.meta['locations']
        species=deepcopy(parent.meta['species']);spidx={v['id']:i for i,v in enumerate(species)}
        tax={v['species_id']:v for v in csv.DictReader((Path(parent.meta['dataset'])/'taxonomy.csv').open())}
        additions=sorted({s for t in trips for o in t['observations'] for s in o['species']}-set(spidx)-held)
        for s in additions:
            spidx[s]=len(species);species.append({'id':s,'name':tax[s]['common_name']})
        portidx={p['id']:nland+i for i,p in enumerate(ports)}
        shutil.copyfile(parent.root/'locations.sqlite',output/'locations.sqlite')
        with sqlite3.connect(output/'locations.sqlite') as db:
            for p in ports:
                loc={'id':'marine-port:'+p['id'],'name':p['name'],'lat':p['boarding_point'][1],
                     'lon':p['boarding_point'][0],'state':p['state'],'kind':'marine_port','port':p['id'],
                     'modeled_boarding_point':True}
                db.execute('INSERT INTO locations VALUES (?,?)',[portidx[p['id']],json.dumps(loc)])
        shutil.copyfile(parent.root/'coordinates.bin',output/'coordinates.bin')
        with (output/'coordinates.bin').open('ab') as stream:
            np.asarray([[p['boarding_point'][1],p['boarding_point'][0]] for p in ports],dtype='<f8').tofile(stream)
        byday={}
        for i,t in enumerate(trips,1):
            d=(date.fromisoformat(t['date'])-date(parent.meta['config']['year'],1,1)).days
            if not 0 <= d < Config(**parent.meta['config']).days:
                raise ValueError('charter date falls outside the source model year')
            spp=sorted({spidx[s] for o in t['observations'] for s in o['species']
                        if t.get('kind')=='island_landing' or s not in held})
            byday.setdefault(d,[]).append((portidx[t['port']],i,spp))
        offset=0;extra=0;old_to_new_day_offsets=[];trip_opps={}
        with (output/'opportunities.bin').open('wb') as records,(output/'species.bin').open('wb') as spp:
            for d in range(Config(**parent.meta['config']).days):
                lo,hi=np.searchsorted(parent.records['day'],[d,d+1])
                old_to_new_day_offsets.append(extra)
                block=parent.records[lo:hi].copy()
                if len(block):
                    begin=int(block[0]['offset']);end=int(block[-1]['offset'])+int(block[-1]['count'])
                    block['offset']+=offset-begin
                    block.tofile(records);parent.species[begin:end].tofile(spp);offset+=end-begin
                for loc,variant,values in sorted(byday.get(d,[])):
                    np.asarray([(d,loc,offset,len(values),variant)],dtype=parent.records.dtype).tofile(records)
                    np.asarray(values,dtype='<u4').tofile(spp)
                    trip_opps[trips[variant-1]['id']]=int(hi)+extra;extra+=1;offset+=len(values)
        with (output/'marine-visits.txt').open('w') as stream:
            stream.write(str(len(trips))+'\n')
            for i,t in enumerate(trips,1):
                stream.write(f"{i} {t['activity_minutes']-parent.meta['config']['birding_minutes']:.17g} {t['sailing_minutes']:.17g}\n")
        binding={'parent_path':str(parent.path),'parent_sha256':sha256(parent.path),
            'catalog_path':str(Path(catalog_path).resolve()),'catalog_sha256':sha256(catalog_path),
            'holds_path':str(Path(holds_path).resolve()),'holds_sha256':sha256(holds_path),
            'land_locations':nland,'land_opportunities':len(parent.records),
            'trip_ids':[t['id'] for t in trips],'trip_opportunities':trip_opps,
            'old_to_new_day_offsets':old_to_new_day_offsets,'port_indices':portidx,
            'marine_data':catalog.get('marine_data',str(Path(catalog_path).resolve().parent)),
            'policy':f'Joint choices over unchanged complete terrestrial cohort and all {len(trips)} constructed exact-pin boat choices; marine construction remains partial and single-pin. No observation archive is reduced or rewritten.'}
        if catalog.get('island_access'):
            binding['island_access']=catalog['island_access']
            binding['policy']+=' Modeled Santa Cruz island landing alternatives are additional visits from Ventura; original terrestrial records and charter options remain unchanged.'
        meta=deepcopy(parent.meta);meta.update(locations=nland+len(ports),opportunities=len(parent.records)+extra,
            species_entries=offset,species=species,joint_marine=binding,
            components={name:sha256(output/name) for name in ['locations.sqlite','coordinates.bin','opportunities.bin','species.bin','marine-visits.txt']})
        # The source derivative bindings refer to the parent, whose components
        # are unchanged. They must not be misapplied to this expanded catalog.
        for key in ['calendar_source','countability_source']:meta.pop(key,None)
        meta['reduction']={**meta['reduction'],'candidate_species_ceiling':len(species),
            'selected_birding_locations':nland,'marine_port_nodes':len(ports),'marine_charter_options':len(trips),
            'policy':binding['policy']}
        coverage=observer_coverage(parent.meta,catalog)
        meta['first_seen']=[{'species':v['species_id'],'date':v['first_date']} for v in coverage['first_seen']]
        dump(target,meta)
        return load(target)
    finally:parent.close()


class JointRoads(MarineRoads,MobilityRoads):
    """Independent road/airport workers plus shared direction-aware port nodes."""
    def __init__(self,*args,source_access_holds_path=None,**kwargs):
        self.source_visit_holds=set();self.source_visit_hold_binding=None
        if source_access_holds_path is not None:
            path=Path(source_access_holds_path).resolve();policy=read(path)
            self.source_visit_holds={tuple(p) for p in policy['exact_coordinate_holds']}
            self.source_visit_hold_binding={'path':str(path),'sha256':sha256(path)}
        super().__init__(*args,**kwargs)

    def access(self,index):
        point=tuple(map(float,self.model.coords[index]))
        if point in self.source_visit_holds:
            # A visit is held, not a physical road. Do not overwrite measured
            # anchors/costs or cache an invented disconnected network edge.
            return *point,float('inf')
        return super().access(index)

    def costs(self,a,b):
        # SearchRoads has a fast cached stop-duration path. Apply this visit
        # mask before that path, without changing any physical anchor/edge.
        if a==-1 and tuple(map(float,self.model.coords[b])) in self.source_visit_holds:
            return 0.,float('inf')
        return super().costs(a,b)

    def selection(self,problem):
        super().selection(problem)
        binding=self.model.meta['joint_marine'];catalog=read(binding['catalog_path'])
        byid={t['id']:t for t in catalog['trips']}
        selected={o['marine_trip']:byid[o['marine_trip']] for o in problem['opportunities'] if o.get('marine_trip')}
        pins={}
        for t in selected.values():
            for obs in t['observations']:
                key=obs['latitude']+','+obs['longitude']
                if key not in pins:
                    pins[key]=len(problem['locations'])
                    problem['locations'].append((dict(obs['source_location'],kind='island_observation')
                        if t.get('kind')=='island_landing' else {'id':'marine:'+key,'name':'Offshore '+key,
                        'lat':obs['lat'],'lon':obs['lon'],'latitude_text':obs['latitude'],'longitude_text':obs['longitude'],
                        'state':None,'kind':'pelagic_observation'}))
        problem['joint_marine']={**binding,'selected_trips':selected,'pin_indices':pins}
        if binding.get('island_access'):
            landing_indices={};landings={l['id']:l for l in binding['island_access']['landings']}
            for key in sorted({t['landing'] for t in selected.values() if t.get('kind')=='island_landing'}):
                landing=landings[key];landing_indices[key]=len(problem['locations'])
                problem['locations'].append({'id':'island-landing:'+key,'name':landing['name'],
                    'lat':landing['water_point'][1],'lon':landing['water_point'][0],
                    'state':'US-CA','kind':'island_landing','modeled_landing_point':True})
            problem['joint_marine']['landing_indices']=landing_indices
        if self.source_visit_hold_binding is not None:
            problem['joint_marine']['source_visit_holds']=self.source_visit_hold_binding

    def finalize_route(self,route,problem,out):
        # This hook runs before problem/route provenance hashes are frozen.
        from .joint_sources import complete_sources
        complete_sources(route,problem,out)
        if getattr(self,'search_lineage',None):
            route['search']['lineage']=self.search_lineage
            problem['joint_marine']['search_lineage']=self.search_lineage
        if getattr(self,'study_context',None):
            route['search']['study']=self.study_context
            problem['joint_marine']['study']=self.study_context
            route['assumptions'] += [f"Calendar strategy study: {self.study_context['arm']}, {self.study_context['allocated_seconds']:g} seconds search allocation. The final native snapshot only materializes the best complete seed; stage search results are separately bound."]


def expand_calendar(problem,route):
    joint=problem['joint_marine'];held={v['species_id'] for v in read(joint['holds_path'])['species']}
    opps=problem['opportunities'];boatdays=0
    for day in route['days']:
        chosen=[s for s in day['stops'] if opps[s['opportunity']].get('marine_trip')]
        if not chosen:continue
        if len(chosen)!=1 or len(day['stops'])!=1:raise ValueError('charter must be the only visit that day')
        original=chosen[0];o=opps[original['opportunity']];t=joint['selected_trips'][o['marine_trip']]
        if t.get('kind')=='island_landing':
            from .island_access import expand_day
            expand_day(problem,day,original,t)
            boatdays+=1
            continue
        port=o['location'];walking=access_walk(problem['locations'][port],problem['config'])
        day.update(kind='pelagic',marine_trip=t['id'],boarding_minutes=t['boarding_minutes'],stops=[],
            boat_miles=t['nautical_miles']*1852/1609.344,boat_nautical_miles=t['nautical_miles'])
        day['travel_minutes']+=t['sailing_minutes']
        previous=port
        for j,obs in enumerate(t['observations']):
            idx=joint['pin_indices'][obs['latitude']+','+obs['longitude']];leg=t['legs'][j]
            day['legs'].append({'from':previous,'to':idx,'mode':'boat','start_fraction':0,'end_fraction':1,
                'miles':leg['meters']/1609.344,'minutes':leg['minutes'],'geometry':leg['geometry'],
                'schematic':False,'modeled_charter':True})
            loc=problem['locations'][idx]
            day['stops'].append({'opportunity':original['opportunity'],'location':idx,'name':loc['name'],
                'lat':obs['lat'],'lon':obs['lon'],'species':sorted(set(obs['species'])-held),'new_species':[],
                'evidence':{},'birding_minutes':problem['config']['birding_minutes'],'walking_minutes':0.,
                'walking_meters':0.,'marine_trip':t['id']})
            previous=idx
        leg=t['legs'][-1]
        day['legs'].append({'from':previous,'to':port,'mode':'boat','start_fraction':0,'end_fraction':1,
            'miles':leg['meters']/1609.344,'minutes':leg['minutes'],'geometry':leg['geometry'],
            'schematic':False,'modeled_charter':True})
        day.update(walking_minutes=walking['walking_minutes'],walking_meters=walking['walking_meters'])
        boatdays+=1
    seen=set()
    for day in route['days']:
        fresh=[]
        for stop in day['stops']:
            stop['new_species']=[s for s in stop['species'] if s not in seen]
            fresh.extend(stop['new_species']);seen.update(stop['species'])
        day.update(new_species=fresh,cumulative_species=len(seen))
    route.update(species_total=len(seen),marine_observer_coverage=observer_coverage(read(joint['parent_path']),read(joint['catalog_path'])),
        marine_extension={'scenario':'modeled_same_port_charters_v1','boat_days':boatdays,'native_joint_search':True,
            'ports':sorted({t['port'] for t in joint['selected_trips'].values()})})
    route['annual_recorded_species_ceiling']=route['marine_observer_coverage']['annual_species']
    route['assumptions']=[s for s in route['assumptions'] if 'offshore and unclassified land' not in s]
    route['assumptions'] += [joint['policy'],
        'Driving, dated flights and modeled charters compete in the same native calendar search. No global optimality is established.',
        'Charters return to the same port at 12 knots, with 60 minutes boarding and 120 minutes birding per exact dated observation pin. No island landings; weather, historical charter availability, tides and bathymetry are assumed.',
        'A charter is the only birding visit on its date. Arrival by at most 30 local road miles is allowed only when full sailing, boarding, birding and 2.2 km/h dock walking still fit 12 hours.',
        'Boat-only unresolved species holds remain excluded from boat credit; terrestrial eligibility is unchanged.']
    if joint.get('island_access'):
        route['marine_extension']['island_days']=sum(d['kind']=='island' for d in route['days'])
        route['marine_extension']['scenario']='modeled_charters_and_santa_cruz_landings_v1'
        route['assumptions']=[s.replace('No island landings;','Pelagic charters do not land;') for s in route['assumptions']]
        route['assumptions']=[s.replace('Boat-only unresolved species holds remain excluded from boat credit; terrestrial eligibility is unchanged.',
            'Unresolved offshore records remain excluded from pelagic credit. Island landings use independently eligible terrestrial records; terrestrial countability is unchanged.') for s in route['assumptions']]
        route['assumptions']+=['Santa Cruz landing trips assume two hours each way from Ventura, 60 minutes total boarding, exact dated terrestrial observations, and separately charged round-trip island walking at 2.2 km/h. Public landing-vicinity access is modeled; historical sailings, trail conditions and weather are not certified.']
    return route
