"""Independent audit of the joint catalog, physical charters and source records.

No search evaluator, model builder or calendar expander is called here.
"""
from collections import Counter
import csv
from datetime import date
from pathlib import Path

import numpy as np
from shapely import from_wkb
from shapely.geometry import LineString

from .compact_model import load
from .config import Config
from .eod import STATE_NAMES
from .eod_archive import SCHEMA,archive_scan
from .marine import Coastline,Jurisdiction,length_m,observer_coverage
from .marine_validate import publisher_check
from .road_matching import gap_m,verify_evidence
from .surface_access import open_water_meters,audit_walk
from .util import ROOT,ManagedDuckDB,q,read,sha256


def context(problem,route,folder,check):
    joint=problem['joint_marine'];data=Path(joint['marine_data'])
    if joint.get('study'):
        study=joint['study'];check(sha256(study['path'])==study['sha256'],'calendar study search binding changed')
        search=read(study['path'])
        check(route['search'].get('study')==study and search['ids']==route['search']['selected_model_opportunities'],
              'calendar study result differs from materialized seed')
        check(set(study['required_final_visits'])<=set(search['ids']),'final calendar lost required tentpole')
    if joint.get('access_resolution'):
        binding=joint['access_resolution'];check(sha256(binding['migration_path'])==binding['migration_sha256'],'access migration changed')
        migration=read(binding['migration_path'])
        check(not migration['old_endpoint_proofs_relabelled'],'old access proofs must not be relabelled')
        check(migration['new_network_sha256']==route['provenance']['road_network_sha256'],'access migration network differs')
        check(problem['access_policy']['extension']==binding['policy'],'access resolution policy differs')
        check(migration['old_provider']['road_release_sha256']==sha256(ROOT/'data/lower48-eod-2024/roads/release.json'),
              'access migration changed the physical car release')
    if joint.get('source_visit_holds'):
        binding=joint['source_visit_holds'];check(sha256(binding['path'])==binding['sha256'],'source visit holds changed')
        holds=read(binding['path']);blocked={tuple(v) for v in holds['exact_coordinate_holds']}
        for source in holds.get('evidence',[]):
            check(sha256(source['path'])==source['sha256'],'source access hold evidence changed')
        for o in problem['opportunities']:
            loc=problem['locations'][o['location']]
            check((loc['lat'],loc['lon']) not in blocked,'held exact source pin selected')
    for name,digest in joint.get('code_snapshot',{}).items():
        check(sha256(Path(folder)/'code-snapshot'/name)==digest,'joint code snapshot changed: '+name)
    check(sha256(joint['parent_path'])==joint['parent_sha256'],'joint terrestrial parent changed')
    check(sha256(joint['catalog_path'])==joint['catalog_sha256'],'joint charter catalog changed')
    check(sha256(joint['holds_path'])==joint['holds_sha256'],'joint boat access policy changed')
    catalog=read(joint['catalog_path']);held={v['species_id'] for v in read(joint['holds_path'])['species']}
    check(joint['trip_ids']==[t['id'] for t in catalog['trips']],'joint variant identity order changed')
    for v in read(joint['holds_path'])['species']:
        check(v['status']=='unresolved' and v['scope']=='boat-only','invalid boat hold status/scope')
        for example in v.get('examples',[]):check(sha256(example['path'])==example['sha256'],'held source example changed')
    for name,key in [('manifest.json','marine_manifest_sha256'),('offshore.parquet','offshore_sha256'),('ports.json','ports_sha256')]:
        check(sha256(data/name)==catalog[key],'marine input changed: '+name)
    check(read(data/'manifest.json')['parent_manifest_sha256']==problem['manifest_sha256'],'marine source parent differs')
    check(catalog['policy']=={'speed_knots':12,'boarding_minutes':60,'birding_minutes_per_exact_pin':120,
        'activity_minutes':720,'walking_kmh':2.2,'same_port_return':True,
        'weather_and_charter_availability':'assumed; historical sailings not claimed',
        'no_island_landing':True,'jurisdiction_uncertainty_hold_m':5000},'joint charter policy differs')
    check(problem['config']['birding_minutes']==120 and problem['config']['activity_minutes']==720
          and problem['config']['walking_speed_kmh']==2.2,'joint charter budgets differ')
    parent=load(joint['parent_path']);model=load(problem['compact_source']['path'])
    try:
        check(model.meta['joint_marine']=={k:v for k,v in joint.items() if k in model.meta['joint_marine']},'joint model binding differs')
        n=parent.meta['locations'];check(n==joint['land_locations'],'joint original location count differs')
        expected_config={**parent.meta['config'],'flights':True}
        if model.meta.get('calendar_source'):
            binding=model.meta['calendar_source']
            check(sha256(binding['path'])==binding['sha256'],'joint calendar source changed')
            original=read(binding['path'])
            before=Config(**original['config']).to_dict()
            after=Config(**model.meta['config']).to_dict()
            changed={k for k in before if before[k]!=after[k]}
            check(bool(changed) and changed<={'local_miles','mixed_road_birding','drive_minutes'},
                  'joint calendar control changed unsupported rules')
            if 'drive_minutes' in changed:
                check(after['mixed_road_birding'] and after['drive_minutes']==after['activity_minutes']==720,
                      'joint driving relaxation must share the 12-hour activity budget')
            check({**original['config'],'flights':True}==expected_config,
                  'joint calendar control parent rules differ')
            check(original['components']==model.meta['components'] and all(
                original[k]==model.meta[k] for k in ['locations','opportunities','species','species_entries']),
                'joint calendar control changed observations or exact coordinates')
            expected_config.update({k:after[k] for k in changed})
        check(problem['config']==expected_config,'joint parent calendar/config changed')
        check(model.meta['species'][:len(parent.meta['species'])]==parent.meta['species'],'joint parent species changed')
        check(np.array_equal(model.coords[:n],parent.coords),'terrestrial coordinates changed')
        a=parent.locations.db.execute('SELECT idx,metadata FROM locations ORDER BY idx')
        b=model.locations.db.execute('SELECT idx,metadata FROM locations WHERE idx<? ORDER BY idx',[n])
        check(all(x==y for x,y in zip(a,b)),'terrestrial location identities changed')
        check(model.locations.db.execute('SELECT count(*) FROM locations WHERE idx<?',[n]).fetchone()[0]==n,
              'terrestrial location catalog incomplete')
        for d in range(Config(**parent.meta['config']).days):
            a,b=np.searchsorted(parent.records['day'],[d,d+1]);x,y=np.searchsorted(model.records['day'],[d,d+1])
            land=model.records[x:y][model.records['location'][x:y]<n]
            old=parent.records[a:b]
            okay=len(old)==len(land) and all(np.array_equal(old[f],land[f]) for f in ['day','location','count','reserved'])
            if len(old) and len(land):
                ao=int(old[0]['offset']);bo=int(land[0]['offset']);count=int(old[-1]['offset'])+int(old[-1]['count'])-ao
                okay=okay and np.array_equal(parent.species[ao:ao+count],model.species[bo:bo+count])
            check(okay,'terrestrial dated observations changed: '+str(d))
        ports={p['id']:p for p in catalog['ports']}
        for key,i in joint['port_indices'].items():
            loc=model.locations[i];p=ports[key]
            check(i>=n and loc['kind']=='marine_port' and loc['id']=='marine-port:'+key and
                  [loc['lon'],loc['lat']]==p['boarding_point'],'joint port differs from frozen boarding point')
        spidx={v['id']:i for i,v in enumerate(model.meta['species'])}
        native=model.records[model.records['location']>=n]
        check(len(native)==len(catalog['trips']),'joint marine catalog lost alternatives')
        for variant,t in enumerate(catalog['trips'],1):
            i=joint['trip_opportunities'][t['id']];row=model.records[i]
            spp=sorted({spidx[s] for o in t['observations'] for s in o['species']
                        if t.get('kind')=='island_landing' or s not in held})
            off=int(row['offset']);day=(date.fromisoformat(t['date'])-date(problem['config']['year'],1,1)).days
            check(int(row['reserved'])==variant and int(row['day'])==day and int(row['location'])==joint['port_indices'][t['port']]
                  and list(model.species[off:off+int(row['count'])])==spp,'joint native charter differs from source option')
        lines=(model.root/'marine-visits.txt').read_text().splitlines()
        check(int(lines[0])==len(catalog['trips']) and len(lines)==len(catalog['trips'])+1,'joint visit durations incomplete')
        for variant,(t,line) in enumerate(zip(catalog['trips'],lines[1:]),1):
            values=line.split();check(int(values[0])==variant and float(values[1])==t['activity_minutes']-120
                and float(values[2])==t['sailing_minutes'],'native boat activity/time differs')
        check(route['marine_observer_coverage']==observer_coverage(parent.meta,catalog),'joint observer curve changed')
    finally:parent.close();model.close()
    selected={o['marine_trip'] for o in problem['opportunities'] if o.get('marine_trip')}
    trips={t['id']:t for t in catalog['trips']}
    check(joint['selected_trips']=={i:trips[i] for i in selected},'selected joint charter altered')
    check(route['marine_extension']['native_joint_search'] is True,'joint search scope mislabeled')
    countries=data/'sources/countries-ne-5.1.2.geojson'
    check(sha256(countries)==catalog['country_coast_sha256'],'country coast source changed')
    stem=ROOT/'build/land-mask/land-polygons-split-4326/land_polygons'
    check(sha256(Path(str(stem)+'.shp'))==catalog['osm_land_sha256'],'original OSM coast changed')
    coasts={}
    ports={p['id']:p for p in catalog['ports']}
    for key in {trips[i]['port'] for i in selected}:
        port=ports[key];coast=Coastline(stem,[port['lon'],port['lat']]);coasts[key]=coast
        check(sha256(port['coast_path'])==port['coast_sha256'] and
              coast.land.equals(from_wkb(Path(port['coast_path']).read_bytes())),'cached marine coast differs from original')
    walks=[]
    selected_land={s['location'] for d in route['days'] if d['kind']=='birding' for s in d['stops']}
    for idx in sorted(selected_land):
        loc=problem['locations'][idx];anchor=loc['road_access']
        gap=gap_m([loc['lon'],loc['lat']],[anchor['lon'],anchor['lat']])
        if gap>problem['config']['walking_threshold_m']:
            coast=Coastline(stem,[(loc['lon']+anchor['lon'])/2,(loc['lat']+anchor['lat'])/2],radius_deg=.3)
            result=audit_walk(loc,problem['config'],coast.land);walks.append({'location_id':loc['id'],**result})
            check(result['status']=='no_large_ocean_crossing_detected','terrestrial access crosses open sea: '+loc['id'])
    for key,idx in joint['pin_indices'].items():
        loc=problem['locations'][idx]
        if loc.get('kind')=='island_observation':
            check(any(t.get('kind')=='island_landing' and any(
                o['source_location']['id']==loc['id'] and (o['lat'],o['lon'])==(loc['lat'],loc['lon'])
                for o in t['observations']) for t in joint['selected_trips'].values()),'unbound island source pin')
            continue
        check(loc['id']=='marine:'+key and loc['kind']=='pelagic_observation' and
              key==loc['latitude_text']+','+loc['longitude_text'] and
              (loc['lat'],loc['lon'])==(float(loc['latitude_text']),float(loc['longitude_text'])),
              'joint exact observation pin changed')
    if catalog.get('island_access'):
        from .island_validate import audit_catalog
        audit_catalog(catalog,check)
    return {'joint':joint,'catalog':catalog,'trips':trips,'ports':ports,'coasts':coasts,'held':held,
            'jurisdiction':Jurisdiction(countries),'folder':Path(folder),'boat_days':0,'max_activity':0.,
            'terrestrial_access_walks':walks}


def audit_day(ctx,day,here,problem,route,roads,seen,check,endpoint_pairs,endpoint_gaps):
    locs=problem['locations'];prefix=day['date'];joint=ctx['joint']
    check(len(day['stops'])==1,'joint charter requires one exact observation pin')
    stop=day['stops'][0];oi=stop['opportunity'];o=problem['opportunities'][oi]
    check(o.get('marine_trip')==day['marine_trip'] and o['date']==prefix,'joint charter opportunity/date differs')
    t=ctx['trips'][day['marine_trip']];pi=o['location'];port=ctx['ports'][t['port']]
    check(t==joint['selected_trips'].get(t['id']) and prefix==t['date'],'joint charter differs from selected source option')
    check(locs[pi]['port']==t['port'] and [locs[pi]['lon'],locs[pi]['lat']]==port['boarding_point'],
          'joint departure/return port changed')
    roadlegs=[v for v in day['legs'] if v['mode']=='road'];boatlegs=[v for v in day['legs'] if v['mode']=='boat']
    check(day['legs']==roadlegs+boatlegs and len(roadlegs)<=1,'charter local driving must precede departure')
    miles=minutes=0.
    for leg in roadlegs:
        a,b=leg['from'],leg['to'];road=roads.get((a,b))
        check(here==a and b==pi and leg['start_fraction']==0 and leg['end_fraction']==1,
              'charter incoming road discontinuity')
        check(road is not None,'charter incoming road lacks evidence')
        if road:
            anchors=[[locs[i]['road_access']['lon'],locs[i]['road_access']['lat']] for i in [a,b]]
            endpoint_gaps.extend(verify_evidence(road['endpoint_evidence'],anchors,road));endpoint_pairs.add((a,b))
            check(road['endpoint_evidence']['network_sha256']==route['provenance']['road_network_sha256'] and
                  road['endpoint_evidence']['policy']==route['provenance']['road_matching_policy'],
                  'charter incoming road identity differs')
            check(abs(leg['miles']-road['miles'])<1e-5 and abs(leg['minutes']-road['minutes'])<1e-5
                  and leg['geometry']==road['geometry'] and leg['schematic']==(not bool(road['geometry'])),
                  'charter incoming road differs from measured cost/path')
        miles+=leg['miles'];minutes+=leg['minutes'];here=b
    check(here==pi,'charter starts without traveler at its port')
    coast=ctx['coasts'][t['port']];anchor=locs[pi]['road_access']
    gap=gap_m([anchor['lon'],anchor['lat']],port['boarding_point'])
    water=open_water_meters(LineString([[anchor['lon'],anchor['lat']],port['boarding_point']]),coast.land)
    wm=2*gap if gap>problem['config']['walking_threshold_m'] else 0.;wt=wm/2200*60
    check(water<=100,'joint port walk crosses open water')
    check(len(boatlegs)==len(t['legs']) and len(day['stops'])==len(t['observations']),'charter exact track incomplete')
    previous=pi;sailing=meters=0.
    for j,(leg,source) in enumerate(zip(boatlegs,t['legs'])):
        dest=day['stops'][j]['location'] if j<len(day['stops']) else pi
        check((leg['from'],leg['to'],leg['mode'],leg['start_fraction'],leg['end_fraction'])==
              (previous,dest,'boat',0,1),'joint boat track discontinuity')
        coords=leg['geometry']['coordinates']
        check(leg['geometry']==source['geometry'] and not leg['schematic'] and leg['modeled_charter'] is True,
              'joint boat geometry/labels differ')
        check(coords[0]==[locs[previous]['lon'],locs[previous]['lat']] and
              coords[-1]==[locs[dest]['lon'],locs[dest]['lat']],'joint boat misses exact endpoint')
        check(coast.clear(coords),'joint boat crosses land or leaves verified coast window')
        length=length_m(coords);duration=length/(12*1852)*60
        check(abs(leg['minutes']-duration)<1e-7 and abs(leg['miles']-length/1609.344)<1e-7,
              'joint boat sailing speed/distance differs')
        meters+=length;sailing+=duration;previous=dest
    added=[]
    for stop,obs in zip(day['stops'],t['observations']):
        loc=locs[stop['location']];key=obs['latitude']+','+obs['longitude']
        check(stop['location']==joint['pin_indices'][key] and stop['opportunity']==oi and
              stop['marine_trip']==t['id'],'joint stop opportunity/pin binding differs')
        check((stop['lat'],stop['lon'],loc['latitude_text'],loc['longitude_text'])==
              (float(obs['latitude']),float(obs['longitude']),obs['latitude'],obs['longitude']) and
              (loc['lat'],loc['lon'],loc['name'])==(stop['lat'],stop['lon'],stop['name']),
              'joint source coordinates/metadata changed')
        check(ctx['jurisdiction'].check(stop['lat'],stop['lon'])['status']=='verified_lower48_waters',
              'joint marine jurisdiction unresolved')
        check(stop['species']==sorted(set(obs['species'])-ctx['held']) and
              set(stop['species'])=={problem['species'][s]['id'] for s in o['species']},'joint boat species/holds differ')
        check(stop['birding_minutes']==120 and stop['walking_minutes']==stop['walking_meters']==0,
              'joint offshore birding allowance changed')
        fresh=[s for s in stop['species'] if s not in seen];check(stop['new_species']==fresh,'joint stop credit differs')
        added.extend(fresh);seen.update(stop['species'])
    activity=minutes+sailing+120*len(day['stops'])+60+wt
    check(activity<=720+1e-5 and miles<=problem['config']['local_miles']+1e-5,'joint charter activity/local road limit exceeded')
    check(abs(day['activity_minutes']-activity)<1e-5 and day['boarding_minutes']==60,'joint charter activity/boarding total differs')
    check(abs(day['road_miles']-miles)<1e-5 and abs(day['travel_minutes']-minutes-sailing)<1e-5
          and abs(day['boat_miles']-meters/1609.344)<1e-5 and
          abs(day['boat_nautical_miles']-meters/1852)<1e-5,'joint charter travel total differs')
    check(abs(day['walking_minutes']-wt)<1e-5 and abs(day['walking_meters']-wm)<1e-5,'joint full dock walk differs')
    check(day['new_species']==added and day['cumulative_species']==len(seen),'joint charter cumulative total differs')
    ctx['boat_days']+=1;ctx['max_activity']=max(ctx['max_activity'],activity)
    return pi,miles,minutes+sailing,wt,wm


def audit_sources(ctx,problem,route,check):
    folder=ctx['folder'];joint=ctx['joint'];data=Path(joint['marine_data'])
    check(sha256(folder/'publisher-retrieval.json')==joint['publisher_retrieval_sha256'],'joint publisher snapshot changed')
    check(sha256(folder/'source-decisions.csv')==joint['publisher_decisions_sha256'],'joint publisher decisions changed')
    retrieval=read(folder/'publisher-retrieval.json');tax={v['source_name']:v for v in csv.DictReader((Path(problem['dataset'])/'taxonomy.csv').open())}
    keys=[];wanted=set();stops=[]
    for day in route['days']:
        if day['kind']=='pelagic':
            for stop in day['stops']:
                loc=route['locations'][stop['location']];key=(day['date'],loc['latitude_text'],loc['longitude_text'])
                keys.append(key);wanted.update((*key,s) for s in stop['species']);stops.append((key,stop,loc))
    rows=[]
    if keys:
        with ManagedDuckDB() as con:
            con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
            con.execute('CREATE TABLE keys(date DATE,latitude VARCHAR,longitude VARCHAR)')
            con.executemany('INSERT INTO keys VALUES (?,?,?)',sorted(set(keys)))
            hold=' AND species_id NOT IN ('+','.join(q(s) for s in ctx['held'])+')' if ctx['held'] else ''
            con.execute(f'''CREATE TABLE selected_marine AS SELECT r.* FROM read_parquet({q(data/'offshore.parquet')}) r
                JOIN keys k ON r.date=k.date AND r.decimallatitude=k.latitude AND r.decimallongitude=k.longitude
                WHERE marine_disposition='policy_eligible_pending_marine_access' {hold}''')
            parent=read(Path(problem['dataset'])/'manifest.json');scan,_=archive_scan(Path(parent['inputs']['eod_archive']['path']),True)
            fields=','.join('r.'+k for k in SCHEMA);names=','.join(SCHEMA)
            con.execute(f'''CREATE TABLE original AS SELECT {fields} FROM {scan} r JOIN
                (SELECT DISTINCT representative_gbifid FROM selected_marine) k USING(representative_gbifid) WHERE r.year={int(route['year'])}''')
            difference=con.execute(f'''SELECT count(*) FROM ((SELECT {names} FROM selected_marine EXCEPT ALL SELECT * FROM original)
                UNION ALL (SELECT * FROM original EXCEPT ALL SELECT {names} FROM selected_marine))''').fetchone()[0]
            check(difference==0,'joint marine supplement differs from original lossless archive')
            cols=[v[0] for v in con.execute('DESCRIBE selected_marine').fetchall()]
            rows=[dict(zip(cols,v)) for v in con.execute('SELECT * FROM selected_marine ORDER BY date,decimallatitude,decimallongitude,species_id,representative_gbifid').fetchall()]
        for key,stop,loc in stops:
            evidence={};states=set()
            for row in rows:
                if (str(row['date']),row['decimallatitude'],row['decimallongitude'])!=key:continue
                record=row['representative_gbifid'];states.add(row['state'])
                evidence.setdefault(row['species_id'],[]).append({'checklist':'','event':'gbif-group:'+record,
                    'record':'GBIF:'+record,'taxon':row['v_scientificname']})
            check(set(evidence)==set(stop['species']) and stop['evidence']==evidence,'joint marine evidence incomplete/fabricated')
            check(loc['source_states']==sorted(states) and loc['state']==(next(iter(states)) if len(states)==1 else None),
                  'joint marine publisher state labels differ')
    marine_count=len(rows)
    for day in route['days']:
        if day['kind']=='pelagic':continue
        for stop in day['stops']:
            loc=route['locations'][stop['location']]
            for species in stop['species']:
                for evidence in stop['evidence'][species]:
                    t=tax[evidence['taxon']]
                    rows.append({'date':day['date'],'lat':stop['lat'],'lon':stop['lon'],
                        'stateprovince':STATE_NAMES[loc['state'].removeprefix('US-')],
                        'representative_gbifid':evidence['record'].removeprefix('GBIF:'),
                        'v_scientificname':evidence['taxon'],'v_taxonconceptid':t['source_taxon_id'],'species_id':species})
    statuses=Counter()
    for row in rows:
        key=row['representative_gbifid'];item=retrieval['records'].get(key,{});path=folder/'publisher-verbatim'/(key+'.json')
        okay=item.get('retrieved') and path.exists() and sha256(path)==item.get('sha256')
        check(okay,'joint publisher source missing/changed: '+key)
        if okay:
            status=publisher_check(row,read(path),tax[row['v_scientificname']]);statuses[status]+=1
            check(status=='supported','joint publisher countability hold: '+key+' '+status)
    return {'records_checked':len(rows),'marine_records_checked':marine_count,'decisions':dict(statuses),
            'individual_wild_status_certified':False}
