from types import SimpleNamespace

import pytest

from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.util import dump, sha256


@pytest.mark.parametrize('changed,expected',[
    ('audit','route-bound'),('model','complete model'),
    ('network','physical road network'),('artifact','artifact changed')])
def test_verified_joint_seed_rejects_changed_bindings_before_carrying_costs(tmp_path,changed,expected):
    model_path=tmp_path/'model.json';dump(model_path,{'complete':'source cohort'})
    problem=tmp_path/'problem.json';dump(problem,{'compact_source':{'sha256':
        'another-model' if changed=='model' else sha256(model_path)}})
    database=tmp_path/'roads.sqlite';database.write_bytes(b'frozen, audited physical roads')
    route=tmp_path/'route.json';dump(route,{'provenance':{
        'problem_sha256':sha256(problem),'roads_sha256':sha256(database),
        'road_network_sha256':'another-network' if changed=='network' else 'same-network'}})
    dump(tmp_path/'validation.json',{'valid':True,'route_sha256':
        'another-route' if changed=='audit' else sha256(route)})
    if changed=='artifact':database.write_bytes(b'changed after positive audit')
    roads=SimpleNamespace(manifest={'network_sha256':'same-network'})
    with pytest.raises(ValueError,match=expected):
        seed_verified_joint(SimpleNamespace(path=model_path),roads,tmp_path)
