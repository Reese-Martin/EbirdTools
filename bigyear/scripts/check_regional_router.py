"""Real car routing checks spanning local, tile-boundary and continental paths."""
import json
import time
import urllib.request,urllib.error,urllib.parse
from bigyear_planner.regional_roads import normalize_route
from bigyear_planner.road_matching import gap_m
from bigyear_planner.util import ROOT,read,dump
r=read(ROOT/'data/lower48-eod-2024/roads/release.json')
base={'profile':'car-vario','alternativeidx':0,'format':'geojson',**{'profile:'+k:str(v) for k,v in r['parameters'].items() if k!='profile'}}

def request(coords,match=False):
    params={**base,'lonlats':'|'.join(','.join(map(str,p)) for p in coords),'profile:waypointCatchingRange':1000 if match else 1}
    if match:params['engineMode']=5
    with urllib.request.urlopen('http://127.0.0.1:17779/brouter?'+urllib.parse.urlencode(params),timeout=45) as f:return json.load(f)

points={'Tuscaloosa':[-87.5692,33.2098],'Birmingham':[-86.8025,33.5207],
        'San Francisco':[-122.4194,37.7749],'Sacramento':[-121.4944,38.5816],
        'Washington DC':[-77.0365,38.8977],'New York':[-74.006,40.7128],
        'Chicago':[-87.6298,41.8781],'Denver':[-104.9903,39.7392],
        'Kansas City':[-94.5786,39.0997]}
anchors={k:request([p],True)['anchors'][0] for k,p in points.items()}
results=[]
for a,b in [('Tuscaloosa','Birmingham'),('San Francisco','Sacramento'),('Washington DC','New York'),
            ('Chicago','Kansas City'),('Denver','Kansas City'),('Tuscaloosa','Washington DC'),
            ('San Francisco','New York')]:
    started=time.monotonic();record={'from':a,'to':b,'source_pins':[points[a],points[b]],'anchors':[anchors[a],anchors[b]]}
    try:
        raw=request(record['anchors']);data=normalize_route(raw,record['anchors']);route=data['routes'][0]
        record.update(status='ok',miles=route['distance']/1609.344,minutes=route['duration']/60,
            max_endpoint_gap_m=max(gap_m(p,v) for p,v in zip(record['anchors'],[route['geometry']['coordinates'][0],route['geometry']['coordinates'][-1]])),
            vertices=len(route['geometry']['coordinates']))
    except Exception as e:record.update(status='deferred',error=str(e),body=getattr(e,'read',lambda:b'')().decode()[:600])
    record['seconds']=time.monotonic()-started;results.append(record);print(json.dumps(record),flush=True)
    dump(ROOT/'runs/lower48-eod-2024/regional-routing-checks.json',{'provider_release_sha256':__import__('bigyear_planner.util',fromlist=['sha256']).sha256(ROOT/'data/lower48-eod-2024/roads/release.json'),'queries':results})
