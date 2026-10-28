"""Audit a marine extension without calling its search or calendar builder."""
from copy import deepcopy
import csv
from datetime import date,timedelta
import math
from pathlib import Path
import re
import sqlite3

from shapely.geometry import LineString

from .countability import region_supported
from .eod import STATE_NAMES
from .eod_archive import SCHEMA,archive_scan
from .marine import Coastline,Jurisdiction,length_m,observer_coverage
from .road_matching import gap_m,verify_evidence
from .surface_access import open_water_meters
from .util import ManagedDuckDB,q,read,sha256


def publisher_check(row,raw,tax):
    """The existing public-review proxy, with exact dated source identity."""
    dwc='http://rs.tdwg.org/dwc/terms/'
    get=lambda k:raw.get(dwc+k,raw.get(k))
    try:
        actual=f"{int(get('year')):04d}-{int(get('month')):02d}-{int(get('day')):02d}"
        valid=(str(raw['key'])==str(row['representative_gbifid'])
            and raw['datasetKey']=='4fa7b334-ce0d-4e88-aaae-2e0c138d049e'
            and get('scientificName')==row['v_scientificname']
            and get('taxonConceptID')==row['v_taxonconceptid']
            and get('basisOfRecord')=='HumanObservation' and get('occurrenceStatus')=='PRESENT'
            and actual==str(row['date']) and get('country')=='United States'
            and get('stateProvince')==row['stateprovince']
            and (float(get('decimalLatitude')),float(get('decimalLongitude')))==(row['lat'],row['lon']))
    except (ValueError,TypeError,KeyError):valid=False
    if not valid:return 'publisher_metadata_mismatch'
    locality=str(get('locality') or '')
    if re.search(r'\baviary\b|\bcaptive\b|\bescape[ed]?\b|rehabilitation',locality,re.I):
        return 'individual_wildness_hold'
    if any(re.search(r'\bcaptive\b|\bdomestic(?:ated)?\b',str(get(k) or ''),re.I)
           for k in ['establishmentMeans','degreeOfEstablishment']):return 'explicit_captive_hold'
    if any(str(raw.get(k,'')).lower()=='true' for k in ['isCaptive','captive']):return 'explicit_captive_hold'
    if re.search(r'\bcaptive (?:bird|individual)\b|\bescaped (?:pet|from)\b|\bin an? aviary\b',
                 str(get('occurrenceRemarks') or ''),re.I):return 'individual_remarks_hold'
    state='US-'+next((s for s,n in STATE_NAMES.items() if n==row['stateprovince']),'unknown')
    if (tax['aba_eligible']!='1' or tax['eod_presence_eligible']!='1'
        or tax['source_taxon_id']!=row['v_taxonconceptid']
        or tax['species_id']!=row['species_id']
        or tax['eligible_states']!='*' and state not in tax['eligible_states'].split('|')
        or not region_supported(tax,state,row['lat'],row['lon'],str(row['date']))):
        return 'taxonomy_or_population_hold'
    return 'supported'


def _ordinary_day(day):
    out=deepcopy(day);out.pop('new_species',None);out.pop('cumulative_species',None)
    for stop in out['stops']:stop.pop('new_species',None)
    return out


def validate_marine(route_path,problem_path,roads_path,base_validation=None):
    from .validate import validate
    r,p=read(route_path),read(problem_path);errors=[]
    def check(ok,message):
        if not ok:errors.append(message)
    check(p['schema']=='marine-extension-v1','unsupported marine problem')
    check(sha256(problem_path)==r['provenance']['problem_sha256'],'marine problem changed')
    check(sha256(roads_path)==r['provenance']['roads_sha256'],'marine road snapshot changed')
    base_path=Path(p['base_route']);base=read(base_path)
    check(sha256(base_path)==p['base_route_sha256'],'terrestrial incumbent changed')
    check(sha256(base_path.parent/'validation.json')==p['base_validation_sha256'],'baseline validation changed')
    if base_validation is None:
        base_validation=validate(base_path,base_path.parent/'problem.json',base_path.parent/base['provenance']['roads_file'])
    check(base_validation['valid'],'terrestrial incumbent failed independent revalidation')
    check(sha256(base_path.parent/'countability-source-validation.json')==p['base_publisher_validation_sha256'],
          'baseline publisher postcheck changed')
    check(read(base_path.parent/'countability-source-validation.json')['route_sha256']==p['base_route_sha256'],
          'baseline publisher audit belongs to another route')
    data=Path(p['marine_data']);catalog_path=data/'charter-candidates.json';catalog=read(catalog_path)
    for name,key in [('manifest.json','marine_manifest_sha256'),('offshore.parquet','offshore_sha256'),
                     ('ports.json','ports_sha256')]:
        check(sha256(data/name)==catalog[key],'marine input changed: '+name)
    check(sha256(catalog_path)==p['marine_catalog_sha256'],'charter catalog changed')
    check(read(data/'manifest.json')['parent_manifest_sha256']==r['provenance']['dataset_manifest_sha256'],
          'marine supplement belongs to another taxonomy/source parent')
    check(catalog['baseline_route_sha256']==p['base_route_sha256'],'charter catalog baseline differs')
    parent=read(p['dataset']+'/manifest.json')
    base_problem=read(base_path.parent/'problem.json')
    check(r['marine_observer_coverage']==observer_coverage(read(base_problem['compact_source']['path']),catalog),
          'marine all-observer comparison changed')
    check(sha256(Path(p['dataset'])/'manifest.json')==r['provenance']['dataset_manifest_sha256'],
          'terrestrial dataset changed')
    check(r['config']==base['config'] and r['year']==base['year'],'calendar/budget policy changed')
    check(r['locations'][:len(base['locations'])]==base['locations'],'original source coordinates changed')
    check(r['species'][:len(base['species'])]==base['species'],'original species catalog changed')
    check(len(r['days'])==len(base['days']),'calendar length changed')
    check(len({l['id'] for l in r['locations']})==len(r['locations']),'location catalog contains duplicate IDs')
    check(r['initial_location']==base['initial_location'],'initial location changed')
    plan_path=Path(problem_path).parent/'marine-plan.json';plan=read(plan_path)
    check(sha256(plan_path)==p['marine_plan_sha256'],'marine plan changed')
    check(plan['config']==base['config'],'marine plan uses different budgets')
    species_holds=set()
    if p.get('marine_access_score_holds'):
        binding=p['marine_access_score_holds'];check(sha256(binding['path'])==binding['sha256'],'marine access holds changed')
        for hold in read(binding['path'])['species']:
            check(hold['scope']=='boat-only' and hold['status']=='unresolved','invalid marine hold scope/status')
            species_holds.add(hold['species_id'])
            for example in hold.get('examples',[]):
                check(sha256(example['path'])==example['sha256'],'held marine source example changed')
    check(sha256(p['road_release_path'])==plan['road_provider']['road_release_sha256'],
          'marine car network release changed')
    # The new selected-road snapshot preserves every original evidence row.
    with sqlite3.connect(f'file:{Path(roads_path).resolve()}?mode=ro',uri=True) as db:
        db.execute('ATTACH DATABASE ? AS original',[str(base_path.parent/base['provenance']['roads_file'])])
        for table in ['roads','road_endpoints']:
            lost=db.execute(f'SELECT count(*) FROM (SELECT * FROM original.{table} EXCEPT SELECT * FROM main.{table})').fetchone()[0]
            check(lost==0,'baseline selected-road evidence changed: '+table)
    policy=catalog['policy']
    check(policy=={'speed_knots':12,'boarding_minutes':60,'birding_minutes_per_exact_pin':120,
        'activity_minutes':720,'walking_kmh':2.2,'same_port_return':True,
        'weather_and_charter_availability':'assumed; historical sailings not claimed',
        'no_island_landing':True,'jurisdiction_uncertainty_hold_m':5000},'charter scenario changed')
    check(base['config']['birding_minutes']==120 and base['config']['activity_minutes']==720
          and base['config']['walking_speed_kmh']==2.2,'charter configuration differs')
    countries=data/'sources/countries-ne-5.1.2.geojson'
    check(sha256(countries)==catalog['country_coast_sha256'],'country coastline changed')
    coast_stem=Path(p['coast_stem'])
    check(sha256(Path(str(coast_stem)+'.shp'))==catalog['osm_land_sha256'],'OSM coastline changed')
    check(sha256(Path(str(coast_stem)+'.shx'))==p['coast_index_sha256'],'OSM spatial index changed')
    jurisdiction=Jurisdiction(countries)
    ports={v['id']:v for v in catalog['ports']};coasts={}
    used_ports={e['port'] for e in plan['expeditions']}
    for key in used_ports:
        port=ports[key]
        coasts[key]=Coastline(coast_stem,[port['lon'],port['lat']])
        check(sha256(port['coast_path'])==port['coast_sha256'],'cached coast changed')
        from shapely import from_wkb
        check(coasts[key].land.equals(from_wkb(Path(port['coast_path']).read_bytes())),
              'derived coast differs from original split shoreline')
    trips={v['id']:v for v in catalog['trips']}
    expected_drives={};expected_boats={};reserved=set();road_pairs=set();max_activity=0.
    for expedition in plan['expeditions']:
        a,b=expedition['begin'],expedition['end'];here=expedition['here'];pi=expedition['port_index']
        check(0<=a<b<=len(base['days']),'invalid expedition window')
        for i in range(a,b):
            check(base['days'][i]['kind']=='rest','expedition overwrites a terrestrial activity')
            check(i not in reserved,'expeditions overlap');reserved.add(i)
        port=ports[expedition['port']];coast=coasts[port['id']];loc=r['locations'][pi]
        check([loc['lon'],loc['lat']]==port['boarding_point'],'boarding coordinate differs')
        walk=plan['port_walks'][port['id']];anchor=loc['road_access']
        gap=gap_m([anchor['lon'],anchor['lat']],port['boarding_point'])
        water=open_water_meters(LineString([[anchor['lon'],anchor['lat']],port['boarding_point']]),coast.land)
        wm=2*gap if gap>r['config']['walking_threshold_m'] else 0.
        wt=wm/(2.2*1000)*60
        check(water<=100,'port access walks across open water')
        check(abs(walk['walking_minutes']-wt)<1e-7 and abs(walk['walking_meters']-wm)<1e-5,
              'port walking duration/distance differs')
        for start,x,y,count in [(a,here,pi,expedition['outbound_days']),
                                (b-expedition['inbound_days'],pi,here,expedition['inbound_days'])]:
            road=next(v for v in plan['road_evidence'] if v['from']==x and v['to']==y)
            anchors=[[r['locations'][i]['road_access']['lon'],r['locations'][i]['road_access']['lat']] for i in [x,y]]
            verify_evidence(road['proof'],anchors,road['cost']);road_pairs.add((x,y))
            from .travel import load_roads
            saved=load_roads({'locations':r['locations'],'manifest':{'synthetic':False}},roads_path)
            try:
                stored=saved[x,y]
                check(stored['miles']==road['cost']['miles'] and stored['minutes']==road['cost']['minutes']
                      and stored['endpoint_evidence']==road['proof']
                      and stored['geometry']==road['proof']['routes'][0]['geometry'],
                      'selected port road differs from frozen SQLite evidence')
            finally:saved.close()
            check(road['proof']['network_sha256']==plan['road_provider']['network_sha256'],'port road network differs')
            check(count==max(int(road['cost']['miles']>0),math.ceil(road['cost']['minutes']/r['config']['drive_minutes'])),
                  'road days shortened')
            for k in range(count):
                expected_drives[start+k]=(x,y,k,count,road)
        for t in expedition['trips']:
            check(t['id'] in trips,'selected charter is absent from frozen candidates')
            check(t==trips.get(t['id']),'selected charter differs from exact candidate')
            d=(date.fromisoformat(t['date'])-date(r['year'],1,1)).days
            check(d not in expected_boats and d not in expected_drives,'activities collide')
            check(a+expedition['outbound_days']<=d<b-expedition['inbound_days'],'boat lacks port access/return days')
            expected_boats[d]=(t,pi,wt,wm)
    seen=set();source_keys=[];boat_days=0;here=r['initial_location']
    for i,(day,old) in enumerate(zip(r['days'],base['days'])):
        check(day['date']==str(date(r['year'],1,1)+timedelta(days=i)),'date indexing changed')
        if old['kind']!='rest':check(_ordinary_day(day)==_ordinary_day(old),'terrestrial activity changed: '+day['date'])
        elif i in expected_drives:
            x,y,k,n,road=expected_drives[i]
            check(day['kind']=='road_transit' and not day['stops'] and len(day['legs'])==1,'invalid port transit day')
            leg=day['legs'][0]
            check((leg['from'],leg['to'],leg['mode'],leg['start_fraction'],leg['end_fraction'])==(x,y,'road',k/n,(k+1)/n),
                  'port drive endpoints/order changed')
            check(leg['geometry']==road['proof']['routes'][0]['geometry'],'port drive shape differs')
            check(abs(leg['minutes']-road['cost']['minutes']/n)<1e-7
                  and abs(leg['miles']-road['cost']['miles']/n)<1e-7,'port drive cost differs')
            check(day['activity_minutes']==day['travel_minutes']==leg['minutes']
                  and day['road_miles']==leg['miles'],'port drive daily totals differ')
            check(day['activity_minutes']<=r['config']['drive_minutes']+1e-7,'driving day exceeds budget')
            if k==0:check(here==x,'port expedition departs from wrong position')
            if k==n-1:here=y
        elif i in expected_boats:
            trip,pi,wt,wm=expected_boats[i];boat_days+=1
            check(here==pi,'boat starts without traveler at its port')
            check(day['kind']=='pelagic' and day['marine_trip']==trip['id'],'boat day identity differs')
            check(len(day['stops'])==len(trip['observations']) and len(day['legs'])==len(day['stops'])+1,'boat stops/legs incomplete')
            coast=coasts[trip['port']];prev=pi;sailing=0.;meters=0.
            for j,leg in enumerate(day['legs']):
                dest=(day['stops'][j]['location'] if j<len(day['stops']) else pi)
                check((leg['from'],leg['to'],leg['mode'])==(prev,dest,'boat'),'boat route not continuous/closed')
                coords=leg['geometry']['coordinates']
                for endpoint,idx in [(coords[0],prev),(coords[-1],dest)]:
                    check(endpoint==[r['locations'][idx]['lon'],r['locations'][idx]['lat']],'boat path misses exact endpoint')
                check(coast.clear(coords),'boat track crosses land or leaves audited coastline window')
                lm=length_m(coords);minutes=lm/(12*1852)*60
                check(abs(leg['minutes']-minutes)<1e-7 and abs(leg['miles']-lm/1609.344)<1e-7,'boat distance/speed differs')
                sailing+=minutes;meters+=lm;prev=dest
            for stop,obs in zip(day['stops'],trip['observations']):
                check((stop['lat'],stop['lon'])==(float(obs['latitude']),float(obs['longitude'])),'marine source pin changed')
                loc=r['locations'][stop['location']]
                check((loc['lat'],loc['lon'],loc['latitude_text'],loc['longitude_text'])==
                      (stop['lat'],stop['lon'],obs['latitude'],obs['longitude']),
                      'display/routing location differs from exact source pin')
                check(set(stop['species'])==set(obs['species'])-species_holds,'boat species differ from candidate/access holds')
                check(jurisdiction.check(stop['lat'],stop['lon'])['status']=='verified_lower48_waters','marine jurisdiction unresolved')
                check(stop['birding_minutes']==120 and stop['walking_minutes']==0,'offshore stop time changed')
                source_keys.append((day['date'],obs['latitude'],obs['longitude'],stop))
            activity=sailing+120*len(day['stops'])+60+wt
            check(day['boarding_minutes']==60,'boarding time differs from charter policy')
            check(abs(day['activity_minutes']-activity)<1e-7 and activity<=720+1e-7,'boat day exceeds/understates full activity')
            check(abs(day['travel_minutes']-sailing)<1e-7 and day['road_miles']==0
                  and abs(day['boat_miles']-meters/1609.344)<1e-7,'boat daily travel totals differ')
            check(abs(day['walking_minutes']-wt)<1e-7 and abs(day['walking_meters']-wm)<1e-6,'boat dock walk differs')
            max_activity=max(max_activity,activity)
        else:
            check(_ordinary_day(day)==_ordinary_day(old),'unplanned activity on free day')
        if old['kind']!='rest':
            if old['stops']:
                entry=old['legs'][0]['from'] if old['legs'] else old['stops'][0]['location']
                check(here==entry,'original birding no longer connects')
                here=old['stops'][-1]['location']
            elif old['legs']:
                # Original split legs occupy consecutive days. At the first
                # original activity following an expedition, verify continuity.
                if old['legs'][0].get('start_fraction',0)==0:
                    check(here==old['legs'][0]['from'],'original transit no longer connects')
                if old['legs'][-1].get('end_fraction',1)==1:here=old['legs'][-1]['to']
        fresh=[]
        for stop in day['stops']:
            new=sorted(set(stop['species'])-seen)
            check(sorted(stop['new_species'])==new,'incorrect stop credit')
            fresh.extend(stop['new_species']);seen.update(stop['species'])
        check(day['new_species']==fresh and day['cumulative_species']==len(seen),'incorrect daily accumulation')
    check(r['species_total']==len(seen),'species total mismatch')
    if errors:
        return {'valid':False,'errors':errors,'route_sha256':sha256(route_path),
            'source_audit_completed':False,
            'source_audit_skipped_reason':'Calendar, access, provenance or transport already failed; no invalid route is accepted.'}
    # Verify the selected marine rows against both the supplement and the
    # original lossless archive, including original coordinate strings.
    tax={t['source_name']:t for t in csv.DictReader((Path(p['dataset'])/'taxonomy.csv').open())}
    retrieval_path=Path(problem_path).parent/'marine-source-retrieval.json'
    retrieval=read(retrieval_path)
    check(sha256(retrieval_path)==p['marine_source_retrieval_sha256'],'marine publisher snapshot changed')
    checked=0
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
        con.execute('CREATE TABLE keys(date DATE,latitude VARCHAR,longitude VARCHAR)')
        con.executemany('INSERT INTO keys VALUES (?,?,?)',[(d,lat,lon) for d,lat,lon,_ in source_keys])
        hold_sql=(' AND r.species_id NOT IN ('+','.join(q(s) for s in species_holds)+')') if species_holds else ''
        con.execute(f'''CREATE TABLE selected_marine AS SELECT r.* FROM read_parquet({q(data/'offshore.parquet')}) r
            JOIN keys k ON r.date=k.date AND r.decimallatitude=k.latitude AND r.decimallongitude=k.longitude
            WHERE r.marine_disposition='policy_eligible_pending_marine_access' {hold_sql}''')
        archive=Path(parent['inputs']['eod_archive']['path'])
        scan,_=archive_scan(archive,True)
        fields=','.join('r.'+k for k in SCHEMA)
        source_fields=','.join(SCHEMA)
        con.execute(f'''CREATE TABLE original AS SELECT {fields} FROM {scan} r JOIN
            (SELECT DISTINCT representative_gbifid FROM selected_marine) k USING(representative_gbifid)
            WHERE r.year={int(r['year'])}''')
        difference=con.execute(f'''SELECT count(*) FROM ((SELECT {source_fields} FROM selected_marine EXCEPT ALL SELECT * FROM original)
            UNION ALL (SELECT * FROM original EXCEPT ALL SELECT {source_fields} FROM selected_marine))''').fetchone()[0]
        check(difference==0,'marine supplement differs from original source records')
        cols=[v[0] for v in con.execute('DESCRIBE selected_marine').fetchall()]
        rows=[dict(zip(cols,v)) for v in con.execute('''SELECT * FROM selected_marine
            ORDER BY date,decimallatitude,decimallongitude,species_id,representative_gbifid''').fetchall()]
        for d,lat,lon,stop in source_keys:
            evidence={}
            for row in rows:
                if str(row['date'])!=d or row['decimallatitude']!=lat or row['decimallongitude']!=lon:continue
                key=row['representative_gbifid'];record=Path(problem_path).parent/'marine-verbatim'/(key+'.json')
                item=retrieval['records'].get(key,{})
                check(item.get('retrieved') and sha256(record)==item.get('sha256'),'marine publisher record missing/changed')
                status=publisher_check(row,read(record),tax[row['v_scientificname']])
                check(status=='supported','marine publisher hold: '+key+' '+status)
                evidence.setdefault(row['species_id'],[]).append({'checklist':'','event':'gbif-group:'+key,
                    'record':'GBIF:'+key,'taxon':row['v_scientificname']});checked+=1
            check(set(evidence)==set(stop['species']),'marine species lack complete source support')
            check(stop['evidence']==evidence,'marine evidence differs from source rows')
    return {'valid':not errors,'errors':errors,'validation_kind':'locked-terrestrial-plus-modeled-charter-v1',
        'baseline_revalidated':base_validation['valid'],'boat_days_checked':boat_days,
        'marine_source_records_checked':checked,'new_directed_port_roads_checked':len(road_pairs),
        'marine_access_species_held':sorted(species_holds),
        'max_boat_day_activity_minutes':max_activity,'boat_tracks_clear_land':not errors,
        'route_sha256':sha256(route_path),'historical_charter_operation_certified':False,
        'bathymetry_weather_and_navigation_certified':False}
