"""Bounded exact-pin charter construction and independent marine checks.

Charter availability, weather and a 12-knot cruise are scenario assumptions.
Coastline clearance is a geographic check, not a bathymetric navigation chart.
An observation is earned only at its dated source coordinate, never by radius.
"""
from collections import OrderedDict
import heapq
import math
from pathlib import Path
import time

import numpy as np
from shapely.geometry import Point, LineString, box, shape
from shapely.ops import nearest_points, unary_union
from shapely.prepared import prep

from .road_matching import gap_m
from .util import read
from .coverage import daily_species_counts


def length_m(coords):
    return sum(gap_m(a, b) for a, b in zip(coords, coords[1:]))


def observer_coverage(parent_meta,catalog):
    """A labeled union, without claiming complete marine geographic coverage."""
    first={r['species']:r['date'] for r in parent_meta['first_seen']}
    for trip in catalog['trips']:
        for observation in trip['observations']:
            for species in observation['species']:
                first[species]=min(first.get(species,trip['date']),trip['date'])
    rows=[{'species_id':s,'first_date':d} for s,d in sorted(first.items())]
    return {'year':parent_meta['config']['year'],'state':None,
        'states':parent_meta['candidate_scope']['states'],
        'scope_label':'Lower 48 land cohort + constructed marine charters (partial marine coverage)',
        'data_release':parent_meta['manifest']['release'],
        'taxonomy_policy':parent_meta['manifest']['taxonomy_policy'],'synthetic':False,
        'annual_species':len(first),
        'method':'All eligible terrestrial observations plus exact dated pins in constructed, geography-checked charters. Marine construction targets species absent from the fixed incumbent; the ocean cohort is incomplete. Counts cover the whole selected day; observation times are unavailable.',
        'first_seen':rows,'days':daily_species_counts(rows,parent_meta['config']['year'])}


class Jurisdiction:
    """Conservative nearest-land test with a 5-km country-coast uncertainty hold.

    A nearest US point supplies an upper bound on US distance. A local angular
    distance times the smallest meridional/parallel scale in the bounding box
    supplies a conservative lower bound on foreign distance. Ambiguous borders
    are held, rather than assigning jurisdiction from publisher state labels.
    """
    def __init__(self, path):
        features = read(path)['features']
        self.us = unary_union([shape(f['geometry']).intersection(box(-130, 20, -60, 52))
            for f in features if f['properties']['ADMIN']=='United States of America'])
        self.foreign = [shape(f['geometry']) for f in features
                        if f['properties']['ADMIN']!='United States of America'
                        and shape(f['geometry']).intersects(box(-135, 15, -55, 57))]
        self.cache = OrderedDict()

    def check(self, lat, lon):
        p = Point(lon, lat)
        if not (20 <= lat <= 52 and -135 <= lon <= -60):
            return {'status':'outside_lower48_marine_bounds'}
        nearest = nearest_points(p, self.us)[1]
        us = gap_m([lon,lat], [nearest.x,nearest.y])
        # A 6-degree box contains any closer foreign point for a US candidate
        # within 200 statute miles in this latitude range.
        key = (math.floor(lat), math.floor(lon))
        if key not in self.cache:
            bounds = box(key[1]-6, key[0]-6, key[1]+7, key[0]+7)
            self.cache[key] = unary_union([g.intersection(bounds) for g in self.foreign if g.intersects(bounds)])
            while len(self.cache)>24:self.cache.popitem(last=False)
        foreign = self.cache[key]
        lower = (foreign.distance(p)*110_000*math.cos(math.radians(min(89,abs(lat)+7)))
                 if not foreign.is_empty else math.inf)
        status = ('verified_lower48_waters' if us+5000 < min(200*1609.344,lower)
                  else 'jurisdiction_boundary_hold')
        return {'status':status,'us_distance_upper_m':us,
                'foreign_distance_lower_m':lower if math.isfinite(lower) else None,
                'coast_uncertainty_margin_m':5000}


class Coastline:
    """Read a bounded spatial window from the frozen split OSM land shapefile."""
    def __init__(self, stem, center, radius_deg=1.8):
        import shapefile
        self.stem = Path(stem)
        lon,lat = center
        self.bounds = (lon-radius_deg/math.cos(math.radians(lat)),lat-radius_deg,
                       lon+radius_deg/math.cos(math.radians(lat)),lat+radius_deg)
        clip = box(*self.bounds)
        with shapefile.Reader(shp=str(stem)+'.shp',shx=str(stem)+'.shx') as reader:
            polygons = [shape(s.__geo_interface__).intersection(clip)
                        for s in reader.iterShapes(bbox=self.bounds)]
        self.land = unary_union(polygons)
        self.prepared = prep(self.land)
        self.memo = OrderedDict()

    def water(self, point):
        x,y=point;left,bottom,right,top=self.bounds
        return left<=x<=right and bottom<=y<=top and not self.prepared.intersects(Point(point))

    def clear(self, coords):
        left,bottom,right,top=self.bounds
        return (all(left<=x<=right and bottom<=y<=top for x,y in coords)
                and not self.prepared.intersects(LineString(coords)))

    def departure(self, approximate):
        """Choose a modeled water-side boarding point near the real marina.

        This is not an observation coordinate and is labeled modeled. No source
        pin is ever moved. A road anchor and the entire dock walk are separate.
        """
        p = Point(approximate)
        edge = nearest_points(p,self.land.boundary)[1]
        candidates=[]
        for angle in np.linspace(0,2*math.pi,32,endpoint=False):
            point=[edge.x+.00015*math.cos(angle),edge.y+.00015*math.sin(angle)]
            if self.water(point):candidates.append((gap_m(approximate,point),point))
        if not candidates:raise ValueError('no water-side marina boarding point')
        distance,point=min(candidates)
        if distance>600:raise ValueError('modeled departure lies too far from the marina reference')
        return point

    def route(self, a, b, max_m=220_000):
        """Exact endpoints; clear direct segments or bounded A* water detours.

        Grid coordinates are routing waypoints only. They never coarsen the
        source catalog or the observation endpoints. Every returned segment is
        checked against the original, unsimplified shoreline.
        """
        key=(tuple(a),tuple(b))
        if key in self.memo:return self.memo[key]
        if not self.water(a) or not self.water(b):return None
        if self.clear([a,b]):result=[list(a),list(b)]
        else:
            # Approximately 220 m grid resolves harbor mouths. The 10 km
            # margin allows headland detours; an unresolved search is held.
            step_y=.002;step_x=step_y/math.cos(math.radians((a[1]+b[1])/2))
            origin=(min(a[0],b[0])-.10,min(a[1],b[1])-.10)
            nx=math.ceil((max(a[0],b[0])+.10-origin[0])/step_x)
            ny=math.ceil((max(a[1],b[1])+.10-origin[1])/step_y)
            if nx*ny>220_000:return None
            def point(node):return [origin[0]+node[0]*step_x,origin[1]+node[1]*step_y]
            def neighbors_pin(pin):
                i=round((pin[0]-origin[0])/step_x);j=round((pin[1]-origin[1])/step_y)
                choices=[]
                for x in range(max(0,i-3),min(nx,i+3)+1):
                    for y in range(max(0,j-3),min(ny,j+3)+1):
                        p=point((x,y))
                        if self.clear([pin,p]):choices.append((gap_m(pin,p),(x,y)))
                return sorted(choices)[:8]
            starts,ends=neighbors_pin(a),dict((n,c) for c,n in neighbors_pin(b))
            costs={n:c for c,n in starts};parent={n:None for _,n in starts}
            todo=[(c+gap_m(point(n),b),c,n) for c,n in starts];heapq.heapify(todo)
            found=None;visited=0;started=time.monotonic()
            while todo and visited<40_000:
                if visited%128==0 and time.monotonic()-started>1.5:return None
                _,cost,node=heapq.heappop(todo)
                if cost!=costs[node]:continue
                visited+=1
                if node in ends:found=node;break
                for dx,dy in [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
                    nxt=(node[0]+dx,node[1]+dy)
                    if not (0<=nxt[0]<=nx and 0<=nxt[1]<=ny):continue
                    p,q=point(node),point(nxt);new=cost+gap_m(p,q)
                    if new>=costs.get(nxt,math.inf) or new+gap_m(q,b)>max_m:continue
                    if not self.clear([p,q]):continue
                    costs[nxt]=new;parent[nxt]=node
                    heapq.heappush(todo,(new+gap_m(q,b),new,nxt))
            if found is None:return None
            nodes=[]
            while found is not None:nodes.append(point(found));found=parent[found]
            raw=[list(a),*reversed(nodes),list(b)]
            result=[raw[0]];i=0
            while i<len(raw)-1:
                j=len(raw)-1
                while j>i+1 and not self.clear([raw[i],raw[j]]):j-=1
                result.append(raw[j]);i=j
        if length_m(result)>max_m or not self.clear(result):return None
        self.memo[key]=result
        self.memo[(tuple(b),tuple(a))]=list(reversed(result))
        while len(self.memo)>4096:self.memo.popitem(last=False)
        return result


def charter_track(coast, port, pins, config, speed_knots=12, boarding_minutes=60):
    """Visit all exact pins in order, returning to port within the activity cap."""
    if not math.isfinite(speed_knots) or speed_knots<=0:raise ValueError('positive vessel speed required')
    vertices=[];legs=[];here=port
    for pin in [*pins,port]:
        coords=coast.route(here,pin)
        if coords is None:return None
        meters=length_m(coords)
        legs.append({'geometry':{'type':'LineString','coordinates':coords},'meters':meters,
                     'minutes':meters/(speed_knots*1852)*60})
        vertices.extend(coords if not vertices else coords[1:]);here=pin
    sailing=sum(x['minutes'] for x in legs)
    birding=config['birding_minutes']*len(pins)
    if sailing+birding+boarding_minutes>config['activity_minutes']+1e-7:return None
    return {'legs':legs,'geometry':{'type':'LineString','coordinates':vertices},
            'sailing_minutes':sailing,'birding_minutes':birding,'boarding_minutes':boarding_minutes,
            'activity_minutes':sailing+birding+boarding_minutes,
            'nautical_miles':sum(x['meters'] for x in legs)/1852,'speed_knots':speed_knots}
