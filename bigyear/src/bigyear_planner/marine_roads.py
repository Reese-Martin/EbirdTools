"""Direction-aware port probes; a failed waterward probe is not inaccessibility."""
import math
from pathlib import Path

import numpy as np

from .bounded_roads import HEADER,SLOT
from .regional_roads import RegionalRoads,DeferredRoad
from .road_matching import gap_m
from .util import read,sha256


PORT_POLICY={'id':'marine-port-four-direction-car-probe-v1','offset_degrees':.02,
    'directions':['east','north','west','south'],
    'failure':'defer if no complete local car path; do not cache as unreachable',
    'selection':'nearest actual start of successful car-profile paths at the first successful radius',
    'walking':'full round trip above 100m at 2.2 km/h; separate coastline check for dock access'}


class MarineRoads(RegionalRoads):
    def __init__(self,*args,**kwargs):
        kwargs.setdefault('access_policy_extension',PORT_POLICY)
        super().__init__(*args,**kwargs)

    def access(self,index):
        if self.model.locations[index].get('kind')!='marine_port':return super().access(index)
        with self.locks[index%len(self.locks)]:
            value=self.anchors[index]
            if math.isfinite(float(value[2])):return tuple(map(float,value))
            lat,lon=map(float,self.model.coords[index]);cfg=self.model.meta['config']
            maximum=(cfg['activity_minutes']-cfg['birding_minutes'])*cfg['walking_speed_kmh']*1000/120
            for radius in [min(1000,maximum+1),maximum+1]:
                choices=[]
                for dx,dy in [(.02,0),(0,.02),(-.02,0),(0,-.02)]:
                    try:
                        raw=self.request([[lon,lat],[lon+dx,lat+dy]],radius=radius)
                        paths=[f for f in raw.get('features',[]) if f.get('geometry',{}).get('type')=='LineString']
                        if len(paths)!=1 or len(paths[0]['geometry']['coordinates'])<2:continue
                        vertices=paths[0]['geometry']['coordinates']
                        for vertex in vertices:gap_m(vertex[:2],vertex[:2])
                        p=vertices[0][:2];gap=gap_m([lon,lat],p)
                        if gap>radius+1:continue
                        choices.append((gap,p))
                    except (DeferredRoad,ValueError):continue
                if choices:
                    gap,p=min(choices)
                    walk=2*gap/(cfg['walking_speed_kmh']*1000)*60 if gap>cfg['walking_threshold_m'] else 0.
                    self.anchors[index]=[p[1],p[0],cfg['birding_minutes']+walk]
                    self.stats['direction_aware_port_matches']+=1
                    return p[1],p[0],cfg['birding_minutes']+walk
            self.stats['unresolved_port_probes']+=1
            raise DeferredRoad('port probe directions unresolved; not cached unreachable')

    def location(self,index):
        loc=super().location(index)
        if loc.get('kind')=='marine_port':loc['road_access']['matching_policy']=PORT_POLICY['id']
        return loc

    def import_unchanged_costs(self,directory):
        """Carry only physical costs whose exact old/new anchors still agree."""
        directory=Path(directory);old=read(directory/'provider.json')
        for key in ['model_sha256','road_release_sha256','matching_policy']:
            if old[key]!=self.manifest[key]:raise ValueError('old port cache physical inputs differ')
        expected=dict(old['access_policy']);expected['extension']=PORT_POLICY
        if self.manifest['access_policy']!=expected:raise ValueError('unexpected cache policy migration')
        anchors=np.memmap(directory/'access.bin',mode='r',dtype='<f8',shape=self.anchors.shape)
        changed=[]
        for i in range(len(self.model.locations)):
            if self.model.locations[i].get('kind')=='marine_port':
                # Match anew under the direction-aware rule, including ports
                # previously marked infinite by the east-only probe.
                try:new=self.access(i)
                except DeferredRoad:continue
                if not np.array_equal(anchors[i],new):changed.append(i)
            elif math.isfinite(float(anchors[i,2])):self.anchors[i]=anchors[i]
        carried=0
        with (directory/'costs.bin').open('rb') as stream:
            stream.seek(HEADER)
            while block:=stream.read(SLOT.size):
                a,b,m,t,stamp,checksum=SLOT.unpack(block)
                if (stamp and a<len(self.anchors) and b<len(self.anchors)
                    and self.store.checksum(a,b,m,t)==checksum and math.isfinite(m) and math.isfinite(t)
                    and np.array_equal(anchors[a],self.anchors[a]) and np.array_equal(anchors[b],self.anchors[b])):
                    self.store.put(a,b,(m,t));carried+=1
        return {'old_provider_sha256':sha256(directory/'provider.json'),
            'costs_carried_with_identical_anchors':carried,'changed_port_indices':changed,
            'cached_unreachable_port_matches_invalidated':int(np.isinf(anchors[:,2]).sum())}
