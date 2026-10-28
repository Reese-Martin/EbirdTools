import subprocess


from bigyear_planner.compact_model import RECORD
from bigyear_planner.national_runner import export_native
from bigyear_planner.util import ROOT
from test_mobility import mobility  # noqa: F401


def test_native_joint_flight_charter_costs_and_exclusive_dates(mobility,tmp_path):  # noqa: F811
    model,roads=mobility
    model.meta.update(opportunities=3,species_entries=3,joint_marine={'trip_ids':['long','short']})
    (model.root/'opportunities.bin').write_bytes(RECORD.pack(0,0,0,1,0)+RECORD.pack(4,1,1,1,1)+RECORD.pack(4,1,2,1,2))
    (model.root/'species.bin').write_bytes(bytes.fromhex('000000000100000001000000'))
    visits=model.root/'marine-visits.txt';visits.write_text('2\n1 600 540\n2 120 60\n')
    roads.transition(0,1,0,4)
    source=tmp_path/'joint.cpp'
    source.write_text('''#include "model.h"
int main(int argc,char**argv){Problem p(argv[1]);auto a=p.evaluate({0,1}),b=p.evaluate({0,2}),both=p.evaluate({0,1,2});
if(!a.feasible||a.species!=2||std::abs(a.minutes-900)>1e-7||std::abs(a.miles-60)>1e-7)return 1;
if(!b.feasible||std::abs(b.minutes-420)>1e-7||both.feasible||p.screen({0,1,2}))return 2;
return 0;}''')
    binary=tmp_path/'joint'
    subprocess.run(['c++','-std=c++17','-O2','-pthread','-I',str(ROOT/'cpp'),str(source),'-o',str(binary)],check=True)
    with roads.bridge():
        native=tmp_path/'joint.by';export_native(model,roads,native)
        subprocess.run([str(binary),str(native)],check=True)
        # A corrupted variant mapping must fail before any route is evaluated.
        visits.write_text('1\n1 600 540\n')
        bad=subprocess.run([str(binary),str(native)],capture_output=True)
        assert bad.returncode!=0 and b'unbound boat variant' in bad.stderr


def test_joint_charter_walking_can_exceed_day_even_when_sailing_fits(mobility,tmp_path):  # noqa: F811
    model,roads=mobility
    model.meta.update(joint_marine={'trip_ids':['long']})
    (model.root/'opportunities.bin').write_bytes(RECORD.pack(0,0,0,1,0)+RECORD.pack(4,1,1,1,1))
    (model.root/'marine-visits.txt').write_text('1\n1 600 540\n')
    roads.transition(0,1,0,4);roads.anchors[1,2]=120.1
    source=tmp_path/'walking.cpp';source.write_text('''#include "model.h"
int main(int argc,char**argv){Problem p(argv[1]);return p.evaluate({0,1}).feasible;}''')
    binary=tmp_path/'walking'
    subprocess.run(['c++','-std=c++17','-O2','-pthread','-I',str(ROOT/'cpp'),str(source),'-o',str(binary)],check=True)
    with roads.bridge():
        native=tmp_path/'walking.by';export_native(model,roads,native)
        subprocess.run([str(binary),str(native)],check=True)


def test_charter_sidecar_is_bound_to_exact_binary_variants(mobility,tmp_path):  # noqa: F811
    model,roads=mobility
    model.meta.update(joint_marine={'trip_ids':['bad']})
    (model.root/'opportunities.bin').write_bytes(RECORD.pack(0,0,0,1,0)+RECORD.pack(4,1,1,1,1))
    (model.root/'marine-visits.txt').write_text('1\n1 20 21\n')
    source=tmp_path/'bad.cpp';source.write_text('#include "model.h"\nint main(int argc,char**argv){try{Problem p(argv[1]);return 1;}catch(const std::exception&e){return std::string(e.what())=="invalid boat visit duration"?0:2;}}')
    binary=tmp_path/'bad'
    subprocess.run(['c++','-std=c++17','-O2','-pthread','-I',str(ROOT/'cpp'),str(source),'-o',str(binary)],check=True)
    with roads.bridge():
        native=tmp_path/'bad.by';export_native(model,roads,native)
        subprocess.run([str(binary),str(native)],check=True)


def test_regional_evaluator_screens_impossible_no_transit_day_without_road_queries(mobility,tmp_path):  # noqa: F811
    from bigyear_planner.regional_exploration import NativeEvaluator
    model,roads=mobility
    # California on Jan 1, Florida on Jan 2: neither whole-day flights nor
    # a local road drive can bridge this. The screen needs no network request.
    (model.root/'opportunities.bin').write_bytes(RECORD.pack(0,0,0,1,0)+RECORD.pack(1,1,1,1,0))
    with NativeEvaluator(model,roads,tmp_path/'screen').open() as evaluator:
        result=evaluator.evaluate([0,1],timeout=2)
    assert not result['pending'] and not result['feasible'] and result['screened']
    assert roads.stats['airport_discovery_submissions']==0
    assert roads.stats['anchor_queries']==0
    assert roads.stats['discovery_successes']==0


def test_source_visit_hold_masks_cached_access_without_poisoning_physical_roads():
    from types import SimpleNamespace
    import numpy as np
    from bigyear_planner.joint_marine import JointRoads
    owner=JointRoads.__new__(JointRoads)
    owner.model=SimpleNamespace(coords=np.asarray([[32.61321,-117.10431]]))
    owner.anchors=np.asarray([[32.618971,-117.103229,155.375]])
    owner.source_visit_holds={(32.61321,-117.10431)}
    assert owner.access(0)==(32.61321,-117.10431,float('inf'))
    assert owner.anchors.tolist()==[[32.618971,-117.103229,155.375]]
