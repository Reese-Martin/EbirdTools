from concurrent.futures import ThreadPoolExecutor
import copy
import math
import shutil
import struct
import subprocess
import time

import pytest

from bigyear_planner.bounded_roads import (
    BoundedRoads, FixedCostCache, HEADER, SLOT, create_bounded_roads,
)
from bigyear_planner.runner import compile_engine, export_engine_problem
from bigyear_planner.travel import transitions
from bigyear_planner.util import ROOT, dump, read
from test_dense_roads import fixture
from test_pipeline import imported  # noqa: F401 - pytest fixture


def test_fixed_cache_evicts_in_place_persists_and_rejects_other_network(tmp_path):
    path=tmp_path/'bounded.bin'
    cache=FixedCostCache(path,{'network':'one'},HEADER+4*SLOT.size)
    for b in range(1,11):
        cache.put(0,b,(float(b),b*2.))
        assert path.stat().st_size==HEADER+4*SLOT.size
    assert cache.stats['evictions']==6
    assert cache.get(0,10)==(10.,20.)
    assert cache.get(0,1) is None
    cache.close()
    cache=FixedCostCache(path,{'network':'one'},HEADER+4*SLOT.size)
    assert cache.get(0,10)==(10.,20.)
    with pytest.raises(ValueError,match='active owner'):
        FixedCostCache(path,{'network':'one'},HEADER+4*SLOT.size)
    cache.close()
    with pytest.raises(ValueError,match='binding'):
        FixedCostCache(path,{'network':'two'},HEADER+4*SLOT.size)


def provider(tmp_path,fetch=None):
    problem,_,legacy=fixture(tmp_path)
    problem['opportunities']=[{'day':day,'location':loc,'species':[loc]}
                              for day in (0,1,3,7) for loc in range(3)]
    path=tmp_path/'problem.json';dump(path,problem)
    target=tmp_path/'roads.bounded.json'
    create_bounded_roads(path,target,'http://fixture','network-1','network-sha',capacity_mib=1,
                         hot_entries=4)
    def measured(a,b):
        r=legacy.get((a,b))
        return (r['miles'],r['minutes']) if r else (math.inf,math.inf)
    return problem,target,BoundedRoads(problem,target,fetch or measured),legacy


def test_directed_costs_unavailable_and_duplicate_anchors(tmp_path):
    p,path,roads,legacy=provider(tmp_path)
    for key,cost in legacy.items():
        assert roads[key]['miles']==cost['miles']
    assert roads.get((2,0)) is None
    assert roads[0,1]['minutes']!=roads[1,0]['minutes']
    with pytest.raises(TypeError,match='all-pairs'):
        list(roads)
    roads.close()
    changed=copy.deepcopy(p);changed['locations'][0]['road_access']={'lat':33,'lon':-87}
    with pytest.raises(ValueError,match='anchors differ'):
        BoundedRoads(changed,path)


def test_corrupt_slot_is_a_miss_not_a_wrong_cost(tmp_path):
    cache=FixedCostCache(tmp_path/'cache.bin',{},HEADER+4*SLOT.size)
    cache.put(0,1,(10.,20.))
    struct.pack_into('<d',cache.data,HEADER+8,999.)
    assert cache.get(0,1) is None
    cache.close()


def test_final_selected_pins_survive_eviction_without_growing_disk_or_accepting_unknowns(tmp_path):
    path=tmp_path/'selected.bin';cache=FixedCostCache(path,{},HEADER+4*SLOT.size)
    assert not cache.pin_current(0,99)
    cache.put(0,1,(10.,20.));assert cache.pin_current(0,1)
    for b in range(2,20):cache.put(0,b,(float(b),float(b)))
    assert cache.get(0,1)==(10.,20.) and cache.get(1,0) is None
    assert len(cache.pinned)==1 and path.stat().st_size==HEADER+4*SLOT.size
    cache.pin_capacity=1
    with pytest.raises(ValueError,match='pin capacity'):cache.pin_current(0,19)
    cache.close()


def test_concurrent_misses_share_query_and_failures_are_not_cached_unreachable(tmp_path):
    calls=[]
    def measured(a,b):
        calls.append((a,b));time.sleep(.02)
        if len(calls)==1:
            raise TimeoutError('network problem')
        return 4.,8.
    _,_,roads,_=provider(tmp_path,measured)
    with pytest.raises(TimeoutError):
        roads[0,1]
    with ThreadPoolExecutor(max_workers=8) as pool:
        result=list(pool.map(lambda _:roads[0,1],range(8)))
    assert len(calls)==2 and all(r['miles']==4 for r in result)
    assert roads.stats['unreachable_queries']==0
    roads.close()


def test_native_bounded_all_subsets_match_dense_and_keep_hot_cache_bounded(tmp_path):
    p,_,roads,legacy=provider(tmp_path)
    export_engine_problem(p,legacy,transitions(p,legacy),tmp_path/'legacy.by')
    source=tmp_path/'equivalence.cpp'
    source.write_text('''#include "model.h"
    int main(int argc,char**argv) {
      Problem a(argv[1]),b(argv[2]);
      for(int mask=0;mask<(1<<a.nopp);++mask) {
        std::vector<int> r;for(int i=0;i<a.nopp;++i)if(mask&(1<<i))r.push_back(i);
        a.canonical(r);auto x=a.evaluate(r),y=b.evaluate(r);
        if(x.feasible!=y.feasible||x.species!=y.species||std::abs(x.miles-y.miles)>1e-7||std::abs(x.minutes-y.minutes)>1e-7)return 1;
      }
      return b.bounded->size()>b.bounded->limit()?2:0;
    }''')
    executable=tmp_path/'equivalence'
    subprocess.run([shutil.which('c++'),'-std=c++17','-O2','-pthread','-I',str(ROOT/'cpp'),str(source),'-o',str(executable)],check=True)
    with roads.bridge():
        export_engine_problem(p,roads,transitions(p,roads),tmp_path/'bounded.by')
        subprocess.run([str(executable),str(tmp_path/'legacy.by'),str(tmp_path/'bounded.by')],check=True)
        out=tmp_path/'native'
        subprocess.run([str(compile_engine()),str(tmp_path/'bounded.by'),str(out),'.1','2','42','1','0','0'],check=True)
    engine=read(out/'engine.json')
    assert engine['road_cache']['hot_entries']<=4
    assert engine['road_cache']['bridge_requests']>0
    roads.close()


def test_native_provider_failure_aborts_instead_of_scoring_missing_road(tmp_path):
    def failed(a,b):
        raise OSError('service unavailable')
    p,_,roads,_=provider(tmp_path,failed)
    with roads.bridge():
        export_engine_problem(p,roads,transitions(p,roads),tmp_path/'bounded.by')
        result=subprocess.run([str(compile_engine()),str(tmp_path/'bounded.by'),str(tmp_path/'failed'),
                               '2','2','42','1','0','0'],capture_output=True,text=True)
    assert result.returncode!=0
    assert 'no substitute road cost' in result.stderr
    roads.close()


def test_run_freezes_only_selected_leg_evidence_and_validates_offline(request,monkeypatch):
    from bigyear_planner.demo import synthetic_roads
    from bigyear_planner.problem import build_problem
    from bigyear_planner.runner import run
    from bigyear_planner.travel import load_roads
    from bigyear_planner.validate import validate

    dataset,config,tmp=request.getfixturevalue('imported')
    problem=tmp/'problem.json';build_problem(dataset,problem,config)
    p=read(problem)
    legacy_path=tmp/'legacy.sqlite'
    synthetic_roads(problem,tmp/'roads.csv',legacy_path)
    legacy=load_roads(p,legacy_path)
    values=dict(legacy.items());legacy.close()
    def measured(self,a,b):
        cost=values.get((a,b))
        return (cost['miles'],cost['minutes']) if cost else (math.inf,math.inf)
    monkeypatch.setattr(BoundedRoads,'fetch_osrm',measured)
    target=tmp/'roads.bounded.json'
    create_bounded_roads(problem,target,'http://local-fixture','fixture','sha',capacity_mib=1)
    out=tmp/'run'
    route=run(problem,target,out,seconds=.1)
    assert route['species_total']>0
    assert read(out/'road-cache-metrics.json')['retained_evidence']['pairs']>0
    assert read(out/'validation.json')['valid']
    assert route['provenance']['roads_file']=='roads.sqlite'
    assert not (out/'roads.bounded.bin').exists()
    # Validation must not depend on the live provider or its evictable cache.
    (target.with_suffix('.bin')).unlink()
    assert validate(out/'route.json',out/'problem.json',out/'roads.sqlite')['valid']
    assert read(out/'road-cache-metrics.json')['logical_bytes']<=2**20
    assert not (out/'problem.by').exists()
    resumed=run(problem,target,tmp/'resumed',seconds=.1,resume=out/'checkpoint.json')
    assert resumed['species_total']>=route['species_total']


def test_duckdb_spill_directory_is_cleaned_on_exit():
    from pathlib import Path
    from bigyear_planner.util import ManagedDuckDB
    with pytest.raises(RuntimeError):
        with ManagedDuckDB() as con:
            temp=Path(con.temp.name)
            assert temp.is_dir()
            raise RuntimeError('interrupted stage')
    assert not temp.exists()


def test_osrm_http_no_route_is_distinct_from_service_error(tmp_path,monkeypatch):
    import io
    import urllib.error
    _,_,roads,_=provider(tmp_path)
    def no_route(url,timeout):
        raise urllib.error.HTTPError(url,400,'NoRoute',{},io.BytesIO(b'{"code":"NoRoute"}'))
    monkeypatch.setattr('urllib.request.urlopen',no_route)
    assert roads.fetch_osrm(0,1)==(math.inf,math.inf)
    def unavailable(url,timeout):
        raise urllib.error.HTTPError(url,503,'unavailable',{},io.BytesIO(b'{}'))
    monkeypatch.setattr('urllib.request.urlopen',unavailable)
    with pytest.raises(urllib.error.HTTPError):roads.fetch_osrm(0,1)
    roads.close()


def test_orphan_cleanup_preserves_live_and_unmarked_directories(tmp_path,monkeypatch):
    import os
    from bigyear_planner import util
    monkeypatch.setattr(util,'ROOT',tmp_path)
    root=tmp_path/'build/query-spill';root.mkdir(parents=True)
    for name,pid in [('query-orphan',2147483647),('query-live',os.getpid())]:
        folder=root/name;folder.mkdir()
        dump(folder/'owner.json',{'purpose':'bigyear-query-spill-v1','root':str(tmp_path),'pid':pid})
        (folder/'spill.tmp').write_bytes(b'only generated query data')
    unmarked=root/'query-unmarked';unmarked.mkdir()
    removed=util.clean_query_spill()
    assert len(removed)==1 and not (root/'query-orphan').exists()
    assert (root/'query-live').exists() and unmarked.exists()


def test_rounded_zero_duration_does_not_drop_positive_distance_from_transit():
    from bigyear_planner.travel import pair_transitions
    p={'config':{'drive_minutes':480,'flights':False}}
    plan=pair_transitions(p,{(0,1):{'miles':.0001,'minutes':0}},0,1)[0]
    assert plan['days']==1 and plan['segments'][0]['days']==1
    assert plan['miles']==.0001 and plan['minutes']==0
