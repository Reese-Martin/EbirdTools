import copy
import csv
from dataclasses import replace

import duckdb
import pytest

from bigyear_planner.config import Config
from bigyear_planner.demo import synthetic_roads
from bigyear_planner.eod import attach_dataset, classify_land_eod, locality_id, prepare_eod
from bigyear_planner.eod_archive import SCHEMA, archive_files, convert
from bigyear_planner.problem import build_problem
from bigyear_planner.runner import run
from bigyear_planner.util import dump, read, write_csv
from bigyear_planner.validate import validate


@pytest.fixture
def eod_input(tmp_path):
    source = tmp_path / "eod.tsv"
    rows = []
    for index, (lat, day) in enumerate([("33.000000100000", 1), ("33.000000100001", 2)]):
        rows.append(
            dict(
                zip(
                    SCHEMA,
                    [
                        "2024",
                        "1",
                        str(day),
                        "Alabama",
                        "Fixture bird",
                        "exact-concept",
                        lat,
                        "-87.000000000001",
                        str(123456 + index),
                        "3",
                        "0",
                        "0",
                        "",
                        "0",
                    ],
                )
            )
        )
    with source.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(SCHEMA), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    archive = tmp_path / "archive"
    convert(source, archive, expected_rows=2, progress=lambda _: None)
    taxonomy = tmp_path / "taxonomy.csv"
    write_csv(
        taxonomy,
        [
            {
                "source_name": "Fixture bird",
                "source_taxon_id": "exact-concept",
                "species_id": "BIRD",
                "common_name": "Fixture bird",
                "aba_eligible": "1",
                "eligible_states": "US-AL",
                "reference": "Fixture only: explicit EOD presence-only decision",
                "eod_presence_eligible": "1",
            }
        ],
        [
            "source_name",
            "source_taxon_id",
            "species_id",
            "common_name",
            "aba_eligible",
            "eligible_states",
            "reference",
            "eod_presence_eligible",
        ],
    )
    land = tmp_path / "land.csv"
    write_csv(
        land,
        [
            {
                "locality_id": locality_id("US-AL", r["decimallatitude"], r["decimallongitude"]),
                "latitude": r["decimallatitude"],
                "longitude": r["decimallongitude"],
                "on_land": "1",
                "source": "Fixture only",
            }
            for r in rows
        ],
        ["locality_id", "latitude", "longitude", "on_land", "source"],
    )
    return source, archive, taxonomy, land, rows


def test_lossless_archive_and_source_retention(eod_input):
    source, archive, _, _, rows = eod_input
    manifest = read(archive / "manifest.json")
    assert manifest["status"] == "verified" and manifest["rows"] == 2
    assert manifest["verified_mismatches"] == 0
    assert source.exists()
    files, _ = archive_files(archive)
    con = duckdb.connect()
    values = con.execute(
        "SELECT decimallatitude,decimallongitude FROM read_parquet(?)", [files]
    ).fetchall()
    assert values == [(r["decimallatitude"], r["decimallongitude"]) for r in rows]
    con.close()


def test_incomplete_or_changed_archive_rejected(eod_input):
    _, archive, _, _, _ = eod_input
    file = archive / read(archive / "manifest.json")["parts"][0]["name"]
    with file.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="changed"):
        archive_files(archive)


def test_conversion_resumes_after_interruption(tmp_path):
    source = tmp_path / "source.tsv"
    header = "\t".join(SCHEMA) + "\n"
    row = (
        "2024\t1\t1\tAlabama\tBird\tconcept\t33.000000000001\t-87.000000000001\t123\t1\t0\t0\t\t0\n"
    )
    source.write_text(header + row * 30000)
    output = tmp_path / "archive"

    def stop(_):
        raise InterruptedError("fixture interruption")

    # Force the progress callback after a completed part.
    from unittest.mock import patch

    with patch("bigyear_planner.eod_archive.time.monotonic", side_effect=[0, 21]):
        with pytest.raises(InterruptedError):
            convert(source, output, expected_rows=30000, chunk_mib=1, progress=stop)
    assert not (output / "manifest.json").exists()
    manifest = convert(source, output, chunk_mib=1, resume=True, progress=lambda _: None)
    assert manifest["rows"] == 30000 and manifest["verified_mismatches"] == 0


def test_eod_preparation_and_full_route_audit(eod_input, tmp_path):
    _, archive, taxonomy, land, _ = eod_input
    config = replace(Config(), start_date="2024-01-01", end_date="2024-01-03", seconds=0.1)
    dataset = tmp_path / "dataset"
    manifest = prepare_eod(
        archive, taxonomy, land, dataset, config, "fixture", "fixture-eod", synthetic=True
    )
    assert manifest["statistics"]["dispositions"] == {"eligible": 2}
    assert not (dataset / "evidence.parquet").exists()
    assert not (dataset / "observations.parquet").exists()
    con = duckdb.connect()
    evidence, _ = attach_dataset(con, dataset)
    assert (
        con.execute(
            f"SELECT count(*) FROM {evidence} WHERE approved IS NULL AND category IS NULL AND checklist_id=''"
        ).fetchone()[0]
        == 2
    )
    assert con.execute(f"SELECT count(DISTINCT locality_id) FROM {evidence}").fetchone()[0] == 2
    con.close()
    problem = tmp_path / "problem.json"
    build_problem(dataset, problem, config)
    roads = tmp_path / "roads.sqlite"
    synthetic_roads(problem, tmp_path / "roads.csv", roads)
    output = tmp_path / "run"
    route = run(problem, roads, output, seconds=0.1)
    assert route["source_format"] == "EOD_DAILY_PRESENCE"
    assert route["species_total"] == 1
    assert read(output / "validation.json")["valid"]
    stop = next(s for d in route["days"] for s in d["stops"])
    assert stop["evidence"]["BIRD"][0]["record"].startswith("GBIF:")
    assert stop["evidence"]["BIRD"][0]["checklist"] == ""
    bad = copy.deepcopy(route)
    stop = next(s for d in bad["days"] for s in d["stops"])
    stop["evidence"]["BIRD"][0]["record"] = "GBIF:99999999"
    dump(output / "tampered.json", bad)
    assert not validate(output / "tampered.json", output / "problem.json", output / "roads.sqlite")[
        "valid"
    ]


def test_eod_requires_explicit_metadata_policy(eod_input, tmp_path):
    _, archive, taxonomy, land, _ = eod_input
    text = taxonomy.read_text().replace(
        "Fixture only: explicit EOD presence-only decision,1",
        "Fixture only: explicit EOD presence-only decision,review",
    )
    taxonomy.write_text(text)
    manifest = prepare_eod(
        archive, taxonomy, land, tmp_path / "dataset", Config(), "fixture", "fixture"
    )
    assert manifest["statistics"]["dispositions"] == {"eod_metadata_policy_review": 2}
    assert manifest["statistics"]["annual_recorded_species_ceiling"] == 0


def test_eod_rejects_approximate_taxon_and_land_matches(eod_input, tmp_path):
    _, archive, taxonomy, land, _ = eod_input
    taxonomy.write_text(taxonomy.read_text().replace("exact-concept", "other-concept"))
    result = prepare_eod(
        archive, taxonomy, land, tmp_path / "bad-taxon", Config(), "fixture", "fixture"
    )
    assert result["statistics"]["dispositions"] == {"unmapped_taxon_review": 2}
    taxonomy.write_text(taxonomy.read_text().replace("other-concept", "exact-concept"))
    land.write_text(land.read_text().replace("33.000000100000", "33.0000001"))
    result = prepare_eod(
        archive, taxonomy, land, tmp_path / "bad-land", Config(), "fixture", "fixture"
    )
    assert result["statistics"]["dispositions"] == {"land_unknown_review": 1, "eligible": 1}


def test_quality_review_preserves_original_archive_rows(eod_input, tmp_path):
    source, _, taxonomy, land, rows = eod_input
    rows[0]["geospatial_flagged_records"] = "1"
    rows[1]["geospatial_unknown_records"] = "1"
    with source.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(SCHEMA), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    archive = tmp_path / "flagged-archive"
    convert(source, archive, expected_rows=2, progress=lambda _: None)
    result = prepare_eod(
        archive, taxonomy, land, tmp_path / "flagged-binding", Config(), "fixture", "fixture"
    )
    assert result["statistics"]["dispositions"] == {"eod_spatial_quality_review": 2}
    assert read(archive / "manifest.json")["rows"] == 2


def test_eod_exact_land_catalog_generation(eod_input, tmp_path):
    _, archive, _, _, rows = eod_input
    mask = tmp_path / "land.geojson"
    dump(
        mask,
        {
            "type": "Polygon",
            "coordinates": [[[-88, 32], [-86, 32], [-86, 34], [-88, 34], [-88, 32]]],
        },
    )
    output = tmp_path / "classified.csv"
    classify_land_eod(archive, 2024, mask, output)
    with output.open() as stream:
        points = list(csv.DictReader(stream))
    assert {p["latitude"] for p in points} == {r["decimallatitude"] for r in rows}
    assert {p["on_land"] for p in points} == {"1"}


def test_eod_county_scope_retains_boundary_and_exact_points(eod_input, tmp_path):
    _, archive, taxonomy, land, rows = eod_input
    boundary = tmp_path / "county.geojson"
    north = float(rows[0]["decimallatitude"])
    dump(boundary, {"type": "Polygon", "coordinates": [
        [[-88, 32], [-86, 32], [-86, north], [-88, north], [-88, 32]]]})
    dataset = tmp_path / "county"
    manifest = prepare_eod(archive, taxonomy, land, dataset, Config(), "fixture", "fixture",
                           scope_geojson=boundary, scope_label="fixture county")
    assert manifest["statistics"]["dispositions"] == {"eligible": 1}
    assert manifest["geographic_scope"]["points"] == 1
    con = duckdb.connect()
    evidence, _ = attach_dataset(con, dataset)
    assert con.execute(f"SELECT latitude_text FROM {evidence}").fetchall() == [
        (rows[0]["decimallatitude"],)]
    con.close()
    output = tmp_path / "county-land.csv"
    classify_land_eod(archive, 2024, boundary, output, boundary)
    with output.open() as stream:
        assert len(list(csv.DictReader(stream))) == 1


def test_eod_validator_independently_checks_scope_geometry(eod_input, tmp_path):
    from bigyear_planner.util import sha256

    _, archive, taxonomy, land, rows = eod_input
    boundary = tmp_path / "county.geojson"
    north = float(rows[0]["decimallatitude"])
    dump(boundary, {"type": "Polygon", "coordinates": [
        [[-88, 32], [-86, 32], [-86, north], [-88, north], [-88, 32]]]})
    dataset = tmp_path / "county"
    config = replace(Config(), start_date="2024-01-01", end_date="2024-01-03", seconds=.1)
    manifest = prepare_eod(archive, taxonomy, land, dataset, config, "fixture", "fixture",
                           synthetic=True, scope_geojson=boundary)
    # Simulate a defective point-scope catalog allowing ONLY the outside point,
    # consistently frozen into a problem. The independent geometry check must catch it.
    write_csv(dataset / "scope-points.csv", [{"state": "US-AL",
        "latitude": rows[1]["decimallatitude"], "longitude": rows[1]["decimallongitude"]}],
        ["state", "latitude", "longitude"])
    manifest["outputs"]["scope-points.csv"] = sha256(dataset / "scope-points.csv")
    dump(dataset / "manifest.json", manifest)
    problem = tmp_path / "problem.json"
    build_problem(dataset, problem, config)
    roads = tmp_path / "roads.sqlite"
    synthetic_roads(problem, tmp_path / "roads.csv", roads)
    with pytest.raises(ValueError, match="outside geographic boundary"):
        run(problem, roads, tmp_path / "run", seconds=.1)
