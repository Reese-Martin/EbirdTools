import copy
import io
import json
import math
import urllib.error

import pytest

from bigyear_planner.bounded_roads import BoundedRoads, freeze_used_roads
from bigyear_planner.road_matching import POLICY, evidence, request_route, verify_evidence
from bigyear_planner.travel import RoadCache
from bigyear_planner.util import dump, read
from test_bounded_roads import provider
from test_pipeline import imported  # noqa: F401 - pytest fixture


ANCHORS = [[-87., 32.], [-88., 32.]]


def response():
    return {'code': 'Ok', 'waypoints': [{'location': p[:], 'distance': 0} for p in ANCHORS],
            'routes': [{'distance': 100, 'duration': 10,
                        'geometry': {'type': 'LineString', 'coordinates': copy.deepcopy(ANCHORS)}}]}


@pytest.mark.parametrize('failure', ['missing-waypoints', 'shifted-waypoint', 'shifted-geometry',
                                    'nonfinite-vertex', 'negative-cost'])
def test_reject_malformed_or_rematched_ok_without_caching(tmp_path, monkeypatch, failure):
    p, path, initial, _ = provider(tmp_path)
    initial.close()
    result = response()
    if failure == 'missing-waypoints':
        result.pop('waypoints')
    elif failure == 'shifted-waypoint':
        result['waypoints'][1]['location'][0] += 1  # Faked reported distance remains zero.
    elif failure == 'shifted-geometry':
        result['routes'][0]['geometry']['coordinates'][-1][0] += 1
    elif failure == 'nonfinite-vertex':
        result['routes'][0]['geometry']['coordinates'].insert(1, [math.nan, 32])
    else:
        result['routes'][0]['distance'] = -1
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(json.dumps(result).encode()))
    with BoundedRoadsForTest(p, path) as roads:
        if failure in {'shifted-geometry', 'nonfinite-vertex'}:
            with pytest.raises(ValueError):
                roads.endpoint_evidence(0, 1, {'miles': 100/1609.344, 'minutes': 10/60})
        else:
            for _ in range(2):
                with pytest.raises(ValueError):
                    roads[0, 1]
            assert roads.store.stats['writes'] == 0 and roads.stats['provider_errors'] == 2


class BoundedRoadsForTest(BoundedRoads):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@pytest.mark.parametrize('http', [False, True])
def test_explicit_no_segment_unavailable_and_cached(tmp_path, monkeypatch, http):
    p, path, initial, _ = provider(tmp_path)
    initial.close()
    def missing(url, timeout):
        body = io.BytesIO(b'{"code":"NoSegment"}')
        if http:
            raise urllib.error.HTTPError(url, 400, 'NoSegment', {}, body)
        return body
    monkeypatch.setattr('urllib.request.urlopen', missing)
    with BoundedRoadsForTest(p, path) as roads:
        assert roads.get((0, 1)) is None and roads.get((0, 1)) is None
        assert roads.stats['unreachable_queries'] == 1
        assert roads.store.stats['hits'] == 1


def test_v1_and_other_policy_are_rejected(tmp_path):
    p, path, initial, _ = provider(tmp_path)
    initial.close()
    original = read(path)
    for change in [{'format': 'bigyear-bounded-roads-v1'},
                   {'matching_policy': {**POLICY, 'radius_m': 100}}]:
        dump(path, {**original, **change})
        with pytest.raises(ValueError):
            BoundedRoads(p, path)


def test_selected_evidence_is_verified_offline_and_detects_tampering(tmp_path, monkeypatch):
    p, path, initial, _ = provider(tmp_path)
    initial.close()
    p['manifest']['synthetic'] = False
    def measured(url, timeout):
        assert 'radiuses=1;1' in url and 'snapping=default' in url
        return io.BytesIO(json.dumps(response()).encode())
    monkeypatch.setattr('urllib.request.urlopen', measured)
    with BoundedRoadsForTest(p, path) as roads:
        freeze_used_roads(p, roads, [{'days': [{'legs': [{'mode': 'road', 'from': 0, 'to': 1}]}]}],
                          tmp_path/'frozen.sqlite')
    path.with_suffix('.bin').unlink()
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('offline validation queried service'))
    saved = RoadCache(p, tmp_path/'frozen.sqlite')
    road = saved[0, 1]
    assert max(verify_evidence(road['endpoint_evidence'], ANCHORS, road)) == 0
    saved.close()
    for mutation in ['missing', 'geometry', 'waypoint', 'cost', 'policy']:
        proof = copy.deepcopy(road['endpoint_evidence'])
        if mutation == 'missing':
            proof = None
        elif mutation == 'geometry':
            proof['routes'][0]['geometry']['coordinates'][-1][0] += 1
        elif mutation == 'waypoint':
            proof['waypoints'][1]['location'][0] += 1
        elif mutation == 'cost':
            proof['routes'][0]['distance'] += 10
        else:
            proof['policy'] = {**POLICY, 'radius_m': 100}
        with pytest.raises(ValueError):
            verify_evidence(proof, ANCHORS, road)


def test_small_rounding_gap_allowed_but_two_meter_match_rejected(monkeypatch):
    data = response()
    data['waypoints'][0]['location'][1] += .000001
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(json.dumps(data).encode()))
    assert request_route('http://fixture', ANCHORS)['code'] == 'Ok'
    proof = evidence(data, ANCHORS, 'network')
    assert proof['policy'] == POLICY
    data['waypoints'][0]['location'][1] += .00002
    with pytest.raises(ValueError, match='endpoint mismatch'):
        request_route('http://fixture', ANCHORS)


def test_shared_anchor_has_zero_cost_proof_without_contacting_router(tmp_path, monkeypatch):
    from bigyear_planner.bounded_roads import create_bounded_roads
    p, _, initial, _ = provider(tmp_path)
    initial.close()
    p['locations'][1]['road_access'] = {k: p['locations'][0][k] for k in ('lat', 'lon')}
    dump(tmp_path/'shared.json', p)
    path = tmp_path/'shared.bounded.json'
    create_bounded_roads(tmp_path/'shared.json', path, 'http://fixture', 'network', 'sha', capacity_mib=1)
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('shared anchor needs no route'))
    with BoundedRoadsForTest(p, path) as roads:
        road = roads[0, 1]
        assert road['miles'] == road['minutes'] == 0
        proof = roads.endpoint_evidence(0, 1, road)
        assert verify_evidence(proof, [ANCHORS[0], ANCHORS[0]], road) == [0, 0, 0, 0]


def test_calendar_validator_requires_real_endpoint_evidence_and_rechecks_geometry(request):
    """Adversarial labels on fictional data exercise the real-data validation path."""
    from bigyear_planner.demo import synthetic_roads
    from bigyear_planner.problem import build_problem
    from bigyear_planner.runner import run
    from bigyear_planner.travel import cache
    from bigyear_planner.util import sha256
    from bigyear_planner.validate import validate
    dataset, config, tmp = request.getfixturevalue('imported')
    build_problem(dataset, tmp/'problem.json', config)
    synthetic_roads(tmp/'problem.json', tmp/'roads.csv', tmp/'source.sqlite')
    folder = tmp/'saved'
    route = run(tmp/'problem.json', tmp/'source.sqlite', folder, seconds=.1)
    manifest = read(dataset/'manifest.json')
    manifest['synthetic'] = False
    dump(dataset/'manifest.json', manifest)
    p = read(folder/'problem.json')
    p['manifest'] = manifest
    p['manifest_sha256'] = sha256(dataset/'manifest.json')
    dump(folder/'problem.json', p)
    route['synthetic'] = False
    route['provenance'].update({'problem_sha256': sha256(folder/'problem.json'),
                               'road_network_sha256': 'fixture-network', 'road_matching_policy': POLICY,
                               'dataset_manifest_sha256': p['manifest_sha256']})
    with cache(folder/'roads.sqlite') as con:
        con.execute("UPDATE roads SET source='adversarial-test-fixture'")
    def validate_current():
        route['provenance']['roads_sha256'] = sha256(folder/'roads.sqlite')
        dump(folder/'route.json', route)
        return validate(folder/'route.json', folder/'problem.json', folder/'roads.sqlite')
    missing = validate_current()
    assert not missing['valid'] and any('endpoint audit failed' in e for e in missing['errors'])
    pairs = {(leg['from'], leg['to']) for d in route['days'] for leg in d['legs'] if leg['mode'] == 'road'}
    assert pairs
    with cache(folder/'roads.sqlite') as con:
        for a,b in pairs:
            x,y = [p['locations'][i] for i in (a,b)]
            anchors = [[l['lon'], l['lat']] for l in (x,y)]
            miles, minutes = con.execute('SELECT miles,minutes FROM roads WHERE origin=? AND destination=?',
                                         (x['id'],y['id'])).fetchone()
            data = {'waypoints': [{'location': point} for point in anchors], 'routes': [
                {'distance': miles*1609.344, 'duration': minutes*60,
                 'geometry': {'type': 'LineString', 'coordinates': anchors}}]}
            proof = evidence(data, anchors, 'fixture-network')
            con.execute('INSERT INTO road_endpoints VALUES (?,?,?)', (x['id'],y['id'],json.dumps(proof)))
    assert validate_current()['valid']
    with cache(folder/'roads.sqlite') as con:
        a,b,raw = con.execute('SELECT * FROM road_endpoints LIMIT 1').fetchone()
        proof = json.loads(raw)
        proof['routes'][0]['geometry']['coordinates'][-1][0] += 1
        con.execute('UPDATE road_endpoints SET evidence=? WHERE origin=? AND destination=?',
                    (json.dumps(proof),a,b))
    tampered = validate_current()
    assert not tampered['valid'] and any('endpoint mismatch' in e for e in tampered['errors'])
