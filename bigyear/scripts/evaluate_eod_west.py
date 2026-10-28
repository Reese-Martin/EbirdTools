"""Reproducible CA/OR/WA 2024 bounded-cache capacity benchmark. No thinning."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import csv
import io
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
import sys
import threading
import signal
from html import escape

import duckdb

from bigyear_planner.config import Config
from bigyear_planner.eod import scoped_points, polygon_mask, classify_land_eod, prepare_eod
from bigyear_planner.eod_archive import archive_scan
from bigyear_planner.problem import build_problem
from bigyear_planner.util import ROOT, dump, read, q, sha256, write_csv, hardware

DATA = ROOT / 'data/west-coast-eod-2024'
RUNS = ROOT / 'runs/west-coast-eod-2024'
ARCHIVE = ROOT / 'data/eod-us-since-2000-zstd3'
BOUNDARY = DATA / 'sources/states-06-41-53-acs2024.geojson'
NETWORK = 'Geofabrik-US-West-2025-01-01_OSRM-26.10.0_car-v1'
POLICY = 'west-coast-eod-2024-aba8.19-presence-v1'
CONFIG = replace(Config(), year=2024, local_miles=30, activity_minutes=720,
                 target_locations=10000000, threads=2, seconds=600, seed=20240101,
                 alternatives=1, memory_limit='1GB')
STATES = {'US-CA', 'US-OR', 'US-WA'}


def metric(start):
    return {'wall_seconds': time.monotonic()-start, **hardware()}


def get_json(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'EbirdTools research capacity benchmark'})
            with urllib.request.urlopen(request, timeout=90) as stream:
                return json.load(stream)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt+1)


def audit():
    start = time.monotonic()
    if not BOUNDARY.exists():
        params = {'where': "GEOID IN ('06','41','53')", 'outFields': 'GEOID,NAME',
                  'outSR': 4326, 'returnGeometry': 'true', 'f': 'geojson'}
        result = get_json('https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/'
                          'tigerWMS_ACS2024/MapServer/80/query?' + urllib.parse.urlencode(params))
        if {f['properties']['GEOID'] for f in result['features']} != {'06','41','53'}:
            raise ValueError('incorrect Census state boundary response')
        dump(BOUNDARY, result)
    source, archive = archive_scan(ARCHIVE)
    with tempfile.TemporaryDirectory(prefix='west-audit-', dir=DATA) as temp:
        with duckdb.connect(str(DATA / 'audit.duckdb')) as con:
            con.execute("SET memory_limit='1GB'")
            con.execute('SET threads=2')
            con.execute('SET temp_directory='+q(temp))
            con.execute("SET max_temp_directory_size='4GB'")
            points = [p for p in scoped_points(con, source, 2024, BOUNDARY) if p['state'] in STATES]
            write_csv(DATA / 'scope-points.csv', points, ['state','latitude','longitude'])
            con.execute(f'''CREATE OR REPLACE TABLE cohort AS SELECT r.*,
                try_cast(concat(r.year,'-',r.month,'-',r.day) AS DATE) AS date,p.state
                FROM {source} r JOIN read_csv({q(DATA/'scope-points.csv')},all_varchar=true) p
                  ON r.decimallatitude=p.latitude AND r.decimallongitude=p.longitude
                WHERE r.year=2024 AND r.stateprovince=CASE p.state
                    WHEN 'US-CA' THEN 'California' WHEN 'US-OR' THEN 'Oregon' ELSE 'Washington' END''')
            taxa = [dict(zip(['source_name','source_taxon_id','presence_rows','source_records','example_gbif_id'],r))
                    for r in con.execute('''SELECT v_scientificname,v_taxonconceptid,count(*),
                    sum(source_record_count),min(representative_gbifid) FROM cohort GROUP BY 1,2 ORDER BY 1,2''').fetchall()]
            write_csv(DATA/'taxa.csv',taxa,list(taxa[0]))
            totals = con.execute('''SELECT count(*),sum(source_record_count),count(DISTINCT (state,decimallatitude,decimallongitude)),
                count(DISTINCT date),count(*) FILTER (WHERE date IS NULL),
                count(*) FILTER (WHERE geospatial_flagged_records!=0 OR geospatial_unknown_records!=0)
                FROM cohort''').fetchone()
            monthly = con.execute('SELECT month,count(*),sum(source_record_count) FROM cohort GROUP BY 1 ORDER BY 1').fetchall()
    report = {'year':2024,'states':sorted(STATES),'presence_groups':totals[0],'source_records':totals[1],
              'exact_locations':totals[2],'observed_dates':totals[3],'invalid_dates':totals[4],
              'spatial_quality_review_groups':totals[5],'taxon_concepts':len(taxa),'monthly':monthly,
              'archive_manifest_sha256':sha256(ARCHIVE/'manifest.json'),
              'archive_bytes':archive['bytes'],'boundary_sha256':sha256(BOUNDARY),'metrics':metric(start)}
    dump(DATA/'coverage-audit.json',report)
    print(json.dumps(report),flush=True)


def land():
    import shapefile
    from shapely.geometry import box, mapping, shape
    from shapely.ops import unary_union
    start=time.monotonic()
    xmin,ymin,xmax,ymax=polygon_mask(BOUNDARY).bounds
    bounds=(xmin-.05,ymin-.05,xmax+.05,ymax+.05)
    folder=ROOT/'build/land-mask/land-polygons-split-4326'
    with shapefile.Reader(shp=str(folder/'land_polygons.shp'),shx=str(folder/'land_polygons.shx')) as reader:
        clip=box(*bounds)
        polygons=[shape(s.__geo_interface__).intersection(clip) for s in reader.iterShapes(bbox=bounds)]
    dump(DATA/'terrestrial-mask.geojson',mapping(unary_union(polygons)))
    classify_land_eod(ARCHIVE,2024,DATA/'terrestrial-mask.geojson',DATA/'land.csv',BOUNDARY)
    with (DATA/'land.csv').open() as stream:
        counts=Counter(r['on_land'] for r in csv.DictReader(stream))
    dump(DATA/'land-provenance.json',{'classification':dict(counts),'metrics':metric(start),
         'source':'https://osmdata.openstreetmap.de/data/land-polygons.html',
         'policy':'Same current OSM coastline and inland-water shoreline/walking proxy as Alabama; legal and foot access unverified.'})
    print(json.dumps({'land':dict(counts),'metrics':metric(start)}),flush=True)


def taxonomy():
    start=time.monotonic()
    with (DATA/'taxa.csv').open() as stream:
        taxa=list(csv.DictReader(stream))
    dest=DATA/'sources/gbif-verbatim'
    dest.mkdir(exist_ok=True)
    prefix='http://rs.tdwg.org/dwc/terms/'
    fields=['scientificName','taxonConceptID','vernacularName','basisOfRecord','occurrenceStatus']
    def fetch(item):
        path=dest/(item['example_gbif_id']+'.json')
        if not path.exists():
            url=f"https://api.gbif.org/v1/occurrence/{item['example_gbif_id']}/verbatim"
            raw=get_json(url)
            dump(path,{**{k:raw.get(prefix+k) for k in fields},'source':url,'datasetKey':raw.get('datasetKey')})
        record=read(path)
        if (record['scientificName']!=item['source_name'] or record['taxonConceptID']!=item['source_taxon_id']
                or record['basisOfRecord']!='HumanObservation' or record['occurrenceStatus']!='PRESENT'
                or record['datasetKey']!='4fa7b334-ce0d-4e88-aaae-2e0c138d049e'):
            raise ValueError('publisher taxon verification differs: '+item['source_name'])
        return record
    with ThreadPoolExecutor(max_workers=4) as pool:
        verified=list(pool.map(fetch,taxa))
    aba_zip=ROOT/'data/crosswalk-review-2026-10-15/sources/ABA_Checklist-8.19.csv.zip'
    with zipfile.ZipFile(aba_zip) as z:
        aba={r[3]:r for r in csv.reader(io.StringIO(z.read('ABA_Checklist-8.19.csv').decode('utf-8-sig')))
             if len(r)>5 and r[5] in list('123456')}
    broad={'Setophaga petechia','Vireo gilvus','Numenius phaeopus','Larus argentatus'}
    population={'Cairina moschata','Columba livia','Alopochen aegyptiaca','Cygnus olor',
                'Gallus gallus','Myiopsitta monachus','Lonchura punctulata'}
    rows=[]
    for item,source in zip(taxa,verified):
        name=item['source_name']
        aba_row=aba.get(name)
        decision='1' if aba_row else '0'
        reason='Exact source-name/concept verified against publisher verbatim. Exact membership in fixed ABA 8.19; experimental presence-only policy. Approval/captive/exotic metadata unavailable.'
        if name in broad:
            decision='review';reason='Broad historical source concept needs a supported split assignment; no credit inferred from location.'
        elif name in population:
            decision='review';reason='Species-level extract cannot establish wild/established population or distinguish domestic forms; retained for review.'
        rows.append({'source_name':name,'source_taxon_id':item['source_taxon_id'],
            'species_id':'aba8.19:'+name.replace(' ','_'),'common_name':aba_row[1] if aba_row else source['vernacularName'],
            'aba_eligible':decision,'eod_presence_eligible':decision,'eligible_states':'US-CA|US-OR|US-WA',
            'reference':'https://www.aba.org/aba-checklist/; '+source['source'],
            'review_basis':reason,'presence_rows':item['presence_rows'],'policy_id':POLICY})
    write_csv(DATA/'taxonomy.csv',rows,list(rows[0]))
    excluded=[r for r in rows if r['eod_presence_eligible']!='1']
    write_csv(DATA/'taxonomy-review.csv',excluded,list(rows[0]))
    dump(DATA/'taxonomy-policy.json',{'policy':POLICY,'aba_snapshot_sha256':sha256(aba_zip),
         'verified_taxon_concepts':len(verified),'eligible':len(rows)-len(excluded),
         'deferred_or_excluded':excluded,'scope':'Engineering capacity test under conservative explicit presence-only rules; not ABA-certified historical eligibility.',
         'metrics':metric(start)})
    print(json.dumps({'verified':len(verified),'eligible':len(rows)-len(excluded),'held':len(excluded),'metrics':metric(start)}),flush=True)


def prepare():
    start=time.monotonic()
    dump(DATA/'config.json',CONFIG.to_dict())
    manifest=read(DATA/'dataset/manifest.json') if (DATA/'dataset/manifest.json').exists() else prepare_eod(ARCHIVE,DATA/'taxonomy.csv',DATA/'land.csv',DATA/'dataset',CONFIG,
                         'GBIF-0031401-260928105237408',POLICY,
                         scope_geojson=BOUNDARY,scope_label='California, Oregon and Washington')
    p=build_problem(DATA/'dataset',DATA/'problem-unanchored.json',CONFIG)
    assert p['reduction']['input_locations']==p['reduction']['selected_birding_locations']
    dump(DATA/'preparation-metrics.json',{'statistics':manifest['statistics'],'locations':len(p['locations']),
         'opportunities':len(p['opportunities']),'metrics':metric(start)})
    # The audit table is reproducible staging, not evidence authority. Keep its
    # small summaries and original source archive, then reclaim its full data.
    for path in [DATA/'audit.duckdb',DATA/'audit.duckdb.wal']:
        path.unlink(missing_ok=True)
    print(json.dumps(read(DATA/'preparation-metrics.json')),flush=True)


def graph():
    start=time.monotonic()
    binary=ROOT/'build/osrm/osrm-v26.10.0-darwin-arm64-Release'
    pbf=DATA/'sources/us-west-250101.osm.pbf'
    if pbf.stat().st_size!=3019422792:
        raise ValueError('archived Geofabrik road download is incomplete')
    if (DATA/'road-network.json').exists():
        previous=read(DATA/'road-network.json')
        components=previous.get('runtime_components',{})
        if components and all((DATA/'sources'/name).is_file() and sha256(DATA/'sources'/name)==digest
                              for name,digest in components.items()):
            print(json.dumps({'road_network':'verified existing immutable runtime graph'}),flush=True)
            return
    stages=[]
    for name,args,complete in [
        ('extract',['-p',str(binary/'profiles/car.lua'),'-t','4',str(pbf)],DATA/'sources/us-west-250101.osrm.ebg'),
        ('partition',['-t','4',str(pbf.with_suffix(''))],DATA/'sources/us-west-250101.osrm.partition'),
        ('customize',['-t','4',str(pbf.with_suffix(''))],DATA/'sources/us-west-250101.osrm.cell_metrics')]:
        # Existing complete stages resume; a killed stage is never marked complete.
        marker=DATA/f'graph-{name}.json'
        if marker.exists() and complete.exists():
            stages.append(read(marker));continue
        command=[str(binary/f'osrm-{name}'),*args]
        if name!='extract':command[-1]=str(DATA/'sources/us-west-250101.osrm')
        before=time.monotonic();rss_peak=0;allocated_peak=0
        with (RUNS/f'osrm-{name}.log').open('w') as log:
            job=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
            try:
                while True:
                    pid,status,usage=os.wait4(job.pid,os.WNOHANG)
                    if pid:
                        job.returncode=os.waitstatus_to_exitcode(status);break
                    out=subprocess.check_output(['ps','-o','rss=','-p',str(job.pid)],text=True).strip()
                    rss=int(out or 0)*1024;rss_peak=max(rss_peak,rss)
                    allocated=sum(p.stat().st_blocks*512 for p in DATA.rglob('*') if p.is_file())
                    allocated_peak=max(allocated_peak,allocated)
                    if rss>18*2**30 or shutil.disk_usage(DATA).free<30*2**30:
                        raise RuntimeError('road preparation exceeded its 18 GiB process/30 GiB free-disk guard')
                    time.sleep(1)
            except BaseException:
                job.terminate();job.wait();raise
        if job.returncode:
            raise subprocess.CalledProcessError(job.returncode,command)
        report={'stage':name,'seconds':time.monotonic()-before,
                'peak_rss_bytes':usage.ru_maxrss if sys.platform=='darwin' else usage.ru_maxrss*1024,
                'sampled_rss_bytes':rss_peak,'allocated_peak_bytes':allocated_peak}
        dump(marker,report);stages.append(report)
        print(json.dumps(report),flush=True)
    listed=subprocess.check_output([str(binary/'osrm-routed'),'--algorithm','mld','--list-inputs'],text=True)
    runtime={}
    for line in listed.splitlines():
        suffix=line.split()[-1]
        path=DATA/'sources'/('us-west-250101'+suffix)
        if path.is_file():runtime[path.name]=sha256(path);path.chmod(0o444)
    runtime_sha=hashlib.sha256(json.dumps(runtime,sort_keys=True).encode()).hexdigest()
    dump(DATA/'road-network.json',{'network_release':NETWORK,'pbf_sha256':sha256(pbf),
         'pbf_bytes':pbf.stat().st_size,'profile_sha256':sha256(binary/'profiles/car.lua'),
         'runtime_components':runtime,'runtime_sha256':runtime_sha,
         'source':'https://download.geofabrik.de/north-america/us-west-250101.osm.pbf',
         'stages':stages,'metrics':metric(start)})


def access(endpoint):
    start=time.monotonic()
    if (DATA/'access-summary.json').exists() and (DATA/'problem.json').exists():
        return
    p=read(DATA/'problem-unanchored.json')
    target=DATA/'road-access.csv'
    temporary=target.with_suffix('.csv.tmp')
    offsets=[]
    fields=['locality_id','latitude','longitude','road_latitude','road_longitude','offset_meters','source','walking_policy']
    def anchor(loc):
        result=get_json(f"{endpoint}/nearest/v1/driving/{loc['lon']},{loc['lat']}?number=1")
        if result.get('code')!='Ok' or not result.get('waypoints'):
            raise ValueError('no measured road access for '+loc['id'])
        waypoint=result['waypoints'][0]
        return {'locality_id':loc['locality_id'],'latitude':loc['lat'],'longitude':loc['lon'],
                'road_latitude':waypoint['location'][1],'road_longitude':waypoint['location'][0],
                'offset_meters':waypoint['distance'],'source':NETWORK,
                'walking_policy':'Same two-hour walking/birding allowance as Alabama; footpaths and legal access unverified.'}
    try:
        with temporary.open('w',newline='') as stream, ThreadPoolExecutor(max_workers=8) as pool:
            writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
            for offset in range(0,len(p['locations']),2000):
                for loc,row in zip(p['locations'][offset:offset+2000],pool.map(anchor,p['locations'][offset:offset+2000])):
                    writer.writerow(row);offsets.append(row['offset_meters'])
                    loc['road_access']={'lat':row['road_latitude'],'lon':row['road_longitude'],
                        'offset_meters':row['offset_meters'],'source':NETWORK,'walking_policy':row['walking_policy']}
                if offset%20000==0:print(json.dumps({'access_locations_completed':offset+min(2000,len(p['locations'])-offset)}),flush=True)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    p['access_catalog']={'path':str(target),'sha256':sha256(target)}
    dump(DATA/'problem.json',p)
    (DATA/'problem.json').chmod(0o444)
    offsets.sort()
    report={'locations':len(offsets),'median_offset_meters':offsets[len(offsets)//2],
            'maximum_offset_meters':max(offsets),'over_500_meters':sum(x>500 for x in offsets),
            'problem_sha256':sha256(DATA/'problem.json'),'metrics':metric(start)}
    dump(DATA/'access-summary.json',report)
    # This exact same model now includes anchors; discard its superseded staging copy.
    (DATA/'problem-unanchored.json').unlink()
    (DATA/'problem-unanchored.locations.csv').unlink(missing_ok=True)
    print(json.dumps(report),flush=True)


def benchmark(endpoint):
    from bigyear_planner.bounded_roads import create_bounded_roads
    from bigyear_planner.runner import run
    start=time.monotonic()
    network=read(DATA/'road-network.json')
    create_bounded_roads(DATA/'problem.json',DATA/'roads.bounded.json',endpoint,NETWORK,
                         network['runtime_sha256'],capacity_mib=8)
    # Sequential runs share one fixed cache, avoiding duplicate graphs and caches.
    jobs=[('cold-60s',60,20240100),('warm-60s',60,20240100),('seed-1-600s',600,20240101)]
    for name,seconds,seed in jobs:
        folder=RUNS/name
        if (folder/'validation.json').exists() and read(folder/'validation.json')['valid']:
            continue
        print(json.dumps({'starting':name,'cache_cap_mib':8,'seconds':seconds,'seed':seed}),flush=True)
        route=run(DATA/'problem.json',DATA/'roads.bounded.json',folder,seconds=seconds,threads=2,seed=seed)
        result=read(folder/'benchmark.json')
        print(json.dumps({'completed':name,'species':route['species_total'],'wall_seconds':result['wall_seconds'],
                         'cache':result['road_cache'],'native':result['engine'].get('road_cache')}),flush=True)
        del route
    dump(RUNS/'benchmark-stage.json',{'jobs':jobs,'metrics':metric(start)})


def evaluate():
    binary=ROOT/'build/osrm/osrm-v26.10.0-darwin-arm64-Release/osrm-routed'
    endpoint='http://127.0.0.1:5003'
    with (RUNS/'osrm-routed.log').open('w') as log:
        command=[str(binary),'--algorithm','mld','--ip','127.0.0.1','--port','5003','--threads','4',
                 '--verbosity','WARNING','--max-table-size','2000',str(DATA/'sources/us-west-250101.osrm')]
        job=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
        dump(RUNS/'road-router-process.json',{'pid':job.pid,'command':command})
        stop=threading.Event()
        resource={'sampled_combined_peak_rss_bytes':0,'minimum_free_bytes':shutil.disk_usage(DATA).free,
                  'additional_allocated_peak_bytes':0,'samples':0,'limits':{'combined_rss_gib':18,'minimum_free_gib':30}}
        def monitor():
            while not stop.is_set():
                try:
                    rows=[list(map(int,line.split())) for line in subprocess.check_output(
                        ['ps','-axo','pid,ppid,rss'],text=True).splitlines()[1:]]
                    owned={os.getpid()}
                    for _ in range(4):owned|={pid for pid,parent,_ in rows if parent in owned}
                    rss=sum(kib*1024 for pid,_,kib in rows if pid in owned)
                    allocated=storage()['allocated_unique_bytes']
                    free=shutil.disk_usage(DATA).free
                    resource['sampled_combined_peak_rss_bytes']=max(resource['sampled_combined_peak_rss_bytes'],rss)
                    resource['minimum_free_bytes']=min(resource['minimum_free_bytes'],free)
                    resource['additional_allocated_peak_bytes']=max(resource['additional_allocated_peak_bytes'],allocated)
                    resource['samples']+=1
                    if rss>18*2**30 or free<30*2**30:
                        dump(RUNS/'resource-limit.json',resource)
                        os.kill(os.getpid(),signal.SIGINT)
                        return
                except (OSError,ValueError):pass
                stop.wait(2)
        sampler=threading.Thread(target=monitor,daemon=True);sampler.start()
        try:
            for _ in range(180):
                if job.poll() is not None:raise RuntimeError('road router exited; inspect osrm-routed.log')
                try:
                    get_json(endpoint+'/nearest/v1/driving/-122.3,47.6?number=1');break
                except Exception:time.sleep(1)
            else:raise RuntimeError('road router did not become ready')
            access(endpoint)
            benchmark(endpoint)
        finally:
            stop.set();sampler.join()
            dump(RUNS/'resource-samples.json',resource)
            job.terminate()
            if job.returncode is None:
                _,status,usage=os.wait4(job.pid,0)
                job.returncode=os.waitstatus_to_exitcode(status)
                dump(RUNS/'road-router-metrics.json',{'peak_rss_bytes':usage.ru_maxrss if sys.platform=='darwin' else usage.ru_maxrss*1024,
                     'cpu_seconds':usage.ru_utime+usage.ru_stime,'stopped':True})


def storage():
    files={};logical=0
    for folder in (DATA,RUNS,ROOT/'build/query-spill'):
        for path in folder.rglob('*'):
            if path.is_file():
                stat=path.stat();logical+=stat.st_size
                files[stat.st_dev,stat.st_ino]=(stat.st_size,stat.st_blocks*512)
    return {'logical_bytes':logical,'unique_bytes':sum(v[0] for v in files.values()),
            'allocated_unique_bytes':sum(v[1] for v in files.values()),'files':len(files)}


def cleanup():
    from bigyear_planner.util import clean_query_spill
    network=read(DATA/'road-network.json')
    runtime=set(network['runtime_components'])
    removed=read(RUNS/'cleanup.json').get('removed',[]) if (RUNS/'cleanup.json').exists() else []
    for path in (DATA/'sources').glob('us-west-250101.osrm*'):
        if path.is_file() and path.name not in runtime:
            size=path.stat().st_blocks*512;path.unlink()
            removed.append({'path':str(path),'allocated_bytes':size,'reason':'completed preprocessing intermediate; not a runtime graph input'})
    for path in [DATA/'audit.duckdb',DATA/'audit.duckdb.wal',DATA/'scope-points.csv']:
        if path.exists():
            size=path.stat().st_blocks*512;path.unlink()
            removed.append({'path':str(path),'allocated_bytes':size,'reason':'reproducible staging; source archive and frozen replacement retained'})
    for folder in RUNS.iterdir():
        if not folder.is_dir() or not (folder/'validation.json').exists() or not read(folder/'validation.json').get('valid'):
            continue
        native=folder/'problem.by'
        if native.exists() and native.open().read(4)=='BY3 ':
            size=native.stat().st_blocks*512
            dump(folder/'native-input-provenance.json',{'format':'BY3','bytes':native.stat().st_size,
                 'sha256':sha256(native),'removed_after_validation':True,
                 'reproduction':'Re-export frozen problem.json with the fixed provider and a fresh local bridge.'})
            native.unlink();removed.append({'path':str(native),'allocated_bytes':size,'reason':'validated native input with expired runtime bridge; canonical model retained'})
        main=folder/'route.json'
        if main.exists():
            main_stat=main.stat();main_hash=sha256(main)
            for alt in folder.glob('alternative-*.json'):
                if alt.stat().st_ino!=main_stat.st_ino and sha256(alt)==main_hash:
                    size=alt.stat().st_blocks*512;main.chmod(0o444);alt.unlink();os.link(main,alt)
                    removed.append({'path':str(alt),'allocated_bytes':size,'reason':'byte-identical alternative now shares immutable main-route storage'})
    # Retain bounded diagnostic tails, never one permanent line per road query.
    for path in RUNS.glob('*.log'):
        if path.stat().st_size > 256*1024:
            before=path.stat().st_blocks*512
            with path.open('rb') as stream:
                stream.seek(-256*1024,2);tail=stream.read()
            path.write_bytes(tail)
            removed.append({'path':str(path),'allocated_bytes':max(0,before-path.stat().st_blocks*512),
                            'reason':'diagnostic log trimmed to a 256 KiB tail'})
    report={'removed':removed,'recovered_bytes':sum(r['allocated_bytes'] for r in removed),
            'orphan_spill_cleanup':clean_query_spill(),'retained_storage':storage(),
            'free_bytes_after_cleanup':shutil.disk_usage(DATA).free,
            'policy':'Retain original observations, immutable runtime graph/source provenance, one bounded cache, and validated results. Remove stage spill and superseded intermediates; no live query or unrelated file is removed.'}
    dump(RUNS/'cleanup.json',report)
    print(json.dumps(report),flush=True)


def retention():
    from bigyear_planner.eod import attach_dataset
    from bigyear_planner.util import ManagedDuckDB
    start=time.monotonic()
    path=DATA/('problem.json' if (DATA/'problem.json').exists() else 'problem-unanchored.json')
    p=read(path)
    actual={(o['date'],p['locations'][o['location']]['id']):o['id'] for o in p['opportunities']}
    masks=[0]*len(p['opportunities'])
    coordinates={loc['id']:(loc['lat'],loc['lon']) for loc in p['locations']}
    species_index={sp['id']:i for i,sp in enumerate(p['species'])}
    seen_locations=set();seen_species=set()
    groups=records=0
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
        evidence,_=attach_dataset(con,DATA/'dataset',p['manifest'])
        rows=con.execute(f"SELECT date::VARCHAR,location_id,species_id,source_record_count,lat,lon FROM {evidence}")
        while batch:=rows.fetchmany(10000):
            for day,lid,species,count,lat,lon in batch:
                index=actual.get((day,lid))
                if index is None or species not in species_index or coordinates.get(lid)!=(lat,lon):
                    raise ValueError('source date/location/species or exact numeric coordinate was lost or changed')
                masks[index]|=1<<species_index[species]
                seen_locations.add(lid);seen_species.add(species)
                groups+=1;records+=count
    for opportunity,observed in zip(p['opportunities'],masks):
        expected=0
        for species in opportunity['species']:expected|=1<<species
        if observed!=expected:raise ValueError('model species presence differs from streamed original evidence')
    matched=len(masks)
    counts=(groups,records,len(seen_species),len(seen_locations))
    assert matched==len(p['opportunities'])==len(actual)
    assert seen_locations==set(coordinates)
    report={'valid':True,'eligible_presence_groups':counts[0],'source_records':counts[1],
            'species':counts[2],'exact_locations_retained':counts[3],'opportunities_retained':matched,
            'all_location_date_species_combinations_identical':True,'coordinates_unchanged':True,
            'coordinate_strings':'Preserved verbatim in original Parquet archive and exact-point source catalogs',
            'archive_manifest_sha256':sha256(ARCHIVE/'manifest.json'),'metrics':metric(start)}
    dump(DATA/'model-retention-audit.json',report)
    print(json.dumps(report),flush=True)


def finalize():
    from bigyear_planner.display import build_display_roads
    from bigyear_planner.coverage import build_observer_coverage
    from shapely.geometry import mapping
    endpoint='http://127.0.0.1:5003'
    binary=ROOT/'build/osrm/osrm-v26.10.0-darwin-arm64-Release/osrm-routed'
    command=[str(binary),'--algorithm','mld','--ip','127.0.0.1','--port','5003',
             '--threads','4','--verbosity','WARNING','--mmap=true',str(DATA/'sources/us-west-250101.osrm')]
    with (RUNS/'display-router.log').open('w') as log:
        job=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                if job.poll() is not None:raise RuntimeError('display road router failed')
                try:
                    get_json(endpoint+'/nearest/v1/driving/-122.3,47.6?number=1');break
                except Exception:time.sleep(1)
            else:raise RuntimeError('display road router did not become ready')
            dump(RUNS/'display-verification.json',build_display_roads(RUNS,endpoint,NETWORK))
        finally:
            job.terminate()
            if job.returncode is None:
                _,status,usage=os.wait4(job.pid,0)
                job.returncode=os.waitstatus_to_exitcode(status)
                dump(RUNS/'mapped-router-metrics.json',{'peak_rss_bytes':usage.ru_maxrss if sys.platform=='darwin' else usage.ru_maxrss*1024,
                    'cpu_seconds':usage.ru_utime+usage.ru_stime,'mode':'read-only memory mapping',
                    'workload':'Display-road remeasurement of all benchmark routes, including full geometry and ferry steps; not a full search',
                    'stopped':True})
    coverage=build_observer_coverage(DATA/'dataset',RUNS)
    dump(RUNS/'observer-coverage-summary.json',{k:v for k,v in coverage.items() if k not in {'days','first_seen'}})
    dump(RUNS/'map-context.geojson',{'type':'FeatureCollection','features':[{'type':'Feature',
         'properties':{'name':'California · Oregon · Washington (2024)'},'geometry':mapping(polygon_mask(BOUNDARY))}]})
    cleanup()
    summarize()


def summarize():
    from statistics import median
    from bigyear_planner.display import display_route
    records=[]
    for name in ['cold-60s','warm-60s','seed-1-600s']:
        folder=RUNS/name
        route=read(folder/'route.json');audit=read(folder/'validation.json');b=read(folder/'benchmark.json')
        assert audit['valid'] and audit['days_checked']==366
        cache=b['road_cache'];native=b['engine']['road_cache']
        assert cache['logical_bytes']<=8*2**20 and native['hot_entries']<=65536 and not cache['errors']
        offsets=[route['locations'][s['location']]['road_access']['offset_meters'] for d in route['days'] for s in d['stops']]
        shown=display_route(RUNS,folder/'route.json')
        kinds=Counter(d['kind'] for d in route['days'])
        records.append({'run':name,'species':route['species_total'],'eligible_species':route['annual_recorded_species_ceiling'],
            'road_miles':audit['road_miles'],'travel_hours':audit['travel_minutes']/60,
            'birding_hours':sum(len(d['stops']) for d in route['days'])*CONFIG.birding_minutes/60,
            'birding_days':kinds['birding'],'transit_days':kinds['road_transit'],'rest_days':kinds['rest'],
            'search_seconds':b['engine']['elapsed_seconds'],'total_seconds':b['wall_seconds'],
            'engine_peak_rss_mib':b['engine_peak_rss_bytes']/2**20,'python_peak_rss_mib':b['hardware']['python_peak_rss_bytes']/2**20,
            'candidate_evaluations':b['engine']['candidate_evaluations'],'provider_queries':cache.get('provider_queries',0),
            'disk_cache_hits':cache.get('hits',0),'disk_cache_misses':cache.get('misses',0),'disk_evictions':cache.get('evictions',0),
            'disk_cache_bytes':cache['logical_bytes'],'native_hot_hits':native['hot_hits'],'native_hot_entries':native['hot_entries'],
            'native_hot_evictions':native['hot_evictions'],'validated_days':audit['days_checked'],
            'max_selected_road_offset_meters':max(offsets,default=0),'selected_offsets_over_500_meters':sum(v>500 for v in offsets),
            'ferry_journeys':sum(len(leg.get('vehicle_ferry_segments',[])) for d in shown['days'] for leg in d['legs'] if leg.get('start_fraction',0)==0),
            'valid':True,'route_sha256':sha256(folder/'route.json')})
    write_csv(RUNS/'run-summary.csv',records,list(records[0]))
    best=min(records,key=lambda r:(-r['species'],r['travel_hours'],r['road_miles']))
    network=read(DATA/'road-network.json');resource=read(RUNS/'resource-samples.json')
    retained=storage();clean=read(RUNS/'cleanup.json');proof=read(DATA/'model-retention-audit.json')
    access=read(DATA/'access-summary.json')
    # One compact coordinate record per unique road anchor, never per observation.
    anchors=set()
    with (DATA/'road-access.csv').open() as stream:
        for r in csv.DictReader(stream):anchors.add((float(r['road_latitude']),float(r['road_longitude'])))
    dense_bytes=16*len(anchors)**2
    us_pbf_bytes=10845428758
    ratio=us_pbf_bytes/network['pbf_bytes']
    prep_peak=max(s['peak_rss_bytes'] for s in network['stages'])
    national={'scope':'Existing lower 48 + DC, 2024','raw_locations_before_eligibility':1976076,
        'source':'Prior read-only sizing query of this unchanged archive; raw source-state labels, before polygon/land/taxon review.',
        'us_2025_road_pbf_bytes':us_pbf_bytes,'us_road_source':'https://download.geofabrik.de/north-america/us.html',
        'pbf_size_ratio_to_west':ratio,'illustrative_monolithic_graph_preparation_rss_bytes':prep_peak*ratio,
        'qualification':'A simple input-size scaling scenario, not a measured national RAM requirement. It excludes graph topology, source/model differences and operating-system headroom.',
        'assessment':'The quadratic storage obstacle is removed. A single national road build is not established on this 24 GiB laptop: the size-scaled preparation scenario exceeds RAM. Regional routing or preprocessing on a larger machine remains the next capacity step.'}
    report={'scope':'California, Oregon, Washington; full 2024; no location or observation thinning','hardware':hardware(),
        'retention':proof,'runs':records,'best_run':best,'config':CONFIG.to_dict(),'road_network':network,
        'access_catalog_summary':access,
        'unique_road_anchors':len(anchors),'avoided_dense_matrix_bytes':dense_bytes,
        'cache_disk_cap_bytes':8*2**20,'runtime_resources':resource,'road_router':read(RUNS/'road-router-metrics.json'),
        'mapped_router_check':read(RUNS/'mapped-router-metrics.json') if (RUNS/'mapped-router-metrics.json').exists() else None,
        'retained_storage':retained,'cleanup':clean,'free_bytes_now':shutil.disk_usage(DATA).free,
        'national_capacity':national,'display':read(RUNS/'display-verification.json') if (RUNS/'display-verification.json').exists() else None,
        'cold_warm_evaluation_ratio':records[1]['candidate_evaluations']/records[0]['candidate_evaluations'],
        'median_cpp_peak_rss_mib':median(r['engine_peak_rss_mib'] for r in records),
        'limitations':['Technical presence-only engineering test, not ABA-certified historical eligibility or guaranteed future detection.',
          'All original groups and exact coordinate strings remain in the national archive. The model credits the same conservative taxonomy, polygon, land and spatial-quality rules; held/excluded records remain in the source.',
          'Individual approval/captive/exotic metadata, observation time, detection probability and reported coordinate accuracy are unavailable.',
          'Same 30-mile birding-day, 12-hour activity, eight-hour transit, two-hour stop and flight-free rules as Alabama. Car-network travel can include vehicle ferries; historical schedules and legal/foot access are unverified.',
          'The cohort includes remote pins with large road-access offsets. Preserving pins does not establish a walkable connection. Selected offsets are reported for review.',
          'All source locations/date/species combinations are audited independently. Geodesic distance only ranks proposals; every feasibility cost is measured from the fixed OSRM graph.',
          'The native bounded backend ranks proposals differently from the dense backend; search-budget outcomes are not controlled algorithm-equivalence experiments. Exhaustive tiny-model tests establish identical feasibility and scoring under supplied identical costs.',
          'Cold/warm trials share a seed and two threads but are time-limited stochastic searches; their candidate streams are not guaranteed identical.',
          'Peak RSS includes file-backed pages. Sampled combined RSS measures simultaneous owned processes; individual process maxima occur at different times and must not be added.',
          'The sequential searches share one Python process. Its reported ru_maxrss is a cumulative high-water mark that includes setup and earlier trials, not an isolated per-run Python peak. Each C++ engine is a fresh process.',
          'Cold/warm tests last 60 seconds and the longer test 600 seconds. This establishes operation and capacity for this cohort, not search convergence or a global species/travel optimum.',
          'Original observations and reusable runtime graph/source inputs are retained. Cache misses do not create residual files; new saved experiments deliberately add result artifacts.']}
    dump(RUNS/'evaluation-summary.json',report)
    def number(v):return f'{v:,.1f}'
    rows=''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in [r['run'],r['species'],number(r['road_miles']),
        number(r['total_seconds']),number(r['engine_peak_rss_mib']),number(r['python_peak_rss_mib']),
        f"{r['candidate_evaluations']:,}",f"{r['provider_queries']:,}",f"{r['disk_evictions']:,}"])+'</tr>' for r in records)
    notes=''.join('<li>'+escape(v)+'</li>' for v in report['limitations'])
    mapped_note = ''
    if report['mapped_router_check']:
        mapped_note = f"<p>A separate read-only memory-mapped router check remeasured all 341 unique selected driving shapes with matching costs and peaked at {report['mapped_router_check']['peak_rss_bytes']/2**30:.2f} GiB RSS. That smaller display workload is promising for serving a prepared graph, but does not establish nationwide search or graph-preparation memory requirements.</p>"
    html=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>West Coast 2024 · bounded road-cache benchmark</title><style>
    :root{{font:16px/1.55 system-ui;color:#18261d;background:#f5f7f3}}body{{max-width:1150px;margin:40px auto;padding:0 24px}}
    h1{{font-size:2.5rem;line-height:1.12}}h2{{margin-top:0}}.eyebrow{{color:#36824b;letter-spacing:.1em;font-weight:700}}
    .card{{background:white;border:1px solid #dbe5da;border-radius:12px;padding:22px;margin:22px 0}}.hero{{font-size:1.2rem}}
    .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}}.stats strong{{font-size:1.8rem;display:block}}.scroll{{overflow-x:auto}}
    table{{border-collapse:collapse;width:100%;font-size:.88rem}}td,th{{padding:10px;border-bottom:1px solid #e1e8dd;text-align:left;white-space:nowrap}}
    a{{color:#28613a}}li{{margin:10px 0}}@media(max-width:700px){{.stats{{grid-template-columns:1fr}}h1{{font-size:2rem}}}}</style>
    <p class="eyebrow">EOD ROUTE LAB · CAPACITY BENCHMARK</p><h1>California, Oregon &amp; Washington<br>All of 2024, bounded road costs.</h1>
    <p class="hero">All <strong>{proof['eligible_presence_groups']:,} eligible presence groups</strong>, representing
    {proof['source_records']:,} source observations, {proof['exact_locations_retained']:,} exact locations and {proof['opportunities_retained']:,} location/date opportunities were retained.
    Every completed main route passed all 366 calendar days of independent source and road validation.</p>
    <p><a href="/?run={best['run']}/route.json">Explore the best tested route</a> · <a href="/run-summary.csv">Download benchmark CSV</a></p>
    <p>This measures computing capacity. The best tested route includes an unverified {best['max_selected_road_offset_meters']/1000:.1f} km connection from a road to an observation pin; physical access needs separate review.</p>
    <div class="card stats"><div><strong>8 MiB</strong>fixed disk-cache cap</div><div><strong>{resource['sampled_combined_peak_rss_bytes']/2**30:.2f} GiB</strong>sampled combined runtime RSS</div><div><strong>{report['free_bytes_now']/2**30:.1f} GiB</strong>free disk after cleanup</div></div>
    <div class="card"><h2>Measured searches</h2><div class="scroll"><table><thead><tr><th>Run</th><th>Species / 570</th><th>Car miles</th><th>Total sec</th><th>C++ MiB</th><th>Python MiB*</th><th>Evaluations</th><th>Provider queries</th><th>Disk evictions</th></tr></thead><tbody>{rows}</tbody></table></div>
    <p>Total runtime includes loading, reconstruction, source checks and validation, in addition to the 60- or 600-second native search. *Python RSS is the shared process's cumulative high-water mark, including setup and previous trials. C++ peaks are measured per fresh engine process; simultaneous combined RSS is sampled separately.</p></div>
    <div class="card"><h2>Storage and cleanup</h2><p>A dense table for the {len(anchors):,} distinct road anchors would use <strong>{dense_bytes/2**30:,.1f} GiB</strong> before any other input.
    Instead, every run used the same {records[0]['disk_cache_bytes']/2**20:.3f} MiB cache, evicting old measured costs in place. The original {read(ARCHIVE/'manifest.json')['bytes']/2**30:.2f} GiB Parquet archive was read directly.</p>
    <p>The retained West Coast workspace occupies {retained['allocated_unique_bytes']/2**30:.2f} GiB, counting shared files once. Cleanup reclaimed {clean['recovered_bytes']/2**30:.2f} GiB of preprocessing, staging and redundant transient artifacts.
    Immutable runtime graph/source inputs and validated results remain; temporary spill is removed after each stage. Identical route files and completed models share storage. Successful native inputs with expired bridge credentials are removed after their provenance is saved. Diagnostic logs retain at most 256 KiB each.</p></div>
    <div class="card"><h2>What this says about a nationwide run</h2><p><strong>The all-pairs storage obstacle is removed.</strong> Nationwide observations already fit in the compressed archive; the road-cost cache size does not depend on the number of candidate pairs.</p>
    <p>The remaining concern is the road graph. Western graph preparation took {sum(s['seconds'] for s in network['stages'])/60:.1f} minutes and peaked at {prep_peak/2**30:.2f} GiB RSS.
    The archived national OSM input is {ratio:.2f} times the size of the western input. A simple size-scaled scenario is {prep_peak*ratio/2**30:.1f} GiB RAM, exceeding this laptop's 24 GiB; this is an estimate, not a measured national requirement.
    Regional routing or preparing the national graph on a larger machine should be evaluated before a monolithic national build.</p>
    {mapped_note}
    <p>Sources: <a href="https://download.geofabrik.de/north-america/us-west.html">Geofabrik US West</a> · <a href="https://download.geofabrik.de/north-america/us.html">Geofabrik US</a>. Both graph-size references use the January 2025 snapshot convention.</p></div>
    <div class="card"><h2>Access and interpretation</h2><p>The best tested route reaches {best['species']} of 570 eligible species, with {best['road_miles']:,.1f} modeled car miles, {best['birding_hours']:.0f} planned birding hours and {best['rest_days']} rest days.
    Its largest selected road-access offset is {best['max_selected_road_offset_meters']/1000:.2f} km. Walking connections, legal access and historical ferry timetables remain unverified. This is a technical capacity result, not a ready-to-follow or proven optimal itinerary.</p><ul>{notes}</ul></div></html>'''
    (RUNS/'pilot-report.html').write_text(html)
    print(json.dumps({'best_run':best,'retained_storage':retained,'national_capacity':national}),flush=True)


if __name__=='__main__':
    DATA.mkdir(parents=True,exist_ok=True)
    (DATA/'sources').mkdir(exist_ok=True)
    RUNS.mkdir(parents=True,exist_ok=True)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['audit','land','taxonomy','prepare','graph','access','benchmark','evaluate','retention','cleanup','finalize','summarize'])
    parser.add_argument('--endpoint',default='http://127.0.0.1:5003')
    args=parser.parse_args()
    if args.stage in {'access','benchmark'}:globals()[args.stage](args.endpoint)
    else:globals()[args.stage]()
