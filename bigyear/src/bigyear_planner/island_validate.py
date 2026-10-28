"""Independent landing-catalog and complete island-day checks.

Does not call the island candidate builder, expander or native scorer.
"""
from datetime import date
from pathlib import Path

from shapely.geometry import LineString,Point

from .compact_model import load
from .marine import Coastline,length_m
from .road_matching import gap_m,verify_evidence
from .surface_access import open_water_meters
from .util import ROOT,ManagedDuckDB,read,sha256


def audit_catalog(catalog,check):
    binding=catalog['island_access'];policy=binding['policy']
    check(policy['one_way_boat_minutes']==120 and policy['boarding_minutes']==60 and
          policy['birding_minutes_per_exact_pin']==120 and policy['activity_minutes']==720 and
          policy['walking_kmh']==2.2 and policy['same_port_return'] is True and
          policy['departure_port']=='ventura','island scenario rules changed')
    check(sha256(binding['original_catalog_path'])==binding['original_catalog_sha256'],'original charter catalog changed')
    original=read(binding['original_catalog_path']);count=binding['original_charters']
    check(count==len(original['trips']) and catalog['trips'][:count]==original['trips'],
          'island extension changed an original charter')
    for key in ['ports','policy','marine_manifest_sha256','offshore_sha256','ports_sha256',
                'country_coast_sha256','osm_land_sha256']:
        check(catalog[key]==original[key],'island extension changed marine input: '+key)
    check(sha256(binding['parent_path'])==binding['parent_sha256'],'island source model changed')
    parent=load(binding['parent_path'])
    coast=Coastline(ROOT/'build/land-mask/land-polygons-split-4326/land_polygons',[-119.55,34.13],radius_deg=.5)
    landings={v['id']:v for v in binding['landings']}
    port=next(p for p in catalog['ports'] if p['id']=='ventura')
    try:
        check(len(catalog['trips'])-count==binding['added_options'],'island option count changed')
        for t in catalog['trips'][count:]:
            check(t['kind']=='island_landing' and t['port']=='ventura' and len(t['observations'])==1,
                  'invalid island trip identity')
            o=t['observations'][0];row=parent.records[o['source_opportunity']]
            loc=parent.locations[int(row['location'])];landing=landings[t['landing']]
            day=(date.fromisoformat(t['date'])-date(parent.meta['config']['year'],1,1)).days
            off=int(row['offset']);expected={parent.meta['species'][int(s)]['id']
                for s in parent.species[off:off+int(row['count'])]}
            check(int(row['reserved'])==0 and int(row['day'])==day and loc==o['source_location']
                  and loc['state']=='US-CA' and o['date']==t['date'] and set(o['species'])==expected,
                  'island credit lacks unchanged terrestrial source support')
            check((o['latitude'],o['longitude'])==(loc['latitude_text'],loc['longitude_text']) and
                  (o['lat'],o['lon'])==(loc['lat'],loc['lon']),'island exact coordinate changed')
            points=t['walk']['geometry']['coordinates'];pin=[loc['lon'],loc['lat']]
            check(points==[landing['water_point'],landing['shore_point'],pin],'island walk misses landing or exact source')
            distance=gap_m(points[0],points[1])+gap_m(points[1],points[2])
            check(abs(2*distance-t['walk']['walking_meters'])<1e-6 and
                  abs(2*distance/2200*60-t['walk']['walking_minutes'])<1e-6,'island round-trip walking understated')
            check(gap_m(points[1],pin)<=landing['maximum_walk_one_way_m'] and
                  loc['lon']>=landing['east_limit_lon'] and
                  open_water_meters(LineString(points[1:]),coast.land)<=5 and
                  coast.land.buffer(1e-9).covers(Point(pin)),'island walk leaves modeled land-access vicinity')
            check(gap_m(points[0],points[1])<=100,'island shore transfer exceeds gangway allowance')
            sailing_meters=0
            check(len(t['legs'])==2,'island must return to its departure port')
            for j,leg in enumerate(t['legs']):
                pts=leg['geometry']['coordinates']
                ends=[port['boarding_point'],landing['water_point']]
                if j:ends.reverse()
                length=length_m(pts);sailing_meters+=length
                check([pts[0],pts[-1]]==ends and coast.clear(pts),'island crossing misses endpoints or crosses land')
                check(leg['minutes']==120 and abs(leg['meters']-length)<1e-6,'island crossing time or distance changed')
            check(t['sailing_minutes']==240 and t['boarding_minutes']==60 and t['birding_minutes']==120 and
                  abs(t['nautical_miles']-sailing_meters/1852)<1e-6 and
                  abs(t['activity_minutes']-(420+2*distance/2200*60))<1e-6 and
                  t['activity_minutes']<=720,'island activity budget changed')
    finally:parent.close()


def audit_day(ctx,day,here,problem,route,roads,seen,check,endpoint_pairs,endpoint_gaps):
    joint=ctx['joint'];locs=problem['locations'];trip=ctx['trips'][day['marine_trip']]
    check(trip.get('kind')=='island_landing' and len(day['stops'])==1,'invalid island day')
    stop=day['stops'][0];oi=stop['opportunity'];o=problem['opportunities'][oi];pi=o['location']
    obs=trip['observations'][0];port=ctx['ports'][trip['port']]
    check(o['date']==day['date']==trip['date'] and o.get('marine_trip')==trip['id'] and
          trip==joint['selected_trips'][trip['id']],'island day/source identity differs')
    check(locs[pi]['port']=='ventura' and [locs[pi]['lon'],locs[pi]['lat']]==port['boarding_point'],
          'island departure port changed')
    roadlegs=[x for x in day['legs'] if x['mode']=='road'];boatlegs=[x for x in day['legs'] if x['mode']=='boat']
    check(day['legs']==roadlegs+boatlegs and len(roadlegs)<=1 and len(boatlegs)==2,'island day leg order changed')
    miles=minutes=0.
    for leg in roadlegs:
        a,b=leg['from'],leg['to'];road=roads.get((a,b))
        check(here==a and b==pi and leg['start_fraction']==0 and leg['end_fraction']==1,
              'island incoming road discontinuity')
        check(road is not None,'island incoming road lacks evidence')
        if road:
            anchors=[[locs[i]['road_access']['lon'],locs[i]['road_access']['lat']] for i in [a,b]]
            endpoint_gaps.extend(verify_evidence(road['endpoint_evidence'],anchors,road));endpoint_pairs.add((a,b))
            check(road['endpoint_evidence']['network_sha256']==route['provenance']['road_network_sha256'] and
                  abs(leg['miles']-road['miles'])<1e-5 and abs(leg['minutes']-road['minutes'])<1e-5 and
                  leg['geometry']==road['geometry'],'island incoming road changed')
        miles+=leg['miles'];minutes+=leg['minutes'];here=b
    check(here==pi,'traveler is not at island departure port')
    pin=stop['location'];loc=locs[pin]
    landing_idx=joint['landing_indices'][trip['landing']]
    landing=next(l for l in joint['island_access']['landings'] if l['id']==trip['landing'])
    check(locs[landing_idx]['kind']=='island_landing' and locs[landing_idx]['id']=='island-landing:'+trip['landing'] and
          [locs[landing_idx]['lon'],locs[landing_idx]['lat']]==landing['water_point'],'modeled landing identity changed')
    check({k:v for k,v in loc.items() if k!='kind'}==
          {k:v for k,v in obs['source_location'].items() if k!='kind'} and
          loc['kind']=='island_observation','island source identity or coordinate changed')
    check((stop['lat'],stop['lon'],stop['name'])==(loc['lat'],loc['lon'],loc['name']) and
          stop['marine_trip']==trip['id'] and stop['landing']==trip['landing']==day['landing'],
          'island stop metadata changed')
    check(stop['species']==sorted(obs['species']) and set(stop['species'])==
          {problem['species'][s]['id'] for s in o['species']},'island species differ from source')
    for j,(leg,source) in enumerate(zip(boatlegs,trip['legs'])):
        check((leg['from'],leg['to'])==((pi,landing_idx) if j==0 else (landing_idx,pi)) and
              leg['modeled_island_transfer'] is True and leg['landing']==trip['landing'] and
              leg['observation_location']==pin and
              leg['geometry']==source['geometry'] and leg['island_walk_geometry']==trip['walk']['geometry'] and
              leg['start_fraction']==0 and leg['end_fraction']==1 and leg['minutes']==120 and
              abs(leg['miles']-source['meters']/1609.344)<1e-6,'island boat/landing/walk geometry changed')
    anchor=locs[pi]['road_access'];gap=gap_m([anchor['lon'],anchor['lat']],port['boarding_point'])
    dock_m=2*gap if gap>problem['config']['walking_threshold_m'] else 0.;dock_t=dock_m/2200*60
    check(open_water_meters(LineString([[anchor['lon'],anchor['lat']],port['boarding_point']]),
          ctx['coasts']['ventura'].land)<=100,'island mainland dock walk crosses water')
    walk=trip['walk'];wm=dock_m+walk['walking_meters'];wt=dock_t+walk['walking_minutes']
    activity=minutes+240+60+120+wt
    check(stop['birding_minutes']==120 and abs(stop['walking_minutes']-walk['walking_minutes'])<1e-6 and
          abs(stop['walking_meters']-walk['walking_meters'])<1e-6,'island stop time differs')
    check(activity<=720+1e-5 and miles<=problem['config']['local_miles']+1e-5 and
          abs(day['activity_minutes']-activity)<1e-5 and day['boarding_minutes']==60,'island daily budget exceeded')
    check(abs(day['travel_minutes']-minutes-240)<1e-5 and abs(day['road_miles']-miles)<1e-5 and
          abs(day['boat_nautical_miles']-trip['nautical_miles'])<1e-5 and
          abs(day['boat_miles']-trip['nautical_miles']*1852/1609.344)<1e-5 and
          abs(day['walking_minutes']-wt)<1e-5 and abs(day['walking_meters']-wm)<1e-5,'island day totals differ')
    # Independent dataset lookup: no species can be created by the access mode.
    from .eod import attach_dataset
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
        view,_=attach_dataset(con,Path(problem['dataset']),problem['manifest'])
        rows=con.execute(f'''SELECT species_id,checklist_id,event_id,record_id,source_name FROM {view}
            WHERE date=? AND location_id=? ORDER BY species_id,record_id''',[day['date'],loc['id']]).fetchall()
    evidence={}
    for s,cl,ev,rec,tax in rows:evidence.setdefault(s,[]).append({'checklist':cl,'event':ev,'record':rec,'taxon':tax})
    check(stop['evidence']==evidence and set(evidence)==set(stop['species']),'island original land evidence differs')
    fresh=[s for s in stop['species'] if s not in seen];seen.update(stop['species'])
    check(stop['new_species']==fresh and day['new_species']==fresh and day['cumulative_species']==len(seen),
          'island distinct species credit differs')
    ctx['boat_days']+=1;ctx['max_activity']=max(ctx['max_activity'],activity)
    return pi,miles,minutes+240,wt,wm
