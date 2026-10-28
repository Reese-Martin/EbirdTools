import math

import numpy as np
import pytest

from bigyear_planner.joint_marine import JointRoads
from bigyear_planner.regional_roads import DeferredRoad
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.road_matching import gap_m
from bigyear_planner.mobility import FlightModel,MobilityRoads
from bigyear_planner.util import dump,read,sha256
from test_mobility import mobility  # noqa: F401


def create(model,tmp_path,name,cls=ResolvedJointRoads):
    return cls(model,tmp_path/'release.json',tmp_path/name,capacity_mib=1)


def test_failed_east_probe_uses_real_north_path_and_charges_full_walk(mobility,tmp_path):  # noqa: F811
    model,_=mobility;roads=create(model,tmp_path,'alternate')
    original=model.coords[0].copy();calls=[]
    def request(coords,match=False,radius=1):
        calls.append(coords)
        if match:return {'features':[]}
        if coords[1][1]>coords[0][1]:
            point=[coords[0][0],coords[0][1]+.002]
            return {'features':[{'geometry':{'type':'LineString','coordinates':[point,coords[1]]}}]}
        raise DeferredRoad('probe endpoint has no complete car path')
    roads.request=request
    try:
        lat,lon,minutes=roads.access(0)
        gap=gap_m([float(original[1]),float(original[0])],[lon,lat])
        assert gap>100 and minutes==pytest.approx(120+2*gap/2200*60)
        assert np.array_equal(model.coords[0],original)
        assert roads.access(0)==(lat,lon,minutes)
        assert roads.stats['resolved_alternate_local_access']==1
        assert calls[0][1][0]>calls[0][0][0]  # Legacy east probe still tried.
    finally:roads.close()


def test_access_hold_never_becomes_a_fallback_anchor(mobility,tmp_path):  # noqa: F811
    model,_=mobility;roads=create(model,tmp_path,'hold')
    roads.source_visit_holds={tuple(map(float,model.coords[0]))}
    roads.anchors[0]=[*model.coords[0],120.]
    roads.request=lambda *a,**k:pytest.fail('held source pin must not be probed')
    try:
        assert math.isinf(roads.access(0)[2]) and math.isinf(roads.costs(-1,0)[1])
        with roads.bridge():assert math.isinf(roads.costs(-1,0)[1])
        assert roads.anchors[0,2]==120. and roads.store.get(0,1) is None
    finally:roads.close()


def test_failed_alternate_probes_stay_unknown_without_infinite_cost(mobility,tmp_path):  # noqa: F811
    model,_=mobility;roads=create(model,tmp_path,'unknown')
    def request(coords,match=False,radius=1):
        if match:return {'features':[]}
        raise DeferredRoad('provider timeout; connectivity unknown')
    roads.request=request
    try:
        with pytest.raises(DeferredRoad):roads.access(0)
        assert np.isnan(roads.anchors[0,2])
        assert roads.store.get(0,1) is None
    finally:roads.close()


def test_cache_migration_preserves_anchors_and_finite_costs_without_old_proofs(mobility,tmp_path):  # noqa: F811
    model,_=mobility;old=create(model,tmp_path,'old',JointRoads);new=create(model,tmp_path,'new')
    try:
        old.anchors[:]=[[*model.coords[i],120.] for i in range(len(model.locations))]
        old.store.put(0,1,(3000.,3000.));old.store.put(1,0,(float('inf'),float('inf')))
        migration=new.migrate_cache(old.work)
        assert np.array_equal(new.anchors,old.anchors)
        assert new.store.get(0,1)==(3000.,3000.) and new.store.get(1,0) is None
        assert migration['old_network_sha256']!=migration['new_network_sha256']
        assert not migration['old_endpoint_proofs_relabelled']
        assert not new.evidence_databases
    finally:new.close();old.close()


def test_only_airports_can_use_offset_approach_probes(mobility,tmp_path):  # noqa: F811
    model,_=mobility;roads=create(model,tmp_path,'airport-offset')
    reference=tuple(map(float,model.coords[2]));lat,lon=reference
    def request(coords,match=False,radius=1):
        if match:return {'features':[]}
        if coords[0]!=[lon,lat]:
            return {'features':[{'geometry':{'type':'LineString','coordinates':coords}}]}
        raise DeferredRoad('centroid maps to disconnected airport service way')
    roads.request=request
    try:
        alat,alon,minutes=roads.access(2)
        assert (alat,alon)!=reference and minutes>120
        assert tuple(model.coords[2])==reference
        assert roads.stats['resolved_airport_public_approach']==1
    finally:roads.close()


def test_rural_gateway_outside_eight_nearest_airports_has_measured_dated_transfer(mobility,tmp_path):  # noqa: F811
    original,_=mobility;network=original.network_path
    airports=read(network/'airports.json')
    airports.extend([{**airports[1],'id':f'airport:R{i}','code':f'R{i}',
        'lat':28.+.0001*(i+1)} for i in range(9)])
    dump(network/'airports.json',airports)
    manifest=read(network/'manifest.json');manifest['airports_sha256']=sha256(network/'airports.json')
    dump(network/'manifest.json',manifest)
    model=FlightModel(original.original,network)
    old=create(model,tmp_path,'rural-old',MobilityRoads);resolved=create(model,tmp_path,'rural-resolved')
    try:
        for roads in [old,resolved]:
            roads.anchors[:]=[[*model.coords[i],120.] for i in range(len(model.locations))]
            for a,b,m,t in [(0,1,3000.,3000.),(0,2,30.,60.),(3,1,30.,60.)]:roads.store.put(a,b,(m,t))
        assert 1 not in old.nearest_airports(1) and 1 in resolved.nearest_airports(1)
        with pytest.raises(DeferredRoad):old.transition(0,1,0,4)
        plan=resolved.transition(0,1,0,4)
        assert plan['days']==3 and plan['miles']==60 and plan['minutes']==360
        assert plan['segments'][1]['service_day']==2
    finally:old.close();resolved.close()
