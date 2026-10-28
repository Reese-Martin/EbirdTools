"""Additional exact-pin Santa Cruz landing choices; original records stay intact.

The two-hour crossings and landing-vicinity walking are modeled access, not a
claim about a historical sailing, available seat, navigable chart or footpath.
"""
from copy import deepcopy
from datetime import date,timedelta
from pathlib import Path

import numpy as np
from shapely.geometry import LineString,Point
from shapely.ops import nearest_points

from .marine import Coastline,length_m
from .road_matching import gap_m
from .surface_access import open_water_meters
from .util import ROOT,ManagedDuckDB,dump,read,sha256


POLICY={'id':'santa-cruz-modeled-landing-v1','one_way_boat_minutes':120,
    'boarding_minutes':60,'birding_minutes_per_exact_pin':120,'walking_kmh':2.2,
    'activity_minutes':720,'same_port_return':True,'departure_port':'ventura',
    'walking':'Full out-and-back landing-to-exact-pin distance, including below100m; no road driving on the island.',
    'availability':'Modeled morning departure and same-day return; historical sailings, weather and seats not certified.',
    'geography':'Additional single-pin visits near two public Santa Cruz landings; on-land walking and water-only crossing checks. Footpaths/public access within the landing vicinity are assumed.',
    'references':['https://www.nps.gov/chis/planyourvisit/birdwatching.htm',
        'https://www.nps.gov/chis/planyourvisit/upload/sci-hiking-2022-ADA.pdf']}
LANDINGS=[{'id':'prisoners','name':'Prisoners Harbor','reference_point':[-119.6838,34.0200],
           'maximum_walk_one_way_m':700,'east_limit_lon':-119.6842},
          {'id':'scorpion','name':'Scorpion Anchorage','reference_point':[-119.5569,34.0499],
           'maximum_walk_one_way_m':1000,'east_limit_lon':-119.57}]


def build_catalog(parent,catalog_path,output):
    """Keep every original charter, add actual source-date landing alternatives."""
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    target=output/'landing-candidates.json'
    if target.exists():
        catalog=read(target)
        if catalog['island_access']['parent_sha256']!=sha256(parent.path):
            raise ValueError('island parent changed')
        return target
    catalog=deepcopy(read(catalog_path));port=next(p for p in catalog['ports'] if p['id']=='ventura')
    stem=ROOT/'build/land-mask/land-polygons-split-4326/land_polygons'
    coast=Coastline(stem,[-119.55,34.13],radius_deg=.5)
    if sha256(Path(str(stem)+'.shp'))!=catalog['osm_land_sha256']:
        raise ValueError('island coast release differs')
    source=parent.original if hasattr(parent,'original') else parent
    records=source.records;coords=source.coords
    additions=[];landings=[];held=[]
    for reference in LANDINGS:
        ref=reference['reference_point']
        water=coast.departure(ref)
        edge=nearest_points(Point(water),coast.land)[1]
        shore=[edge.x,edge.y]
        # The short water-to-shore gangway is modeled explicitly; all longer
        # land walks must stay on land (up to a 5 m shoreline tolerance).
        crossing=coast.route(port['boarding_point'],water)
        if crossing is None:raise ValueError('unresolved island water crossing: '+reference['id'])
        landing={**reference,'water_point':water,'shore_point':shore,
            'gangway_meters':gap_m(water,shore),'crossing_geometry':{'type':'LineString','coordinates':crossing},
            'crossing_meters':length_m(crossing)}
        landings.append(landing)
        # Bounding box is construction only. The full terrestrial model is
        # copied unaltered; no source pin or date is removed or rounded.
        nearby=np.flatnonzero((np.abs(coords[:,0]-shore[1])<.012)&
                             (np.abs(coords[:,1]-shore[0])<.016)&
                             (coords[:,1]>=reference['east_limit_lon']))
        walks={}
        for loc in map(int,nearby):
            original=source.locations[loc]
            if original.get('kind')!='birding' or original['state']!='US-CA':continue
            pin=[original['lon'],original['lat']];distance=gap_m(shore,pin)
            if distance>reference['maximum_walk_one_way_m']:continue
            water_m=open_water_meters(LineString([shore,pin]),coast.land)
            if water_m>5 or not coast.land.buffer(1e-9).covers(Point(pin)):
                held.append({'location':loc,'landing':reference['id'],'reason':'walk_crosses_water','water_meters':water_m});continue
            full=2*(landing['gangway_meters']+distance)
            minutes=full/2200*60
            if 240+60+120+minutes>720:continue
            walks[loc]={'geometry':{'type':'LineString','coordinates':[water,shore,pin]},
                        'meters':full/2,'walking_meters':full,'walking_minutes':minutes}
        options=np.flatnonzero(np.isin(records['location'],list(walks))&(records['reserved']==0))
        for i in map(int,options):
            row=records[i];loc=int(row['location']);original=source.locations[loc];walk=walks[loc]
            offset,count=int(row['offset']),int(row['count'])
            spp=[source.meta['species'][int(s)]['id'] for s in source.species[offset:offset+count]]
            day=str(date(parent.meta['config']['year'],1,1)+timedelta(days=int(row['day'])))
            obs={'date':day,'latitude':original['latitude_text'],'longitude':original['longitude_text'],
                 'lat':original['lat'],'lon':original['lon'],'species':spp,'source_location':original,
                 'source_opportunity':i}
            outbound={'geometry':landing['crossing_geometry'],'meters':landing['crossing_meters'],'minutes':120}
            inbound={'geometry':{'type':'LineString','coordinates':list(reversed(crossing))},
                     'meters':landing['crossing_meters'],'minutes':120}
            additions.append({'id':f'island:{reference["id"]}:{day}:{original["id"]}',
                'kind':'island_landing','date':day,'port':'ventura','observations':[obs],
                'landing':reference['id'],'walk':walk,'legs':[outbound,inbound],
                'sailing_minutes':240,'boarding_minutes':60,'birding_minutes':120,
                'activity_minutes':420+walk['walking_minutes'],
                'nautical_miles':2*landing['crossing_meters']/1852})
    if not additions:raise ValueError('no source-supported island landing visits')
    catalog['marine_data']=str(Path(read(catalog_path)['ports'][0]['coast_path']).parent.parent)
    catalog['island_access']={'policy':POLICY,'parent_path':str(source.path),
        'parent_sha256':sha256(source.path),'original_catalog_path':str(Path(catalog_path).resolve()),
        'original_catalog_sha256':sha256(catalog_path),'original_charters':len(catalog['trips']),
        'landings':landings,'added_options':len(additions),'held_walks':held}
    catalog['trips']+=sorted(additions,key=lambda t:(t['date'],t['id']))
    dump(target,catalog)
    return target


def source_evidence(problem,trip):
    """Retrieve original eligible land evidence only for the selected exact day."""
    from .eod import attach_dataset
    obs=trip['observations'][0];loc=obs['source_location']
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
        view,_=attach_dataset(con,Path(problem['dataset']),problem['manifest'])
        rows=con.execute(f'''SELECT species_id,checklist_id,event_id,record_id,source_name
            FROM {view} WHERE date=? AND location_id=? ORDER BY species_id,record_id''',
            [trip['date'],loc['id']]).fetchall()
    evidence={}
    for s,cl,ev,rec,tax in rows:
        evidence.setdefault(s,[]).append({'checklist':cl,'event':ev,'record':rec,'taxon':tax})
    if set(evidence)!=set(obs['species']):raise ValueError('island source species differ')
    return evidence


def expand_day(problem,day,original,trip):
    joint=problem['joint_marine'];o=problem['opportunities'][original['opportunity']]
    port=o['location'];obs=trip['observations'][0]
    pin=joint['pin_indices'][obs['latitude']+','+obs['longitude']]
    landing=joint['landing_indices'][trip['landing']]
    loc=problem['locations'][pin]
    from .walking import access_walk
    dock=access_walk(problem['locations'][port],problem['config']);walk=trip['walk']
    # Boat geometry ends at a modeled landing. The on-island walk is a
    # separately labeled geometry, never an offshore observation at the port.
    for j,source in enumerate(trip['legs']):
        day['legs'].append({'from':port if j==0 else landing,'to':landing if j==0 else port,
            'mode':'boat','start_fraction':0,'end_fraction':1,'miles':source['meters']/1609.344,
            'minutes':120,'geometry':source['geometry'],'schematic':False,
            'modeled_island_transfer':True,'landing':trip['landing'],'observation_location':pin,
            'island_walk_geometry':walk['geometry']})
    day.update(kind='island',marine_trip=trip['id'],boarding_minutes=60,landing=trip['landing'],
        boat_miles=trip['nautical_miles']*1852/1609.344,boat_nautical_miles=trip['nautical_miles'])
    day['travel_minutes']+=240
    day['walking_minutes']=dock['walking_minutes']+walk['walking_minutes']
    day['walking_meters']=dock['walking_meters']+walk['walking_meters']
    day['stops']=[{'opportunity':original['opportunity'],'location':pin,'name':loc['name'],
        'lat':loc['lat'],'lon':loc['lon'],'species':sorted(obs['species']),'new_species':[],
        'evidence':source_evidence(problem,trip),'birding_minutes':120,
        'walking_minutes':walk['walking_minutes'],'walking_meters':walk['walking_meters'],
        'marine_trip':trip['id'],'landing':trip['landing']}]
