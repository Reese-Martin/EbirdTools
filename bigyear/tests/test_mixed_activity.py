import copy
from contextlib import closing
import csv
from dataclasses import replace
import os
import subprocess

import pytest

from bigyear_planner.config import Config
from bigyear_planner.demo import fixtures, synthetic_roads
from bigyear_planner.ingest import prepare
from bigyear_planner.problem import build_problem
from bigyear_planner.runner import compile_engine, materialize, run
from bigyear_planner.travel import load_roads, transitions
from bigyear_planner.util import dump, read, sha256
from bigyear_planner.validate import validate
from test_eod_archive import eod_input  # noqa: F401
from test_mobility import mobility  # noqa: F401


def native_case(tmp_path, minutes, *, mixed=True, durations=(120, 120)):
    path = tmp_path / 'problem.by'
    magic = 'BY1W' + ('M' if mixed else '')
    path.write_text(f'{magic} 366 2 2 2 -1 30 720 480 120\n'
                    f'0 0 1 0\n0 1 1 1\n{durations[0]}\n{durations[1]}\n'
                    f'1\n0 1 500 {minutes}\n0\n')
    output = tmp_path / 'search'
    subprocess.run([str(compile_engine()), str(path), str(output), '1', '1', '42',
                    '1', '0', '1'], check=True)
    return read(output / 'engine.json')


def test_two_birding_stops_plus_eight_hours_driving_fit(tmp_path):
    score = native_case(tmp_path, 480)
    assert score['optimal'] and score['species'] == 2
    assert score['travel_minutes'] == 480 and score['road_miles'] == 500


@pytest.mark.parametrize('minutes,durations', [(481, (120, 120)), (480, (121, 120))])
def test_shared_activity_and_driving_limits_include_walking(tmp_path, minutes, durations):
    assert native_case(tmp_path, minutes, durations=durations)['species'] == 1


def test_legacy_mile_cap_remains_reproducible(tmp_path):
    assert native_case(tmp_path, 480, mixed=False)['species'] == 1


def test_eight_hour_driving_cap_applies_even_with_activity_to_spare(tmp_path):
    path = tmp_path / 'problem.by'
    # A fixed start plus one 2-hour stop leaves ten activity hours, but driving
    # still cannot exceed eight. There is no earlier transit date available.
    path.write_text('BY1M 1 2 1 1 0 30 720 480 120\n0 1 1 0\n'
                    '1\n0 1 550 540\n0\n')
    subprocess.run([str(compile_engine()), str(path), str(tmp_path/'search'),
                    '1', '1', '42', '1', '0', '1'], check=True)
    assert read(tmp_path/'search/engine.json')['species'] == 0


def test_calendar_reconstruction_can_relocate_existing_species(tmp_path):
    path = tmp_path / 'problem.by'
    # The first species occupies the only date for the second species. Moving
    # it to another actual reported date permits both; simple insertion fails.
    path.write_text('BY1M 366 2 2 3 -1 30 720 480 120\n'
                    '0 0 1 0\n0 1 1 1\n10 0 1 0\n'
                    '2\n0 1 600 600\n1 0 600 600\n'
                    '2\n0 1 2 600 600\n1 0 2 600 600\n')
    dump(tmp_path/'seed.json', [0])
    (tmp_path/'blocks.txt').write_text('0 11 2 1 2\n')
    env = dict(os.environ)
    env.pop('BIGYEAR_REQUIRED_OPPORTUNITIES', None)
    env['BIGYEAR_BLOCK_PROPOSALS'] = str(tmp_path/'blocks.txt')
    subprocess.run([str(compile_engine()), str(path), str(tmp_path/'search'),
                    '.15', '1', '2024', '1', '2', '0', str(tmp_path/'seed.json')],
                   env=env, check=True)
    score = read(tmp_path/'search/engine.json')
    assert score['species'] == 2 and score['selected'] == [1, 2]
    assert score['regional_block_feasible'] > 0


@pytest.mark.parametrize('stop_count,driving_minutes', [(2, 480), (1, 600)])
def test_mixed_schedule_export_materialization_and_independent_audit(tmp_path, stop_count, driving_minutes):
    raw = fixtures(tmp_path/'raw', years=(2024,), small=True)
    source = raw/'ebd-2024.tsv'
    with source.open() as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        fields = reader.fieldnames
        rows = list(reader)
    for row in rows:
        if row['LOCALITY ID'] == 'LDEMO1':
            row['OBSERVATION DATE'] = '2024-01-03'
    with source.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)
    config = replace(Config(), mixed_road_birding=True, activity_minutes=720, drive_minutes=720)
    prepare(source, raw/'taxonomy.csv', raw/'land.csv', tmp_path/'dataset', config,
            'synthetic', 'synthetic', synthetic=True)
    problem_path = tmp_path/'problem.json'
    problem = build_problem(tmp_path/'dataset', problem_path, config)
    chosen = [o for o in problem['opportunities'] if o['day'] == 2 and
              problem['locations'][o['location']]['locality_id'] in {'LDEMO0', 'LDEMO1'}]
    assert len(chosen) == 2
    if stop_count == 1:
        problem['config']['start_location'] = problem['locations'][chosen[0]['location']]['id']
        chosen = chosen[1:]
    for i, opportunity in enumerate(chosen):
        opportunity['id'] = i
    problem['opportunities'] = chosen
    dump(problem_path, problem)
    roads_path = tmp_path/'roads.sqlite'
    synthetic_roads(problem_path, tmp_path/'roads.csv', roads_path)
    import sqlite3
    with sqlite3.connect(roads_path) as db:
        db.execute('UPDATE roads SET miles=500,minutes=? WHERE origin!=destination', [driving_minutes])
    output = tmp_path/'run'
    route = run(problem_path, roads_path, output, seconds=1, exact=1)
    day = route['days'][2]
    assert len(day['stops']) == stop_count and day['activity_minutes'] == 720
    assert day['road_miles'] == 500 and read(output/'validation.json')['valid']
    saved_problem = read(output/'problem.json')
    with closing(load_roads(saved_problem, output/'roads.sqlite')) as saved_roads:
        calendar = materialize(saved_problem, saved_roads, transitions(saved_problem, saved_roads),
                               route['search']['selected'], {}, calendar_only=True)
    assert calendar['calendar_only'] is True and calendar['year'] == 2024
    assert [(d['date'],d['kind'],d['activity_minutes']) for d in calendar['days']] == [
        (d['date'],d['kind'],d['activity_minutes']) for d in route['days']]
    for changes, error in [({'drive_minutes': 479}, 'mixed-day driving budget'),
                           ({'activity_minutes': 719}, 'activity budget'),
                           ({'mixed_road_birding': False}, 'road-mile budget')]:
        bad_problem = copy.deepcopy(read(output/'problem.json'))
        bad_route = copy.deepcopy(route)
        bad_problem['config'].update(changes)
        bad_route['config'].update(changes)
        dump(output/'bad-problem.json', bad_problem)
        bad_route['provenance']['problem_sha256'] = sha256(output/'bad-problem.json')
        dump(output/'bad-route.json', bad_route)
        audit = validate(output/'bad-route.json', output/'bad-problem.json', output/'roads.sqlite')
        assert not audit['valid'] and any(error in e for e in audit['errors']), audit


def test_mixed_setting_requires_a_boolean():
    with pytest.raises(ValueError, match='boolean'):
        Config(mixed_road_birding='false')
    with pytest.raises(ValueError, match='total activity budget'):
        Config(mixed_road_birding=True, activity_minutes=720, drive_minutes=721)


def test_compact_screen_allows_measured_eight_hour_same_day_road(mobility, tmp_path):  # noqa: F811
    import numpy as np
    from bigyear_planner.bounded_roads import SLOT
    from bigyear_planner.compact_model import RECORD
    from bigyear_planner.regional_exploration import NativeEvaluator

    model, roads = mobility
    model.meta['config']['mixed_road_birding'] = True
    (model.root/'opportunities.bin').write_bytes(
        RECORD.pack(0, 0, 0, 1, 0) + RECORD.pack(0, 1, 1, 1, 0))
    (model.root/'coordinates.bin').write_bytes(np.asarray([[37., -122.], [37., -114.]],
                                                        dtype='<f8').tobytes())
    # The fixture's original directed cost belongs to its California/Florida
    # example. Replace that synthetic cache entry along with these test pins.
    for offset in roads.store.offsets(0, 1):
        if SLOT.unpack_from(roads.store.data, offset)[:2] == (0, 1):
            roads.store.data[offset:offset+SLOT.size] = bytes(SLOT.size)
    roads.store.put(0, 1, (500., 480.))
    with NativeEvaluator(model, roads, tmp_path/'evaluator').open() as evaluator:
        score = evaluator.evaluate([0, 1], timeout=2)
    assert score['feasible'] and score['species'] == 2 and score['minutes'] == 480, repr(score)


def test_policy_derivative_preserves_every_component_and_routing_binding(eod_input, tmp_path):  # noqa: F811
    from bigyear_planner.compact_model import build, load
    from bigyear_planner.daily_policy import prepare_mixed_activity
    from bigyear_planner.eod import prepare_eod

    _, archive, taxonomy, land, _ = eod_input
    config = Config(activity_minutes=720, walking_speed_kmh=2.2,
                    target_locations=10000000, threads=1, memory_limit='256MB')
    prepare_eod(archive, taxonomy, land, tmp_path/'dataset', config,
                'fixture', 'fixture', synthetic=True)
    source = build(tmp_path/'dataset', tmp_path/'physical-model', config)
    source.close()
    physical_path = tmp_path/'physical-model/model.json'
    # The real island model already inherits the same coordinate/road cache
    # binding. A daily-policy derivative must preserve that inheritance too.
    original_path = tmp_path/'original/model.json'
    original_path.parent.mkdir()
    original = read(physical_path)
    for name in original['components']:
        os.link(physical_path.parent/name, original_path.parent/name)
    original['routing_source'] = {'path': str(physical_path), 'sha256': sha256(physical_path)}
    dump(original_path, original)
    report = prepare_mixed_activity(original_path, tmp_path/'mixed')
    assert report['all_observations_retained'] and report['exact_coordinates']
    model = load(tmp_path/'mixed/model.json')
    try:
        assert model.meta['config']['mixed_road_birding'] is True
        assert model.meta['config']['drive_minutes'] == model.meta['config']['activity_minutes'] == 720
        assert model.routing_model_sha256 == sha256(physical_path)
        assert model.meta['components'] == original['components']
        for name in original['components']:
            assert os.stat(model.root/name).st_ino == os.stat(original_path.parent/name).st_ino
        assert not read(original_path)['config']['mixed_road_birding']
    finally:
        model.close()
    bad = read(tmp_path/'mixed/model.json')
    bad['config']['walking_speed_kmh'] = 3
    dump(tmp_path/'mixed/model.json', bad)
    with pytest.raises(ValueError, match='preserve every observation'):
        load(tmp_path/'mixed/model.json')


def test_regional_proposals_use_adjacent_and_longer_same_day_visits(tmp_path):
    import numpy as np
    from types import SimpleNamespace
    from bigyear_planner.regional_exploration import RegionalExplorer
    from bigyear_planner.targeted_exploration import TargetedExplorer

    dtype = [('day', 'u4'), ('location', 'u4'), ('offset', 'u8'),
             ('count', 'u4'), ('reserved', 'u4')]
    records = np.asarray([(5, 0, 0, 1, 0), (5, 1, 1, 1, 0), (6, 2, 2, 1, 0)], dtype=dtype)
    model = SimpleNamespace(records=records, species=np.arange(3, dtype='u4'),
        coords=np.asarray([[40., -90.], [40., -89.], [40., -88.]]),
        meta={'config': Config(mixed_road_birding=True, activity_minutes=720).to_dict(),
              'species': [{'id': str(i), 'name': str(i)} for i in range(3)]},
        path=tmp_path/'model.json')
    regional = RegionalExplorer(model, None, tmp_path)
    assert regional._trip(0, 0, 12, int(regional.regions[0]), []) == [0, 1, 2]
    targeted = TargetedExplorer(model, None, tmp_path, [{'species_id': '0'}])
    assert targeted.campaign([], 0, 3)['trip'] == [0, 1, 2]
    model.meta['config']['mixed_road_birding'] = False
    assert regional._trip(0, 0, 12, int(regional.regions[0]), []) == [0]
    assert targeted.campaign([], 0, 3)['trip'] == [0]


def test_old_mileage_conflict_is_not_reused_for_relaxed_calendar():
    import numpy as np
    from bigyear_planner.calendar_conflicts import pair_conflict

    a = np.asarray([[1, 100, 40., -90., 0]])
    b = np.asarray([[2, 100, 40., -85., 0]])
    config = Config(activity_minutes=720, walking_speed_kmh=2.2).to_dict()
    assert pair_conflict(a, b, config)
    config['mixed_road_birding'] = True
    assert pair_conflict(a, b, config) is None


@pytest.mark.parametrize('minutes,expected', [(720, 1), (721, 0)])
def test_driving_only_day_can_use_twelve_hours(tmp_path, minutes, expected):
    path = tmp_path/'problem.by'
    days = (minutes+719)//720
    path.write_text(f'BY1M 2 2 1 1 0 30 720 720 120\n1 1 1 0\n'
                    f'1\n0 1 600 {minutes}\n1\n0 1 {days} {minutes} 600\n')
    subprocess.run([str(compile_engine()), str(path), str(tmp_path/'search'),
                    '1', '1', '42', '1', '0', '1'], check=True)
    assert read(tmp_path/'search/engine.json')['species'] == expected


@pytest.mark.parametrize('known', ['flight', 'road', 'neither'])
def test_pending_alternative_does_not_block_a_measured_connection(mobility, tmp_path, monkeypatch, known):  # noqa: F811
    import numpy as np
    from bigyear_planner.bounded_roads import SLOT
    from bigyear_planner.regional_exploration import NativeEvaluator
    from bigyear_planner.regional_roads import DeferredRoad

    model, roads = mobility
    roads.transition(0, 1, 0, 4)  # A measured, dated three-day flight connection.
    model.meta['config'].update(mixed_road_birding=True, drive_minutes=720)
    # Keep the synthetic points within the conservative direct-road screen.
    (model.root/'coordinates.bin').write_bytes(np.asarray([[37.,-122.],[37.,-114.]],dtype='<f8').tobytes())
    for offset in roads.store.offsets(0, 1):
        if SLOT.unpack_from(roads.store.data,offset)[:2] == (0, 1):
            roads.store.data[offset:offset+SLOT.size] = bytes(SLOT.size)
    if known == 'road':
        roads.store.put(0, 1, (500., 600.))
    original_costs = roads.costs
    def costs(a,b):
        if (a,b)==(0,1) and known!='road':
            raise DeferredRoad('overland alternative still pending')
        return original_costs(a,b)
    monkeypatch.setattr(roads,'costs',costs)
    if known != 'flight':
        def transition(*args):
            raise DeferredRoad('airport alternative still pending')
        monkeypatch.setattr(roads,'transition',transition)
    with NativeEvaluator(model,roads,tmp_path/'pending-alternative').open() as evaluator:
        score = evaluator.evaluate([0,1],timeout=.1)
    if known == 'neither':
        assert score['pending'] and not score['feasible']
    else:
        assert not score['pending'] and score['feasible'] and score['species']==2
        assert score['minutes'] == (360 if known=='flight' else 600)
