import io
import json
import sqlite3

import pytest

from bigyear_planner.travel import osrm_roads
from bigyear_planner.util import dump


@pytest.mark.parametrize("route_distance", [.7, -.6])
def test_negative_table_cost_uses_measured_route_or_fails(tmp_path, monkeypatch, route_distance):
    problem = tmp_path / "problem.json"
    dump(problem, {"locations": [
        {"id": "a", "lat": 33.255985, "lon": -87.588532},
        {"id": "b", "lat": 33.255984, "lon": -87.588526}]})
    requests = []

    def response(url, timeout):
        requests.append(url)
        if "/table/" in url:
            data = {"code": "Ok", "durations": [[0, 0], [0, 0]],
                    "distances": [[0, -.6], [-.6, 0]]}
        else:
            data = {"code": "Ok", "routes": [{"duration": .1, "distance": route_distance,
                    "geometry": {"type": "LineString", "coordinates": [
                        [-87.588532, 33.255985], [-87.588526, 33.255984]]}}]}
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr("urllib.request.urlopen", response)
    cache = tmp_path / "roads.sqlite"
    if route_distance < 0:
        with pytest.raises(ValueError, match="invalid costs in both"):
            osrm_roads(problem, "http://local-router", cache, "fixture", batch=2)
        return
    osrm_roads(problem, "http://local-router", cache, "fixture", batch=2)
    assert sum("/route/" in url for url in requests) == 2
    with sqlite3.connect(cache) as con:
        rows = con.execute("SELECT miles,geometry FROM roads WHERE origin!=destination").fetchall()
        assert all(abs(r[0] - .7 / 1609.344) < 1e-12 and r[1] for r in rows)
        # A resumed cache must repair an existing invalid entry instead of skipping it.
        con.execute("UPDATE roads SET miles=-1 WHERE origin='a' AND destination='b'")
    osrm_roads(problem, "http://local-router", cache, "fixture", batch=2)
    assert sum("/route/" in url for url in requests) == 4
