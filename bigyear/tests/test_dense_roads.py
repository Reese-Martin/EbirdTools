import json
import math
import struct

import pytest

from bigyear_planner.dense_roads import DenseRoads, FORMAT, freeze_matrix, location_signature
from bigyear_planner.runner import compile_engine, export_engine_problem
from bigyear_planner.travel import transitions
from bigyear_planner.util import dump, read, sha256


def fixture(tmp_path):
    p = {'config': {'year': 2024, 'start_location': None, 'local_miles': 30,
         'activity_minutes': 720, 'drive_minutes': 480, 'birding_minutes': 120,
         'flights': False}, 'manifest': {'synthetic': True},
         'locations': [{'id': str(i), 'lat': 32.0, 'lon': -87.0-i} for i in range(3)],
         'species': [{'id': str(i)} for i in range(3)],
         'opportunities': [{'day': i * 3, 'location': i, 'species': [i]}
                           for i in range(3)]}
    binary = tmp_path / 'roads.matrix.bin'
    # Asymmetry, one unreachable direction, a road requiring two transit days.
    costs = [(0, 0), (18, 40), (500, 900), (20, 42), (0, 0), (40, 60),
             (math.inf, math.inf), (41, 61), (0, 0)]
    binary.write_bytes(b''.join(struct.pack('<dd', *r) for r in costs))
    manifest = {'format': FORMAT, 'binary': binary.name, 'bytes': binary.stat().st_size,
        'sha256': sha256(binary), 'locations': [location_signature(l) for l in p['locations']],
        'anchors': [[l['lat'], l['lon']] for l in p['locations']], 'location_nodes': [0, 1, 2],
        'source': 'synthetic-fixture', 'network_release': 'fixture', 'reachable_location_pairs': 8}
    path = tmp_path / 'roads.matrix.json'
    dump(path, manifest)
    roads = {(i // 3, i % 3): {'miles': m, 'minutes': t, 'geometry': None}
             for i, (m, t) in enumerate(costs) if math.isfinite(m)}
    return p, path, roads


def test_matrix_and_native_match_sqlite_model(tmp_path):
    import subprocess

    p, path, legacy = fixture(tmp_path)
    dense = DenseRoads(p, path)
    assert len(dense) == len(legacy)
    for key, expected in legacy.items():
        assert dense[key]['miles'] == expected['miles']
        assert dense[key]['minutes'] == expected['minutes']
    assert dense.get((2, 0)) is None
    assert transitions(p, dense).get((0, 2))[0]['days'] == 2
    results = []
    for name, roads in [('legacy', legacy), ('dense', dense)]:
        target = tmp_path / name
        target.mkdir()
        export_engine_problem(p, roads, transitions(p, roads), target / 'problem.by')
        subprocess.run([str(compile_engine()), str(target / 'problem.by'), str(target),
                        '10', '1', '42', '5', '2', '1'], check=True)
        result = read(target / 'engine.json')
        results.append({k: result[k] for k in ('species', 'travel_minutes', 'road_miles', 'selected')})
    assert results[0] == results[1]
    dense.close()


def test_matrix_detects_anchor_change_and_corruption(tmp_path):
    p, path, _ = fixture(tmp_path)
    changed = json.loads(json.dumps(p))
    changed['locations'][0]['road_access'] = {'lat': 33, 'lon': -87}
    with pytest.raises(ValueError, match='anchors differ'):
        DenseRoads(changed, path)
    binary = path.parent / read(path)['binary']
    binary.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='truncated'):
        DenseRoads(p, path)


def test_matrix_snapshot_requires_read_only_binary(tmp_path):
    p, path, _ = fixture(tmp_path)
    target = tmp_path / 'snapshot'
    target.mkdir()
    with pytest.raises(ValueError, match='read-only'):
        freeze_matrix(path, target / path.name)
    binary = path.parent / read(path)['binary']
    binary.chmod(0o444)
    frozen = freeze_matrix(path, target / path.name)
    assert sha256(frozen) == sha256(path)
    roads = DenseRoads(p, frozen)
    assert roads[0, 1]['miles'] == 18
    roads.close()


def test_osrm_matrix_preserves_direction_missing_costs_and_duplicate_anchors(tmp_path, monkeypatch):
    import io
    import urllib.parse
    from bigyear_planner.dense_roads import osrm_matrix

    p, _, _ = fixture(tmp_path)
    # A fourth observation location shares the exact first road anchor, never its evidence ID.
    p['locations'].append({'id': 'different-evidence-pin', 'lat': 32.001, 'lon': -87.001,
                           'road_access': {'lat': 32, 'lon': -87}})
    problem_path = tmp_path / 'problem.json'
    dump(problem_path, p)
    requests = []

    def response(url, timeout):
        requests.append(url)
        parts = urllib.parse.urlsplit(url)
        points = parts.path.rsplit('/', 1)[1].split(';')
        nodes = [round(-87 - float(pt.split(',')[0])) for pt in points]
        params = urllib.parse.parse_qs(parts.query)
        if '/route/' in parts.path:
            data = {'code': 'Ok', 'routes': [{'duration': .1, 'distance': .6}]}
        else:
            durations, distances = [], []
            for a in map(int, params['sources'][0].split(';')):
                tt, mm = [], []
                for b in map(int, params['destinations'][0].split(';')):
                    x, y = nodes[a], nodes[b]
                    t, m = (None, None) if (x, y) == (2, 0) else (100*x+10*y, 1000*x+100*y)
                    if (x, y) == (0, 1):
                        t, m = 0, -.6
                    tt.append(t); mm.append(m)
                durations.append(tt); distances.append(mm)
            data = {'code': 'Ok', 'durations': durations, 'distances': distances}
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr('urllib.request.urlopen', response)
    path = tmp_path / 'generated.matrix.json'
    result = osrm_matrix(problem_path, 'http://router', path, 'network', batch=2)
    assert len(result['anchors']) == 3 and len(result['location_nodes']) == 4
    assert result['reachable_location_pairs'] == 14
    assert len(result['repaired_pairs']) == 1
    roads = DenseRoads(p, path)
    assert roads[0, 1]['miles'] == .6 / 1609.344
    assert roads[3, 1] == roads[0, 1]
    assert roads.get((2, 0)) is None and roads.get((2, 3)) is None
    assert roads[1, 0]['minutes'] == 100 / 60
    roads.close()
    count = len(requests)
    assert osrm_matrix(problem_path, 'http://router', path, 'network', batch=2) == result
    assert len(requests) == count


def test_native_matrix_all_calendar_subsets_match_legacy(tmp_path):
    import shutil
    import subprocess
    from bigyear_planner.util import ROOT

    p, path, legacy = fixture(tmp_path)
    p['opportunities'] = [{'day': day, 'location': loc, 'species': [loc]}
                          for day in (0, 1, 3, 7) for loc in range(3)]
    dense = DenseRoads(p, path)
    for name, roads in [('legacy', legacy), ('dense', dense)]:
        export_engine_problem(p, roads, transitions(p, roads), tmp_path / f'{name}.by')
    source = tmp_path / 'equivalence.cpp'
    source.write_text('''#include "model.h"
    int main(int argc,char**argv) {
        Problem a(argv[1]),b(argv[2]);
        for(int mask=0;mask<(1<<a.nopp);++mask) {
            std::vector<int> route;
            for(int i=0;i<a.nopp;++i)if(mask&(1<<i))route.push_back(i);
            a.canonical(route);auto x=a.evaluate(route),y=b.evaluate(route);
            if(x.feasible!=y.feasible || x.species!=y.species ||
               std::abs(x.miles-y.miles)>1e-7 || std::abs(x.minutes-y.minutes)>1e-7)return 1;
        }
        return 0;
    }''')
    executable = tmp_path / 'equivalence'
    subprocess.run([shutil.which('c++'), '-std=c++17', '-O2', '-I', str(ROOT / 'cpp'),
                    str(source), '-o', str(executable)], check=True)
    subprocess.run([str(executable), str(tmp_path / 'legacy.by'), str(tmp_path / 'dense.by')], check=True)
    dense.close()
