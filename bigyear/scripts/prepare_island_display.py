"""Display-only island outline; never changes observation or routing geometry."""
from shapely.geometry import Point,mapping

from bigyear_planner.marine import Coastline
from bigyear_planner.util import ROOT,dump,read,sha256


def main():
    catalog=read(ROOT/'data/calendar-strategy-study-2024-v1/landing-candidates.json')
    stem=ROOT/'build/land-mask/land-polygons-split-4326/land_polygons'
    if sha256(stem.with_suffix('.shp'))!=catalog['osm_land_sha256']:raise ValueError('display coast release differs')
    coast=Coastline(stem,[-119.55,34.13],radius_deg=.5)
    parts=list(coast.land.geoms) if hasattr(coast.land,'geoms') else [coast.land]
    island=next(p for p in parts if p.covers(Point(-119.56158,34.04788)))
    target=ROOT/'runs/lower48-eod-2024/map-context.geojson'
    context=read(target) if target.exists() else {'type':'FeatureCollection','features':[]}
    context['features']=[f for f in context['features'] if f.get('properties',{}).get('role')!='island_coast']
    context['features'].append({'type':'Feature','properties':{'name':'Santa Cruz Island',
        'role':'island_coast','source':'© OpenStreetMap contributors','display_only':True,
        'source_sha256':catalog['osm_land_sha256'],'display_simplification_degrees':.0001},
        'geometry':mapping(island.simplify(.0001,preserve_topology=True))})
    dump(target,context);print({'display_only':True,'path':str(target),'bytes':target.stat().st_size})


if __name__=='__main__':main()
