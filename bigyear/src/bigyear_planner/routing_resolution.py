"""Validated alternate access probes and explicit unchanged-anchor cache migration."""
import math
from pathlib import Path

import numpy as np

from .bounded_roads import HEADER,SLOT
from .joint_marine import JointRoads
from .marine_roads import PORT_POLICY
from .regional_roads import DeferredRoad
from .road_matching import gap_m
from .util import dump,read,sha256


FALLBACK_POLICY={'id':'unresolved-local-car-probe-fallback-v1',
    'directions':['north','west','south'],'offset_degrees':.02,
    'eligibility':'Only after the legacy eastward probe defers; finite cached anchors and explicit access holds remain unchanged.',
    'anchor':'Actual start of a complete car-profile path; closest successful start at the first successful radius.',
    'walking':'Original exact source coordinate, full round trip above100m at2.2km/h; independent selected-walk audit.'}
RESOLVED_POLICY={**PORT_POLICY,'unresolved_local_access':FALLBACK_POLICY}
RESOLVED_POLICY['airport_public_approach']={'id':'airport-offset-public-car-approach-v1',
    'eligibility':'Airport centroid local probes all deferred; observation locations are never offset.',
    'rings_degrees':[.01,.02,.04],'directions':['north','east','south','west'],
    'selection':'Closest actual start of complete local car paths at the first successful ring; within original maximum access radius.',
    'source_coordinates':'Frozen airport identity/centroid remains unchanged; road_access stores the distinct actual car endpoint.'}


class ResolvedJointRoads(JointRoads):
    def __init__(self,*args,**kwargs):
        self.access_migration=None
        kwargs['access_policy_extension']=RESOLVED_POLICY
        super().__init__(*args,**kwargs)
        migration=self.work/'access-cache-migration.json'
        if migration.exists():self.access_migration=read(migration)
        self.airport_pair_limit=6
        self.airport_shortlist_count=24
        self.transition_discovery_seconds=150

    def nearest_airports(self,location,count=24):
        # Rural targets can have eight closer regional airfields without any
        # useful nonstop service. Include larger gateways; measured transfers
        # and dated service still decide feasibility.
        return super().nearest_airports(location,count)

    def access(self,index):
        try:return super().access(index)
        except DeferredRoad:pass
        with self.locks[index%len(self.locks)]:
            value=self.anchors[index]
            if not math.isnan(float(value[2])):return tuple(map(float,value))
            lat,lon=map(float,self.model.coords[index]);cfg=self.model.meta['config']
            maximum=(cfg['activity_minutes']-cfg['birding_minutes'])*cfg['walking_speed_kmh']*1000/120
            for radius in [min(1000,maximum+1),maximum+1]:
                choices=[]
                for dx,dy in [(0,.02),(-.02,0),(0,-.02)]:
                    try:
                        raw=self.request([[lon,lat],[lon+dx,lat+dy]],radius=radius)
                        paths=[f for f in raw.get('features',[]) if f.get('geometry',{}).get('type')=='LineString']
                        if len(paths)!=1:continue
                        points=paths[0]['geometry']['coordinates']
                        if len(points)<2:continue
                        for point in points:gap_m(point[:2],point[:2])
                        p=points[0][:2];gap=gap_m([lon,lat],p)
                        if gap<=radius+1:choices.append((gap,p))
                    except (DeferredRoad,ValueError):continue
                if choices:
                    gap,p=min(choices)
                    walk=2*gap/(cfg['walking_speed_kmh']*1000)*60 if gap>cfg['walking_threshold_m'] else 0
                    self.anchors[index]=[p[1],p[0],cfg['birding_minutes']+walk]
                    self.stats['resolved_alternate_local_access']+=1
                    return p[1],p[0],cfg['birding_minutes']+walk
            if self.model.locations[index].get('kind')=='airport':
                for ring in [.01,.02,.04]:
                    choices=[]
                    for dx,dy in [(0,ring),(ring,0),(0,-ring),(-ring,0)]:
                        origin=[lon+dx,lat+dy]
                        destination=[origin[0]+(.02 if dy else 0),origin[1]+(.02 if dx else 0)]
                        try:
                            raw=self.request([origin,destination],radius=1000)
                            paths=[f for f in raw.get('features',[]) if f.get('geometry',{}).get('type')=='LineString']
                            if len(paths)!=1:continue
                            points=paths[0]['geometry']['coordinates']
                            if len(points)<2:continue
                            for point in points:gap_m(point[:2],point[:2])
                            p=points[0][:2];gap=gap_m([lon,lat],p)
                            if gap<=maximum and gap_m(origin,p)<=1001:choices.append((gap,p))
                        except (DeferredRoad,ValueError):continue
                    if choices:
                        gap,p=min(choices);walk=2*gap/(cfg['walking_speed_kmh']*1000)*60 if gap>cfg['walking_threshold_m'] else 0
                        self.anchors[index]=[p[1],p[0],cfg['birding_minutes']+walk]
                        self.stats['resolved_airport_public_approach']+=1
                        return p[1],p[0],cfg['birding_minutes']+walk
            raise DeferredRoad('all validated local probe directions unresolved; no unreachable cost cached')

    def migrate_cache(self,directory):
        directory=Path(directory);old=read(directory/'provider.json');new=self.manifest
        for key in ['model_sha256','road_release_sha256','matching_policy','additional_locations']:
            if old.get(key)!=new.get(key):raise ValueError('access migration physical input changed: '+key)
        before=dict(old['access_policy']);after=dict(new['access_policy'])
        if before.pop('extension')!=PORT_POLICY or after.pop('extension')!=RESOLVED_POLICY or before!=after:
            raise ValueError('access migration is not the declared unresolved-probe extension')
        source=np.memmap(directory/'access.bin',mode='r',dtype='<f8',shape=self.anchors.shape)
        # Preserve every already measured finite anchor exactly. No invalid
        # route, timeout, NaN or infinite cost is promoted to a successful edge.
        for lo in range(0,len(source),100000):
            hi=min(len(source),lo+100000);block=source[lo:hi];mask=np.isfinite(block[:,2])
            self.anchors[lo:hi][mask]=block[mask]
        carried=0
        with (directory/'costs.bin').open('rb') as stream:
            stream.seek(HEADER)
            while raw:=stream.read(SLOT.size):
                a,b,m,t,stamp,checksum=SLOT.unpack(raw)
                if (stamp and a<len(source) and b<len(source) and math.isfinite(m) and math.isfinite(t)
                    and checksum==self.store.checksum(a,b,m,t)
                    and np.isfinite(source[[a,b],2]).all()
                    and np.array_equal(source[[a,b]],self.anchors[[a,b]])):
                    self.store.put(a,b,(m,t));carried+=1
        self.anchors.flush();source._mmap.close()
        self.access_migration={'old_provider':old,'old_provider_sha256':sha256(directory/'provider.json'),
            'old_anchor_cache_sha256':sha256(directory/'access.bin'),'old_cost_cache_sha256':sha256(directory/'costs.bin'),
            'new_network_sha256':new['network_sha256'],'old_network_sha256':old['network_sha256'],
            'unchanged_anchor_costs_carried':carried,'old_endpoint_proofs_relabelled':False,
            'selected_road_proofs':'Fresh measured car paths required under the new binding; no old endpoint proof is imported.'}
        dump(self.work/'access-cache-migration.json',self.access_migration)
        return self.access_migration

    def selection(self,problem):
        super().selection(problem)
        if self.access_migration:
            problem['joint_marine']['access_resolution']={'policy':RESOLVED_POLICY,
                'migration_path':str((self.work/'access-cache-migration.json').resolve()),
                'migration_sha256':sha256(self.work/'access-cache-migration.json')}

    def location(self,index):
        loc=super().location(index)
        loc['road_access']['matching_policy']='resolved-local-car-access-v1; retained validated anchor or complete alternate car path; source coordinate unchanged'
        return loc
