"""Small saved-geometry inspection maps; simplification affects display only."""
import gzip
import json
import math
from html import escape

from shapely.geometry import LineString


def case_maps(root, web, comparison):
    sample=json.loads((root/'road-sample.json').read_text())
    inputs={q['id']:q for q in sample['queries']}
    paired=[q for q in comparison['comparisons'] if q.get('distance_ratio')]
    compatible=[q for q in paired if max(q['brouter_endpoint_offsets_meters']+q['osrm_endpoint_offsets_meters'])<=100]
    shifted=[q for q in paired if max(q['brouter_endpoint_offsets_meters']+q['osrm_endpoint_offsets_meters'])>100]
    chosen=sorted(compatible,key=lambda q:abs(math.log(q['distance_ratio'])),reverse=True)[:2]
    chosen+=sorted(shifted,key=lambda q:abs(math.log(q['distance_ratio'])),reverse=True)[:2]
    for row in paired:
        if 'selected-ferry' in row['tags'] and row not in chosen:
            chosen.append(row)
            if len(chosen)>=6:
                break
    states=json.loads((web/'states.geojson').read_text())
    def coordinates(provider, key):
        with gzip.open(root/provider/(key.replace(':','-')+'.json.gz'),'rt') as stream:
            data=json.load(stream)
        if provider.startswith('osrm'):
            return data['routes'][0]['geometry']['coordinates']
        return next(f['geometry']['coordinates'] for f in data['features']
                    if f.get('geometry',{}).get('type')=='LineString')
    cards=[]
    for index,row in enumerate(chosen):
        a=coordinates('osrm-mmap',row['id']);b=coordinates('brouter',row['id'])
        points=a+b
        lat=sum(p[1] for p in points)/len(points)
        scale=math.cos(math.radians(lat))
        xmin,xmax=min(p[0]*scale for p in points),max(p[0]*scale for p in points)
        ymin,ymax=min(p[1] for p in points),max(p[1] for p in points)
        factor=min(550/max(xmax-xmin,.001),320/max(ymax-ymin,.001))
        def xy(p):
            return (300+(p[0]*scale-(xmin+xmax)/2)*factor,
                    185-(p[1]-(ymin+ymax)/2)*factor)
        def path(points):
            return ' '.join(('M' if i==0 else 'L')+f'{xy(p)[0]:.2f},{xy(p)[1]:.2f}'
                            for i,p in enumerate(points))
        background=[]
        for state in states['features']:
            geom=state['geometry'];polys=geom['coordinates'] if geom['type']=='MultiPolygon' else [geom['coordinates']]
            for poly in polys:
                for ring in poly:
                    background.append('<path d="'+path(ring)+' Z"/>')
        # Full source responses remain compressed on disk. Overview drawings
        # remove only redundant vertices to keep the report small and readable.
        a=list(LineString([p[:2] for p in a]).simplify(.0004).coords)
        b=list(LineString([p[:2] for p in b]).simplify(.0004).coords)
        pins=''
        for label,pin in zip(('A','B'),inputs[row['id']]['anchors']):
            x,y=xy([pin['lon'],pin['lat']])
            pins+=f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="#111" stroke="white" stroke-width="2"/><text x="{x+7:.2f}" y="{y-7:.2f}">{label}</text>'
        svg=f'''<svg viewBox="0 0 600 370" role="img" aria-label="OSRM and BRouter road paths for query {row['id']}">
        <defs><clipPath id="case-{index}"><rect x="0" y="0" width="600" height="370"/></clipPath></defs>
        <rect width="600" height="370" fill="#edf3f5"/><g clip-path="url(#case-{index})"><g fill="#fafcf8" stroke="#cdd6cc" stroke-width=".8">{''.join(background)}</g>
        <path d="{path(a)}" fill="none" stroke="#36824b" stroke-width="3.5"/>
        <path d="{path(b)}" fill="none" stroke="#416caa" stroke-width="2"/>{pins}</g></svg>'''
        endpoint_shift=max(row['brouter_endpoint_offsets_meters']+row['osrm_endpoint_offsets_meters'])
        match_note=f'Endpoint mismatch: up to {endpoint_shift:,.0f} m shift' if endpoint_shift>100 else 'Both providers: endpoints within 100 m'
        cards.append(f'''<article><h3>{escape(row['id'])} · {row['distance_ratio']:.2f}× distance</h3>
        <p>{escape(', '.join(row['tags']))} · <strong>{match_note}</strong></p>{svg}<p>OSRM {row['osrm_miles']:.1f} mi / {row['osrm_minutes']:.1f} min · BRouter {row['brouter_miles']:.1f} mi / {row['brouter_minutes']:.1f} min</p></article>''')
    return '<div class="case-maps">'+''.join(cards)+'</div>'
