"""Controlled national 2024 marine extension with explicit charter assumptions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from shapely import from_wkb

from bigyear_planner.marine_search import road_model,choose_expeditions,extend_calendar
from bigyear_planner.marine_validate import publisher_check,validate_marine
from bigyear_planner.marine import observer_coverage
from bigyear_planner.marine_roads import MarineRoads
from bigyear_planner.travel import cache
from bigyear_planner.util import ROOT,ManagedDuckDB,dump,q,read,sha256,write_csv
from evaluate_lower48 import OUT,DATA as NATIONAL,guarded,router
from build_marine_candidates import BASE,COAST
from prepare_marine_charter import DATA,PARENT

TAG='marine-charter-2024-v2'


def source_rows(route):
    keys=[];wanted=set()
    for day in route['days']:
        if day['kind']=='pelagic':
            for stop in day['stops']:
                loc=route['locations'][stop['location']]
                keys.append((day['date'],loc['latitude_text'],loc['longitude_text']))
                wanted.update((day['date'],loc['latitude_text'],loc['longitude_text'],s) for s in stop['species'])
    if not keys:return []
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
        con.execute('CREATE TABLE keys(date DATE,latitude VARCHAR,longitude VARCHAR)')
        con.executemany('INSERT INTO keys VALUES (?,?,?)',keys)
        cur=con.execute(f'''SELECT r.* FROM read_parquet({q(DATA/'offshore.parquet')}) r JOIN keys k
          ON r.date=k.date AND r.decimallatitude=k.latitude AND r.decimallongitude=k.longitude
          WHERE r.marine_disposition='policy_eligible_pending_marine_access'
          ORDER BY r.date,r.decimallatitude,r.decimallongitude,r.species_id,r.representative_gbifid''')
        columns=[v[0] for v in cur.description]
        rows=[dict(zip(columns,row)) for row in cur.fetchall()]
        return [r for r in rows if (str(r['date']),r['decimallatitude'],r['decimallongitude'],r['species_id']) in wanted]


def fetch_sources(rows,out):
    target=out/'marine-verbatim';target.mkdir(exist_ok=True)
    def get(key):
        path=target/(key+'.json')
        if not path.exists():
            previous=next((PARENT.parent/'sources').glob('*-verbatim/'+key+'.json'),None)
            if previous is not None:os.link(previous,path)
        if not path.exists():
            temporary=path.with_suffix('.part')
            cmd=['/usr/bin/curl','-fLsS','--retry','2','--max-time','35',
                'https://api.gbif.org/v1/occurrence/'+key+'/verbatim','-o',str(temporary)]
            result=subprocess.run(cmd,capture_output=True,text=True)
            if result.returncode:
                temporary.unlink(missing_ok=True)
                return key,{'retrieved':False,'reason':result.stderr[-400:]}
            if str(read(temporary).get('key'))!=key:raise ValueError('publisher record ID differs')
            temporary.replace(path)
        return key,{'retrieved':True,'sha256':sha256(path)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        records=dict(pool.map(get,sorted({r['representative_gbifid'] for r in rows})))
    result={'retrieved_at':datetime.now(timezone.utc).isoformat(),'records':records}
    taxonomy={v['source_name']:v for v in csv.DictReader((PARENT/'taxonomy.csv').open())}
    failures=[];decisions=[]
    for row in rows:
        key=row['representative_gbifid'];item=records[key]
        status=(publisher_check(row,read(target/(key+'.json')),taxonomy[row['v_scientificname']])
                if item['retrieved'] else 'source_unavailable')
        record={'date':str(row['date']),'latitude':row['decimallatitude'],'longitude':row['decimallongitude'],
                'record':key,'species_id':row['species_id'],'source_name':row['v_scientificname'],
                'decision':status}
        decisions.append(record)
        if status!='supported':failures.append(record)
    return result,decisions,failures


def add_evidence(route,rows):
    for day in route['days']:
        if day['kind']!='pelagic':continue
        for stop in day['stops']:
            loc=route['locations'][stop['location']];evidence={}
            states=set(loc.get('source_states',[]))
            for row in rows:
                if (str(row['date']),row['decimallatitude'],row['decimallongitude'])!=(day['date'],loc['latitude_text'],loc['longitude_text']):continue
                key=row['representative_gbifid']
                states.add(row['state'])
                evidence.setdefault(row['species_id'],[]).append({'checklist':'','event':'gbif-group:'+key,
                    'record':'GBIF:'+key,'taxon':row['v_scientificname']})
            stop['evidence']=evidence
            loc['source_states']=sorted(states)
            loc['state']=next(iter(states)) if len(states)==1 else None
            loc['state_attribution']='publisher state labels; geographic eligibility independently uses nearest-country land'


def main():
    started=time.monotonic();out=OUT/TAG;out.mkdir(exist_ok=True)
    if (out/'route.json').exists():raise ValueError('finished run exists; use a new run version')
    base=read(BASE/'route.json');catalog_path=DATA/'charter-candidates.json';catalog=read(catalog_path)
    if catalog['baseline_route_sha256']!=sha256(BASE/'route.json'):raise ValueError('baseline differs')
    model=road_model(base,catalog['ports'],catalog_path)
    hold_path=out/'marine-access-score-holds.json'
    if not hold_path.exists() and (DATA/'boat-access-score-holds.json').exists():
        shutil.copyfile(DATA/'boat-access-score-holds.json',hold_path)
    holds=read(hold_path) if hold_path.exists() else {'species':[]}
    held_species={v['species_id'] for v in holds['species']}
    port_land={p['id']:from_wkb(Path(p['coast_path']).read_bytes()) for p in catalog['ports']}
    awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())])
    try:
        with guarded(TAG),router(TAG,45):
            roads=MarineRoads(model,NATIONAL/'roads/release.json',DATA/'road-cache-v2',query_timeout_seconds=55)
            try:
                for i,loc in enumerate(base['locations']):
                    a=loc['road_access'];gap=a['offset_meters']
                    walk=2*gap/(2.2*1000)*60 if gap>100 else 0.
                    roads.anchors[i]=[a['lat'],a['lon'],base['config']['birding_minutes']+walk]
                if not (out/'port-cache-invalidation.json').exists():
                    migration=(roads.import_unchanged_costs(DATA/'road-cache')
                        if (DATA/'road-cache/costs.bin').exists() else
                        {'previous_staging_cache_retired':True,'current_bounded_cache_reused':True})
                    dump(out/'port-cache-invalidation.json',migration)
                evidence_seed={};deferred_seed=[]
                if (out/'marine-plan.json').exists():
                    old_problem=read(out/'problem.json');old_plan=read(out/'marine-plan.json')
                    if (sha256(out/'marine-plan.json')!=old_problem['marine_plan_sha256']
                        or old_problem['base_route_sha256']!=sha256(BASE/'route.json')
                        or old_problem['marine_catalog_sha256']!=sha256(catalog_path)
                        or old_plan['road_provider']!=roads.manifest):
                        raise ValueError('road proof warm start belongs to changed physical inputs')
                    evidence_seed={(v['from'],v['to']):v for v in old_plan['road_evidence']}
                    deferred_seed=old_plan['deferred']
                held=[]
                for attempt in range(4):
                    selected=choose_expeditions(base,catalog,roads,port_land,
                        progress=lambda value:print(json.dumps(value),flush=True),
                        species_holds=held_species,evidence_seed=evidence_seed,deferred_seed=deferred_seed)
                    evidence_seed.update(selected['road_evidence']);deferred_seed=selected['deferred']
                    locations=deepcopy(base['locations'])
                    used_ports={e['port'] for e in selected['expeditions']}
                    for i,p in enumerate(catalog['ports']):
                        if p['id'] in used_ports:locations.append(roads.location(len(base['locations'])+i))
                        else:locations.append(model.locations[len(base['locations'])+i])
                    route,trips=extend_calendar(base,selected,locations,catalog,species_holds=held_species)
                    print(json.dumps({'stage':'portfolio','attempt':attempt,'species':route['species_total'],
                        'new':len(selected['new_species']),'boat_days':len(trips)}),flush=True)
                    rows=source_rows(route);retrieval,decisions,failures=fetch_sources(rows,out)
                    if not failures:break
                    bad={(r['date'],r['latitude'],r['longitude']) for r in failures};held.extend(failures)
                    catalog={**catalog,'trips':[t for t in catalog['trips'] if not any(
                        (o['date'],o['latitude'],o['longitude']) in bad for o in t['observations'])]}
                else:raise ValueError('marine publisher postcheck remains held after four repairs')
                if not trips:raise ValueError('no validated marine extension found')
                add_evidence(route,rows)
                if held_species:route['assumptions'].append('Boat-only access holds: '+', '.join(sorted(held_species))+'. These taxa remain eligible on land; mixed beach/anchorage pins do not establish observation from a vessel.')
                dump(out/'marine-source-retrieval.json',retrieval)
                write_csv(out/'marine-source-decisions.csv',decisions,list(decisions[0]))
                if held:write_csv(out/'marine-source-holds.csv',held,list(held[0]))
                expedition_rows=[]
                for e in selected['expeditions']:
                    expedition_rows.append({k:([ {a:b for a,b in t.items() if a not in ['day','species_set']} for t in v]
                        if k=='trips' else sorted(v) if isinstance(v,set) else v)
                        for k,v in e.items()})
                plan={'schema':1,'config':base['config'],'expeditions':expedition_rows,
                    'road_evidence':[{'from':a,'to':b,**v} for (a,b),v in selected['road_evidence'].items()],
                    'road_provider':roads.manifest,'port_walks':selected['port_walks'],
                    'proposal_rule':selected['proposal_rule'],'deferred':selected['deferred']}
                dump(out/'marine-plan.json',plan)
                shutil.copyfile(BASE/'roads.sqlite',out/'roads.sqlite')
                db=cache(out/'roads.sqlite')
                for (a,b),v in selected['road_evidence'].items():
                    x,y=[route['locations'][i] for i in (a,b)];cost=v['cost'];proof=v['proof']
                    db.execute('INSERT OR REPLACE INTO roads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (x['id'],y['id'],cost['miles'],cost['minutes'],cost['source'],cost['network_release'],
                         json.dumps(proof['routes'][0]['geometry']),x['lat'],x['lon'],y['lat'],y['lon'],
                         x['road_access']['lat'],x['road_access']['lon'],y['road_access']['lat'],y['road_access']['lon']))
                    db.execute('INSERT OR REPLACE INTO road_endpoints VALUES (?,?,?)',(x['id'],y['id'],json.dumps(proof)))
                db.commit();db.close()
                taxonomy=list(csv.DictReader((PARENT/'taxonomy.csv').open()))
                all_species={v['id'] for v in route['species']}
                marine_species={s for t in read(catalog_path)['trips'] for o in t['observations'] for s in o['species']}
                additions=marine_species-all_species
                route['species'].extend({'id':s,'name':next(v['common_name'] for v in taxonomy if v['species_id']==s)} for s in sorted(additions))
                # The full geographic marine pool remains unassessed. This is
                # the union with constructed, checked charter opportunities.
                route['annual_recorded_species_ceiling']=len(route['species'])
                route['candidate_species_ceiling']=len(route['species'])
                route['marine_extension']['annual_pool_scope']='terrestrial cohort plus constructed charter-accessible exact-pin opportunities; not complete ocean coverage'
                base_problem=read(BASE/'problem.json')
                route['marine_observer_coverage']=observer_coverage(read(base_problem['compact_source']['path']),read(catalog_path))
                route['assumptions']=[s for s in route['assumptions'] if 'offshore and unclassified land locations excluded' not in s
                    and 'Marine observations and unsupported island transport remain unscored' not in s
                    and 'current search retains the terrestrial' not in s
                    and 'Passenger boats require dated port-to-port service' not in s]
                route['search']={'species':route['species_total'],'elapsed_seconds':time.monotonic()-started,
                    'candidate_evaluations':selected['scored_portfolios'],
                    'charter_options_scored':selected['scored_charters'],'optimal':False,
                    'model':'locked terrestrial calendar plus exact-pin marine charter portfolios',
                    'terrestrial_search':base['search']}
                problem={'schema':'marine-extension-v1','dataset':str(PARENT),'config':base['config'],
                    'base_route':str(BASE/'route.json'),'base_route_sha256':sha256(BASE/'route.json'),
                    'base_validation_sha256':sha256(BASE/'validation.json'),
                    'base_publisher_validation_sha256':sha256(BASE/'countability-source-validation.json'),
                    'marine_data':str(DATA),'marine_catalog_sha256':sha256(catalog_path),
                    'marine_plan_sha256':sha256(out/'marine-plan.json'),
                    'marine_source_retrieval_sha256':sha256(out/'marine-source-retrieval.json'),
                    'coast_stem':str(COAST),'coast_index_sha256':sha256(Path(str(COAST)+'.shx')),
                    'road_release_path':str(NATIONAL/'roads/release.json')}
                if hold_path.exists():problem['marine_access_score_holds']={'path':str(hold_path),'sha256':sha256(hold_path)}
                dump(out/'problem.json',problem)
                snapshot=out/'code-snapshot';snapshot.mkdir(exist_ok=True)
                for name in ['marine_search.py','marine_roads.py','marine_validate.py','marine.py']:
                    shutil.copyfile(ROOT/'src/bigyear_planner'/name,snapshot/name)
                shutil.copyfile(Path(__file__),snapshot/'benchmark_marine_charter.py')
                route['provenance']={'terrestrial_incumbent':base['provenance'],
                    'base_route_sha256':sha256(BASE/'route.json'),
                    'problem_sha256':sha256(out/'problem.json'),'roads_sha256':sha256(out/'roads.sqlite'),
                    'roads_file':'roads.sqlite','dataset_manifest_sha256':base['provenance']['dataset_manifest_sha256'],
                    'port_road_network_sha256':roads.manifest['network_sha256'],
                    'road_release_sha256':roads.manifest['road_release_sha256'],
                    'marine_code_sha256':{f.name:sha256(f) for f in snapshot.iterdir()},
                    'marine_catalog_sha256':sha256(catalog_path),'marine_plan_sha256':sha256(out/'marine-plan.json'),
                    'marine_extension_source':'Modeled same-port charter scenario, not historical operated service'}
                dump(out/'route.json',route)
                print(json.dumps({'stage':'independent-validation','species':route['species_total']}),flush=True)
                checked=validate_marine(out/'route.json',out/'problem.json',out/'roads.sqlite')
                dump(out/'validation.json',checked)
                if not checked['valid']:raise ValueError('marine validation failed: '+str(checked['errors'][:5]))
                result={'baseline_species':base['species_total'],'species':route['species_total'],
                    'new_species':sorted(selected['new_species']),'boat_days':len(trips),
                    'ports':route['marine_extension']['ports'],'validation':checked,
                    'elapsed_seconds':time.monotonic()-started,'road_cache':roads.metrics(),
                    'source_records_checked':len(rows),'all_observations_preserved':True,
                    'source_coordinates_rounded':False,'walking_kmh':2.2,
                    'charter_scenario':read(catalog_path)['policy'],'route_sha256':sha256(out/'route.json'),
                    'joint_native_road_air_boat_search':False}
                dump(out/'result.json',result)
                write_csv(out/'marine-added-species.csv',[{'species_id':s,'common_name':next(t['name'] for t in route['species'] if t['id']==s)}
                    for s in sorted(selected['new_species'])],['species_id','common_name'])
                print(json.dumps(result),flush=True)
            finally:roads.close()
    finally:
        if awake.poll() is None:awake.terminate();awake.wait(timeout=5)


if __name__=='__main__':main()
