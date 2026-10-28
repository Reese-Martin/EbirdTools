from types import SimpleNamespace

import numpy as np

from bigyear_planner.targeted_exploration import TargetedExplorer


def model(tmp_path):
    records = np.array([(5,0,0,2,0),(7,1,2,1,0),(10,2,3,1,0),(365,3,4,1,0)],
        dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')])
    path=tmp_path/'model.json';path.write_text('{}')
    return SimpleNamespace(records=records,species=np.array([0,1,2,0,3],dtype='u4'),
        coords=np.array([[42.,-88.],[42.,-88.000000000001],[41.,-90.],[43.,-74.]]),
        meta={'species':[{'id':str(i),'name':str(i)} for i in range(4)]},path=path)


def test_target_support_uses_every_actual_record_and_exact_coordinate(tmp_path):
    source=model(tmp_path)
    explorer=TargetedExplorer(source,None,tmp_path,[{'species_id':'0','species':'A'},{'species_id':'3','species':'B'}])
    assert explorer.support=={0:[0,2],3:[3]}
    assert explorer.coords[0,1]!=explorer.coords[1,1]
    assert len(explorer.records)==4
    assert set(explorer.heads([],0))=={0,2}


def test_target_campaign_replaces_window_and_preserves_december_31(tmp_path):
    explorer=TargetedExplorer(model(tmp_path),None,tmp_path,[{'species_id':'3','species':'B'}])
    proposal=explorer.campaign([0,1,2],3,7)
    assert proposal['begin']==358 and proposal['end']==366
    assert proposal['trip']==[3] and proposal['outside']==[0,1,2]
    assert proposal['ids']==[0,1,2,3]
    # No synthesized occurrences or rounded source coordinates.
    assert int(explorer.records[proposal['head']]['day'])==365


def test_campaign_can_recover_a_neighboring_target_in_same_region(tmp_path):
    explorer=TargetedExplorer(model(tmp_path),None,tmp_path,[{'species_id':'0','species':'A'},{'species_id':'2','species':'C'}])
    proposal=explorer.campaign([],0,7)
    assert 0 in proposal['trip']
    assert explorer.seen(proposal['trip'])[2]


def test_each_target_gets_a_short_first_probe_before_longer_retries(tmp_path):
    calls = []

    class Evaluator:
        def evaluate(self, ids, timeout):
            calls.append((list(ids), timeout))
            return {'pending':False, 'feasible':not ids, 'species':0, 'minutes':0, 'miles':0}

    explorer = TargetedExplorer(model(tmp_path), Evaluator(), tmp_path,
        [{'species_id':'0','species':'A'},{'species_id':'2','species':'C'}])
    result = explorer.explore([], seconds=100, first_probe_seconds=2)
    assert [c[1] for c in calls[1:3]] == [2, 2]
    assert calls[3][1] == 16
    assert all(t['attempts'] for t in result['targets'])
    assert result['score']['species'] == 0  # Proposals never earn artificial target points.
