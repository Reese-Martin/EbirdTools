import copy
from dataclasses import replace
import math
import subprocess

import pytest

from bigyear_planner.config import Config
from bigyear_planner.road_matching import gap_m
from bigyear_planner.runner import export_engine_problem, run
from bigyear_planner.travel import transitions
from bigyear_planner.util import dump, read, sha256
from bigyear_planner.validate import validate
from bigyear_planner.walking import access_walk, stop_minutes
from test_dense_roads import fixture
from test_pipeline import imported  # noqa: F401 - pytest fixture


def location(meters):
    return {'lat': 0, 'lon': 0, 'road_access': {'lat': math.degrees(meters/6371008.8),
                                             'lon': 0, 'offset_meters': 0}}


def test_threshold_full_distance_round_trip_units_and_legacy():
    config = Config(walking_speed_kmh=1.609344).to_dict()
    assert access_walk(location(99.99), config)['walking_minutes'] == 0
    assert access_walk(location(100.01), config)['walking_meters'] == pytest.approx(200.02)
    assert access_walk(location(1609.344), config)['walking_minutes'] == pytest.approx(120)
    assert stop_minutes(location(1609.344), config) == pytest.approx(240)
    assert access_walk(location(1100), {**config, 'walking_speed_kmh':2.2})['walking_minutes'] == pytest.approx(60)
    assert stop_minutes(location(100000), Config().to_dict()) == 120
    assert access_walk({'lat':33,'lon':-87},config)['walking_minutes'] == 0


@pytest.mark.parametrize('change', [{'walking_speed_kmh':0}, {'walking_speed_kmh':-1},
                                    {'walking_speed_kmh':math.nan}, {'walking_speed_kmh':math.inf},
                                    {'walking_threshold_m':-1}, {'walking_threshold_m':math.nan}])
def test_invalid_policy_rejected(change):
    with pytest.raises(ValueError):
        Config(**change)


def test_native_enforces_single_and_combined_stop_walking_and_free_start(tmp_path):
    p, _, roads = fixture(tmp_path)
    p['config']['walking_speed_kmh'] = 1.609344
    p['config']['walking_threshold_m'] = 100
    p['config']['activity_minutes'] = 300
    # Two 120-minute walks plus birding cannot share a day, even with zero roads.
    for loc in p['locations']:
        loc['road_access'] = {'lat': loc['lat']+.01447, 'lon':loc['lon']}
    p['locations'][2]['road_access']['lat'] += 1  # Infeasible even as the free first stop.
    p['opportunities'] = [{'day':0, 'location':i, 'species':[i]} for i in range(3)]
    for road in roads.values():
        road.update(miles=0,minutes=0)
    target = tmp_path/'case.by'
    export_engine_problem(p,roads,transitions(p,roads),target)
    assert target.read_text().startswith('BY1W ')
    source = tmp_path/'evaluate.cpp'
    source.write_text('''#include "model.h"
    int main(int argc,char**argv){Problem p(argv[1]);
      return !p.evaluate({0}).feasible || p.evaluate({0,1}).feasible || p.evaluate({2}).feasible;}
    ''')
    from bigyear_planner.util import ROOT
    subprocess.run(['c++','-std=c++17','-O2','-pthread','-I',str(ROOT/'cpp'),str(source),
                    '-o',str(tmp_path/'evaluate')],check=True)
    subprocess.run([str(tmp_path/'evaluate'),str(target)],check=True)


def test_walking_durations_agree_for_bounded_dense_and_legacy_native_formats(tmp_path):
    from bigyear_planner.bounded_roads import BoundedRoads, create_bounded_roads
    from bigyear_planner.dense_roads import DenseRoads, location_signature
    p, matrix_path, legacy = fixture(tmp_path)
    p['config'].update(walking_speed_kmh=1.609344, walking_threshold_m=100, activity_minutes=300)
    for loc in p['locations']:
        loc['road_access']={'lat':loc['lat']+.01,'lon':loc['lon']}
    m=read(matrix_path)
    m['locations']=[location_signature(l) for l in p['locations']]
    m['anchors']=[sig[3:] for sig in m['locations']]
    dump(matrix_path,m)
    dump(tmp_path/'walking.json',p)
    provider=tmp_path/'walking.bounded.json'
    create_bounded_roads(tmp_path/'walking.json',provider,'http://fixture','network','sha',capacity_mib=1)
    def fetch(a,b):
        road=legacy.get((a,b))
        return (road['miles'],road['minutes']) if road else (math.inf,math.inf)
    dense=DenseRoads(p,matrix_path)
    bounded=BoundedRoads(p,provider,fetch)
    try:
        with bounded.bridge():
            files=[]
            for name,roads in [('legacy',legacy),('dense',dense),('bounded',bounded)]:
                path=tmp_path/(name+'.by')
                export_engine_problem(p,roads,transitions(p,roads),path)
                files.append(path)
            source=tmp_path/'equivalence.cpp'
            source.write_text('''#include "model.h"
            int main(int argc,char**argv){Problem a(argv[1]),b(argv[2]),c(argv[3]);
              for(int mask=0;mask<8;++mask){std::vector<int> r;
                for(int i=0;i<3;++i)if(mask&(1<<i))r.push_back(i);
                auto x=a.evaluate(r),y=b.evaluate(r),z=c.evaluate(r);
                if(x.feasible!=y.feasible||x.feasible!=z.feasible||x.species!=y.species||x.species!=z.species)return 1;}
              return 0;}
            ''')
            from bigyear_planner.util import ROOT
            subprocess.run(['c++','-std=c++17','-O2','-pthread','-I',str(ROOT/'cpp'),str(source),
                            '-o',str(tmp_path/'equivalence')],check=True)
            subprocess.run([str(tmp_path/'equivalence'),*[str(p) for p in files]],check=True)
    finally:
        bounded.close();dense.close()


def test_search_materialization_and_audit_charge_each_visit_and_detect_tampering(request):
    from bigyear_planner.demo import synthetic_roads
    from bigyear_planner.problem import build_problem
    dataset,config,tmp=request.getfixturevalue('imported')
    config=replace(config,activity_minutes=720,walking_speed_kmh=1.609344)
    problem=tmp/'problem.json'
    p=build_problem(dataset,problem,config)
    for loc in p['locations']:
        loc['road_access']={'lat':loc['lat']+.01,'lon':loc['lon']}
    dump(problem,p)
    roads=tmp/'roads.sqlite';synthetic_roads(problem,tmp/'roads.csv',roads)
    route=run(problem,roads,tmp/'run',seconds=.1)
    assert route['species_total']>0
    total=0
    for day in route['days']:
        walking=0
        for stop in day['stops']:
            loc=p['locations'][stop['location']]; anchor=loc['road_access']
            expected=2*gap_m([loc['lon'],loc['lat']],[anchor['lon'],anchor['lat']])/1609.344*60
            assert stop['walking_minutes']==pytest.approx(expected)
            walking+=expected
        assert day['activity_minutes']==pytest.approx(day['travel_minutes']+120*len(day['stops'])+walking)
        total+=walking
    audit=read(tmp/'run/validation.json')
    assert audit['walking']['minutes']==pytest.approx(total) and total>0
    bad=copy.deepcopy(route)
    day=next(d for d in bad['days'] if d['stops'])
    day['stops'][0]['walking_minutes']=0
    day['walking_minutes']=0
    day['activity_minutes']=day['travel_minutes']+120*len(day['stops'])
    dump(tmp/'run/tampered.json',bad)
    result=validate(tmp/'run/tampered.json',tmp/'run/problem.json',tmp/'run/roads.sqlite')
    assert not result['valid'] and any('walking' in e or 'activity total' in e for e in result['errors'])
    assert route['provenance']['problem_sha256']==sha256(tmp/'run/problem.json')
