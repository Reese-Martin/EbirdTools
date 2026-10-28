import json
import os
import subprocess
from types import SimpleNamespace

import numpy as np

from bigyear_planner.calendar_study import NearbyPolisher,TentpoleExplorer,superior
from bigyear_planner.runner import compile_engine
from test_mobility import mobility  # noqa: F401


def test_tentpole_survives_replacements_and_date_moves(tmp_path):
    # The cheaper date does not contain the required rarity. An unconstrained
    # search should move; an anchored search must keep its actual dated visit.
    native=tmp_path/'anchor.by'
    native.write_text('BY1 366 1 2 3 -1 30 720 480 120\n0 0 1 0\n10 0 1 1\n20 0 1 0\n0\n0\n')
    seed=tmp_path/'seed.json';seed.write_text('[0,1]')
    required=tmp_path/'required.txt';required.write_text('1\n')
    blocks=tmp_path/'blocks.txt';blocks.write_text('0 366 1 2\n')
    env={**os.environ,'BIGYEAR_REQUIRED_OPPORTUNITIES':str(required),'BIGYEAR_BLOCK_PROPOSALS':str(blocks)}
    subprocess.run([str(compile_engine()),str(native),str(tmp_path/'search'),'.1','2','2024','1','2','0',str(seed)],env=env,check=True)
    result=json.loads((tmp_path/'search/engine.json').read_text())
    assert 1 in result['selected'] and result['species']==2 and result['required_visits']==1
    assert result['regional_block_proposals']>0 and result['date_move_attempts']>0
    seed.write_text('[0]')
    failed=subprocess.run([str(compile_engine()),str(native),str(tmp_path/'bad'),'.1','1','2024','1','2','0',str(seed)],env=env,capture_output=True)
    assert failed.returncode!=0 and b'every required visit' in failed.stderr


def test_species_gain_always_outranks_travel_saving():
    assert superior({'feasible':True,'species':683,'minutes':100000,'miles':50000},
                    {'feasible':True,'species':682,'minutes':1,'miles':1})
    assert not superior({'pending':True,'feasible':True,'species':800,'minutes':0,'miles':0},
                        {'feasible':True,'species':682,'minutes':1,'miles':1})


def test_phase_handoff_cancels_only_unstarted_speculation():
    from collections import Counter
    from concurrent.futures import Future
    import threading
    from bigyear_planner.search_roads import SearchRoads
    owner=SearchRoads.__new__(SearchRoads);owner.discovery_lock=threading.RLock();owner.stats=Counter()
    running=Future();running.set_running_or_notify_cancel()
    queued=Future();flight=Future();completed=Future();completed.set_result((1.,2.))
    owner.pending={(1,2):running,(2,3):queued,(3,4):completed};owner.transit_pending={(1,4,0,5):flight}
    owner.store=object();measured=owner.store
    result=owner.begin_phase('nearby')
    assert result=={'phase':'nearby','cancelled_road':1,'cancelled_flight':1,'running_road':1,'running_flight':0}
    assert owner.pending=={(1,2):running} and not owner.transit_pending
    assert queued.cancelled() and flight.cancelled() and not running.cancelled()
    assert owner.store is measured and owner.stats['phase_transitions']==1


def test_nearby_cleanup_checks_calendar_and_retains_exact_dates(tmp_path):
    dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')]
    records=np.asarray([(10,0,0,1,0),(10,1,1,1,0),(10,2,2,1,0),(11,3,3,1,0)],dtype=dtype)
    # Pin1 is nearby but infeasible (e.g. walking budget); pin2 is farther and
    # feasible. Geometric proximity must never substitute for native checks.
    model=SimpleNamespace(records=records,species=np.arange(4,dtype='u4'),
        coords=np.asarray([[35.,-100.],[35.,-100.001],[35.,-100.002],[45.,-70.]]),
        meta={'species':list(range(4))},path=tmp_path/'model.json')
    calls=[]
    def evaluate(ids,timeout):
        calls.append(ids)
        return {'pending':False,'feasible':1 not in ids,'species':len(ids),'minutes':0,'miles':0}
    evaluator=SimpleNamespace(evaluate=evaluate)
    result=NearbyPolisher(model,evaluator,tmp_path).polish([0],seconds=1)
    assert result['ids']==[0,2] and result['score']['species']==2
    assert [0,1] in calls and 3 not in result['ids']
    assert int(records[result['ids'][1]]['day'])==10


def test_fresh_preparation_remeasures_evicted_airport_car_costs(mobility,tmp_path,monkeypatch):  # noqa: F811
    from bigyear_planner.bounded_roads import SLOT
    from bigyear_planner.calendar_study import verify_fresh_calendar
    from bigyear_planner.regional_exploration import NativeEvaluator
    model,roads=mobility;plan=roads.transition(0,1,0,4)
    with NativeEvaluator(model,roads,tmp_path/'old-client').open() as evaluator:
        assert evaluator.evaluate([0,1],timeout=2)['feasible']
        for offset in roads.store.offsets(0,2):
            row=SLOT.unpack_from(roads.store.data,offset)
            if row[:2]==(0,2):roads.store.data[offset:offset+SLOT.size]=bytes(SLOT.size)
        assert roads.store.get(0,2) is None
        # The old client's aggregate flight plan remains feasible even though
        # a physical access leg needed to save it has left the provider cache.
        assert evaluator.evaluate([0,1],timeout=2)['feasible']
    calls=[]
    def query(a,b):
        calls.append((a,b));assert (a,b)==(0,2)
        return {'routes':[{'distance':30*1609.344,'duration':60*60}]}
    monkeypatch.setattr(roads,'query',query)
    monkeypatch.setattr('bigyear_planner.calendar_study.selected_problem',lambda *args:{
        'routing_locations':[0,1,2,3],'compact_source':{'selected_locations':[0,1]},
        'opportunities':[{'day':0,'location':0},{'day':4,'location':1}],
        'window_transitions':[{'plan':plan}]})
    score=verify_fresh_calendar(model,roads,tmp_path/'fresh-client',[0,1],timeout=3)
    assert score['feasible'] and score['species']==2
    assert calls==[(0,2)] and roads.store.pinned[(0,2)]==(30.,60.)
    assert roads.store.pinned[(3,1)]==(30.,60.)


def test_bad_optional_regional_stop_does_not_discard_feasible_tentpole(tmp_path,monkeypatch):
    dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')]
    model=SimpleNamespace(records=np.asarray([(10,0,0,1,0),(20,1,1,1,0),(22,2,2,1,0)],dtype=dtype),
        species=np.arange(3,dtype='u4'),coords=np.asarray([[35.,-100.],[35.01,-100.],[35.02,-100.]]),
        meta={'species':[{'id':str(i)} for i in range(3)]},path=tmp_path/'model.json')
    def evaluate(ids,timeout):
        return {'pending':False,'feasible':2 not in ids,'species':len(ids),'minutes':0,'miles':0}
    explorer=TentpoleExplorer(model,SimpleNamespace(evaluate=evaluate),tmp_path,[{'species_id':'1','species':'Target'}])
    monkeypatch.setattr(explorer,'heads',lambda *args,**kwargs:[1])
    monkeypatch.setattr(explorer,'campaign',lambda *args:{'ids':[0,1,2],'outside':[0],'begin':1,'end':30})
    result=explorer.footholds([0],seconds=1,limit=1)
    assert result['scenarios'][0]['ids']==[0,1] and result['scenarios'][0]['required']==[1]
    assert result['scenarios'][0]['construction']=='anchor_only'


def test_nearby_corridor_uses_spare_transfer_days_and_rechecks_calendar(tmp_path):
    from bigyear_planner.util import dump
    dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')]
    model=SimpleNamespace(records=np.asarray([(0,0,0,1,0),(5,2,1,1,0),(10,1,2,1,0)],dtype=dtype),
        species=np.arange(3,dtype='u4'),coords=np.asarray([[35.,-100.],[35.,-90.],[35.,-95.]]),
        meta={'species':[{'id':str(i)} for i in range(3)]},path=tmp_path/'model.json')
    calls=[]
    def evaluate(ids,timeout):
        calls.append(ids);return {'pending':False,'feasible':True,'species':len(ids),'minutes':0,'miles':0}
    evaluator=SimpleNamespace(evaluate=evaluate)
    assert not NearbyPolisher(model,evaluator,tmp_path).proposals([0,2])
    days=[{'stops':[],'legs':[]} for _ in range(11)]
    days[0]['stops']=[{'location':0}];days[10]['stops']=[{'location':1}]
    days[9]['legs']=[{'from':0,'to':1,'mode':'road',
        'geometry':{'type':'LineString','coordinates':[[-100.,35.],[-90.,35.]]}}]
    reference=tmp_path/'reference.json';dump(reference,{'days':days})
    polisher=NearbyPolisher(model,evaluator,tmp_path,reference_route=reference)
    proposals=polisher.proposals([0,2])
    assert proposals==[(1,0.,1)]  # Midpoint is hundreds of miles from either stop.
    result=polisher.polish([0,2],seconds=1)
    assert result['ids']==[0,1,2] and [0,1,2] in calls
    assert result['reference_corridors']['path']==str(reference)
