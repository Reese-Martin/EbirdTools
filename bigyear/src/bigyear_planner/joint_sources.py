"""Freeze original publisher payloads for every credited land and marine group."""
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime,timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .eod import STATE_NAMES
from .marine_validate import publisher_check
from .util import ROOT,ManagedDuckDB,dump,q,read,sha256,write_csv


def marine_rows(route,problem):
    keys=[];wanted=set()
    for day in route['days']:
        if day['kind']!='pelagic':continue
        for stop in day['stops']:
            loc=route['locations'][stop['location']]
            key=(day['date'],loc['latitude_text'],loc['longitude_text'])
            keys.append(key);wanted.update((*key,s) for s in stop['species'])
    if not keys:return []
    data=Path(problem['joint_marine']['marine_data'])
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
        con.execute('CREATE TABLE keys(date DATE,latitude VARCHAR,longitude VARCHAR)')
        con.executemany('INSERT INTO keys VALUES (?,?,?)',sorted(set(keys)))
        cur=con.execute(f'''SELECT r.* FROM read_parquet({q(data/'offshore.parquet')}) r JOIN keys k
          ON r.date=k.date AND r.decimallatitude=k.latitude AND r.decimallongitude=k.longitude
          WHERE r.marine_disposition='policy_eligible_pending_marine_access'
          ORDER BY r.date,r.decimallatitude,r.decimallongitude,r.species_id,r.representative_gbifid''')
        cols=[v[0] for v in cur.description]
        return [row for values in cur.fetchall() if (row:=dict(zip(cols,values))) and
                (str(row['date']),row['decimallatitude'],row['decimallongitude'],row['species_id']) in wanted]


def complete_sources(route,problem,out):
    out=Path(out);data=Path(problem['dataset']).parent
    taxonomy={v['source_name']:v for v in csv.DictReader((Path(problem['dataset'])/'taxonomy.csv').open())}
    marine=marine_rows(route,problem);rows=[]
    for day in route['days']:
        for stop in day['stops']:
            loc=route['locations'][stop['location']]
            if day['kind']=='pelagic':
                matches=[r for r in marine if (str(r['date']),r['decimallatitude'],r['decimallongitude'])==
                         (day['date'],loc['latitude_text'],loc['longitude_text'])]
                evidence={};states=set(loc.get('source_states',[]))
                for row in matches:
                    key=row['representative_gbifid'];states.add(row['state'])
                    evidence.setdefault(row['species_id'],[]).append({'checklist':'','event':'gbif-group:'+key,
                        'record':'GBIF:'+key,'taxon':row['v_scientificname']})
                    rows.append(row)
                if set(evidence)!=set(stop['species']):raise ValueError('marine species missing original supplement support')
                stop['evidence']=evidence;loc['source_states']=sorted(states)
                loc['state']=next(iter(states)) if len(states)==1 else None
                loc['state_attribution']='publisher state labels; geographic eligibility independently uses nearest-country land'
            else:
                for species in stop['species']:
                    for evidence in stop['evidence'][species]:
                        tax=taxonomy[evidence['taxon']]
                        rows.append({'date':day['date'],'lat':loc['lat'],'lon':loc['lon'],
                            'stateprovince':STATE_NAMES[loc['state'].removeprefix('US-')],
                            'representative_gbifid':evidence['record'].removeprefix('GBIF:'),
                            'v_scientificname':evidence['taxon'],'v_taxonconceptid':tax['source_taxon_id'],
                            'species_id':species})
    target=out/'publisher-verbatim';target.mkdir(exist_ok=True)
    reuse={}
    for root,pattern in [(data/'sources','*-verbatim/*.json'),
                         (ROOT/'data/lower48-aba2024-aligned/sources','*-verbatim/*.json'),
                         (out.parent,'*/marine-verbatim/*.json'),(out.parent,'*/publisher-verbatim/*.json')]:
        for path in root.glob(pattern):reuse.setdefault(path.stem,path)
    def get(key):
        path=target/(key+'.json')
        if not path.exists() and key in reuse:os.link(reuse[key],path)
        if not path.exists():
            temporary=path.with_suffix('.part')
            result=subprocess.run(['/usr/bin/curl','-fLsS','--retry','2','--max-time','35',
                'https://api.gbif.org/v1/occurrence/'+key+'/verbatim','-o',str(temporary)],capture_output=True,text=True)
            if result.returncode:
                temporary.unlink(missing_ok=True);return key,{'retrieved':False,'reason':result.stderr[-400:]}
            if str(read(temporary).get('key'))!=key:raise ValueError('publisher ID differs')
            temporary.replace(path)
        return key,{'retrieved':True,'sha256':sha256(path)}
    keys=sorted({r['representative_gbifid'] for r in rows})
    print({'stage':'joint-publisher-check','records':len(keys)},flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:records=dict(pool.map(get,keys))
    decisions=[]
    for row in rows:
        key=row['representative_gbifid'];item=records[key]
        status=publisher_check(row,read(target/(key+'.json')),taxonomy[row['v_scientificname']]) if item['retrieved'] else 'source_unavailable'
        decisions.append({'record':key,'date':str(row['date']),'latitude':row['lat'],'longitude':row['lon'],
            'species_id':row['species_id'],'source_name':row['v_scientificname'],'decision':status,
            'score_tier':taxonomy[row['v_scientificname']]['score_tier']})
    dump(out/'publisher-retrieval.json',{'retrieved_at':datetime.now(timezone.utc).isoformat(),'records':records})
    write_csv(out/'source-decisions.csv',decisions,list(decisions[0]))
    failures=[r for r in decisions if r['decision']!='supported']
    dump(out/'publisher-precheck.json',{'valid':not failures,'records_checked':len(rows),'failures':failures})
    if failures:raise ValueError('selected publisher records require review; see publisher-precheck.json')
    problem['joint_marine'].update(publisher_retrieval_sha256=sha256(out/'publisher-retrieval.json'),
        publisher_decisions_sha256=sha256(out/'source-decisions.csv'))
    selected={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
    tiers={t['species_id']:t['score_tier'] for t in taxonomy.values()}
    provisional=sum(tiers[s].startswith('provisional') for s in selected)
    route['scoring_tiers']={'primary':len(selected)-provisional,'provisional':provisional}
    snapshots=out/'code-snapshot';snapshots.mkdir(exist_ok=True)
    files=[ROOT/'src/bigyear_planner'/name for name in ['joint_marine.py','joint_sources.py','joint_validate.py',
        'joint_seed.py','joint_exploration.py','targeted_exploration.py','routing_resolution.py','calendar_study.py','rest_exploration.py','daily_policy.py',
        'island_access.py','island_validate.py','national_runner.py','runner.py','compact_model.py','validate.py',
        'mobility.py','marine.py','marine_roads.py','regional_roads.py','search_roads.py','bounded_roads.py','regional_exploration.py',
        'countability.py','eod.py','eod_archive.py','walking.py','travel.py','util.py','config.py','coverage.py','surface_access.py']]
    files += [ROOT/'cpp'/name for name in ['model.h','compact_catalog.h','search.cpp','evaluate.cpp','bounded_roads.h']]
    files += [ROOT/'scripts/benchmark_joint_mobility.py']
    entry=Path(sys.argv[0]).resolve()
    if entry.is_relative_to(ROOT/'scripts') and entry.suffix=='.py' and entry not in files:files.append(entry)
    for path in files:shutil.copyfile(path,snapshots/path.name)
    problem['joint_marine']['code_snapshot']={p.name:sha256(p) for p in snapshots.iterdir()}
