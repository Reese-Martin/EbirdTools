import io
import json

import pytest

from bigyear_planner.display import DISPLAY_FILE, build_display_roads, display_route
from bigyear_planner.travel import cache
from bigyear_planner.util import dump, read, sha256


@pytest.fixture
def saved_route(tmp_path, monkeypatch):
    folder = tmp_path / "run"
    folder.mkdir()
    with cache(folder / "roads.sqlite") as con:
        con.execute("INSERT INTO roads(origin,destination,miles,minutes,source,network_release) "
                    "VALUES ('a','b',2,4,'local-router','network-1')")
        con.execute("UPDATE roads SET origin_lat=33,origin_lon=-87.1,destination_lat=33.01,destination_lon=-87.09")
    road = {"from": 0, "to": 1, "mode": "road", "miles": 1, "minutes": 2,
            "start_fraction": .25, "end_fraction": .75, "geometry": None, "schematic": True}
    flight = {"from": 1, "to": 0, "mode": "flight", "miles": 0, "minutes": 90,
              "start_fraction": 0, "end_fraction": 1, "geometry": None, "schematic": True}
    route = {"year": 2024, "species_total": 1, "synthetic": False,
             "locations": [{"id": "a", "lat": 33, "lon": -87.1},
                           {"id": "b", "lat": 33.01, "lon": -87.09}],
             "days": [{"legs": [road]}, {"legs": [flight]}],
             "provenance": {"roads_sha256": sha256(folder / "roads.sqlite")}}
    path = folder / "route.json"
    dump(path, route)
    measured = {"duration": 240, "distance": 2 * 1609.344,
                "geometry": {"type": "LineString", "coordinates": [
                    [-87.1, 33], [-87.1, 33.01], [-87.09, 33.01]]}}

    def response(url, timeout):
        assert 'radiuses=1;1' in url
        return io.BytesIO(json.dumps({"code": "Ok", "routes": [measured],
            'waypoints': [{'location': [-87.1, 33]}, {'location': [-87.09, 33.01]}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", response)
    return tmp_path, path, route, measured


def test_display_adds_driving_shape_without_changing_source_costs_or_flights(saved_route):
    root, path, original, measured = saved_route
    original_hash = sha256(path)
    result = build_display_roads(root, "http://router", "network-1")
    assert result["unique_driving_shapes"] == 1
    shown = display_route(root, path)
    road = shown["days"][0]["legs"][0]
    assert road["geometry"] == measured["geometry"] and not road["schematic"]
    assert (road["miles"], road["minutes"], road["start_fraction"], road["end_fraction"]) == (1, 2, .25, .75)
    assert shown["days"][1] == original["days"][1]
    assert shown["locations"] == original["locations"]
    assert shown["display_geometry"]["verified_road_legs"] == 1
    assert sha256(path) == original_hash and read(path) == original


def test_stale_or_incompatible_display_shapes_are_not_used(saved_route):
    root, path, original, _ = saved_route
    build_display_roads(root, "http://router", "network-1")
    supplement = read(root / DISPLAY_FILE)
    next(iter(supplement["shapes"].values()))["miles"] = 100
    dump(root / DISPLAY_FILE, supplement)
    assert display_route(root, path) == original
    changed = {**original, "species_total": 2}
    dump(path, changed)
    assert display_route(root, path) == changed


def test_display_rechecks_endpoints_even_when_costs_and_bindings_match(saved_route):
    root, path, original, _ = saved_route
    build_display_roads(root, 'http://router', 'network-1')
    supplement = read(root / DISPLAY_FILE)
    next(iter(supplement['shapes'].values()))['geometry']['coordinates'][-1][0] += 1
    dump(root / DISPLAY_FILE, supplement)
    assert display_route(root, path) == original


@pytest.mark.parametrize("field", ["distance", "duration"])
def test_route_service_must_match_frozen_costs(saved_route, field):
    root, path, original, measured = saved_route
    measured[field] += 10
    with pytest.raises(ValueError, match="differs from frozen"):
        build_display_roads(root, "http://router", "network-1")
    assert not (root / DISPLAY_FILE).exists()
    assert read(path) == original


def test_count_bound_is_bound_to_both_frozen_inputs(tmp_path):
    from bigyear_planner.display import attach_count_bound
    route = {'species_total': 343, 'annual_recorded_species_ceiling': 344,
             'provenance': {'problem_sha256': 'problem', 'roads_sha256': 'roads'}}
    certificate = {'valid': True, 'upper_bound': 343, 'eligible_species_ceiling': 344,
                   'problem_sha256': 'problem', 'roads_manifest_sha256': 'roads', 'scope': 'Count only'}
    dump(tmp_path / 'count-bound.json', certificate)
    shown = attach_count_bound(tmp_path, {**route})
    assert shown['analytical_count_bound']['upper_bound'] == 343
    assert 'analytical_count_bound' not in route
    for change in [{'problem_sha256': 'other'}, {'roads_manifest_sha256': 'other'},
                   {'upper_bound': 342}, {'valid': False}]:
        dump(tmp_path / 'count-bound.json', {**certificate, **change})
        assert attach_count_bound(tmp_path, {**route}) == route


def test_vehicle_ferry_steps_are_display_metadata_not_changed_frozen_modes(saved_route):
    root, path, original, measured = saved_route
    measured['legs'] = [{'steps': [{'mode': 'ferry', 'name': 'Vehicle ferry',
                                    'duration': 90, 'distance': 1609.344}]}]
    build_display_roads(root, 'http://router', 'network-1')
    shown = display_route(root, path)
    leg = shown['days'][0]['legs'][0]
    assert leg['mode'] == 'road' and leg['minutes'] == original['days'][0]['legs'][0]['minutes']
    assert leg['vehicle_ferry_segments'] == [{'name': 'Vehicle ferry', 'minutes': 1.5, 'miles': 1}]
    assert read(path) == original
