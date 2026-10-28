import sqlite3
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from bigyear_planner.compact_model import RECORD
from bigyear_planner.config import Config
from bigyear_planner.mobility import FlightModel, MobilityRoads
from bigyear_planner.national_runner import export_native
from bigyear_planner.regional_roads import DeferredRoad
from bigyear_planner.regional_roads import RegionalRoads
from bigyear_planner.util import ROOT, dump, sha256
from test_eod_archive import eod_input  # noqa: F401


@pytest.fixture
def mobility(tmp_path):
    config = Config(activity_minutes=720, walking_speed_kmh=2.2).to_dict()
    coordinates = np.array([[37., -122.], [28., -82.]], dtype='<f8')
    locations = [{'id': str(i), 'name': str(i), 'lat': lat, 'lon': lon, 'state': 'US-CA', 'kind': 'birding'}
                 for i, (lat, lon) in enumerate(coordinates)]
    meta = {'config': config, 'locations': 2, 'opportunities': 2, 'species_entries': 2, 'species': [{'id':'a'}, {'id':'b'}]}
    dump(tmp_path/'model.json', meta)
    (tmp_path/'coordinates.bin').write_bytes(coordinates.tobytes())
    (tmp_path/'opportunities.bin').write_bytes(RECORD.pack(0, 0, 0, 1, 0)+RECORD.pack(4, 1, 1, 1, 0))
    (tmp_path/'species.bin').write_bytes(struct.pack('<II', 0, 1))
    model = SimpleNamespace(path=tmp_path/'model.json', root=tmp_path, meta=meta,
                            coords=coordinates, locations=locations, opportunity=lambda i: None)
    network = tmp_path/'network'
    airports = [{'id':'airport:AAA','code':'AAA','name':'A','lat':37.01,'lon':-122.,'state':'US-CA','kind':'airport'},
                {'id':'airport:BBB','code':'BBB','name':'B','lat':28.01,'lon':-82.,'state':'US-FL','kind':'airport'}]
    dump(network/'airports.json', airports)
    with sqlite3.connect(network/'flights.sqlite') as db:
        db.execute('CREATE TABLE flights(origin,destination,day,minutes,operated)')
        db.execute("INSERT INTO flights VALUES ('AAA','BBB',2,240,5)")
    dump(network/'manifest.json', {'airports_sha256':sha256(network/'airports.json'),
                                  'database_sha256':sha256(network/'flights.sqlite')})
    extended = FlightModel(model, network)
    dump(tmp_path/'release.json', {'parameters':{}})
    roads = MobilityRoads(extended, tmp_path/'release.json', tmp_path/'cache', capacity_mib=1)
    roads.anchors[:] = [[*point, 120.] for point in [*coordinates, *extended.coords.extra]]
    for a, b, miles, minutes in [(0,1,3000.,3000.),(0,2,30.,60.),(3,1,30.,60.)]:
        roads.store.put(a,b,(miles,minutes))
    try:
        yield extended, roads
    finally:
        roads.close()


def test_flight_requires_observed_date_and_separate_measured_access_days(mobility):
    model, roads = mobility
    plan = roads.transition(0,1,0,4)
    assert plan['days'] == 3 and plan['start_day'] == 1
    assert plan['minutes'] == 360 and plan['miles'] == 60
    assert [s['mode'] for s in plan['segments']] == ['road','flight','road']
    assert plan['segments'][1]['service_day'] == 2
    with pytest.raises(DeferredRoad):
        roads.transition(0,1,0,3)  # Only two days: flight+ground cannot fit.
    with pytest.raises(DeferredRoad):
        roads.transition(0,1,3,7)  # No service on any date in this window.


def test_native_compact_flight_calendar_agrees_with_measured_python_plan(mobility, tmp_path):
    model, roads = mobility
    roads.transition(0,1,0,4)
    roads._remember((0,0,-1,0), roads.ground_plan(0,0))
    # Native first stop is free; the long car connection cannot fit, while
    # one access day + observed flight day + one egress day fit exactly.
    with roads.bridge():
        native = tmp_path/'flight.by'
        export_native(model, roads, native)
        source = tmp_path/'check.cpp'
        source.write_text('''#include "model.h"
int main(int argc,char**argv){Problem p(argv[1]);auto s=p.evaluate({0,1});
return !s.feasible||s.species!=2||std::abs(s.minutes-360)>1e-7||std::abs(s.miles-60)>1e-7;}''')
        subprocess.run(['c++','-std=c++17','-O2','-pthread','-I',str(ROOT/'cpp'),str(source),'-o',str(tmp_path/'check')],check=True)
        subprocess.run([str(tmp_path/'check'),str(native)],check=True)


def test_airport_snapshot_tamper_is_rejected(mobility):
    model, roads = mobility
    (model.network_path/'airports.json').write_text('[]')
    with pytest.raises(ValueError, match='frozen flight input changed'):
        FlightModel(model.original, model.network_path)


def test_flight_incumbent_rekeys_indices_only_after_exact_identity_checks(mobility,tmp_path):
    model,roads=mobility
    # Old canonical integers have no relationship to the new model's indices.
    old=[101,707,9001,9002]
    plan=roads.transition(0,1,0,4)
    problem={'locations':[model.locations[i] for i in range(4)],'routing_locations':old,
        'flight_network':{'manifest_sha256':sha256(model.network_path/'manifest.json')},
        'window_transitions':[{'from':0,'to':1,'previous':0,'target':4,'plan':plan}]}
    dump(tmp_path/'problem.json',problem)
    route={'config':{'flights':True},'provenance':{'problem_sha256':sha256(tmp_path/'problem.json')}}
    dump(tmp_path/'route.json',route)
    roads.plan_cache.clear();roads.flight_plans.clear()
    roads.seed_incumbent(tmp_path/'route.json',dict(zip(old,range(4))))
    assert roads.plan_cache[(0,1,0,4)]['segments']==plan['segments']
    with pytest.raises(ValueError,match='remapped incumbent location differs'):
        roads.seed_incumbent(tmp_path/'route.json',{101:1,707:0,9001:2,9002:3})


def test_pending_road_discovery_does_not_block_cached_candidates(mobility, monkeypatch):
    model, roads = mobility
    released = threading.Event()

    def discover(owner, a, b):
        assert released.wait(timeout=2)
        owner.store.put(a,b,(15.,20.))
        return 15.,20.

    monkeypatch.setattr(RegionalRoads, 'costs', discover)
    roads.executor = ThreadPoolExecutor(max_workers=1)
    try:
        with pytest.raises(DeferredRoad, match='pending'):
            roads.costs(1,0)
        assert roads.store.get(1,0) is None
        assert roads.costs(0,1) == (3000.,3000.)
        assert len(roads.pending) == 1
        released.set()
        roads.pending[1,0].result(timeout=2)
        assert roads.costs(1,0) == (15.,20.)
    finally:
        released.set();roads.executor.shutdown(wait=True);roads.executor=None


def test_temporary_road_failure_is_cooled_down_without_unreachable_cost(mobility, monkeypatch):
    model, roads = mobility

    def discover(owner, a, b):
        raise DeferredRoad('provider timeout')

    monkeypatch.setattr(RegionalRoads, 'costs', discover)
    roads.executor = ThreadPoolExecutor(max_workers=1)
    try:
        with pytest.raises(DeferredRoad):
            roads.costs(1,0)
        with pytest.raises(DeferredRoad):
            roads.pending[1,0].result(timeout=2)
        with pytest.raises(DeferredRoad):
            roads.costs(1,0)
        with pytest.raises(DeferredRoad, match='cooldown'):
            roads.costs(1,0)
        assert roads.store.get(1,0) is None
        assert roads.stats['discovery_submissions'] == 1
    finally:
        roads.executor.shutdown(wait=True);roads.executor=None


def test_known_ground_leg_faster_than_every_flight_skips_airport_discovery(mobility, monkeypatch):
    model, roads = mobility
    assert model.minimum_flight_minutes == 240
    def unexpected(*args):
        raise AssertionError('a slower flight cannot improve this measured ground leg')
    monkeypatch.setattr(roads,'_build_transition',unexpected)
    roads.executor = ThreadPoolExecutor(max_workers=1)
    try:
        # Another discovery worker may hold this striped lock while saving
        # geometry. A cached transition must not wait for that worker.
        with ThreadPoolExecutor(max_workers=1) as caller:
            with roads.cost_locks[hash((0,2)) % len(roads.cost_locks)]:
                plan = caller.submit(roads.transition,0,2,0,4).result(timeout=1)
        assert plan['minutes']==60 and plan['days']==1
        assert not roads.transit_pending
    finally:
        roads.executor.shutdown(wait=True);roads.executor=None


def test_full_compact_flight_run_independently_audits_source_and_service_dates(eod_input, tmp_path):  # noqa: F811
    import csv
    from bigyear_planner.compact_model import build
    from bigyear_planner.eod import prepare_eod, locality_id
    from bigyear_planner.eod_archive import convert, SCHEMA
    from bigyear_planner.national_runner import run
    from bigyear_planner.util import read, write_csv
    from bigyear_planner.validate import validate

    source, _, taxonomy, _, original_rows = eod_input
    rows = [dict(r) for r in original_rows]
    rows[1].update(day='5', v_scientificname='Second bird', v_taxonconceptid='second-concept',
                   decimallongitude='-85.000000000001')
    rows.append({**rows[1], 'day':'6', 'v_scientificname':'Third bird', 'v_taxonconceptid':'third-concept',
                 'decimallongitude':'-85.010000000001','representative_gbifid':'123458'})
    with source.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(SCHEMA), delimiter='\t', extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)
    archive = tmp_path/'flight-archive'
    convert(source, archive, expected_rows=3, progress=lambda _:None)
    with taxonomy.open() as stream:
        tax = list(csv.DictReader(stream))
    tax.append({**tax[0], 'source_name':'Second bird', 'source_taxon_id':'second-concept', 'species_id':'SECOND', 'common_name':'Second bird'})
    tax.append({**tax[0], 'source_name':'Third bird', 'source_taxon_id':'third-concept', 'species_id':'THIRD', 'common_name':'Third bird'})
    write_csv(taxonomy, tax, list(tax[0]))
    land = tmp_path/'flight-land.csv'
    write_csv(land, [{'locality_id':locality_id('US-AL',r['decimallatitude'],r['decimallongitude']), 'latitude':r['decimallatitude'], 'longitude':r['decimallongitude'], 'on_land':'1','source':'Synthetic fixture'} for r in rows],
              ['locality_id','latitude','longitude','on_land','source'])
    cfg = Config(activity_minutes=720, walking_speed_kmh=2.2, target_locations=10000000)
    dataset = tmp_path/'flight-dataset'
    prepare_eod(archive, taxonomy, land, dataset, cfg, 'fixture', 'fixture', synthetic=True)
    original = build(dataset, tmp_path/'full-model', cfg)
    o0, o1, o2 = [original.opportunity(i) for i in range(3)]
    a, b = o0['location'], o1['location']
    c = o2['location']
    network = tmp_path/'full-network'
    airports = [{**original.locations[i], 'id':'airport:'+code,'code':code,'name':code,'kind':'airport'} for i,code in [(a,'AAA'),(b,'BBB')]]
    for row in airports:
        row.pop('latitude_text',None);row.pop('longitude_text',None)
    dump(network/'airports.json', airports)
    with sqlite3.connect(network/'flights.sqlite') as db:
        db.execute('CREATE TABLE flights(origin,destination,day,minutes,operated)')
        db.execute("INSERT INTO flights VALUES ('AAA','BBB',2,240,5)")
    dump(network/'manifest.json', {'airports_sha256':sha256(network/'airports.json'), 'database_sha256':sha256(network/'flights.sqlite'), 'source':'Synthetic test only', 'policy':'Synthetic dated flights'})
    model = FlightModel(original, network)
    dump(tmp_path/'full-release.json', {'parameters':{}, 'snapshot_caveat':'Synthetic test only'})
    roads = MobilityRoads(model, tmp_path/'full-release.json', tmp_path/'full-cache', capacity_mib=1)
    roads.anchors[:] = [[*model.coords[i],120.] for i in range(len(model.locations))]
    costs = {(a,b):(3000.,3000.),(a,c):(3001.,3010.),(a,3):(30.,60.),(4,b):(30.,60.),(4,c):(31.,70.),(b,c):(1.,10.)}
    for key,cost in costs.items():
        roads.store.put(*key,cost)
    def query(x,y):
        miles,minutes = costs[x,y]
        points = [[float(model.coords[i][1]),float(model.coords[i][0])] for i in [x,y]]
        return {'waypoints':[{'location':v} for v in points], 'routes':[{'distance':miles*1609.344,'duration':minutes*60,'geometry':{'type':'LineString','coordinates':points},'legs':[]}]}
    roads.query = query
    try:
        roads.transition(a,b,0,4)
        seed = tmp_path/'source-derived-control.json'
        dump(seed, {'ids':[0,1,2], 'model_sha256':sha256(original.path),
                    'source_file':str(original.path), 'source_sha256':sha256(original.path)})
        route = run(original.path, roads, tmp_path/'audited-flight', seconds=.1, threads=1,seed_file=seed)
        assert route['species_total'] == 3
        assert route['days'][2]['kind'] == 'flight'
        assert read(tmp_path/'audited-flight/validation.json')['valid']
        roads.plan_cache.clear();roads.flight_plans.clear()
        roads.seed_incumbent(tmp_path/'audited-flight/route.json')
        restored = roads.transition(a,b,0,4)
        assert any(segment['mode']=='flight' for segment in restored['segments'])
        problem = read(tmp_path/'audited-flight/problem.json')
        problem['flight_links'][0]['service_day'] = 3
        dump(tmp_path/'audited-flight/problem.json', problem)
        audit = validate(tmp_path/'audited-flight/route.json',tmp_path/'audited-flight/problem.json',tmp_path/'audited-flight/roads.sqlite')
        assert not audit['valid'] and any('flight' in e for e in audit['errors'])
    finally:
        roads.close();original.close()


def test_busy_airport_discovery_cannot_starve_nearby_roads(mobility,monkeypatch):
    _,roads=mobility
    release=threading.Event();airport_started=threading.Event();road_finished=threading.Event()
    def airport(*args):
        airport_started.set()
        assert release.wait(3)
        return {'segments':[]}
    def nearby(owner,a,b):
        road_finished.set();owner.store.put(a,b,(10.,10.));return 10.,10.
    monkeypatch.setattr(roads,'_build_transition',airport)
    monkeypatch.setattr(RegionalRoads,'costs',nearby)
    with roads.bridge():
        try:
            for previous in [0,1]:
                with pytest.raises(DeferredRoad):roads.transition(0,1,previous,20)
            assert airport_started.wait(1)
            revision=roads.discovery_revision
            with pytest.raises(DeferredRoad):roads.costs(1,0)
            assert road_finished.wait(1)
            assert roads.wait_for_discovery(revision,1)>revision
            assert roads.costs(1,0)==(10.,10.)
            assert len(roads.pending)<=roads.queue_capacity
            assert len(roads.transit_pending)<=roads.transit_capacity
        finally:release.set()


def test_explicit_water_access_hold_is_not_cached_as_disconnected_road(mobility,tmp_path):
    model,_=mobility
    holds=tmp_path/'holds.json'
    dump(holds,{'exact_coordinate_holds':[[37.,-122.]],'reason':'synthetic open sea crossing'})
    roads=MobilityRoads(model,tmp_path/'release.json',tmp_path/'held-cache',capacity_mib=1,access_holds_path=holds)
    try:
        import math
        assert math.isinf(roads.costs(-1,0)[1])
        assert roads.store.get(0,1) is None
        assert roads.manifest['access_policy']['surface_access_holds']['sha256']==sha256(holds)
        with roads.bridge():assert math.isinf(roads.costs(-1,0)[1])
    finally:roads.close()
