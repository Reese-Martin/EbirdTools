import json
import os
import subprocess
from types import SimpleNamespace

import numpy as np

from bigyear_planner.regional_exploration import NativeEvaluator, RegionalExplorer, region_codes
from bigyear_planner.runner import compile_engine
from bigyear_planner.util import ROOT
from test_mobility import mobility  # noqa: F401


def test_full_record_novelty_preserves_adjacent_exact_points(tmp_path):
    dtype = [('day','<u4'),('location','<u4'),('offset','<u8'),('count','<u4'),('reserved','<u4')]
    records = np.array([(10,0,0,2,0),(12,1,2,2,0),(15,2,4,1,0)],dtype=dtype)
    coords = np.array([[40.,-75.000000000001],[40.,-75.000000000002],[35.,-110.]])
    source = tmp_path/'model.json';source.write_text('{}')
    model = SimpleNamespace(records=records,species=np.array([0,1,1,2,3],dtype='<u4'),coords=coords,
                            meta={'species':list(range(4))},path=source)
    explorer = RegionalExplorer(model,None,tmp_path)
    assert explorer.novelty(np.array([True,False,False,False])).tolist()==[1,2,1]
    assert region_codes(coords).tolist()==[7,7,2]
    assert len(explorer.records)==3 and coords[0,1]!=coords[1,1]
    assert explorer.seen([0,1]).tolist()==[True,True,True,False]


def test_complete_block_crosses_a_single_stop_calendar_barrier(tmp_path):
    native = tmp_path/'barrier.by'
    native.write_text('''BY1 366 2 6 6 -1 30 720 480 120
0 0 1 0
8 0 1 1
9 0 1 1
9 1 2 2 3
11 1 2 4 5
19 0 1 0
0
2
0 1 3 300 60
1 0 3 300 60
''')
    source = tmp_path/'barrier.cpp'
    source.write_text('''#include "model.h"
int main(int argc,char**argv){Problem p(argv[1]);
auto old=p.evaluate({0,1,2,5}),one=p.evaluate({0,1,2,3,5}),swap=p.evaluate({0,1,3,5}),block=p.evaluate({0,3,4,5});
return !old.feasible||old.species!=2||one.feasible||swap.feasible||!block.feasible||block.species!=5||block.minutes!=600;}
''')
    subprocess.run(['c++','-std=c++17','-O2','-pthread','-I',str(ROOT/'cpp'),str(source),'-o',str(tmp_path/'barrier')],check=True)
    subprocess.run([str(tmp_path/'barrier'),str(native)],check=True)
    blocks = tmp_path/'blocks.txt';blocks.write_text('4 16 2 3 4\n')
    resume = tmp_path/'resume.json';resume.write_text('[0,1,2,5]')
    output = tmp_path/'search'
    subprocess.run([str(compile_engine()),str(native),str(output),'.08','1','2024','1','2','0',str(resume)],
                   env={**os.environ,'BIGYEAR_BLOCK_PROPOSALS':str(blocks)},check=True)
    result = json.loads((output/'engine.json').read_text())
    assert result['species']==5 and result['regional_block_library']==1
    assert result['regional_block_proposals']>0 and result['regional_block_feasible']>0


def test_native_regional_evaluator_uses_dated_measured_flights(mobility,tmp_path):  # noqa: F811
    model,roads = mobility
    roads.transition(0,1,0,4)
    with NativeEvaluator(model,roads,tmp_path/'rpc').open() as evaluator:
        score = evaluator.evaluate([0,1],timeout=2)
        assert score['feasible'] and score['species']==2
        assert score['minutes']==360 and score['miles']==60
    assert not (tmp_path/'rpc/regional-evaluation.by').exists()
    assert json.loads((tmp_path/'rpc/regional-evaluator-provenance.json').read_text())['expired_bridge_credentials_removed']
    roads.anchors[1,2] = model.meta['config']['activity_minutes']+1
    with NativeEvaluator(model,roads,tmp_path/'over-budget').open() as evaluator:
        impossible = evaluator.evaluate([0,1],timeout=2)
        assert not impossible['pending'] and not impossible['feasible']
        assert impossible['failure_day']==4


def test_regions_take_turns_and_trip_construction_is_lazy(tmp_path,monkeypatch):
    dtype=[('day','<u4'),('location','<u4'),('offset','<u8'),('count','<u4'),('reserved','<u4')]
    coords=np.array([[lat,lon] for lon in [-120.,-110.,-95.,-75.] for lat in [30.,40.]])
    records=np.array([(50,i,i,1,0) for i in range(8)],dtype=dtype)
    model=SimpleNamespace(records=records,species=np.arange(8,dtype='<u4'),coords=coords,
        meta={'species':list(range(8))},path=tmp_path/'model.json')
    explorer=RegionalExplorer(model,None,tmp_path)
    constructed=[]
    def trip(head,*args):constructed.append(head);return [head]
    monkeypatch.setattr(explorer,'_trip',trip)
    proposals=explorer.proposals([])
    assert not constructed
    first=[next(proposals) for _ in range(8)]
    assert [p['region'] for p in first]==list(range(8))
    assert len(constructed)==8


def test_unknown_trip_is_retryable_and_complete_block_is_tested_first(tmp_path):
    import time
    records=np.array([(3,0,0,1,0),(5,1,1,1,0)],dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')])
    model=SimpleNamespace(records=records,species=np.array([0,1]),coords=np.array([[40.,-75.],[40.,-76.]]),
        meta={'species':[0,1]},path=tmp_path/'model.json')
    calls=[]
    def pending(ids,timeout):calls.append(ids);return {'pending':True,'feasible':False,'species':0}
    evaluator=SimpleNamespace(evaluate=pending)
    explorer=RegionalExplorer(model,evaluator,tmp_path)
    proposal={'head':0,'trip':[0,1],'outside':[],'begin':1,'end':10,'region':7}
    candidate,score=explorer.construct(proposal,time.monotonic()+2)
    assert candidate is None and score['pending']
    assert (1,10,0) not in explorer.attempted_heads
    assert (1,10,0) in explorer.retry_heads
    assert calls[0]==[0,1]
    evaluator.evaluate=lambda ids,timeout:{'pending':False,'feasible':len(ids)==2,'species':len(ids)}
    candidate,score=explorer.construct(proposal,time.monotonic()+2)
    assert candidate==[0,1] and score['species']==2
    assert (1,10,0) in explorer.attempted_heads and not explorer.retry_heads


def test_date_moves_select_an_actual_different_recorded_date(tmp_path):
    native=tmp_path/'dates.by'
    native.write_text('BY1 366 1 2 2 -1 30 720 480 120\n10 0 1 0\n20 0 1 1\n0\n0\n')
    seed=tmp_path/'seed.json';seed.write_text('[0]')
    output=tmp_path/'search'
    subprocess.run([str(compile_engine()),str(native),str(output),'.08','1','2024','1','2','0',str(seed)],check=True)
    result=json.loads((output/'engine.json').read_text())
    assert result['date_move_attempts']>0 and result['date_move_changed']==result['date_move_attempts']
    assert result['date_index_bytes']==16  # 2 offsets + 2 actual date IDs


def test_feasible_partial_does_not_discard_pending_extra_stops(tmp_path):
    import time
    records=np.array([(3,0,0,1,0),(5,1,1,1,0)],dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')])
    model=SimpleNamespace(records=records,species=np.array([0,1]),coords=np.array([[40.,-75.],[40.,-76.]]),
        meta={'species':[0,1]},path=tmp_path/'model.json')
    evaluator=SimpleNamespace(evaluate=lambda ids,timeout:{'pending':len(ids)>1,'feasible':len(ids)==1,'species':len(ids)})
    explorer=RegionalExplorer(model,evaluator,tmp_path)
    proposal={'head':0,'trip':[0,1],'outside':[],'begin':1,'end':10,'region':7}
    candidate,score=explorer.construct(proposal,time.monotonic()+2)
    assert candidate==[0] and score['feasible']
    assert (1,10,0) in explorer.retry_heads and (1,10,0) not in explorer.attempted_heads


def test_regional_calendar_respects_nonleap_year_and_last_day(tmp_path):
    from bigyear_planner.config import Config
    records=np.array([(364,0,0,1,0)],dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')])
    model=SimpleNamespace(records=records,species=np.array([0]),coords=np.array([[40.,-75.]]),
       meta={'species':[0],'config':Config(year=2023,walking_speed_kmh=2.2).to_dict()},path=tmp_path/'model.json')
    explorer=RegionalExplorer(model,None,tmp_path)
    proposals=list(explorer.proposals([]))
    assert proposals and all(p['end']<=365 for p in proposals)
    assert any(p['end']==365 and p['head']==0 for p in proposals)


def test_date_index_also_accepts_legacy_records_not_sorted_by_day(tmp_path):
    native=tmp_path/'dates.by'
    native.write_text('BY1 366 1 2 2 -1 30 720 480 120\n20 0 1 0\n10 0 1 1\n0\n0\n')
    seed=tmp_path/'seed.json';seed.write_text('[0]')
    output=tmp_path/'search'
    subprocess.run([str(compile_engine()),str(native),str(output),'.08','1','2024','1','2','0',str(seed)],check=True)
    result=json.loads((output/'engine.json').read_text())
    assert result['date_move_attempts']>0 and result['date_move_changed']==result['date_move_attempts']
    assert result['species']==2


def test_nearby_date_moves_work_at_the_start_of_january(tmp_path):
    native=tmp_path/'january.by'
    native.write_text('BY1 366 1 2 2 -1 30 720 480 120\n0 0 1 0\n10 0 1 1\n0\n0\n')
    seed=tmp_path/'seed.json';seed.write_text('[0]')
    output=tmp_path/'search'
    subprocess.run([str(compile_engine()),str(native),str(output),'.08','1','2024','1','2','0',str(seed)],check=True)
    result=json.loads((output/'engine.json').read_text())
    assert result['nearby_date_moves']>0
    assert result['species']==2
