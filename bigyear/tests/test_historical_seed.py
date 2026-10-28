import hashlib
import json
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from bigyear_planner.historical_seed import seed_physical_evidence
from bigyear_planner.util import dump, read, sha256


def test_cross_year_cost_reuse_requires_exact_physical_identity_and_fresh_proofs(tmp_path):
    reference = tmp_path/'reference'
    reference.mkdir()
    current = [{'id':k,'lat':float(i),'lon':-90.,'state':'US-AL'}
               for i,k in enumerate(['a','island','c','airport:X'],1)]
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE locations(idx INTEGER PRIMARY KEY,metadata TEXT)')
    db.executemany('INSERT INTO locations VALUES (?,?)',[(i,json.dumps(v)) for i,v in enumerate(current[:3])])
    old = []
    for i,loc in enumerate(current):
        saved = dict(loc)
        if i!=1:
            saved['road_access'] = {'lat':loc['lat'],'lon':loc['lon'],'offset_meters':0}
        if i==3:
            saved['lat'] += .001  # Same airport code, different exact centroid.
        old.append(saved)
    access = {'policy':'fixture'}
    provider = {'model_sha256':'old-model','road_release_sha256':'same-release',
        'matching_policy':'same-matching','access_policy':access}
    digest = hashlib.sha256(json.dumps(provider,sort_keys=True).encode()).hexdigest()
    dump(reference/'problem.json',{'locations':old})
    with sqlite3.connect(reference/'roads.sqlite') as roads_db:
        roads_db.execute('CREATE TABLE roads(origin,destination,miles,minutes)')
        roads_db.executemany('INSERT INTO roads VALUES (?,?,?,?)',[('a','c',10,20),('a','airport:X',15,25)])
    dump(reference/'route.json',{'provenance':{'road_network_sha256':digest,
        'access_policy':access,'problem_sha256':sha256(reference/'problem.json'),
        'roads_sha256':sha256(reference/'roads.sqlite')}})
    dump(reference/'validation.json',{'valid':True,'route_sha256':sha256(reference/'route.json')})
    dump(reference/'search-binding.json',{'road_provider':provider})
    costs = []
    roads = SimpleNamespace(manifest={**provider,'model_sha256':'new-model','network_sha256':'new-binding'},
        anchors=np.full((4,3),np.nan),store=SimpleNamespace(put=lambda *args:costs.append(args)),
        seed_evidence=lambda _:pytest.fail('old endpoint proofs must not be rebound'))
    model = SimpleNamespace(original=SimpleNamespace(locations=SimpleNamespace(db=db)),
        locations=current,airports=[current[-1]],meta={'locations':3})
    try:
        result = seed_physical_evidence(model,roads,reference,tmp_path/'reuse.json')
        assert result['anchors_reused']==2 and costs==[(0,2,(10,20))]
        assert np.isnan(roads.anchors[1]).all() and np.isnan(roads.anchors[3]).all()
        assert result['final_endpoint_proofs_must_be_remeasured']
        assert read(tmp_path/'reuse.json')['old_endpoint_proofs_relabelled'] is False
        roads.manifest['road_release_sha256'] = 'different-physical-release'
        with pytest.raises(ValueError,match='physical road release'):
            seed_physical_evidence(model,roads,reference,tmp_path/'bad.json')
    finally:
        db.close()
