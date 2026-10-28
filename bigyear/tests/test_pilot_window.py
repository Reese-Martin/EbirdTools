import copy

import pytest

from bigyear_planner.config import Config
from bigyear_planner.demo import fixtures, synthetic_roads
from bigyear_planner.ingest import prepare
from bigyear_planner.problem import build_problem
from bigyear_planner.runner import run
from bigyear_planner.review import review_route
from bigyear_planner.travel import import_roads, load_roads
from bigyear_planner.util import dump, read, sha256, write_csv
from bigyear_planner.validate import validate


def test_short_window_uses_relative_days_and_independent_calendar_audit(tmp_path):
    raw = fixtures(tmp_path / "raw", years=(2024,), small=True)
    cfg = Config(start_date="2024-01-03", end_date="2024-01-05", activity_minutes=720)
    assert cfg.days == 3
    dataset = tmp_path / "dataset"
    prepare(
        raw / "ebd-2024.tsv",
        raw / "taxonomy.csv",
        raw / "land.csv",
        dataset,
        cfg,
        "test",
        "test",
        synthetic=True,
    )
    problem = tmp_path / "problem.json"
    p = build_problem(dataset, problem, cfg)
    assert {o["day"] for o in p["opportunities"]} == {0, 2}
    assert all(cfg.start_date <= o["date"] <= cfg.end_date for o in p["opportunities"])
    roads = tmp_path / "roads.sqlite"
    synthetic_roads(problem, tmp_path / "roads.csv", roads)
    output = tmp_path / "run"
    route = run(problem, roads, output, seconds=1, exact=1)
    assert [d["date"] for d in route["days"]] == ["2024-01-03", "2024-01-04", "2024-01-05"]
    assert read(output / "validation.json")["valid"]
    original_hash = sha256(output / "route.json")
    report = review_route(output)
    assert report["species_total"] == route["species_total"]
    assert report["stops_reviewed"] == sum(len(d["stops"]) for d in route["days"])
    assert report["route_sha256"] == original_hash == sha256(output / "route.json")
    assert any(r["removal_feasible"] and r["species_lost_if_removed"] for r in report["stops"])
    route["days"].insert(0, copy.deepcopy(route["days"][0]))
    dump(output / "tampered.json", route)
    assert not validate(output / "tampered.json", output / "problem.json", output / "roads.sqlite")[
        "valid"
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"year": 2025, "start_date": "2024-12-31"},
        {"year": 2025, "start_date": "2025-03-31", "end_date": "2025-03-01"},
        {"year": 2025, "end_date": "2025-02-29"},
    ],
)
def test_invalid_calendar_window_rejected(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


def test_distinct_reported_pins_can_share_road_access_without_free_relocation(tmp_path):
    point = {"lat": 33.0, "lon": -87.0, "source": "test", "walking_policy": "included in stop"}
    p = {
        "locations": [
            {"id": "a", "lat": 33.001, "lon": -87.0, "road_access": dict(point)},
            {"id": "b", "lat": 33.002, "lon": -87.0, "road_access": dict(point)},
        ],
        "manifest": {"synthetic": False},
    }
    dump(tmp_path / "problem.json", p)
    write_csv(
        tmp_path / "roads.csv",
        [
            dict(
                origin="a",
                destination="b",
                miles=0,
                minutes=0,
                source="test road access",
                network_release="test",
            )
        ],
        ["origin", "destination", "miles", "minutes", "source", "network_release"],
    )
    import_roads(tmp_path / "problem.json", tmp_path / "roads.csv", tmp_path / "roads.sqlite")
    roads = load_roads(p, tmp_path / "roads.sqlite")
    assert roads[0, 1]["miles"] == 0
    assert p["locations"][0]["lat"] == 33.001
    roads.con.close()
    p["locations"][0]["road_access"]["lat"] += 0.01
    roads = load_roads(p, tmp_path / "roads.sqlite")
    with pytest.raises(ValueError, match="anchors"):
        _ = roads[0, 1]
    roads.con.close()
