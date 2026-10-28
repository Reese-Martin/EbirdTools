"""Construct exact-pin, water-only charter options in bounded port windows."""
import csv
import json
from pathlib import Path
import time

from shapely import from_wkb, to_wkb

from bigyear_planner.marine import Coastline, Jurisdiction, charter_track
from bigyear_planner.road_matching import gap_m
from bigyear_planner.util import ROOT, ManagedDuckDB, dump, q, read, sha256
from prepare_marine_charter import DATA

BASE = ROOT/'runs/lower48-eod-2024/reconciled-2024-provisional-access-v3'
COAST = ROOT/'build/land-mask/land-polygons-split-4326/land_polygons'


def main():
    started=time.monotonic()
    if (DATA/'charter-candidates.json').exists():raise ValueError('finished charter catalog exists; use a new version')
    route=read(BASE/'route.json')
    seen={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
    ports=read(DATA/'ports.json')
    jurisdiction=Jurisdiction(DATA/'sources/countries-ne-5.1.2.geojson')
    (DATA/'coasts').mkdir(exist_ok=True)
    (DATA/'port-candidates').mkdir(exist_ok=True)
    countries_digest=sha256(DATA/'sources/countries-ne-5.1.2.geojson')
    coast_digest=sha256(Path(str(COAST)+'.shp'))
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'")
        con.execute('SET threads=2')
        con.execute('CREATE TABLE seen(id VARCHAR)')
        con.executemany('INSERT INTO seen VALUES (?)',[(s,) for s in seen])
        con.execute(f'''CREATE TABLE candidates AS SELECT date,lat,lon,decimallatitude latitude,
            decimallongitude longitude,list(DISTINCT species_id) species,
            list(DISTINCT species_id) FILTER (WHERE species_id NOT IN (SELECT id FROM seen)) novel
            FROM read_parquet({q(DATA/'offshore.parquet')})
            WHERE marine_disposition='policy_eligible_pending_marine_access'
            GROUP BY 1,2,3,4,5 HAVING novel IS NOT NULL''')
        counts=con.execute('SELECT count(*) FROM candidates').fetchone()[0]
        all_trips=[];held=[];port_proofs=[];total_examined=0
        for port in ports:
            checkpoint=DATA/'port-candidates'/(port['id']+'.json')
            checkpoint_binding={'version':2,'port':port,'baseline_route_sha256':sha256(BASE/'route.json'),
                'offshore_sha256':sha256(DATA/'offshore.parquet'),'countries_sha256':countries_digest,
                'coast_sha256':coast_digest,'marine_code_sha256':sha256(ROOT/'src/bigyear_planner/marine.py')}
            if checkpoint.exists():
                saved=read(checkpoint)
                if saved['binding']!=checkpoint_binding:raise ValueError('partial marine candidates belong to changed inputs')
                all_trips.extend(saved['trips']);held.extend(saved['held']);total_examined+=saved['examined']
                if saved['port_proof']:port_proofs.append(saved['port_proof'])
                print(json.dumps({'port':port['id'],'reused_candidates':len(saved['trips'])}),flush=True)
                continue
            pin=[port['lon'],port['lat']]
            # Cheap geodesic lower bound is ranking/feasibility pruning only;
            # the accepted path always visits exact pins and clears OSM land.
            rows=con.execute('''SELECT date,lat,lon,latitude,longitude,species,novel FROM candidates
                WHERE abs(lat-?)<1.7 AND abs(lon-?)*cos(radians(?))<1.7
                ORDER BY date,len(novel) DESC,latitude,longitude''',[port['lat'],port['lon'],port['lat']]).fetchall()
            if not rows:continue
            coast_file=DATA/'coasts'/(port['id']+'.wkb')
            binding_file=coast_file.with_suffix('.json')
            binding={'coast_sha256':coast_digest,'port':port,'radius_degrees':1.8}
            if coast_file.exists():
                saved=read(binding_file)
                if any(saved.get(k)!=v for k,v in binding.items()) or sha256(coast_file)!=saved['wkb_sha256']:
                    raise ValueError('cached coast differs')
                coast=Coastline.__new__(Coastline)
                from collections import OrderedDict
                from shapely.prepared import prep
                coast.bounds=tuple(saved['bounds']);coast.land=from_wkb(coast_file.read_bytes())
                coast.prepared=prep(coast.land);coast.memo=OrderedDict()
            else:
                coast=Coastline(COAST,pin)
                coast_file.write_bytes(to_wkb(coast.land))
                dump(binding_file,{**binding,'bounds':coast.bounds,'wkb_sha256':sha256(coast_file)})
            try:departure=coast.departure(pin)
            except ValueError as error:
                hold={'port':port['id'],'date':'','latitude':str(port['lat']),
                    'longitude':str(port['lon']),'reason':str(error)}
                held.append(hold)
                dump(checkpoint,{'binding':checkpoint_binding,'trips':[],'held':[hold],'examined':0,'port_proof':None})
                print(json.dumps({'port':port['id'],'held':str(error)}),flush=True)
                continue
            pproof={**port,'boarding_point':departure,'reference_coordinate_is_approximate':True,
                'boarding_point_rule':'Modeled water-side point near the named marina; public dock/foot access assumed.',
                'coast_path':str(coast_file),'coast_sha256':sha256(coast_file)}
            port_proofs.append(pproof)
            trips=[];examined=0;by_date={};first_hold=len(held)
            for row in rows:
                date,lat,lon,latitude,longitude,species,novel=row
                exact=[lon,lat]
                # Even a straight out-and-back at cruise plus a 2h stop and
                # 1h boarding cannot fit beyond this distance.
                if 2*gap_m(departure,exact)/(12*1852)*60+180>route['config']['activity_minutes']:
                    continue
                examined+=1
                if examined%20==0:
                    print(json.dumps({'port':port['id'],'examined':examined,'feasible_so_far':len(trips)}),flush=True)
                if not coast.water(exact):
                    held.append({'port':port['id'],'date':str(date),'latitude':latitude,'longitude':longitude,'reason':'on_land_or_outside_local_coast_window'})
                    continue
                proof=jurisdiction.check(lat,lon)
                if proof['status']!='verified_lower48_waters':
                    held.append({'port':port['id'],'date':str(date),'latitude':latitude,'longitude':longitude,'reason':proof['status']})
                    continue
                track=charter_track(coast,departure,[exact],route['config'])
                if track is None:continue
                obs={'date':str(date),'latitude':latitude,'longitude':longitude,
                     'lat':lat,'lon':lon,'species':species,'novel':novel,'jurisdiction':proof}
                trip={'id':f'{port["id"]}:{date}:{latitude}:{longitude}',
                      'date':str(date),'port':port['id'],'observations':[obs],**track}
                trips.append(trip);by_date.setdefault(str(date),[]).append(trip)
            # Keep all feasible exact-pin options. This controlled benchmark
            # chooses one per date; multi-pin extensions need a fresh complete
            # water-path/time check, supported by charter_track's pin list.
            all_trips.extend(trips);total_examined+=examined
            dump(checkpoint,{'binding':checkpoint_binding,'trips':trips,'held':held[first_hold:],
                'examined':examined,'port_proof':pproof})
            print(json.dumps({'port':port['id'],'examined':examined,'feasible_charters':len(trips),
                              'elapsed_seconds':time.monotonic()-started}),flush=True)
            dump(DATA/'candidate-progress.json',{'last_port':port['id'],'trips':len(all_trips),'held':len(held)})
        dump(DATA/'charter-candidates.json',{'schema':1,'baseline_route':str(BASE/'route.json'),
            'baseline_route_sha256':sha256(BASE/'route.json'),'marine_manifest_sha256':sha256(DATA/'manifest.json'),
            'offshore_sha256':sha256(DATA/'offshore.parquet'),'ports_sha256':sha256(DATA/'ports.json'),
            'country_coast_sha256':countries_digest,'osm_land_sha256':coast_digest,
            'policy':{'speed_knots':12,'boarding_minutes':60,'birding_minutes_per_exact_pin':120,
                      'activity_minutes':720,'walking_kmh':2.2,'same_port_return':True,
                      'weather_and_charter_availability':'assumed; historical sailings not claimed',
                      'no_island_landing':True,'jurisdiction_uncertainty_hold_m':5000},
            'raw_exact_pin_date_options_with_unseen_species':counts,'examined_port_pin_pairs':total_examined,
            'ports':port_proofs,'trips':all_trips,'elapsed_seconds':time.monotonic()-started})
        if held:
            with (DATA/'marine-holds.csv').open('w',newline='') as stream:
                w=csv.DictWriter(stream,fieldnames=list(held[0]));w.writeheader();w.writerows(held)
    print(json.dumps({'stage':'complete','trips':len(all_trips),'held':len(held)}),flush=True)


if __name__=='__main__':main()
