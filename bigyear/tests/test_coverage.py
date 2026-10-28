import copy

import duckdb
import pytest

from bigyear_planner.coverage import (
    COVERAGE_FILE, attach_observer_coverage, build_observer_coverage, daily_species_counts,
)
from bigyear_planner.util import dump, q, read, sha256


def test_first_reports_are_distinct_and_calendar_year_to_date():
    daily = daily_species_counts([
        {"species_id": "a", "first_date": "2024-01-02"},
        {"species_id": "a", "first_date": "2024-03-01"},
        {"species_id": "b", "first_date": "2024-02-29"},
        {"species_id": "c", "first_date": "2024-12-31"},
    ], 2024)
    dates = {day["date"]: day for day in daily}
    assert len(daily) == 366
    assert dates["2024-01-01"]["cumulative_species"] == 0
    assert dates["2024-01-02"]["cumulative_species"] == 1
    assert dates["2024-02-28"]["cumulative_species"] == 1
    assert dates["2024-02-29"]["new_species"] == 1
    assert dates["2024-03-01"]["new_species"] == 0
    assert dates["2024-12-31"]["cumulative_species"] == 3
    assert len(daily_species_counts([], 2023)) == 365
    with pytest.raises(ValueError, match="outside"):
        daily_species_counts([{"species_id": "a", "first_date": "2023-12-31"}], 2024)


@pytest.fixture
def prepared_coverage(tmp_path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    with duckdb.connect() as con:
        # Both sites and every eligible report contribute, including a bird the
        # selected itinerary never visits. A previous year's report gives no credit.
        con.execute("CREATE TABLE evidence(state VARCHAR,species_id VARCHAR,date DATE,location_id VARCHAR)")
        con.execute("INSERT INTO evidence VALUES ('US-AL','a','2024-01-02','unvisited'),"
                    "('US-AL','a','2024-01-31','visited'),"
                    "('US-AL','b','2024-02-29','unvisited'),"
                    "('US-AL','b','2024-02-29','unvisited'),"
                    "('US-AL','old-only','2023-01-01','unvisited')")
        for name in ["evidence", "observations"]:
            con.execute(f"COPY evidence TO {q(dataset / (name + '.parquet'))} (FORMAT PARQUET)")
    manifest = {"year": 2024, "release": "test", "taxonomy_policy": "same-rules", "synthetic": True,
                "geographic_scope": {"label": "Alabama"},
                "statistics": {"annual_recorded_species_ceiling": 2},
                "outputs": {p.name: sha256(p) for p in dataset.glob("*.parquet")}}
    dump(dataset / "manifest.json", manifest)
    route = {"year": 2024, "data_release": "test", "taxonomy_policy": "same-rules", "synthetic": True,
             "annual_recorded_species_ceiling": 2, "species_total": 1,
             "days": [{"date": "2024-01-31", "cumulative_species": 1}],
             "provenance": {"dataset_manifest_sha256": sha256(dataset / "manifest.json")}}
    dump(tmp_path / "run/route.json", route)
    coverage = build_observer_coverage(dataset, tmp_path)
    return tmp_path, dataset, route, coverage


def test_coverage_uses_all_source_locations_and_preserves_saved_routes(prepared_coverage):
    root, _, route, coverage = prepared_coverage
    assert coverage["eligible_evidence_rows"] == 4
    assert coverage["annual_species"] == 2
    assert coverage["days"][1]["cumulative_species"] == 1
    assert coverage["days"][-1]["cumulative_species"] == 2
    assert coverage["first_seen"] == [{"species_id": "a", "first_date": "2024-01-02"},
                                       {"species_id": "b", "first_date": "2024-02-29"}]
    shown = attach_observer_coverage(root, copy.deepcopy(route))
    assert shown["observer_coverage"]["annual_species"] == 2
    assert "first_seen" not in shown["observer_coverage"]
    assert read(root / "run/route.json") == route
    assert "observer_coverage" not in route


def test_mismatched_or_stale_coverage_is_not_shown(prepared_coverage):
    root, _, route, _ = prepared_coverage
    for change in [{"year": 2023}, {"data_release": "other"}, {"taxonomy_policy": "other"},
                   {"synthetic": False}, {"annual_recorded_species_ceiling": 3},
                   {"provenance": {"dataset_manifest_sha256": "other"}}]:
        changed = {**route, **change}
        assert "observer_coverage" not in attach_observer_coverage(root, changed)
    target = root / COVERAGE_FILE
    supplement = read(target)
    next(iter(supplement["datasets"].values()))["days"][0]["cumulative_species"] = 99
    dump(target, supplement)
    assert attach_observer_coverage(root, copy.deepcopy(route)) == route
    target.write_text("invalid JSON")
    assert attach_observer_coverage(root, copy.deepcopy(route)) == route


def test_changed_source_catalog_is_rejected(prepared_coverage):
    root, dataset, _, _ = prepared_coverage
    with (dataset / "evidence.parquet").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="dataset hash mismatch"):
        build_observer_coverage(dataset, root)


def test_multi_state_coverage_counts_shared_species_once(prepared_coverage):
    root,dataset,_,_=prepared_coverage
    for name in ['evidence','observations']:
        target=dataset/(name+'.parquet');temp=dataset/(name+'-new.parquet')
        with duckdb.connect() as con:
            con.execute(f"COPY (SELECT CASE WHEN date=DATE '2024-01-31' THEN 'US-OR' ELSE state END AS state,"
                        f"* EXCLUDE(state) FROM read_parquet({q(target)})) TO {q(temp)} (FORMAT PARQUET)")
        temp.replace(target)
    manifest=read(dataset/'manifest.json')
    manifest['geographic_scope']['label']='Alabama and Oregon'
    manifest['outputs']={p.name:sha256(p) for p in dataset.glob('*.parquet')}
    dump(dataset/'manifest.json',manifest)
    coverage=build_observer_coverage(dataset,root)
    assert coverage['states']==['US-AL','US-OR']
    assert coverage['state'] is None and coverage['scope_label']=='Alabama and Oregon'
    assert coverage['annual_species']==2
    assert coverage['first_seen'][0]=={'species_id':'a','first_date':'2024-01-02'}
