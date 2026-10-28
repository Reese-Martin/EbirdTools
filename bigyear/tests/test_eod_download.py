"""Check daily-presence semantics without creating GBIF download jobs."""

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("download_eod", ROOT / "scripts/download_eod.py")
eod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eod)


@pytest.fixture
def database():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute('''CREATE TABLE occurrence (
        gbifid TEXT, datasetkey TEXT, countrycode TEXT, "class" TEXT,
        basisofrecord TEXT, "year" INTEGER, "month" INTEGER, "day" INTEGER,
        stateprovince TEXT, v_scientificname TEXT, v_taxonconceptid TEXT,
        decimallatitude REAL, decimallongitude REAL, hascoordinate BOOLEAN,
        hasgeospatialissues BOOLEAN, coordinateuncertaintyinmeters REAL,
        occurrencestatus TEXT
    )''')

    def add(gbifid, **changes):
        row = dict(
            gbifid=gbifid, datasetkey=eod.EOD_DATASET, countrycode="US",
            basisofrecord="HUMAN_OBSERVATION", year=2000, month=1, day=1,
            stateprovince="Alabama", v_scientificname="Reported bird",
            v_taxonconceptid="concept-1", decimallatitude=33.0, decimallongitude=-87.0,
            hascoordinate=True, hasgeospatialissues=False, occurrencestatus="PRESENT",
            coordinateuncertaintyinmeters=None,
        )
        row["class"] = "Aves"
        row.update(changes)
        keys = list(row)
        con.execute(
            "INSERT INTO occurrence (" + ",".join('"' + k + '"' for k in keys)
            + ") VALUES (" + ",".join("?" for _ in keys) + ")", list(row.values()),
        )

    yield con, add
    con.close()


def test_repeated_reports_collapse_with_quality_summaries(database):
    con, add = database
    add("103", coordinateuncertaintyinmeters=25)
    add("101", hasgeospatialissues=True, coordinateuncertaintyinmeters=1500)
    add("102", hasgeospatialissues=None)
    rows = con.execute(eod.build_sql()).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert len(row.keys()) == 14
    assert row["representative_gbifid"] == "101"
    assert row["source_record_count"] == 3
    assert row["geospatial_flagged_records"] == 1
    assert row["geospatial_unknown_records"] == 1
    assert row["max_reported_coordinate_uncertainty_m"] == 1500
    assert row["records_with_reported_coordinate_uncertainty"] == 2


def test_distinct_days_points_source_taxa_and_states_remain_separate(database):
    con, add = database
    add("1")
    add("2", day=2)
    add("3", decimallatitude=33.0000001)
    add("4", v_taxonconceptid="subspecies-concept")
    add("5", v_scientificname="Other source name")
    add("6", stateprovince="Mississippi")
    assert len(con.execute(eod.build_sql()).fetchall()) == 6


def test_full_us_time_scope_and_presence_requirements(database):
    con, add = database
    add("included-2000")
    add("included-hawaii", stateprovince="Hawaii", year=2024)
    add("included-alaska", stateprovince="Alaska", year=2025)
    add("old", year=1999)
    add("foreign", countrycode="CA")
    add("other-dataset", datasetkey="different-dataset")
    add("nonhuman", basisofrecord="MACHINE_OBSERVATION")
    add("nonbird", **{"class": "Mammalia"})
    add("no-date", day=None)
    add("no-coordinate", hascoordinate=False)
    add("absent", occurrencestatus="ABSENT")
    rows = con.execute(eod.build_sql()).fetchall()
    assert {r["representative_gbifid"] for r in rows} == {
        "included-2000", "included-hawaii", "included-alaska",
    }


def test_optional_quality_filter_runs_before_grouping(database):
    con, add = database
    add("clean", coordinateuncertaintyinmeters=25)
    add("flagged", hasgeospatialissues=True, coordinateuncertaintyinmeters=1500)
    add("unknown", hasgeospatialissues=None)
    row = con.execute(eod.build_sql(mappable=True)).fetchone()
    assert row["source_record_count"] == 1
    assert row["representative_gbifid"] == "clean"
    assert row["max_reported_coordinate_uncertainty_m"] == 25


def test_optional_scope_filters_and_source_record_counts(database):
    con, add = database
    add("1")
    add("2")
    add("3", year=2024, stateprovince="Florida")
    assert dict(con.execute(eod.build_sql(count=True)).fetchall()) == {2000: 2, 2024: 1}
    rows = con.execute(eod.build_sql(start_year=2024, end_year=2024, states=["Florida"])).fetchall()
    assert len(rows) == 1
    assert rows[0]["representative_gbifid"] == "3"
    assert not con.execute(eod.build_sql(states=["Alabama') OR 1=1 --"])).fetchall()


def test_pasteable_sql_and_json_match_default_request():
    query = eod.build_sql()
    assert (ROOT / "examples/eod-us-since-2000.sql").read_text().strip() == query
    payload = json.loads((ROOT / "examples/eod-us-since-2000.json").read_text())
    assert payload == {"format": "SQL_TSV_ZIP", "sendNotification": False, "sql": query}


@pytest.mark.parametrize("first,last", [(0, None), (2000, 1999), (2000, 10000)])
def test_invalid_year_ranges(first, last):
    with pytest.raises(ValueError):
        eod.build_sql(first, last)
