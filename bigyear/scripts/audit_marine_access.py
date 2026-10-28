"""Audit selected land walks against frozen OSM coastline; keep boats explicit."""
from pathlib import Path

import shapefile
from shapely.geometry import shape,box
from shapely.ops import unary_union

from bigyear_planner.surface_access import audit_walk
from bigyear_planner.util import ROOT,read,dump,sha256,write_csv

OUT=ROOT/'runs/lower48-eod-2024/search-calendar-controls-v2/rules-access'
ROUTE=ROOT/'runs/lower48-eod-2024/aba2024-aligned-flights-900s-600s/route.json'
SHAPE=ROOT/'build/land-mask/land-polygons-split-4326/land_polygons'


def main(route_path=ROUTE,output=OUT):
    route=read(route_path);results=[];cfg=route['config'];output=Path(output)
    for loc in route['locations']:
        if loc.get('kind')=='airport' or loc.get('road_access',{}).get('offset_meters',0)<=cfg['walking_threshold_m']:continue
        anchor=loc['road_access']
        xmin,xmax=sorted([loc['lon'],anchor['lon']]);ymin,ymax=sorted([loc['lat'],anchor['lat']])
        bounds=(xmin-1e-6,ymin-1e-6,xmax+1e-6,ymax+1e-6)
        with shapefile.Reader(shp=str(SHAPE)+'.shp',shx=str(SHAPE)+'.shx') as reader:
            clip=box(*bounds)
            land=unary_union([shape(s.__geo_interface__).intersection(clip) for s in reader.iterShapes(bbox=bounds)])
        results.append({'location_id':loc['id'],'state':loc['state'],**audit_walk(loc,cfg,land)})
    output.mkdir(parents=True,exist_ok=True)
    if results:write_csv(output/'selected-walks.csv',results,list(results[0]))
    summary={'route_sha256':sha256(route_path),
      'coastline_sha256':sha256(Path(str(SHAPE)+'.shp')),'walks_checked':len(results),
      'water_crossing_holds':sum(r['status']=='marine_access_required' for r in results),
      'boat_routes_implemented':False,
      'record_rule':'Lower-48 nearest-land waters to 200 miles or jurisdiction midpoint; marine/island observations can count.',
      'model_gap':'Census state polygon + land mask + longitude -125 bound omit marine candidates before routing. Removing only the land mask does not restore the complete marine cohort.',
      'not_inferred':['No walking across open sea','No boat distance treated as a measured road',
        'No 2026 ferry schedule treated as 2024 operation','No private-island permission assumed'],
      'dated_services':[{'operator':'Island Packers','evidence':'https://home.nps.gov/chis/learn/management/visitor-services-list.htm',
        'page_date':'2024-10-04','status':'Concessionaire documented; exact operated sailing still required for dated search block.'}],
      'pelagic_duration_rule':'Full trip duration counts; a 14-hour pelagic trip fails the current 12-hour activity cap.'}
    dump(output/'marine-access-audit.json',summary)
    print({'walks_checked':len(results),'water_holds':sum(r['status']=='marine_access_required' for r in results)},flush=True)
    return summary


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--route',type=Path,default=ROUTE);parser.add_argument('--output',type=Path,default=OUT)
    args=parser.parse_args();main(args.route,args.output)
