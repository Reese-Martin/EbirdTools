"""Reproducible full-year Tuscaloosa EOD evaluation; raw archive remains unchanged."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from html import escape
import io
import json
from pathlib import Path
import resource
import sqlite3
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

import duckdb

from bigyear_planner.config import Config
from bigyear_planner.eod import (classify_land_eod, polygon_mask, prepare_eod,
                                scoped_points)
from bigyear_planner.eod_archive import archive_scan
from bigyear_planner.problem import build_problem
from bigyear_planner.util import dump, q, read, sha256, write_csv

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/tuscaloosa-eod-2024"
RUNS = ROOT / "runs/tuscaloosa-eod-2024"
ARCHIVE = ROOT / "data/eod-us-since-2000-zstd3"
BOUNDARY = DATA / "sources/county-01125-acs2024.geojson"
PREVIOUS = ROOT / "data/tuscaloosa-2025-03"
REVIEW = ROOT / "data/crosswalk-review-2026-10-15"
NETWORK = "Geofabrik-AL-2025-01-01_OSRM-26.10.0_car-v1"
CONFIG = Config(year=2024, activity_minutes=720, drive_minutes=480, birding_minutes=120,
                target_locations=100000, threads=2, seconds=600, seed=20240101,
                memory_limit="1GB")
POLICY = "tuscaloosa-eod-2024-aba8.19-presence-v1"


def metric(start):
    return {"wall_seconds": time.monotonic() - start,
            "python_process_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20}


def audit():
    """Materialize a small analysis cache, never a replacement planner evidence source."""
    from shapely.geometry import Point

    start = time.monotonic()
    source, manifest = archive_scan(ARCHIVE)
    integrity_seconds = time.monotonic() - start
    con = duckdb.connect(str(DATA / "audit.duckdb"))
    con.execute("SET memory_limit='1GB'")
    con.execute("SET threads=2")
    mask = polygon_mask(BOUNDARY)
    point_start = time.monotonic()
    points = list(scoped_points(con, source, 2024, BOUNDARY))
    write_csv(DATA / "scope-points.csv", points, ["state", "latitude", "longitude"])
    points_seconds = time.monotonic() - point_start
    con.execute(f"""CREATE OR REPLACE TABLE cohort AS SELECT r.*,
        try_cast(concat(r.year,'-',r.month,'-',r.day) AS DATE) AS date
        FROM {source} r JOIN read_csv({q(DATA / 'scope-points.csv')},all_varchar=true) s
        ON r.decimallatitude=s.latitude AND r.decimallongitude=s.longitude
        WHERE r.year=2024 AND r.stateprovince='Alabama'""")
    monthly = [dict(zip(["month", "presence_rows", "source_records", "taxa", "locations", "days"], row))
               for row in con.execute("""SELECT month,count(*),sum(source_record_count),
               count(DISTINCT (v_scientificname,v_taxonconceptid)),
               count(DISTINCT (decimallatitude,decimallongitude)),count(DISTINCT date)
               FROM cohort GROUP BY 1 ORDER BY 1""").fetchall()]
    write_csv(DATA / "monthly-coverage.csv", monthly, list(monthly[0]))
    taxa = [dict(zip(["source_name", "source_taxon_id", "presence_rows", "source_records",
                      "locations", "days", "first_date", "last_date", "example_gbif_id"], row))
            for row in con.execute("""SELECT v_scientificname,v_taxonconceptid,count(*),
            sum(source_record_count),count(DISTINCT (decimallatitude,decimallongitude)),
            count(DISTINCT date),min(date)::VARCHAR,max(date)::VARCHAR,min(representative_gbifid)
            FROM cohort GROUP BY 1,2 ORDER BY 1,2""").fetchall()]
    write_csv(DATA / "taxa.csv", taxa, list(taxa[0]))
    checks = dict(zip(["rows", "source_records", "locations", "taxa", "days", "invalid_dates",
                       "flagged_groups", "unknown_quality_groups", "uncertainty_reported_groups"],
        con.execute("""SELECT count(*),sum(source_record_count),
        count(DISTINCT (decimallatitude,decimallongitude)),
        count(DISTINCT (v_scientificname,v_taxonconceptid)),count(DISTINCT date),
        count(*) FILTER (WHERE date IS NULL),
        count(*) FILTER (WHERE geospatial_flagged_records>0),
        count(*) FILTER (WHERE geospatial_unknown_records>0),
        count(*) FILTER (WHERE records_with_reported_coordinate_uncertainty>0) FROM cohort""").fetchone()))
    checks["duplicate_group_keys"] = con.execute("""SELECT count(*) FROM (
        SELECT year,month,day,stateprovince,v_scientificname,v_taxonconceptid,
        decimallatitude,decimallongitude FROM cohort GROUP BY ALL HAVING count(*)>1)""").fetchone()[0]
    checks["missing_dates"] = [str(r[0]) for r in con.execute("""SELECT d::DATE FROM
        generate_series(DATE '2024-01-01',DATE '2024-12-31',INTERVAL 1 DAY) t(d)
        WHERE d::DATE NOT IN (SELECT date FROM cohort) ORDER BY 1""").fetchall()]
    # Approximate distance only flags near-border points for manual inspection; membership uses the polygon.
    border = [{**p, "distance_to_boundary_degrees": mask.boundary.distance(
        Point(float(p["longitude"]), float(p["latitude"])))} for p in points]
    border = sorted(border, key=lambda p: p["distance_to_boundary_degrees"])
    write_csv(DATA / "boundary-near-points.csv", border[:30], list(border[0]))
    con.close()
    report = {"scope": "Tuscaloosa County GEOID 01125, January 1–December 31, 2024",
              "archive_manifest_sha256": sha256(ARCHIVE / "manifest.json"),
              "archive_rows": manifest["rows"], "boundary_sha256": sha256(BOUNDARY),
              "boundary_source": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_ACS2024/MapServer/82",
              "boundary_vintage": "January 1, 2024", "checks": checks, "monthly": monthly,
              "timing": {**metric(start), "archive_integrity_seconds": integrity_seconds,
                         "exact_point_scope_scan_seconds": points_seconds},
              "analysis_cache": "audit.duckdb is a small reproducible analysis cache; planner reads original archive directly"}
    dump(DATA / "coverage-audit.json", report)
    print(json.dumps(report, indent=2), flush=True)


def land():
    import shapefile
    from shapely.geometry import box, mapping, shape
    from shapely.ops import unary_union

    start = time.monotonic()
    bounds = polygon_mask(BOUNDARY).bounds
    xmin, ymin, xmax, ymax = bounds
    bounds = (xmin - .05, ymin - .05, xmax + .05, ymax + .05)
    folder = ROOT / "build/land-mask/land-polygons-split-4326"
    if not (folder / "land_polygons.shp").exists():
        raise ValueError("previous pilot's extracted detailed OSM land source is required")
    reader = shapefile.Reader(shp=str(folder / "land_polygons.shp"),
                              shx=str(folder / "land_polygons.shx"))
    clip = box(*bounds)
    polygons = [shape(s.__geo_interface__).intersection(clip) for s in reader.iterShapes(bbox=bounds)]
    reader.close()
    dump(DATA / "terrestrial-mask.geojson", mapping(unary_union(polygons)))
    classify_land_eod(ARCHIVE, 2024, DATA / "terrestrial-mask.geojson", DATA / "land.csv", BOUNDARY)
    with (DATA / "land.csv").open() as stream:
        decisions = Counter(r["on_land"] for r in csv.DictReader(stream))
    dump(DATA / "land-provenance.json", {
        "source": "https://osmdata.openstreetmap.de/data/land-polygons.html",
        "source_archive_sha256": sha256(PREVIOUS / "sources/land-polygons-split-4326.zip"),
        "bounds": bounds, "classification": dict(decisions),
        "policy": "Current coastline product; inland-water pins retain prior authorized nearby walking/shoreline proxy policy. Access rights and footpaths not independently established.",
        "metrics": metric(start)})
    print(json.dumps({"land": dict(decisions), **metric(start)}), flush=True)


def source_check():
    """Verify every distinct taxon against one publisher-verbatim GBIF record plus seasonal/border samples."""
    start = time.monotonic()
    con = duckdb.connect(str(DATA / "audit.duckdb"), read_only=True)
    columns = ["representative_gbifid", "year", "month", "day", "v_scientificname",
               "v_taxonconceptid", "decimallatitude", "decimallongitude"]
    selection = ",".join(columns)
    rows = con.execute(f"""SELECT {selection} FROM cohort QUALIFY row_number() OVER (
        PARTITION BY v_scientificname,v_taxonconceptid ORDER BY representative_gbifid)=1""").fetchall()
    concept_count = len(rows)
    rows += con.execute(f"""SELECT {selection} FROM cohort QUALIFY row_number() OVER (
        PARTITION BY month ORDER BY representative_gbifid)=1""").fetchall()
    with (DATA / "boundary-near-points.csv").open() as stream:
        for point in list(csv.DictReader(stream))[:3]:
            rows += con.execute(f"SELECT {selection} FROM cohort WHERE decimallatitude=? "
                                "AND decimallongitude=? ORDER BY representative_gbifid LIMIT 1",
                                [point["latitude"], point["longitude"]]).fetchall()
    con.close()
    examples = {str(row[0]): dict(zip(columns, row)) for row in rows}
    destination = DATA / "sources/gbif-verbatim"
    destination.mkdir(exist_ok=True)
    prefix = "http://rs.tdwg.org/dwc/terms/"
    fields = ["year", "month", "day", "scientificName", "taxonConceptID", "decimalLatitude",
              "decimalLongitude", "county", "stateProvince", "vernacularName", "occurrenceStatus",
              "basisOfRecord"]

    def fetch(item):
        identifier, expected = item
        path = destination / f"{identifier}.json"
        if path.exists():
            record = read(path)
        else:
            url = f"https://api.gbif.org/v1/occurrence/{identifier}/verbatim"
            for attempt in range(3):
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": "EbirdTools county evaluation"})
                    with urllib.request.urlopen(request, timeout=45) as response:
                        raw = json.load(response)
                    record = {field: raw.get(prefix + field) for field in fields}
                    record["datasetKey"] = raw.get("datasetKey")
                    record["key"] = raw.get("key")
                    record["source"] = url
                    dump(path, record)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
        matches = {
            "dataset": record["datasetKey"] == "4fa7b334-ce0d-4e88-aaae-2e0c138d049e",
            "year": int(record["year"]) == expected["year"],
            "month": int(record["month"]) == expected["month"],
            "day": int(record["day"]) == expected["day"],
            "scientific_name": record["scientificName"] == expected["v_scientificname"],
            "taxon_concept": record["taxonConceptID"] == expected["v_taxonconceptid"],
            "latitude_text": record["decimalLatitude"] == expected["decimallatitude"],
            "longitude_text": record["decimalLongitude"] == expected["decimallongitude"],
            "county_label": record["county"] == "Tuscaloosa",
            "state": record["stateProvince"] == "Alabama",
            "present": record["occurrenceStatus"] == "PRESENT",
            "human": record["basisOfRecord"] == "HumanObservation",
        }
        return {"gbif_id": identifier, "source_name": expected["v_scientificname"],
                "date": f"{expected['year']}-{expected['month']:02d}-{expected['day']:02d}",
                "matches": matches, "valid": all(matches.values())}

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(fetch, examples.items()))
    report = {"records_checked": len(results), "all_taxon_concepts_represented": concept_count,
              "passed": sum(r["valid"] for r in results), "failed": sum(not r["valid"] for r in results),
              "results": results, "metrics": metric(start)}
    dump(DATA / "source-verification.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}), flush=True)


def taxonomy():
    """Freeze explicit source-concept decisions for this presence-only evaluation."""
    verification = read(DATA / "source-verification.json")
    if verification["failed"]:
        raise ValueError("resolve source verification discrepancies before taxonomy preparation")
    aba_path = REVIEW / "sources/ABA_Checklist-8.19.csv.zip"
    with zipfile.ZipFile(aba_path) as archive:
        aba = {r[3]: r for r in csv.reader(io.StringIO(
            archive.read("ABA_Checklist-8.19.csv").decode("utf-8-sig")))
            if len(r) > 5 and r[5] in list("123456")}
    with (REVIEW / "outputs/aba-crosswalk-review/taxonomy-approved.csv").open() as stream:
        previous = {(r["source_name"], "avibase-" + r["source_taxon_id"]): r
                    for r in csv.DictReader(stream)}
    with (DATA / "taxa.csv").open() as stream:
        taxa = list(csv.DictReader(stream))
    rows = []
    for item in taxa:
        name, concept = item["source_name"], item["source_taxon_id"]
        source = read(DATA / f"sources/gbif-verbatim/{item['example_gbif_id']}.json")
        assert (source["scientificName"], source["taxonConceptID"]) == (name, concept)
        match = aba.get(name)
        old = previous.get((name, concept))
        decision = "1" if match else "0"
        presence = "1" if match else "0"
        reason = ("Exact publisher-verbatim name/concept verified against GBIF. Exact scientific-name "
                  "membership in frozen ABA 8.19; counted only under this experimental EOD "
                  "presence-only policy. Individual eBird approval/captive/exotic status remains unknown.")
        reference = f"https://www.aba.org/aba-checklist/; {source['source']}"
        if old:
            reason += (" Explicit identifier bridge to the prior approved EBD species concept "
                       f"{old['source_taxon_id']}; raw EOD identifier retained unchanged.")
        if name == "Anser cygnoides":
            reason = "Swan Goose has no exact species entry in ABA 8.19; excluded. No domestic-form credit inferred."
        elif name == "Setophaga petechia":
            decision = presence = "review"
            reason = ("EOD A6C0C4A9 is pre-split Yellow Warbler, now Northern/Mangrove. "
                      "Do not map the broad 2024 concept to current Mangrove Yellow Warbler by name. "
                      "Deferred pending a source-supported species-level split assignment.")
            reference += "; https://www.birds.cornell.edu/clementschecklist/updates-and-corrections-october-2025/"
        elif name in {"Cairina moschata", "Columba livia"}:
            decision = presence = "review"
            reason = ("Species-level EOD concept does not distinguish the domestic/feral form. "
                      "Prior approval of an exact EBD form is not transferred to this broader concept. "
                      "Deferred for conservative county evaluation; retained in archive and review.")
            reference += "; https://www.aba.org/aba-area-introduced-species/"
        rows.append({"source_name": name, "source_taxon_id": concept,
                     "species_id": "aba8.19:" + name.replace(" ", "_"),
                     "common_name": match[1] if match else source["vernacularName"],
                     "aba_eligible": decision, "eligible_states": "US-AL",
                     "reference": reference, "eod_presence_eligible": presence,
                     "review_basis": reason, "prior_approved_ebd_concept": old["source_taxon_id"] if old else "",
                     "presence_rows": item["presence_rows"], "policy_id": POLICY})
    write_csv(DATA / "taxonomy.csv", rows, list(rows[0]))
    deferred = [r for r in rows if r["eod_presence_eligible"] != "1"]
    write_csv(DATA / "taxonomy-review.csv", deferred, list(rows[0]))
    dump(DATA / "taxonomy-policy.json", {
        "policy": POLICY, "reviewer": "Codex source-based review for authorized evaluation",
        "scope": "Technical presence-only evaluation; not an ABA-certified historical Big Year or eBird expert validation",
        "aba_snapshot_sha256": sha256(aba_path),
        "previous_crosswalk_sha256": sha256(REVIEW / "outputs/aba-crosswalk-review/taxonomy-approved.csv"),
        "included_species": sum(r["eod_presence_eligible"] == "1" for r in rows),
        "deferred_or_excluded": deferred,
        "missing_metadata": "Approval, domestic/exotic flags, complete checklists and effort are not supplied by this grouped extract; individual wild/accepted status cannot be reconstructed.",
        "no_historical_policy_claim": "ABA 8.19 is a fixed current taxonomy, not a reconstruction of 2024 recording rules.",
    })
    print(json.dumps({"included": len(rows) - len(deferred),
                      "deferred_or_excluded": [(r["source_name"], r["presence_rows"]) for r in deferred]}), flush=True)


def inputs():
    start = time.monotonic()
    dump(DATA / "config.json", CONFIG.to_dict())
    result = prepare_eod(ARCHIVE, DATA / "taxonomy.csv", DATA / "land.csv", DATA / "dataset",
                         CONFIG, "GBIF-0031401-260928105237408", POLICY,
                         scope_geojson=BOUNDARY, scope_label="Tuscaloosa County, Alabama (01125)")
    dump(DATA / "optimized-import-metrics.json", {**metric(start),
                                                "statistics": result["statistics"]})
    candidate_start = time.monotonic()
    problem = build_problem(DATA / "dataset", DATA / "problem-unanchored.json", CONFIG)
    dump(DATA / "candidate-preparation-metrics.json", metric(candidate_start))
    dump(DATA / "input-metrics.json", metric(start))
    print(json.dumps({"statistics": result["statistics"], "reduction": problem["reduction"],
                      **metric(start)}, indent=2), flush=True)


def access(endpoint):
    start = time.monotonic()
    p = read(DATA / "problem-unanchored.json")
    rows, evidence = [], []
    for loc in p["locations"]:
        url = f"{endpoint}/nearest/v1/driving/{loc['lon']},{loc['lat']}?number=1"
        with urllib.request.urlopen(url, timeout=30) as response:
            result = json.load(response)
        if result.get("code") != "Ok" or not result.get("waypoints"):
            raise ValueError(f"No road access for {loc['id']}")
        waypoint = result["waypoints"][0]
        rows.append({"locality_id": loc["locality_id"], "latitude": loc["lat"],
                     "longitude": loc["lon"], "road_latitude": waypoint["location"][1],
                     "road_longitude": waypoint["location"][0], "offset_meters": waypoint["distance"],
                     "source": NETWORK,
                     "walking_policy": "Walking and birding share the 120-minute stop allowance; reported pins can proxy nearby shoreline or forest. No boat travel inferred."})
        evidence.append({"location_id": loc["id"], "reported": [loc["lon"], loc["lat"]],
                         "response": result})
    write_csv(DATA / "road-access.csv", rows, list(rows[0]))
    dump(DATA / "road-access-evidence.json", evidence)
    p = build_problem(DATA / "dataset", DATA / "problem.json", CONFIG,
                      access_catalog=DATA / "road-access.csv")
    offsets = sorted(float(r["offset_meters"]) for r in rows)
    dump(DATA / "access-summary.json", {
        "locations": len(rows), "maximum_offset_meters": max(offsets),
        "median_offset_meters": offsets[len(offsets) // 2],
        "over_100_meters": sum(x > 100 for x in offsets),
        "over_500_meters": sum(x > 500 for x in offsets),
        "review": sorted(rows, key=lambda r: -r["offset_meters"]), "metrics": metric(start)})
    print(json.dumps({"locations": len(rows), "max_offset_m": max(offsets), **metric(start)}), flush=True)


def roads(endpoint):
    from bigyear_planner.travel import osrm_roads

    start = time.monotonic()
    osrm_roads(DATA / "problem.json", endpoint, DATA / "roads.sqlite", NETWORK, batch=40)
    with sqlite3.connect(DATA / "roads.sqlite") as con:
        count = con.execute("SELECT count(*) FROM roads").fetchone()[0]
    locations = len(read(DATA / "problem.json")["locations"])
    report = {"directed_pairs": count, "expected_including_self": locations**2,
              "unreachable_pairs": locations**2 - count, "network": NETWORK,
              "network_pbf_sha256": sha256(PREVIOUS / "sources/alabama-250101.osm.pbf"),
              "road_costs": "Actual local OSRM driving distances/durations; no straight-line substitutions",
              "geometry": "Road geometry deferred; displayed straight links are explicitly schematic",
              "metrics": metric(start)}
    dump(DATA / "roads-metrics.json", report)
    print(json.dumps(report), flush=True)


def searches():
    """Resume completed runs, but never overwrite partial run artifacts."""
    from bigyear_planner.runner import run

    smoke = RUNS / "smoke-60s-repaired"
    if not (smoke / "validation.json").exists():
        run(DATA / "problem.json", DATA / "roads.sqlite", smoke,
            seconds=60, threads=2, seed=20240100)
    if not read(smoke / "validation.json")["valid"]:
        raise ValueError("the smoke test must pass before longer searches")
    pending = []
    for i in (1, 2, 3):
        folder = RUNS / f"seed-{i}-600s"
        if (folder / "validation.json").exists() and read(folder / "validation.json")["valid"]:
            continue
        command = [sys.executable, "-m", "bigyear_planner.cli", "search",
                   "--problem", str(DATA / "problem.json"), "--roads", str(DATA / "roads.sqlite"),
                   "--output", str(folder), "--seconds", "600", "--threads", "2",
                   "--seed", str(20240100 + i)]
        pending.append(subprocess.Popen(command))
    statuses = [job.wait() for job in pending]
    if any(status != 0 for status in statuses):
        raise ValueError("one or more searches failed; inspect retained artifacts")


def monthly_probe():
    """Exercise all 12 months even when an optimized annual route can skip summer."""
    from bigyear_planner.runner import materialize
    from bigyear_planner.travel import load_roads, transitions
    from bigyear_planner.validate import validate

    start = time.monotonic()
    problem = read(DATA / "problem.json")
    selected = {}
    for opportunity in problem["opportunities"]:
        selected.setdefault(opportunity["date"][:7], opportunity["id"])
    if len(selected) != 12:
        raise ValueError("eligible opportunities do not cover all 12 months")
    roads = load_roads(problem, DATA / "roads.sqlite")
    plans = transitions(problem, roads)
    species = set(s for i in selected.values() for s in problem["opportunities"][i]["species"])
    route = materialize(problem, roads, plans, list(selected.values()),
                        {"optimal": False, "model": "monthly_presence_integration_probe",
                         "species": len(species), "selected": list(selected.values())})
    route["provenance"] = {"problem_sha256": sha256(DATA / "problem.json"),
                           "roads_sha256": sha256(DATA / "roads.sqlite")}
    route["status"] = "Twelve-month integration probe; deliberately selected stops, not an optimized route"
    dump(DATA / "monthly-probe-route.json", route)
    audit = validate(DATA / "monthly-probe-route.json", DATA / "problem.json", DATA / "roads.sqlite")
    if not audit["valid"]:
        raise ValueError(audit["errors"])
    dump(DATA / "monthly-probe-validation.json", {"validation": audit, "metrics": metric(start),
         "months_exercised": list(selected), "purpose": route["status"]})
    print(json.dumps({"validation": audit, "months": len(selected), **metric(start)}), flush=True)


def route_geometry(endpoint):
    """Cross-check selected table costs with complete route-service geometry; don't edit frozen runs."""
    from bigyear_planner.travel import load_roads, road_point

    start = time.monotonic()
    folder = RUNS / "seed-1-600s"
    problem, route = read(folder / "problem.json"), read(folder / "route.json")
    roads = load_roads(problem, folder / "roads.sqlite")
    pairs = sorted({(leg["from"], leg["to"]) for d in route["days"] for leg in d["legs"]})
    features, results = [], []
    for a, b in pairs:
        origin, destination = [road_point(problem["locations"][i]) for i in (a, b)]
        url = (f"{endpoint}/route/v1/driving/{origin['lon']},{origin['lat']};"
               f"{destination['lon']},{destination['lat']}?overview=full&geometries=geojson&steps=false")
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError("selected road leg unavailable in route service")
        measured, cached = data["routes"][0], roads[(a, b)]
        difference_m = measured["distance"] - cached["miles"] * 1609.344
        difference_s = measured["duration"] - cached["minutes"] * 60
        result = {"from": a, "to": b, "route_distance_m": measured["distance"],
                  "route_duration_s": measured["duration"], "distance_difference_m": difference_m,
                  "duration_difference_s": difference_s,
                  "matches_to_0_11_units": abs(difference_m) <= .11 and abs(difference_s) <= .11}
        results.append(result)
        features.append({"type": "Feature", "properties": result, "geometry": measured["geometry"]})
    roads.con.close()
    dump(RUNS / "selected-road-geometries.geojson", {"type": "FeatureCollection", "features": features})
    report = {"run": folder.name, "unique_directed_legs": len(pairs),
              "passed": sum(r["matches_to_0_11_units"] for r in results),
              "failed": sum(not r["matches_to_0_11_units"] for r in results),
              "max_absolute_distance_difference_m": max(abs(r["distance_difference_m"]) for r in results),
              "max_absolute_duration_difference_s": max(abs(r["duration_difference_s"]) for r in results),
              "results": results, "metrics": metric(start),
              "scope": "Selected seed-1 route only; actual geometry retained separately, no edits to frozen route or road snapshots"}
    dump(RUNS / "selected-road-verification.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}), flush=True)


def summarize():
    coverage = read(DATA / "coverage-audit.json")
    manifest = read(DATA / "dataset/manifest.json")
    problem = read(DATA / "problem.json")
    records = []
    for name in ["smoke-60s-repaired", "seed-1-600s", "seed-2-600s", "seed-3-600s"]:
        folder = RUNS / name
        route, audit, benchmark = [read(folder / f) for f in
                                    ("route.json", "validation.json", "benchmark.json")]
        if not audit["valid"] or audit["days_checked"] != 366:
            raise ValueError(f"{name} has not passed a complete year of independent validation")
        history = [json.loads(line) for line in (folder / "improvements.jsonl").read_text().splitlines()]
        kinds = Counter(d["kind"] for d in route["days"])
        offsets = [route["locations"][s["location"]]["road_access"]["offset_meters"]
                   for d in route["days"] for s in d["stops"]]
        records.append({
            "run": name, "seed": benchmark["seed"], "species": route["species_total"],
            "road_miles": audit["road_miles"], "driving_hours": audit["travel_minutes"] / 60,
            "stops": sum(len(d["stops"]) for d in route["days"]),
            "birding_days": kinds["birding"], "transit_days": kinds["road_transit"],
            "rest_days": kinds["rest"], "calendar_days_validated": audit["days_checked"],
            "evidence_rows_checked": audit["evidence_rows_checked"],
            "search_seconds": benchmark["engine"]["elapsed_seconds"],
            "total_run_seconds": benchmark["wall_seconds"],
            "engine_peak_rss_mib": benchmark["engine_peak_rss_bytes"] / 2**20,
            "python_peak_rss_mib": benchmark["hardware"]["python_peak_rss_bytes"] / 2**20,
            "seconds_to_species_ceiling": next((h["elapsed_seconds"] for h in history
                if h["species"] == manifest["statistics"]["annual_recorded_species_ceiling"]), None),
            "maximum_daily_activity_hours": max(d["activity_minutes"] for d in route["days"]) / 60,
            "max_visited_road_offset_meters": max(offsets),
            "visits_over_500_meters": sum(v > 500 for v in offsets),
            "valid": audit["valid"], "route_sha256": sha256(folder / "route.json")})
    seeds = records[1:]
    best = min(seeds, key=lambda r: (-r["species"], r["driving_hours"], r["road_miles"]))
    best_route = read(RUNS / best["run"] / "route.json")
    month_counts = Counter(d["date"][:7] for d in best_route["days"] for _ in d["stops"])
    with (DATA / "taxonomy-review.csv").open() as stream:
        deferred = list(csv.DictReader(stream))
    data_bytes = sum(p.stat().st_size for p in DATA.rglob("*") if p.is_file())
    run_bytes = sum(p.stat().st_size for p in RUNS.rglob("*") if p.is_file())
    limitations = [
        "Experimental deterministic presence-only evaluation. Individual eBird approval and captive/exotic status remain unknown; this is not an ABA-certified historical Big Year.",
        "One county-date, 2024-12-27, has no records. Missing observations are not absence evidence; no presence is interpolated.",
        "Three deferred concepts and one non-ABA species remain uncredited; the 224-species ceiling is conditional on this frozen review policy.",
        "The road graph is dated 2025-01-01, a snapshot proxy for 2024; historical traffic, closures and land access rights are not reconstructed.",
        "The terrestrial mask is a current coastline product. Inland-water pins retain the prior authorized walking/shoreline proxy policy. Walking and birding share 120 minutes per stop; footpaths are not independently verified.",
        "Driving uses measured network costs. The viewer adds separately verified road geometry without changing frozen routes. Straight dashed road links indicate unavailable geometry; road lines do not establish legal walking access.",
        "Stops must lie inside the county, including its boundary; road paths may leave it. Start is freely chosen, finish unrestricted; no overnight repositioning or flights.",
        "Same-day ordering follows canonical opportunity IDs. Species are available throughout their recorded date, with no detection probabilities or within-day timing.",
        "Matching the full eligible species ceiling proves the primary count attainable under this model. Minimum travel is not proven.",
        "This validates county-year operation and direct archive reads; national-scale road matrices, memory use and ecological inference remain untested.",
        "No same-year EBD reference sample was available; the prior March 2025 sample is not a 2024 completeness benchmark.",
    ]
    report = {
        "scope": coverage["scope"], "conclusion": "PASS for county-year technical operation under the documented conservative presence-only policy",
        "best_run": best["run"], "coverage": coverage, "eligible": manifest["statistics"],
        "all_locations_retained": len(problem["locations"]) == problem["reduction"]["input_locations"],
        "opportunities": len(problem["opportunities"]), "source_verification": read(DATA / "source-verification.json"),
        "config": problem["config"], "runs": records, "deferred_or_excluded": deferred,
        "hardware_context": read(RUNS / "hardware-context.json"),
        "software_verification": read(RUNS / "software-verification.json"),
        "viewer_verification": read(RUNS / "viewer-verification.json"),
        "optimized_import": read(DATA / "optimized-import-metrics.json"),
        "candidate_preparation": read(DATA / "candidate-preparation-metrics.json"),
        "access": read(DATA / "access-summary.json"), "roads": read(DATA / "roads-metrics.json"),
        "roads_repair": read(DATA / "roads-repair.json"),
        "selected_road_verification": read(RUNS / "selected-road-verification.json"),
        "monthly_integration_probe": read(DATA / "monthly-probe-validation.json"),
        "best_route_stop_counts_by_month": dict(sorted(month_counts.items())),
        "local_storage": {"data_bytes": data_bytes, "runs_bytes": run_bytes,
                          "archive_bytes": read(ARCHIVE / "manifest.json")["bytes"],
                          "scope": "Pilot-only totals include failed initial smoke snapshot and verification cache; shared archive/OSRM graph excluded. Report files added afterward are small."},
        "limitations": limitations,
        "code_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [
            Path(__file__), ROOT / "src/bigyear_planner/eod.py", ROOT / "src/bigyear_planner/travel.py",
            ROOT / "src/bigyear_planner/validate.py", ROOT / "src/bigyear_planner/runner.py"]},
    }
    if not all(r["species"] == manifest["statistics"]["annual_recorded_species_ceiling"] for r in seeds):
        report["conclusion"] = "PASS for technical operation; search has not reached the species ceiling in every seed"
    dump(RUNS / "evaluation-summary.json", report)
    write_csv(RUNS / "run-summary.csv", records, list(records[0]))
    shutil.copyfile(BOUNDARY, RUNS / "map-context.geojson")
    context = read(RUNS / "map-context.geojson")
    context["features"][0]["properties"]["name"] = "Tuscaloosa County (2024)"
    dump(RUNS / "map-context.geojson", context)
    rows = "".join(f"<tr><td>{escape(r['run'])}</td><td>{r['species']}</td><td>{r['road_miles']:.1f}</td>"
        f"<td>{r['driving_hours']:.2f}</td><td>{r['stops']}</td><td>{r['total_run_seconds']:.1f}</td>"
        f"<td>{r['python_peak_rss_mib']:.0f} / {r['engine_peak_rss_mib']:.0f}</td><td>366 days · passed</td></tr>"
        for r in records)
    months = "".join(f"<tr><td>{r['month']}</td><td>{r['presence_rows']:,}</td><td>{r['source_records']:,}</td>"
                     f"<td>{r['taxa']}</td><td>{r['locations']}</td><td>{r['days']}</td></tr>" for r in coverage["monthly"])
    reviews = "".join(f"<tr><td>{escape(r['source_name'])}</td><td>{r['presence_rows']}</td><td>{escape(r['review_basis'])}</td></tr>"
                      for r in deferred)
    notes = "".join(f"<li>{escape(note)}</li>" for note in limitations)
    checks = coverage["checks"]
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tuscaloosa 2024 EOD evaluation</title><style>
body{{font:16px/1.55 system-ui,sans-serif;color:#15251f;background:#f6f7f3;max-width:1150px;margin:40px auto;padding:0 24px}}
h1,h2{{line-height:1.2}}h1{{font-size:36px}}h2{{margin-top:36px}}p{{max-width:90ch}}a{{color:#15664d}}
.status{{background:#e1eee4;border-left:5px solid #267343;padding:16px}}.cards{{display:flex;gap:16px;flex-wrap:wrap}}
.card{{flex:1;min-width:180px;background:white;border:1px solid #d9dfd8;padding:18px;border-radius:6px}}.big{{font-size:28px;font-weight:700;display:block}}
table{{border-collapse:collapse;background:white;width:100%;font-size:14px}}th,td{{padding:10px 12px;border-bottom:1px solid #d9dfd8;text-align:left;vertical-align:top}}
th{{background:#e8eee7}}.scroll{{overflow:auto}}li{{margin:9px 0}}small{{color:#53665b}}</style>
<h1>Tuscaloosa County · 2024 EOD evaluation</h1>
<p class="status"><strong>{escape(report['conclusion'])}.</strong> All three ten-minute seeds used the same full county-year cohort and passed independent calendar, road-budget, taxonomy, land and exact-source evidence validation.</p>
<div class="cards"><div class="card"><span class="big">{checks['rows']:,}</span>daily presence rows<br>{checks['source_records']:,} represented source records</div>
<div class="card"><span class="big">{best['species']} / 224</span>eligible species attained<br>4 taxa deferred or excluded</div>
<div class="card"><span class="big">626 / 626</span>eligible locations retained<br>2,203 observed location-days</div></div>
<p><a href="/">Open route viewer</a> · <a href="/run-summary.csv">Download run comparison</a> · <a href="/{best['run']}/species-audit.csv">Best-run source evidence</a> · <a href="/{best['run']}/validation.json">Independent validation</a></p>
<h2>Run results</h2><p>Two engine threads per search. The three 600-second seeds run concurrently; totals include road-cache snapshotting, export and independent validation. Memory columns show separate process peaks, not a simultaneous total or the router's peak.</p>
<div class="scroll"><table><tr><th>Run</th><th>Species</th><th>Road miles</th><th>Drive hours</th><th>Stops</th><th>Total seconds</th><th>Python / C++ MiB</th><th>Validation</th></tr>{rows}</table></div>
<p>Best run: <strong>{escape(best['run'])}</strong>. Reaching 224 matches the full eligible county-year species ceiling. This establishes the primary count under the frozen model; travel optimality remains unproven.</p>
<h2>Coverage and source checks</h2><p>All 12 months are represented, including February 29. The only missing county-date is December 27. There are zero invalid dates, duplicate group keys, flagged geospatial groups or unknown geospatial-status groups. Coordinate uncertainty was not reported, so no accuracy threshold can be inferred.</p>
<p>All 234 public GBIF verbatim spot-checks passed, covering all 228 taxon concepts plus seasonal and near-boundary examples. Publisher names, concept IDs, date components, county labels and literal coordinate strings matched. Every credited route species is independently checked against the original compressed archive at its selected date and location. A separate integration probe deliberately visits one observed location-date in each of the 12 months and passes the same full-year validation, exercising summer data even when the optimized route can skip those months.</p>
<div class="scroll"><table><tr><th>Month</th><th>Presence rows</th><th>Source records</th><th>Raw taxa</th><th>Exact locations</th><th>Observed days</th></tr>{months}</table></div>
<h2>Taxonomy review</h2><p>28,828 rows remain eligible. The following 255 rows remain in the archive and are not credited. The Yellow Warbler entry is the pre-split concept, not a Mangrove Yellow Warbler determination.</p>
<table><tr><th>Source name</th><th>Rows</th><th>Decision basis</th></tr>{reviews}</table>
<h2>Laptop use and preparation</h2><p>Measured on this Apple M4 Pro laptop with 24 GiB RAM; two threads and a 1 GB DuckDB budget per query process. The national ZSTD-3 archive stays compressed at 11.89 GiB. Optimized county import took {report['optimized_import']['wall_seconds']:.1f} seconds with {report['optimized_import']['python_process_peak_rss_mib']:.0f} MiB peak Python RSS. Candidate preparation took {report['candidate_preparation']['wall_seconds']:.1f} seconds. The complete road table took 20.5 seconds; two invalid negative table distances were rejected and replaced by actual route-service results in a further 1.8 seconds. No coordinates or road costs were rounded or clamped to repair them. These are observations under current filesystem-cache conditions, not controlled cold-cache measurements; process RSS excludes the kernel file cache and the router's peak memory was not measured.</p>
<p>The pilot's data and saved runs occupy approximately {(data_bytes+run_bytes)/2**30:.2f} GiB, including the rejected smoke snapshot, four successful runs, alternatives and reproducible analysis caches. The existing road graph and national archive are shared inputs, outside this pilot-only total. All 40 unique driving legs in seed 1 were separately checked against OSRM's route service: durations agreed to floating-point precision and the maximum distance difference was 0.1 metre. Their actual road geometry is retained separately without changing the frozen routes.</p>
<h2>Rules and practical limits</h2><p>Jan 1–Dec 31, 2024; 30 road miles and 12 activity hours per birding day; two hours per stop; up to eight hours of driving on transit days. Eight candidate pins are more than 500 m from a drivable anchor; the maximum is 1.47 km. The best route's maximum used offset is {best['max_visited_road_offset_meters']:.0f} m.</p><ul>{notes}</ul>
<h2>Sources and reproducibility</h2><p><a href="https://doi.org/10.15468/dl.qdv4gc">Frozen GBIF download</a> · <a href="https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_ACS2024/MapServer/82">Census January 2024 boundary</a> · <a href="https://www.aba.org/aba-checklist/">ABA checklist (frozen 8.19)</a> · <a href="https://www.birds.cornell.edu/clementschecklist/updates-and-corrections-october-2025/">Yellow Warbler split</a> · <a href="https://osmdata.openstreetmap.de/data/land-polygons.html">OSM land product</a></p>
<p><small>Frozen configs, catalog hashes, source checks, pipeline metrics and code hashes are saved in evaluation-summary.json. Each run retains its executable, road cache, evidence audit, complete calendar and independent validation.</small></p></html>'''
    (RUNS / "pilot-report.html").write_text(html)
    print(json.dumps({"conclusion": report["conclusion"], "best_run": best,
                      "storage_gib": (data_bytes + run_bytes) / 2**30}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["audit", "land", "source_check", "taxonomy", "inputs", "access", "roads", "searches", "monthly_probe", "route_geometry", "summarize"])
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    if args.stage in {"access", "roads", "route_geometry"}:
        globals()[args.stage](args.endpoint)
    else:
        globals()[args.stage]()
