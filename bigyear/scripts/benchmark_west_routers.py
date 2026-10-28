"""Reproducible full-cohort OSRM mmap search and matched road-provider benchmarks."""
import argparse
from contextlib import contextmanager
import gc
import gzip
from html import escape
import json
import os
import math
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from statistics import median

from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from bigyear_planner.travel import road_point

DATA = ROOT / 'data/west-coast-eod-2024'
WORK = ROOT / 'data/west-coast-routing-2024'
OUT = ROOT / 'runs/west-coast-routing-2024'
OLD = ROOT / 'runs/west-coast-eod-2024'
OSRM = ROOT / 'build/osrm/osrm-v26.10.0-darwin-arm64-Release/osrm-routed'
ENDPOINT = 'http://127.0.0.1:5004'


def get_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as stream:
        return json.load(stream)


@contextmanager
def monitor(name):
    stop = threading.Event()
    report = {'sample_seconds': .5, 'samples': 0, 'combined_peak_rss_bytes': 0,
              'minimum_free_bytes': shutil.disk_usage(WORK).free,
              'limits': {'combined_rss_bytes': 18*2**30, 'minimum_free_bytes': 30*2**30}}
    def sample():
        while not stop.is_set():
            try:
                rows = [list(map(int, s.split())) for s in subprocess.check_output(
                    ['ps', '-axo', 'pid,ppid,rss'], text=True).splitlines()[1:]]
                owned = {os.getpid()}
                for _ in range(5):
                    owned |= {pid for pid, parent, _ in rows if parent in owned}
                rss = sum(kib*1024 for pid, _, kib in rows if pid in owned)
                free = shutil.disk_usage(WORK).free
                report['combined_peak_rss_bytes'] = max(report['combined_peak_rss_bytes'], rss)
                report['minimum_free_bytes'] = min(report['minimum_free_bytes'], free)
                report['samples'] += 1
                if rss > 18*2**30 or free < 30*2**30:
                    report['guard_triggered'] = True
                    os.kill(os.getpid(), signal.SIGINT)
                    return
            except (OSError, ValueError):
                pass
            stop.wait(.5)
    worker = threading.Thread(target=sample, daemon=True)
    worker.start()
    try:
        yield report
    finally:
        stop.set()
        worker.join()
        dump(OUT / f'{name}-resources.json', report)


@contextmanager
def router(name, command, ready):
    start = time.monotonic()
    with (OUT / f'{name}.log').open('w') as log:
        job = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        dump(OUT / f'{name}-process.json', {'pid': job.pid, 'command': command})
        try:
            for _ in range(120):
                if job.poll() is not None:
                    raise RuntimeError(f'{name} exited during startup; see its log')
                try:
                    ready()
                    break
                except (OSError, ValueError):
                    time.sleep(.5)
            else:
                raise RuntimeError(f'{name} did not become ready')
            yield job
        finally:
            if job.returncode is None:
                job.terminate()
                _, status, usage = os.wait4(job.pid, 0)
                job.returncode = os.waitstatus_to_exitcode(status)
                dump(OUT / f'{name}-process-metrics.json', {
                    'peak_rss_bytes': usage.ru_maxrss if sys.platform == 'darwin' else usage.ru_maxrss*1024,
                    'cpu_seconds': usage.ru_utime+usage.ru_stime,
                    'lifetime_seconds': time.monotonic()-start, 'stopped': True})


def osrm_command(mapped):
    return [str(OSRM), '--algorithm', 'mld', '--ip', '127.0.0.1', '--port', '5004',
            '--threads', '4', '--verbosity', 'WARNING', '--max-table-size', '2000',
            *(['--mmap=true'] if mapped else []), str(DATA/'sources/us-west-250101.osrm')]


def full_search():
    from bigyear_planner.bounded_roads import create_bounded_roads
    from bigyear_planner.runner import run
    if (OUT/'mmap-600s/validation.json').exists():
        raise ValueError('completed search exists; keep its immutable result')
    network = read(DATA/'road-network.json')
    source_hash = sha256(DATA/'problem.json')
    manifest_hash = sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json')
    create_bounded_roads(DATA/'problem.json', WORK/'mmap.bounded.json', ENDPOINT,
                         network['network_release'], network['runtime_sha256'], capacity_mib=8)
    gc.collect()
    ready = lambda: get_json(ENDPOINT+'/nearest/v1/driving/-122.3,47.6?number=1')
    with monitor('mmap-full-search'):
        with router('mmap-search-router', osrm_command(True), ready):
            print(json.dumps({'starting': 'full mmap search', 'seconds': 600,
                              'threads': 2, 'seed': 20240101, 'cache_mib': 8}), flush=True)
            route = run(DATA/'problem.json', WORK/'mmap.bounded.json', OUT/'mmap-600s',
                        seconds=600, threads=2, seed=20240101)
            print(json.dumps({'completed': 'full mmap search', 'species': route['species_total']}), flush=True)
    assert source_hash == sha256(DATA/'problem.json')
    assert manifest_hash == sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json')
    dump(OUT/'cohort-verification.json', {'problem_sha256': source_hash,
         'source_archive_manifest_sha256': manifest_hash,
         'retention': read(DATA/'model-retention-audit.json'),
         'observations_and_exact_coordinates_unchanged': True,
         'flight_policy': 'unchanged flights=false; this compares road providers'})


def haversine(a, b):
    lat1, lat2 = map(math.radians, (a['lat'], b['lat']))
    dl = math.radians(b['lon']-a['lon'])
    value = math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dl/2)**2
    return 3958.7613*2*math.asin(min(1, math.sqrt(value)))


def make_sample():
    """Sample routing queries, never reduce the observation/search cohort."""
    rng = random.Random(20241017)
    route = read(OLD/'seed-1-600s/route.json')
    shapes = read(OLD/'road-display-geometries.json')
    locations = route['locations']
    binding = shapes['routes']['seed-1-600s/route.json']['legs']
    selected = [(tuple(map(int, key.split(':'))), shapes['shapes'][value])
                for key, value in binding.items()]
    records = {}
    def add(a, b, category):
        if a == b:
            return
        anchors = [{k:road_point(locations[i])[k] for k in ('lat','lon')} for i in (a,b)]
        if anchors[0] == anchors[1]:
            return
        key = f'{a}:{b}'
        if key in records:
            records[key]['tags'].append(category)
            return
        records[key] = {'id': key, 'tags': [category], 'indices': [a,b],
                        'endpoints': [locations[i] for i in (a,b)],
                        'anchors': anchors,
                        'geodesic_miles': haversine(*anchors)}
    for name, low, high, count in [('selected-local',0,30,16),
            ('selected-regional',30,150,16),('selected-long',150,500,16),
            ('selected-very-long',500,math.inf,12)]:
        choices = [pair for pair, shape in selected if low <= shape['miles'] < high]
        for a,b in rng.sample(choices, min(count,len(choices))):
            add(a,b,name)
    for pair, shape in selected:
        if shape.get('vehicle_ferry_segments'):
            add(*pair,'selected-ferry')
    interstate = [pair for pair,_ in selected if locations[pair[0]]['state']!=locations[pair[1]]['state']]
    for a,b in rng.sample(interstate,min(12,len(interstate))):
        add(a,b,'selected-interstate')
    # Include previously unselected nearby endpoints in each state's full catalog.
    cells = {}
    for i,loc in enumerate(locations):
        key = (loc['state'], int(loc['lat']*10), int(loc['lon']*10))
        cells.setdefault(key,[]).append(i)
    for state in ['US-CA','US-OR','US-WA']:
        options = [v for k,v in cells.items() if k[0]==state and len(v)>1]
        for cell in rng.sample(options, min(6,len(options))):
            a,b = rng.sample(cell,2)
            add(a,b,'unselected-local')
    for _ in range(18):
        a,b = rng.sample(range(len(locations)),2)
        add(a,b,'unselected-regional' if locations[a]['state']==locations[b]['state'] else 'unselected-interstate')
    # Reachability challenges use actual source locations on Catalina and Channel
    # Islands, not synthetic coordinates or an invented road connection.
    mainland = min(range(len(locations)),key=lambda i:abs(locations[i]['lat']-34.05)+abs(locations[i]['lon']+118.25))
    island = [i for i,loc in enumerate(locations) if (33.2<loc['lat']<33.5 and -118.65<loc['lon']<-118.2)
              or (33.9<loc['lat']<34.15 and -120.3<loc['lon']<-119.4)]
    for i in rng.sample(island,min(4,len(island))):
        add(i,mainland,'island-reachability')
        add(mainland,i,'island-reachability-reverse')
    for a,b in [r['indices'] for r in rng.sample(list(records.values()),12)]:
        add(b,a,'reverse-direction')
    result = {'seed': 20241017, 'queries': list(records.values()),
              'full_source_locations': len(locations),
              'problem_sha256': route['provenance']['problem_sha256'],
              'source_route_sha256': sha256(OLD/'seed-1-600s/route.json'),
              'method': 'Fixed stratified routing sample from the full exact-point cohort and selected route; identical road-access anchors sent to both providers. Sampling queries does not thin any observations.',
              'endpoint_note': 'Exact original observation coordinates remain in endpoints; the frozen road-access anchors are separate. Physical access from road to pin is not established.'}
    dump(OUT/'road-sample.json',result)
    print(json.dumps({'sample_queries':len(records)}),flush=True)


def query_url(provider, record, geometry=True, vmax=130, strict=False):
    a,b = record['anchors']
    if provider.startswith('osrm'):
        points = ';'.join(f"{p['lon']},{p['lat']}" for p in (a,b))
        return ENDPOINT+'/route/v1/driving/'+points+(
            '?overview=full&geometries=geojson&steps=true' if geometry else '?overview=false&steps=false')+(
            '&radiuses=100;100' if strict else '')
    params = {'lonlats': '|'.join(f"{p['lon']},{p['lat']}" for p in (a,b)),
              'profile': 'car-vario', 'alternativeidx': 0, 'format': 'geojson',
              'profile:vmax': vmax, 'profile:considerTurnRestrictions': '1',
              'profile:add_beeline': '0', 'profile:use_offroad': '0'}
    if strict:
        params.update({'profile:use_dynamic_range':'0','profile:waypointCatchingRange':'100',
                       'profile:correctMisplacedViaPoints':'0'})
    return 'http://127.0.0.1:17778/brouter?'+urllib.parse.urlencode(params)


def error_kind(message):
    text = message.lower()
    if ('timed out' in text or 'timeout' in text or 'maximum running time' in text
            or 'operation killed' in text):
        return 'timeout'
    if (('not found' in text and ('waypoint' in text or 'position' in text))
            or 'position not mapped' in text or 'nosegment' in text):
        return 'endpoint-not-matched'
    if 'not found' in text and ('.rd5' in text or 'segment' in text):
        return 'missing-tile'
    if ('no track' in text or 'no route' in text or 'noroute' in text
            or 'connection not found' in text or 'island detected' in text):
        return 'unreachable'
    if 'outofmemory' in text or 'memory limit' in text:
        return 'memory-limit'
    return 'provider-error'


def request_road(provider, record, geometry=True, vmax=130, strict=False):
    url = query_url(provider, record, geometry, vmax, strict)
    start = time.monotonic()
    raw = b''
    request_seconds = None
    try:
        with urllib.request.urlopen(url, timeout=35) as stream:
            raw = stream.read()
        data = json.loads(raw)
        request_seconds = time.monotonic()-start
        if provider.startswith('osrm'):
            if data.get('code') != 'Ok':
                raise ValueError(str(data.get('code')))
            route = data['routes'][0]
            meters, seconds = route['distance'], route['duration']
            coordinates = route.get('geometry',{}).get('coordinates',[]) if geometry else []
            ferry = [step for leg in route['legs'] for step in leg.get('steps',[])
                     if step.get('mode') == 'ferry']
            details = {'ferry_steps': len(ferry), 'weight_name': route.get('weight_name')}
        else:
            route = next(f for f in data['features'] if f.get('geometry',{}).get('type')=='LineString')
            props = route['properties']
            meters, seconds = float(props['track-length']), float(props['total-time'])
            coordinates = [p[:2] for p in route['geometry']['coordinates']]
            messages = props.get('messages',[])
            ferry = [m for m in messages[1:] if any('route=ferry' in str(v) for v in m)]
            details = {'ferry_message_rows': len(ferry), 'creator': props.get('creator'),
                       'vmax_kmh': vmax, 'reported_cost': props.get('cost')}
        if not all(math.isfinite(v) and v >= 0 for v in (meters, seconds)):
            raise ValueError('non-finite or negative road costs')
        result = {'id': record['id'], 'status': 'ok', 'distance_meters': meters,
                  'duration_seconds': seconds, 'coordinate_count': len(coordinates), **details}
        if coordinates:
            endpoints = [{'lon': coordinates[i][0], 'lat': coordinates[i][1]} for i in (0,-1)]
            result['endpoint_offsets_meters'] = [haversine(a,b)*1609.344
                                                for a,b in zip(record['anchors'],endpoints)]
            result['polyline_meters'] = sum(haversine({'lon':a[0],'lat':a[1]},
                                                    {'lon':b[0],'lat':b[1]})*1609.344
                                            for a,b in zip(coordinates,coordinates[1:]))
    except urllib.error.HTTPError as error:
        raw = error.read()
        message = raw.decode(errors='replace')[:1500]
        result = {'id': record['id'], 'status': error_kind(message),
                  'http_status': error.code, 'error': message}
    except (OSError, ValueError, KeyError, StopIteration) as error:
        result = {'id': record['id'], 'status': error_kind(str(error)),
                  'error': type(error).__name__+': '+str(error)[:1500]}
    elapsed = time.monotonic()-start
    result['seconds'] = request_seconds if result['status']=='ok' else elapsed
    result['client_analysis_seconds'] = elapsed-result['seconds']
    result['response_bytes'] = len(raw)
    return result, raw


def replay(provider):
    """Fresh process per provider, identical fixed query order, no pair cache."""
    sample = read(OUT/'road-sample.json')
    folder = OUT/provider
    folder.mkdir(exist_ok=True)
    if (folder/'replay.json').exists():
        raise ValueError('completed provider replay already exists')
    shutil.copyfile(__file__,folder/'benchmark-driver.py')
    if provider.startswith('osrm'):
        command = osrm_command(provider=='osrm-mmap')
        ready = lambda: get_json(ENDPOINT+'/nearest/v1/driving/-122.3,47.6?number=1')
    else:
        release = read(OUT/'brouter-release.json')
        if sha256(release['jar']) != release['jar_sha256']:
            raise ValueError('pinned BRouter executable changed')
        profiles_path = WORK.parent.parent/'build/brouter-benchmark/brouter-1.7.10/misc/profiles2'
        if sha256(profiles_path/'car-vario.brf') != release['profile_sha256'] or sha256(profiles_path/'lookups.dat') != release['lookup_sha256']:
            raise ValueError('pinned BRouter profile or lookup dictionary changed')
        for tile in release['tiles']:
            if sha256(WORK/'brouter-tiles'/tile['file']) != tile['sha256']:
                raise ValueError('pinned BRouter road tile changed: '+tile['file'])
        custom = WORK/'custom-profiles'
        custom.mkdir(exist_ok=True)
        command = [release['java'], '-Xms64m', '-Xmx2g',
                   '-DmaxRunningTime=20', '-cp', release['jar'], 'btools.server.RouteServer',
                   release['tile_directory'], release['profiles'], str(custom), '17778','1','127.0.0.1']
        def ready():
            with socket.create_connection(('127.0.0.1',17778),timeout=1):
                pass
    results = []
    with monitor(provider+'-replay'):
        with router(provider+'-replay-router', command, ready):
            # Warm up parser/JIT with a short, real measured road; excluded from timings.
            warmup = min((r for r in sample['queries'] if r['geodesic_miles']>.25),
                         key=lambda r:r['geodesic_miles'])
            first,_ = request_road(provider,warmup)
            dump(folder/'warmup.json',first)
            if first['status'] != 'ok':
                raise RuntimeError('provider warmup failed: '+str(first))
            for repeat in range(2):
                start = time.monotonic()
                rows=[]
                for i,record in enumerate(sample['queries']):
                    row,raw = request_road(provider,record)
                    row['pass'] = repeat+1
                    rows.append(row)
                    if repeat==0:
                        with gzip.open(folder/(record['id'].replace(':','-')+'.json.gz'),'wb') as stream:
                            stream.write(raw)
                    with (folder/'progress.jsonl').open('a') as stream:
                        stream.write(json.dumps(row)+'\n')
                    if (i+1)%10==0:
                        print(json.dumps({'provider':provider,'pass':repeat+1,'completed':i+1,
                                          'of':len(sample['queries']),'status':row['status']}),flush=True)
                results.append({'pass':repeat+1,'wall_seconds':time.monotonic()-start,
                                'request_seconds':sum(row['seconds'] for row in rows),'queries':rows})
            # OSRM costs-only workload mirrors native provider requests. BRouter's
            # GeoJSON interface also returns the route shape, which is recorded.
            if provider.startswith('osrm'):
                rows=[]
                start=time.monotonic()
                for record in sample['queries']:
                    row,_=request_road(provider,record,geometry=False)
                    rows.append(row)
                dump(folder/'costs-only.json',{'wall_seconds':time.monotonic()-start,'queries':rows})
            else:
                # Separate sensitivity, not mixed into the primary vmax=130 trial.
                sensitivity=[]
                records=[r for r in sample['queries'] if 'selected-long' in r['tags']
                         or 'selected-very-long' in r['tags']][:8]
                for record in records:
                    row,_=request_road(provider,record,vmax=90)
                    sensitivity.append(row)
                dump(folder/'default-90kmh-sensitivity.json',sensitivity)
    dump(folder/'replay.json', {'provider':provider,'sample_sha256':sha256(OUT/'road-sample.json'),
         'driver_sha256':sha256(folder/'benchmark-driver.py'),
         'passes':results,'worker_threads':1 if provider=='brouter' else 4,
         'concurrent_client_requests':1,'pair_cache':'none; sequential direct requests',
         'cache_note':'Fresh server process; first/warm repeats. OS filesystem cache is not flushed, so first pass is not a cold disk measurement.',
         'brouter_algorithm_memory_class_mib':128 if provider=='brouter' else None,
         'brouter_jvm_heap_cap_mib':2048 if provider=='brouter' else None,
         'brouter_profile_parameters':{'vmax':130,'considerTurnRestrictions':True,'add_beeline':False,'use_offroad':False} if provider=='brouter' else None})
    print(json.dumps({'completed_provider':provider}),flush=True)


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values)-1)*fraction
    a = int(position)
    return values[a]+(values[min(a+1,len(values)-1)]-values[a])*(position-a)


def response_coordinates(provider, key):
    with gzip.open(OUT/provider/(key.replace(':','-')+'.json.gz'),'rb') as stream:
        data = json.load(stream)
    if provider.startswith('osrm'):
        return data['routes'][0]['geometry']['coordinates']
    return next(f['geometry']['coordinates'] for f in data['features']
                if f.get('geometry',{}).get('type')=='LineString')


def shape_agreement(first, second):
    """Approximate geometric agreement, not a legality or accuracy certificate."""
    from shapely.geometry import LineString
    latitude = sum(p[1] for p in first+second)/len(first+second)
    scale = math.cos(math.radians(latitude))*111195
    def line(points):
        return LineString([(p[0]*scale,p[1]*111195) for p in points])
    a,b = line(first),line(second)
    def directional(x,y):
        if not x.length:
            return float(x.distance(y)<=200)
        return sum(x.interpolate(i/199,normalized=True).distance(y)<=200 for i in range(200))/200
    return (directional(a,b)+directional(b,a))/2


def summarize_replay(provider):
    data = read(OUT/provider/'replay.json')
    metrics = []
    for trial in data['passes']:
        good = [q for q in trial['queries'] if q['status']=='ok']
        latencies = [q['seconds'] for q in good]
        request_seconds=sum(q['seconds'] for q in trial['queries'])
        metrics.append({'pass':trial['pass'],'wall_seconds':trial['wall_seconds'],
             'request_seconds':request_seconds,
             'status_counts':dict(Counter(q['status'] for q in trial['queries'])),
             'successful_queries':len(good),'total_queries':len(trial['queries']),
             'median_success_seconds':median(latencies) if latencies else None,
             'p95_success_seconds':percentile(latencies,.95),
             'all_requests_per_second':len(trial['queries'])/request_seconds})
    first = {r['id']:r for r in data['passes'][0]['queries']}
    second = {r['id']:r for r in data['passes'][1]['queries']}
    changed = [key for key,row in first.items() if row['status']!=second[key]['status']
               or (row['status']=='ok' and (abs(row['distance_meters']-second[key]['distance_meters'])>.11
                    or abs(row['duration_seconds']-second[key]['duration_seconds'])>.11))]
    return {'passes':metrics,'repeat_changed_ids':changed,
            'router':read(OUT/f'{provider}-replay-router-process-metrics.json'),
            'resources':read(OUT/f'{provider}-replay-resources.json')}


def strict_sensitivity():
    release=read(OUT/'brouter-release.json')
    sample=read(OUT/'road-sample.json')
    inputs={r['id']:r for r in sample['queries']}
    primary=read(OUT/'brouter/replay.json')['passes'][0]['queries']
    shifted=[r for r in primary if r['status']=='ok' and max(r['endpoint_offsets_meters'])>100]
    failures=[r for r in primary if r['status']=='unreachable'][:3]
    controls=[r for r in primary if r['status']=='ok' and max(r['endpoint_offsets_meters'])<10][:3]
    chosen={r['id']:r for r in [*shifted,*failures,*controls]}
    folder=OUT/'brouter-strict'
    folder.mkdir(exist_ok=True)
    shutil.copyfile(__file__,folder/'benchmark-driver.py')
    command=[release['java'],'-Xms64m','-Xmx2g','-DmaxRunningTime=20','-cp',release['jar'],
             'btools.server.RouteServer',release['tile_directory'],release['profiles'],
             str(WORK/'custom-profiles'),'17778','1','127.0.0.1']
    def ready():
        with socket.create_connection(('127.0.0.1',17778),timeout=1):
            pass
    rows=[]
    with monitor('brouter-strict'):
        with router('brouter-strict-router',command,ready):
            for key in chosen:
                row,raw=request_road('brouter',inputs[key],strict=True)
                with gzip.open(folder/(key.replace(':','-')+'.json.gz'),'wb') as stream:
                    stream.write(raw)
                rows.append({'primary':chosen[key],'strict':row})
                print(json.dumps({'strict':key,'status':row['status']}),flush=True)
    dump(folder/'sensitivity.json',{'purpose':'Targeted endpoint-matching sensitivity, not a population-weighted or timed throughput trial.',
         'parameters':{'vmax':130,'use_dynamic_range':0,'waypointCatchingRange_meters':100,'correctMisplacedViaPoints':0},
         'queries':rows,'driver_sha256':sha256(folder/'benchmark-driver.py')})


def strict_osrm():
    sample=read(OUT/'road-sample.json')
    inputs={r['id']:r for r in sample['queries']}
    primary=read(OUT/'osrm-mmap/replay.json')['passes'][0]['queries']
    shifted=[r for r in primary if r['status']=='ok' and max(r['endpoint_offsets_meters'])>100]
    controls=[r for r in primary if r['status']=='ok' and max(r['endpoint_offsets_meters'])<10][:3]
    chosen={r['id']:r for r in [*shifted,*controls]}
    folder=OUT/'osrm-strict';folder.mkdir(exist_ok=True)
    rows=[]
    with router('osrm-strict-router',osrm_command(True),
                lambda:get_json(ENDPOINT+'/nearest/v1/driving/-122.3,47.6?number=1')):
        for key in chosen:
            row,raw=request_road('osrm-mmap',inputs[key],strict=True)
            with gzip.open(folder/(key.replace(':','-')+'.json.gz'),'wb') as stream:
                stream.write(raw)
            rows.append({'primary':chosen[key],'strict':row})
    dump(folder/'sensitivity.json',{'parameters':{'radiuses_meters':[100,100]},'queries':rows,
         'purpose':'Targeted endpoint diagnostic on affected queries and controls; does not change the frozen optimizer result.'})


def audit_selected_endpoints():
    route=read(OUT/'mmap-600s/route.json')
    supplement=read(OUT/'road-display-geometries.json')
    binding=supplement['routes']['mmap-600s/route.json']['legs']
    rows=[]
    for pair,key in binding.items():
        indices=list(map(int,pair.split(':')))
        points=supplement['shapes'][key]['geometry']['coordinates']
        gaps=[]
        for i,p in zip(indices,[points[0],points[-1]]):
            anchor=road_point(route['locations'][i])
            gaps.append(haversine(anchor,{'lon':p[0],'lat':p[1]})*1609.344)
        rows.append({'id':pair,'anchor_to_returned_road_meters':gaps})
    dump(OUT/'selected-endpoint-audit.json',{'unique_driving_pairs':len(rows),
         'passes_100m_endpoint_check':all(max(r['anchor_to_returned_road_meters'])<=100 for r in rows),
         'route_sha256':sha256(OUT/'mmap-600s/route.json'),
         'shifted_pairs_over_100m':sum(max(r['anchor_to_returned_road_meters'])>100 for r in rows),
         'max_anchor_to_returned_road_meters':max(max(r['anchor_to_returned_road_meters']) for r in rows),
         'queries':rows,'note':'Diagnostic only. Frozen costs, reported pins and species credits remain unchanged; existing validation does not certify physical access.'})


def analyze():
    sample = read(OUT/'road-sample.json')
    byid = {q['id']:q for q in sample['queries']}
    providers = {name:read(OUT/name/'replay.json') for name in ['osrm-ram','osrm-mmap','brouter']}
    rows = {name:{r['id']:r for r in value['passes'][0]['queries']}
            for name,value in providers.items()}
    agreement = []
    identical_shapes=0
    max_mode_distance_delta=0
    max_mode_duration_delta=0
    comparisons = []
    for key,record in byid.items():
        a,b,c = (rows[name][key] for name in ['osrm-ram','osrm-mmap','brouter'])
        if a['status']!=b['status'] or (a['status']=='ok' and (
                abs(a['distance_meters']-b['distance_meters'])>.11
                or abs(a['duration_seconds']-b['duration_seconds'])>.11)):
            raise ValueError('OSRM loading modes disagree on measured road cost: '+key)
        agreement.append(key)
        mmap_coordinates=[]
        if a['status']=='ok':
            max_mode_distance_delta=max(max_mode_distance_delta,abs(a['distance_meters']-b['distance_meters']))
            max_mode_duration_delta=max(max_mode_duration_delta,abs(a['duration_seconds']-b['duration_seconds']))
            mmap_coordinates=response_coordinates('osrm-mmap',key)
            identical_shapes+=mmap_coordinates==response_coordinates('osrm-ram',key)
        result = {'id':key,'tags':record['tags'],
                  'origin_state':record['endpoints'][0]['state'],
                  'destination_state':record['endpoints'][1]['state'],
                  'osrm_status':b['status'],'brouter_status':c['status'],
                  'osrm_endpoint_offsets_meters':b.get('endpoint_offsets_meters'),
                  'brouter_endpoint_offsets_meters':c.get('endpoint_offsets_meters')}
        if b['status']==c['status']=='ok':
            result.update({'osrm_miles':b['distance_meters']/1609.344,
                 'brouter_miles':c['distance_meters']/1609.344,
                 'distance_ratio':c['distance_meters']/b['distance_meters'] if b['distance_meters'] else None,
                 'duration_ratio':c['duration_seconds']/b['duration_seconds'] if b['duration_seconds'] else None,
                 'osrm_minutes':b['duration_seconds']/60,'brouter_minutes':c['duration_seconds']/60,
                 'osrm_endpoint_offsets_meters':b['endpoint_offsets_meters'],
                 'brouter_endpoint_offsets_meters':c['endpoint_offsets_meters'],
                 'osrm_ferry_steps':b['ferry_steps'],'brouter_ferry_rows':c['ferry_message_rows'],
                 'shape_fraction_within_200m':shape_agreement(
                     mmap_coordinates,response_coordinates('brouter',key))})
        comparisons.append(result)
    result = {'sample_sha256':sha256(OUT/'road-sample.json'), 'queries':len(byid),
              'cohort':read(OUT/'cohort-verification.json'),
              'full_mmap_search':read(OUT/'mmap-600s/benchmark.json'),
              'full_mmap_resources':read(OUT/'mmap-full-search-resources.json'),
              'full_mmap_router':read(OUT/'mmap-search-router-process-metrics.json'),
              'previous_ram_search':read(OLD/'seed-1-600s/benchmark.json'),
              'previous_ram_combined_resources':read(OLD/'resource-samples.json'),
              'providers':{name:summarize_replay(name) for name in providers},
              'identical_osrm_cost_queries':len(agreement), 'comparisons':comparisons,
              'identical_osrm_geometry_queries':identical_shapes,
              'osrm_max_distance_difference_meters':max_mode_distance_delta,
              'osrm_max_duration_difference_seconds':max_mode_duration_delta,
              'strict_matching_sensitivity':read(OUT/'brouter-strict/sensitivity.json') if (OUT/'brouter-strict/sensitivity.json').exists() else None,
              'osrm_strict_matching_sensitivity':read(OUT/'osrm-strict/sensitivity.json') if (OUT/'osrm-strict/sensitivity.json').exists() else None,
              'limitations':[
                'All full-cohort observations and exact coordinates are unchanged. Only the routing replay is sampled.',
                'BRouter current weekly tiles and OSRM January 2025 OSM differ; routing agreement is not independent accuracy or legal-access validation.',
                'BRouter bundled car-vario with vmax=130 km/h is a different costing model from the fixed OSRM car profile. A separate default-90 sensitivity is retained.',
                'Both receive identical frozen road anchors. Original observation pins remain distinct; access from road to pin remains unverified.',
                'The primary BRouter trial retains its default dynamic matching range. Large returned-road endpoint shifts are flagged; they cannot be treated as arrival at the submitted anchor.',
                f'{len(byid)} stratified queries are a diagnostic sample, not a population-weighted national route-quality estimate.',
                'Fresh router processes do not imply cold disk caches. Query order is fixed; no per-pair application cache is used in replays.',
                'Replay geometry timings request full shapes from both providers; OSRM costs-only timings are recorded separately to reflect optimizer requests.',
                'Previous RAM search reused a warm disk cache and an earlier engine build; new mmap search starts with an empty cache. The time-limited, two-thread searches do not follow identical candidate streams.',
                'RSS counts file-backed resident pages and is not a measurement of private heap. Combined samples count simultaneous owned processes.',
                'BRouter replay is not a full-year optimizer run or proof of nationwide throughput. Timeouts and failures remain explicit, never converted to straight-line routes.'
              ]}
    dump(OUT/'routing-comparison.json',result)
    print(json.dumps({'query_count':len(byid),'osrm_modes_costs_match':len(agreement),
                      'providers':result['providers']}),flush=True)


def display():
    from bigyear_planner.display import build_display_roads
    network = read(DATA/'road-network.json')
    with router('mmap-display-router',osrm_command(True),
                lambda:get_json(ENDPOINT+'/nearest/v1/driving/-122.3,47.6?number=1')):
        dump(OUT/'display-verification.json',build_display_roads(OUT,ENDPOINT,network['network_release']))
    for name in ['map-context.geojson','observer-species-coverage.json']:
        shutil.copyfile(OLD/name,OUT/name)


def report():
    from router_quality_maps import case_maps
    data = read(OUT/'routing-comparison.json')
    providers = data['providers']
    paired = [q for q in data['comparisons'] if q['osrm_status']==q['brouter_status']=='ok']
    distances = [q['distance_ratio'] for q in paired if q.get('distance_ratio')]
    durations = [q['duration_ratio'] for q in paired if q.get('duration_ratio')]
    shapes = [q['shape_fraction_within_200m'] for q in paired]
    full = data['full_mmap_search']
    baseline = data['previous_ram_search']
    current_route = read(OUT/'mmap-600s/route.json')
    release = read(OUT/'brouter-release.json')
    best_offsets = [current_route['locations'][s['location']]['road_access']['offset_meters']
                    for d in current_route['days'] for s in d['stops']]
    summary = {'paired_successes':len(paired),'median_distance_ratio':median(distances) if distances else None,
               'median_duration_ratio':median(durations) if durations else None,
               'median_shape_fraction_within_200m':median(shapes) if shapes else None,
               'over_20pct_distance_difference':sum(abs(x-1)>.2 for x in distances),
               'brouter_endpoint_offsets_over_100m':sum(max(q['brouter_endpoint_offsets_meters'])>100 for q in paired),
               'osrm_endpoint_offsets_over_100m':sum(q['osrm_status']=='ok' and max(q['osrm_endpoint_offsets_meters'])>100 for q in data['comparisons']),
               'full_route_max_pin_road_offset_m':max(best_offsets)}
    selected_audit=read(OUT/'selected-endpoint-audit.json') if (OUT/'selected-endpoint-audit.json').exists() else None
    if selected_audit:
        summary['selected_route_endpoint_audit']={k:v for k,v in selected_audit.items() if k!='queries'}
    compatible=[q for q in paired if max(q['brouter_endpoint_offsets_meters'])<=100
                and max(q['osrm_endpoint_offsets_meters'])<=100]
    summary['endpoints_within_100m']={'pairs':len(compatible),
         'median_distance_ratio':median(q['distance_ratio'] for q in compatible),
         'median_duration_ratio':median(q['duration_ratio'] for q in compatible),
         'median_shape_fraction_within_200m':median(q['shape_fraction_within_200m'] for q in compatible),
         'over_20pct_distance_difference':sum(abs(q['distance_ratio']-1)>.2 for q in compatible)}
    dump(OUT/'quality-summary.json',summary)
    rows=[]
    for name,value in providers.items():
        for trial in value['passes']:
            rows.append({'provider':name,'pass':trial['pass'],'queries':trial['total_queries'],
                'successful':trial['successful_queries'],'seconds':trial['request_seconds'],
                'median_success_ms':trial['median_success_seconds']*1000 if trial['median_success_seconds'] is not None else '',
                'p95_success_ms':trial['p95_success_seconds']*1000 if trial['p95_success_seconds'] is not None else '',
                'router_peak_rss_mib':value['router']['peak_rss_bytes']/2**20,
                'status_counts':json.dumps(trial['status_counts'],sort_keys=True)})
    write_csv(OUT/'run-summary.csv',rows,list(rows[0]))
    def num(value):
        return f'{value:,.2f}' if isinstance(value,(float,int)) else str(value)
    table=''.join('<tr>'+''.join('<td>'+escape(num(row[k]))+'</td>' for k in
              ['provider','pass','successful','seconds','median_success_ms','p95_success_ms','router_peak_rss_mib','status_counts'])+'</tr>' for row in rows)
    flags=sorted(paired,key=lambda q:abs(math.log(q['distance_ratio'] or 1)),reverse=True)[:12]
    cases=''.join('<tr>'+''.join('<td>'+escape(num(v))+'</td>' for v in [r['id'],', '.join(r['tags']),
                  r['osrm_miles'],r['brouter_miles'],r['distance_ratio'],r['duration_ratio'],
                  r['shape_fraction_within_200m']*100,max(r['brouter_endpoint_offsets_meters'])])+'</tr>' for r in flags)
    failure_rows=[q for q in data['comparisons'] if q['osrm_status']!='ok' or q['brouter_status']!='ok']
    failures=''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in [r['id'],', '.join(r['tags']),r['osrm_status'],r['brouter_status']])+'</tr>' for r in failure_rows)
    notes=''.join('<li>'+escape(s)+'</li>' for s in data['limitations'])
    maps=case_maps(OUT,ROOT/'web',data)
    strict_note=''
    osrm_strict_note=''
    if data.get('osrm_strict_matching_sensitivity'):
        sr=data['osrm_strict_matching_sensitivity']['queries']
        counts=Counter(r['strict']['status'] for r in sr)
        accepted=sum(r['strict']['status']=='ok' and max(r['strict']['endpoint_offsets_meters'])<=100 for r in sr)
        summary['osrm_strict_matching']={'cases':len(sr),'status_counts':dict(counts),'returned_routes_within_100m':accepted}
        shifted_count=sum(max(r['primary']['endpoint_offsets_meters'])>100 for r in sr)
        osrm_strict_note=f"<p><strong>OSRM radius follow-up:</strong> {len(sr)} targeted cases ({shifted_count} shifted queries plus controls) were repeated with radiuses=100;100. Statuses: {escape(json.dumps(dict(counts)))}; {accepted} returned routes ended within 100 m of both anchors. These diagnostic results do not change the frozen search or establish walking/legal access.</p>"
    if data.get('strict_matching_sensitivity'):
        strict_rows=data['strict_matching_sensitivity']['queries']
        strict_status=Counter(r['strict']['status'] for r in strict_rows)
        shifted=[r for r in strict_rows if r['primary']['status']=='ok' and max(r['primary']['endpoint_offsets_meters'])>100]
        strict_shift_ok=sum(r['strict']['status']=='ok' for r in shifted)
        strict_excess=[r['strict']['id'] for r in strict_rows if r['strict']['status']=='ok' and max(r['strict']['endpoint_offsets_meters'])>101]
        strict_accepted=sum(r['strict']['status']=='ok' and max(r['strict']['endpoint_offsets_meters'])<=100 for r in strict_rows)
        max_strict_shift=max((max(r['strict']['endpoint_offsets_meters']) for r in strict_rows if r['strict']['status']=='ok'),default=0)
        summary['strict_matching']={'cases':len(strict_rows),'status_counts':dict(strict_status),
            'previously_shifted_cases':len(shifted),'previously_shifted_returned':strict_shift_ok,
            'returned_routes_within_100m':strict_accepted,'nominal_limit_overshoots':strict_excess,
            'max_returned_endpoint_offset_meters':max_strict_shift}
        dump(OUT/'quality-summary.json',summary)
        strict_note=f"<p><strong>Strict endpoint follow-up:</strong> {len(strict_rows)} targeted cases were repeated with dynamic range disabled and a nominal 100 m waypoint-matching limit. Statuses: {escape(json.dumps(dict(strict_status)))}. Of {len(shifted)} previously shifted routes, {strict_shift_ok} still returned a route. Only {strict_accepted} of all {strict_status['ok']} returned routes ended within 100 m of both submitted anchors; the largest remaining shift was {max_strict_shift:.1f} m. An integration must independently check returned endpoints rather than trusting the configured limit. This is a matching diagnostic, not a verified walking allowance from the observation pin.</p>"
    audit_banner=''
    if selected_audit and not selected_audit['passes_100m_endpoint_check']:
        audit_banner=f"<div class='card warning'><h2>Endpoint audit failed: the species score is not a feasible-itinerary result.</h2><p>{selected_audit['shifted_pairs_over_100m']} of {selected_audit['unique_driving_pairs']} distinct selected OSRM road legs miss their submitted anchors by more than 100 m. The largest shift is {selected_audit['max_anchor_to_returned_road_meters']/1000:.1f} km. The original validator checks calendars, cached costs and source evidence; it does not reject this router rematching. Runtime and memory measurements remain valid, but endpoint enforcement and a new search are required before interpreting the species score or attempting nationwide optimization.</p></div>"
    retention=data['cohort']['retention']
    html=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>West Coast · OSRM mmap and BRouter comparison</title><style>
    :root{{font:16px/1.55 system-ui;color:#19271e;background:#f5f7f3}}body{{max-width:1180px;margin:40px auto;padding:0 24px}}
    h1{{font-size:2.4rem;line-height:1.15}}h2{{margin-top:0}}.eyebrow{{color:#36824b;font-weight:750;letter-spacing:.1em}}
    .card{{background:white;border:1px solid #dbe5da;border-radius:12px;padding:22px;margin:22px 0}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}}strong.big{{display:block;font-size:1.8rem}}
    .warning{{background:#fff4e6;border-color:#c78c3d}}.warning h2{{font-size:1.2rem}}
    table{{border-collapse:collapse;width:100%;font-size:.86rem}}td,th{{padding:9px;border-bottom:1px solid #e1e8dd;text-align:left;white-space:nowrap}}.scroll{{overflow:auto}}a{{color:#28613a}}li{{margin:10px 0}}
    .case-maps{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}}.case-maps article{{min-width:0}}.case-maps svg{{width:100%;border:1px solid #dbe5da;border-radius:6px}}.case-maps p{{font-size:.85rem}}.case-maps h3{{margin-bottom:0}}
    @media(max-width:700px){{.stats,.case-maps{{grid-template-columns:1fr}}h1{{font-size:2rem}}}}</style>
    <p class="eyebrow">EOD ROUTE LAB · ROUTING COMPARISON</p><h1>West Coast 2024<br>Memory-mapped OSRM and tiled BRouter.</h1>
    <p>All {retention['eligible_presence_groups']:,} eligible presence groups, representing {retention['source_records']:,} observations and {retention['exact_locations_retained']:,} exact locations, remain in the full search.
    The separate {data['queries']}-query replay samples road-provider behavior without reducing that cohort.</p>
    {audit_banner}
    <p><a href="/?run=mmap-600s/route.json">Inspect the diagnostic route (endpoint audit failed)</a> · <a href="/run-summary.csv">Download timing CSV</a></p>
    <div class="card stats"><div><strong class="big">{current_route['species_total']} / 570</strong>model score; endpoint audit failed</div>
    <div><strong class="big">{data['full_mmap_resources']['combined_peak_rss_bytes']/2**30:.2f} GiB</strong>sampled combined search RSS</div>
    <div><strong class="big">{data['identical_osrm_cost_queries']} / {data['queries']}</strong>OSRM loading-mode cost/status agreement</div></div>
    <div class="card"><h2>Full-year optimizer</h2><p>The new memory-mapped search took {full['wall_seconds']:.1f} seconds end to end, with {full['engine']['elapsed_seconds']:.1f} seconds in C++, {full['engine']['candidate_evaluations']:,} candidate evaluations, an 8 MiB disk cache and two native threads. All 366 days passed the original calendar, cached-cost and evidence checks; the added endpoint audit failed.
    The router process peaked at {data['full_mmap_router']['peak_rss_bytes']/2**30:.2f} GiB RSS.</p>
    <p>The earlier ordinary-RAM trial took {baseline['wall_seconds']:.1f} seconds and found {baseline['engine']['species']} species from {baseline['engine']['candidate_evaluations']:,} evaluations. Its previous multi-stage pipeline sampled {data['previous_ram_combined_resources']['sampled_combined_peak_rss_bytes']/2**30:.2f} GiB combined RSS.
    These are historical context, not a controlled speedup test: the old cache was warm, its engine build differs, and the stochastic candidate streams differ. The identical-query replay below isolates loading-mode cost agreement.</p>
    <p>Physical access remains separate: the new route's largest selected road-to-pin gap is {max(best_offsets)/1000:.2f} km, with no verified walking connection.</p></div>
    <div class="card"><h2>Matched routing replay</h2><p>One client sends the same {data['queries']} directed queries in the same order, twice per freshly started server. Latencies below include full geometry responses. Repeat 2 warms the process and its data cache; the operating system's disk cache is never forcibly cleared.</p>
    <div class="scroll"><table><thead><tr><th>Provider</th><th>Pass</th><th>Routes returned</th><th>Request sec</th><th>Median ms*</th><th>P95 ms*</th><th>Router MiB</th><th>Statuses</th></tr></thead><tbody>{table}</tbody></table></div>
    <p>*Latency quantiles include successful queries. Request seconds include HTTP transfer and JSON parsing for every request, timeout, unreachable route and error; subsequent client quality calculations and artifact compression are excluded. Batch wall times are saved separately. Router RSS is the peak across both passes, repeated in each row. Different statuses are kept distinct. OSRM costs-only replay results are saved separately.</p></div>
    <div class="card"><h2>Routing agreement and cases for review</h2><p>Both providers returned routes for {len(paired)} queries. Of these, {len(compatible)} returned routes with both endpoints within 100 m of the submitted anchors. Within that compatible-endpoint subset, the median BRouter/OSRM distance ratio was {num(summary['endpoints_within_100m']['median_distance_ratio'])}, median duration ratio {num(summary['endpoints_within_100m']['median_duration_ratio'])}, and median symmetric path coverage within approximately 200 m was {100*summary['endpoints_within_100m']['median_shape_fraction_within_200m']:.1f}%.
    {summary['endpoints_within_100m']['over_20pct_distance_difference']} compatible-endpoint routes differed by more than 20% in distance. Endpoint shifts over 100 m affected {summary['brouter_endpoint_offsets_over_100m']} of 109 BRouter and {summary['osrm_endpoint_offsets_over_100m']} of 125 OSRM returned routes, with overlap between these sets. Shifted distance ratios cannot be interpreted as travel savings between equivalent endpoints.</p>
    <p>This measures agreement, not true driving accuracy. BRouter uses its frozen current weekly tiles and the bundled car-vario profile with vmax=130 km/h; OSRM uses the January 2025 western graph and its existing car profile. Costs, ferries and shapes can differ for both reasons. BRouter default-90 km/h sensitivity results are saved separately.</p>{strict_note}{osrm_strict_note}
    <div class="scroll"><table><thead><tr><th>Query</th><th>Tags</th><th>OSRM miles</th><th>BRouter miles</th><th>Distance ratio</th><th>Time ratio</th><th>Path coverage %</th><th>BRouter endpoint shift m</th></tr></thead><tbody>{cases}</tbody></table></div></div>
    <div class="card"><h2>Inspect saved driving paths</h2><p><strong style="color:#36824b">Green: OSRM</strong> · <strong style="color:#416caa">Blue: BRouter</strong>. A and B mark the identical submitted road anchors. A returned path that does not reach those markers shows a matching discrepancy. Its distance ratio is not a comparison between equivalent endpoints. These overview drawings simplify redundant vertices for display; full measured responses remain saved. Observation pins and modeled costs are unchanged. Background boundaries: Natural Earth, public domain.</p>{maps}</div>
    <div class="card"><h2>Reachability and failures</h2><div class="scroll"><table><thead><tr><th>Query</th><th>Tags</th><th>OSRM</th><th>BRouter</th></tr></thead><tbody>{failures}</tbody></table></div>
    <p>Island-origin cases test reachability and ferry handling; supplied road anchors or modeled ferries can connect them. A timeout is a failed request, not evidence that a road does not exist. No fallback flight, beeline or offroad mode is enabled.</p></div>
    <div class="card"><h2>Storage, provenance and limits</h2><p>BRouter uses {len(release['tiles'])} pinned western tiles occupying {release['tile_bytes']/2**30:.3f} GiB. The source archive and prepared OSRM graph are reused. Downloads use checked staging files; the Java download archive and temporary compiled classes were removed after successful installation. Completed routes and compressed diagnostic responses remain intentionally for review.</p>
    <p>Sources: <a href="https://project-osrm.org/docs/v26.4.0/tools">OSRM tools</a> · <a href="https://github.com/abrensch/brouter">BRouter</a> · <a href="https://brouter.de/brouter/segments4/">BRouter road tiles</a>.</p><ul>{notes}</ul></div></html>'''
    (OUT/'pilot-report.html').write_text(html)
    print(json.dumps(summary),flush=True)


def cleanup():
    old=WORK/'superseded-timing-v0'
    reclaimed=0
    if old.exists():
        for provider in ['osrm-mmap','osrm-ram']:
            first=read(old/provider/'replay.json')['passes'][0]['queries']
            second=read(OUT/provider/'replay.json')['passes'][0]['queries']
            assert len(first)==len(second)
            for a,b in zip(first,second):
                assert a['id']==b['id'] and a['status']==b['status']
                if a['status']=='ok':
                    assert abs(a['distance_meters']-b['distance_meters'])<.11
                    assert abs(a['duration_seconds']-b['duration_seconds'])<.11
        reclaimed=sum(p.stat().st_blocks*512 for p in old.rglob('*') if p.is_file())
        shutil.rmtree(old)
    trimmed=[]
    for path in OUT.glob('*.log'):
        if path.stat().st_size>256*1024:
            with path.open('rb') as stream:
                stream.seek(-256*1024,os.SEEK_END)
                tail=stream.read()
            path.write_bytes(tail)
            trimmed.append(str(path))
    assert not list(WORK.rglob('*.part'))
    assert not list((ROOT/'build/query-spill').glob('*'))
    files={}
    shared_model=(DATA/'problem.json').stat()
    existing={(shared_model.st_dev,shared_model.st_ino)}
    for folder in [WORK,OUT,ROOT/'build/brouter-benchmark']:
        for path in folder.rglob('*'):
            if path.is_file():
                s=path.stat()
                if (s.st_dev,s.st_ino) not in existing:
                    files[s.st_dev,s.st_ino]=s.st_blocks*512
    dump(OUT/'cleanup.json',{'superseded_timing_reclaimed_bytes':reclaimed,'trimmed_logs':trimmed,
         'additional_nonshared_allocated_bytes':sum(files.values()),'free_bytes':shutil.disk_usage(WORK).free,
         'policy':'Original observations, existing road graph and immutable earlier runs retained; source model shared by hard link. New results, compressed road responses, portable Java and pinned tiles retained. Verified superseded timing staging removed; no query spill or partial downloads remain.'})


if __name__ == '__main__':
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['full-search','sample','osrm-mmap','osrm-ram','brouter','analyze','display','report','strict','strict-osrm','endpoint-audit','cleanup'])
    args = parser.parse_args()
    if args.stage == 'full-search':
        full_search()
    elif args.stage == 'sample':
        make_sample()
    elif args.stage == 'analyze':
        analyze()
    elif args.stage == 'display':
        display()
    elif args.stage == 'report':
        report()
    elif args.stage == 'strict':
        strict_sensitivity()
    elif args.stage == 'cleanup':
        cleanup()
    elif args.stage == 'strict-osrm':
        strict_osrm()
    elif args.stage == 'endpoint-audit':
        audit_selected_endpoints()
    else:
        replay(args.stage)
