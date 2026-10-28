import copy
import csv
from dataclasses import replace
import subprocess

import duckdb
import pytest

from bigyear_planner.config import Config
from bigyear_planner.demo import fixtures, synthetic_roads
from bigyear_planner.ingest import prepare
from bigyear_planner.problem import build_problem
from bigyear_planner.runner import compile_engine, run
from bigyear_planner.util import dump, read, write_csv
from bigyear_planner.validate import validate


@pytest.fixture
def imported(tmp_path):
    raw = fixtures(tmp_path / "raw", years=(2024,), small=True)
    config = replace(Config(), seconds=0.1, threads=1)
    dataset = tmp_path / "dataset"
    prepare(
        raw / "ebd-2024.tsv",
        raw / "taxonomy.csv",
        raw / "land.csv",
        dataset,
        config,
        "fixture",
        "fixture",
        synthetic=True,
    )
    return dataset, config, tmp_path


def test_import_exact_taxonomy_land_and_evidence(imported):
    dataset, config, tmp = imported
    m = read(dataset / "manifest.json")
    assert m["statistics"]["annual_recorded_species_ceiling"] == 7
    assert m["statistics"]["dispositions"]["unmapped_taxon_review"] == 1
    assert m["statistics"]["dispositions"]["not_validated"] == 1
    assert m["statistics"]["dispositions"]["unresolved_or_hybrid"] == 1
    assert m["statistics"]["dispositions"]["land_unknown_review"] == 1
    con = duckdb.connect()
    assert (
        con.execute(
            "SELECT count(*) FROM read_parquet(?) WHERE bird_count='X'",
            [str(dataset / "evidence.parquet")],
        ).fetchone()[0]
        > 0
    )
    assert (
        con.execute(
            "SELECT count(*) FROM read_parquet(?) WHERE source_name='Demo taxon subspecies' AND species_id='DEMO000'",
            [str(dataset / "evidence.parquet")],
        ).fetchone()[0]
        == 1
    )
    # No interpolated daily presence: only reported dates survive.
    assert (
        con.execute(
            "SELECT count(*) FROM read_parquet(?) WHERE date='2024-01-04'",
            [str(dataset / "observations.parquet")],
        ).fetchone()[0]
        == 0
    )


def test_full_calendar_and_independent_tamper_rejection(imported):
    dataset, config, tmp = imported
    problem = tmp / "problem.json"
    build_problem(dataset, problem, config)
    roads = tmp / "roads.sqlite"
    synthetic_roads(problem, tmp / "roads.csv", roads)
    output = tmp / "run"
    route = run(problem, roads, output, seconds=0.15)
    assert len(route["days"]) == 366
    assert read(output / "validation.json")["valid"]
    assert route["species_total"] > 0
    bad = copy.deepcopy(route)
    stop = next(s for d in bad["days"] for s in d["stops"])
    stop["evidence"][stop["species"][0]][0]["checklist"] = "S999999999"
    dump(output / "tampered.json", bad)
    result = validate(output / "tampered.json", output / "problem.json", output / "roads.sqlite")
    assert not result["valid"]
    assert any("fabricated" in e for e in result["errors"])
    bad = copy.deepcopy(route)
    bad["days"][0]["kind"] = "road_transit"
    bad["days"][0]["new_species"] = ["DEMO000"]
    dump(output / "tampered.json", bad)
    assert not validate(output / "tampered.json", output / "problem.json", output / "roads.sqlite")[
        "valid"
    ]


def engine_case(
    tmp_path, opportunities, edges, species=3, start=-1, exact=True, plans=(), bird=120
):
    text = f"BY1 366 3 {species} {len(opportunities)} {start} 30 480 480 {bird}\n"
    for day, loc, spp in opportunities:
        text += f"{day} {loc} {len(spp)} " + " ".join(map(str, spp)) + "\n"
    text += str(len(edges)) + "\n"
    for a, b, m, t in edges:
        text += f"{a} {b} {m} {t}\n"
    # Make normal road transit plans explicit, matching the coarse boundary.
    roads = [(a, b, int(-(-t // 480)), t, m) for a, b, m, t in edges]
    pp = roads + list(plans)
    text += str(len(pp)) + "\n"
    for a, b, d, t, m in pp:
        text += f"{a} {b} {d} {t} {m}\n"
    problem = tmp_path / "test.by"
    problem.write_text(text)
    output = tmp_path / "engine"
    subprocess.run(
        [
            str(compile_engine()),
            str(problem),
            str(output),
            "2",
            "1",
            "42",
            "5",
            "2",
            "1" if exact else "0",
        ],
        check=True,
    )
    return read(output / "engine.json")


def test_exact_known_optimum_cannot_combine_distant_same_day(tmp_path):
    # A and B share a date, but 31 road miles separates them. C is reachable later.
    result = engine_case(
        tmp_path,
        [(0, 0, [0]), (0, 1, [1]), (2, 2, [2])],
        [(0, 1, 31, 35), (1, 0, 31, 35), (0, 2, 40, 60), (1, 2, 40, 60)],
    )
    assert result["optimal"]
    assert result["species"] == 2


def test_exact_local_budget_accumulates_and_presence_is_date_specific(tmp_path):
    # Pairwise 20-mile edges would incorrectly permit 40 miles over a day.
    result = engine_case(
        tmp_path,
        [(0, 0, [0]), (0, 1, [1]), (0, 2, [2])],
        [(0, 1, 20, 20), (1, 2, 20, 20), (0, 2, 40, 40)],
    )
    assert result["species"] == 2


def test_exact_activity_and_fixed_start_transit_exclusion(tmp_path):
    result = engine_case(
        tmp_path,
        [(0, 1, [0]), (1, 1, [1]), (3, 2, [2])],
        [(0, 1, 400, 480), (1, 2, 20, 30)],
        start=0,
    )
    assert result["species"] == 2  # Jan 1 must be transit, so its opportunity is unavailable.
    assert 0 not in result["selected"]


def test_exact_multiple_local_stops_can_hit_known_optimum(tmp_path):
    result = engine_case(
        tmp_path,
        [(0, 0, [0]), (0, 1, [1]), (0, 2, [2])],
        [(0, 1, 10, 20), (1, 2, 10, 20), (0, 2, 20, 40)],
    )
    assert result["species"] == 3 and result["optimal"]


def test_exact_eight_hour_activity_limit(tmp_path):
    result = engine_case(
        tmp_path,
        [(0, 0, [0]), (0, 1, [1]), (0, 2, [2])],
        [(0, 1, 1, 5), (1, 2, 1, 5), (0, 2, 2, 10)],
        bird=240,
    )
    assert result["species"] == 1  # Two four-hour stops plus travel exceed the budget.


def test_airport_flight_requires_a_whole_nonbirding_day(tmp_path):
    # Synthetic precomputed airport plan: one flight transit day, then birding.
    result = engine_case(
        tmp_path, [(0, 0, [0]), (1, 1, [1]), (2, 1, [2])], [], plans=[(0, 1, 1, 180, 0)]
    )
    assert result["species"] == 2
    assert not ({0, 1} <= set(result["selected"]))


def test_missing_roads_never_use_geodesic_fallback(tmp_path):
    result = engine_case(tmp_path, [(0, 0, [0]), (10, 1, [1]), (20, 2, [2])], [])
    assert result["species"] == 1


def test_secondary_objective_prefers_less_travel(tmp_path):
    result = engine_case(
        tmp_path, [(0, 1, [0]), (0, 2, [0])], [(0, 1, 20, 30), (0, 2, 10, 15)], species=1, start=0
    )
    assert result["species"] == 1
    assert result["selected"] == [1]
    assert result["travel_minutes"] == 15


def test_flight_export_and_validator(imported):
    dataset, cfg, tmp = imported
    initial = build_problem(dataset, tmp / "initial.json", cfg)
    by_local = {l["locality_id"]: l for l in initial["locations"]}
    a, b = by_local["LDEMO0"], by_local["LDEMO1"]
    airport_rows = [
        {
            "airport_id": name,
            "name": name + " synthetic airport",
            "latitude": l["lat"] + 0.01,
            "longitude": l["lon"],
            "state": l["state"],
            "on_land": "1",
        }
        for name, l in [("AAA", a), ("BBB", b)]
    ]
    write_csv(tmp / "airports.csv", airport_rows, list(airport_rows[0]))
    write_csv(
        tmp / "flights.csv",
        [{"from_airport": "AAA", "to_airport": "BBB", "minutes": 120}],
        ["from_airport", "to_airport", "minutes"],
    )
    cfg = replace(cfg, flights=True, start_location=a["id"])
    problem = tmp / "problem.json"
    p = build_problem(
        dataset, problem, cfg, airports=tmp / "airports.csv", flights=tmp / "flights.csv"
    )
    chosen = [
        next(
            o
            for o in p["opportunities"]
            if p["locations"][o["location"]]["locality_id"] == "LDEMO0" and o["day"] == 2
        ),
        next(
            o
            for o in p["opportunities"]
            if p["locations"][o["location"]]["locality_id"] == "LDEMO1" and o["day"] == 14
        ),
    ]
    for i, o in enumerate(chosen):
        o["id"] = i
    p["opportunities"] = chosen
    dump(problem, p)
    synthetic_roads(problem, tmp / "roads.csv", tmp / "roads.sqlite")
    route = run(problem, tmp / "roads.sqlite", tmp / "run", seconds=2, exact=1)
    assert any(d["kind"] == "flight" for d in route["days"])
    assert all(not d["stops"] for d in route["days"] if d["kind"] in {"flight", "road_transit"})
    assert read(tmp / "run" / "validation.json")["valid"]


def test_land_helper_uses_polygon_not_display_boundaries(tmp_path):
    from bigyear_planner.ingest import classify_land

    raw = fixtures(tmp_path / "raw", years=(2024,), small=True)
    polygon = {
        "type": "Polygon",
        "coordinates": [[[-122, 36], [-121, 36], [-121, 37], [-122, 37], [-122, 36]]],
    }
    dump(tmp_path / "mask.geojson", polygon)
    classify_land(raw / "ebd-2024.tsv", tmp_path / "mask.geojson", tmp_path / "land.csv")
    rows = list(csv.DictReader((tmp_path / "land.csv").open()))
    assert next(r["on_land"] for r in rows if r["locality_id"] == "LDEMO0") == "1"
    assert next(r["on_land"] for r in rows if r["locality_id"] == "LDEMO1") == "0"


def test_explicit_offshore_and_exact_point_keys(tmp_path):
    raw = fixtures(tmp_path / "raw", years=(2024,), small=True)
    rows = list(csv.DictReader((raw / "land.csv").open()))
    offshore = dict(rows[0])
    offshore["locality_id"] = "LDEMO-OFFSHORE"
    offshore["on_land"] = "0"
    rows.append(offshore)
    rows[0]["latitude"] = str(float(rows[0]["latitude"]) + 0.000001)
    write_csv(raw / "land.csv", rows, list(rows[0]))
    m = prepare(
        raw / "ebd-2024.tsv",
        raw / "taxonomy.csv",
        raw / "land.csv",
        tmp_path / "dataset",
        Config(),
        "fixture",
        "fixture",
        synthetic=True,
    )
    assert m["statistics"]["dispositions"]["offshore_excluded"] == 1
    assert m["statistics"]["dispositions"]["land_unknown_review"] > 1


def test_shared_checklist_collapses_but_all_provenance_survives(tmp_path):
    raw = fixtures(tmp_path / "raw", years=(2024,), small=True)
    ebd = raw / "ebd-2024.tsv"
    with ebd.open(newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
        names = list(rows[0])
    rows[0]["GROUP IDENTIFIER"] = "G-SHARED"
    duplicate = dict(rows[0])
    duplicate["SAMPLING EVENT IDENTIFIER"] = "DEMO-SHARED"
    duplicate["GLOBAL UNIQUE IDENTIFIER"] = "OBS-SHARED"
    rows.append(duplicate)
    with ebd.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=names, delimiter="\t")
        w.writeheader()
        w.writerows(rows)
    out = tmp_path / "dataset"
    m = prepare(
        ebd,
        raw / "taxonomy.csv",
        raw / "land.csv",
        out,
        Config(),
        "fixture",
        "fixture",
        synthetic=True,
    )
    assert m["statistics"]["shared_or_duplicate_species_rows"] >= 1
    con = duckdb.connect()
    checklists = con.execute(
        "SELECT checklists FROM read_parquet(?) WHERE event_id='G-SHARED'",
        [str(out / "observations.parquet")],
    ).fetchone()[0]
    assert set(checklists) == {rows[0]["SAMPLING EVENT IDENTIFIER"], "DEMO-SHARED"}


def test_resume_rejects_changed_model(imported):
    dataset, config, tmp = imported
    problem = tmp / "problem.json"
    build_problem(dataset, problem, config)
    roads = tmp / "roads.sqlite"
    synthetic_roads(problem, tmp / "roads.csv", roads)
    run(problem, roads, tmp / "first", seconds=0.05)
    p = read(problem)
    p["config"]["local_miles"] = 20
    dump(problem, p)
    with pytest.raises(ValueError, match="hash"):
        run(problem, roads, tmp / "second", seconds=0.05, resume=tmp / "first" / "checkpoint.json")
