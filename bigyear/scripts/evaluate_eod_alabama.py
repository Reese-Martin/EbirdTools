"""Reproducible full-year Alabama EOD evaluation; raw archive remains unchanged."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from html import escape
import io
import json
from pathlib import Path
import resource
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
DATA = ROOT / "data/alabama-eod-2024"
RUNS = ROOT / "runs/alabama-eod-2024"
ARCHIVE = ROOT / "data/eod-us-since-2000-zstd3"
BOUNDARY = DATA / "sources/state-01-acs2024.geojson"
PREVIOUS = ROOT / "data/tuscaloosa-2025-03"
REVIEW = ROOT / "data/crosswalk-review-2026-10-15"
NETWORK = "Geofabrik-AL-2025-01-01_OSRM-26.10.0_car-v1"
CONFIG = Config(year=2024, activity_minutes=720, drive_minutes=480, birding_minutes=120,
                target_locations=100000, threads=2, seconds=600, seed=20240101,
                memory_limit="1GB")
POLICY = "alabama-eod-2024-aba8.19-presence-v1"


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
    report = {"scope": "Alabama GEOID 01, January 1–December 31, 2024",
              "archive_manifest_sha256": sha256(ARCHIVE / "manifest.json"),
              "archive_rows": manifest["rows"], "boundary_sha256": sha256(BOUNDARY),
              "boundary_source": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_ACS2024/MapServer/80",
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
    with (ROOT / 'data/tuscaloosa-eod-2024/taxonomy.csv').open() as stream:
        county = {(r['source_name'], r['source_taxon_id']): r for r in csv.DictReader(stream)}
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
                      "Deferred for conservative statewide evaluation; retained in archive and review.")
            reference += "; https://www.aba.org/aba-area-introduced-species/"
        elif name in {"Alopochen aegyptiaca", "Cygnus olor", "Callipepla californica",
                      "Gallus gallus", "Myiopsitta monachus", "Lonchura punctulata"}:
            decision = presence = "review"
            reason = ("Introduced or captive population needs an Alabama-specific establishment/"
                      "wild-status decision. Species membership alone does not establish population "
                      "eligibility, and the EOD extract omits the exotic/captive fields. Retained for review.")
            reference += "; https://www.aba.org/aba-area-introduced-species/"
        elif name in {"Vireo gilvus", "Numenius phaeopus"}:
            decision = presence = "review"
            reason = ("Publisher-verbatim record retains the broad pre-2025 split name/concept. "
                      "Do not assign current split-species credit from location or name alone. "
                      "Retained for source-supported split assignment review.")
            reference += "; https://www.birds.cornell.edu/clementschecklist/updates-and-corrections-october-2025/"
        rows.append({"source_name": name, "source_taxon_id": concept,
                     "species_id": "aba8.19:" + name.replace(" ", "_"),
                     "common_name": match[1] if match else source["vernacularName"],
                     "aba_eligible": decision, "eligible_states": "US-AL",
                     "reference": reference, "eod_presence_eligible": presence,
                     "review_basis": reason, "prior_approved_ebd_concept": old["source_taxon_id"] if old else "",
                     "presence_rows": item["presence_rows"], "policy_id": POLICY})
    for row in rows:
        old = county.get((row['source_name'], row['source_taxon_id']))
        if old:
            for field in ('species_id', 'common_name', 'aba_eligible', 'eod_presence_eligible', 'eligible_states'):
                assert row[field] == old[field], (row['source_name'], field)
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
                         scope_geojson=BOUNDARY, scope_label="Alabama (01)")
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
    def anchor(loc):
        url = f"{endpoint}/nearest/v1/driving/{loc['lon']},{loc['lat']}?number=1"
        with urllib.request.urlopen(url, timeout=30) as response:
            result = json.load(response)
        if result.get('code') != 'Ok' or not result.get('waypoints'):
            raise ValueError(f"No road access for {loc['id']}")
        waypoint = result['waypoints'][0]
        return ({'locality_id': loc['locality_id'], 'latitude': loc['lat'],
                 'longitude': loc['lon'], 'road_latitude': waypoint['location'][1],
                 'road_longitude': waypoint['location'][0], 'offset_meters': waypoint['distance'],
                 'source': NETWORK,
                 'walking_policy': 'Walking and birding share the 120-minute stop allowance; reported pins can proxy nearby shoreline or forest. No boat travel inferred.'},
                {'location_id': loc['id'], 'reported': [loc['lon'], loc['lat']], 'response': result})
    with ThreadPoolExecutor(max_workers=4) as executor:
        for row, record in executor.map(anchor, p['locations']):
            rows.append(row)
            evidence.append(record)
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
    from bigyear_planner.dense_roads import osrm_matrix

    start = time.monotonic()
    result = osrm_matrix(DATA / 'problem.json', endpoint, DATA / 'roads.matrix.json', NETWORK)
    dump(DATA / 'roads-metrics.json', {'directed_pairs': result['reachable_location_pairs'],
        'anchors': len(result['anchors']), 'bytes': result['bytes'],
        'matrix_sha256': result['sha256'], 'network': NETWORK,
        'metrics': metric(start), 'matrix_wall_seconds': result['wall_seconds'],
        'exploratory_serial_seconds': (read(DATA / 'roads.matrix.serial-progress.json')['wall_seconds']
            if (DATA / 'roads.matrix.serial-progress.json').exists() else 0),
        'unreachable_pairs': len(result['locations'])**2 - result['reachable_location_pairs']})
    print(json.dumps(read(DATA / 'roads-metrics.json')), flush=True)


def searches():
    """Resume completed runs, but never overwrite partial run artifacts."""
    from bigyear_planner.runner import run

    smoke = RUNS / "smoke-60s"
    if not (smoke / "validation.json").exists():
        run(DATA / "problem.json", DATA / "roads.matrix.json", smoke,
            seconds=60, threads=2, seed=20240100)
    if not read(smoke / "validation.json")["valid"]:
        raise ValueError("the smoke test must pass before longer searches")
    pending = []
    for i in (1, 2, 3):
        folder = RUNS / f"seed-{i}-600s"
        if (folder / "validation.json").exists() and read(folder / "validation.json")["valid"]:
            continue
        command = [sys.executable, "-m", "bigyear_planner.cli", "search",
                   "--problem", str(DATA / "problem.json"), "--roads", str(DATA / "roads.matrix.json"),
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
    roads = load_roads(problem, DATA / "roads.matrix.json")
    plans = transitions(problem, roads)
    species = set(s for i in selected.values() for s in problem["opportunities"][i]["species"])
    route = materialize(problem, roads, plans, list(selected.values()),
                        {"optimal": False, "model": "monthly_presence_integration_probe",
                         "species": len(species), "selected": list(selected.values())})
    route["provenance"] = {"problem_sha256": sha256(DATA / "problem.json"),
                           "roads_sha256": sha256(DATA / "roads.matrix.json")}
    route["status"] = "Twelve-month integration probe; deliberately selected stops, not an optimized route"
    dump(DATA / "monthly-probe-route.json", route)
    audit = validate(DATA / "monthly-probe-route.json", DATA / "problem.json", DATA / "roads.matrix.json")
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
    roads = load_roads(problem, folder / "roads.matrix.json")
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
    roads.close()
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


def retention_audit():
    start = time.monotonic()
    p = read(DATA / 'problem-unanchored.json')
    m = read(DATA / 'dataset/manifest.json')
    from bigyear_planner.eod import attach_dataset
    con = duckdb.connect()
    con.execute("SET memory_limit='1GB'")
    con.execute('SET threads=2')
    evidence, _ = attach_dataset(con, DATA / 'dataset', m)
    con.execute('CREATE TEMP TABLE eligible AS SELECT * FROM ' + evidence)
    columns = ['presence_rows', 'source_records', 'locations', 'species', 'opportunities', 'days']
    values = con.execute('''SELECT count(*),sum(source_record_count),count(DISTINCT location_id),
        count(DISTINCT species_id),count(DISTINCT (date,location_id)),count(DISTINCT date) FROM eligible''').fetchone()
    result = dict(zip(columns, values))
    assert result['locations'] == len(p['locations']) == p['reduction']['input_locations']
    assert result['opportunities'] == len(p['opportunities'])
    expected = con.execute('''SELECT location_id,date::VARCHAR,list(DISTINCT species_id ORDER BY species_id)
        FROM eligible GROUP BY 1,2 ORDER BY 1,2''').fetchall()
    actual = sorted((p['locations'][o['location']]['id'], o['date'], sorted(p['species'][s]['id']
                     for s in o['species'])) for o in p['opportunities'])
    assert expected == actual
    with (DATA / 'taxonomy.csv').open() as stream:
        state = {(r['source_name'], r['source_taxon_id']): r for r in csv.DictReader(stream)}
    with (ROOT / 'data/tuscaloosa-eod-2024/taxonomy.csv').open() as stream:
        county = list(csv.DictReader(stream))
    fields = ['species_id', 'aba_eligible', 'eod_presence_eligible', 'eligible_states']
    assert all(all(state[(r['source_name'], r['source_taxon_id'])][k] == r[k] for k in fields) for r in county)
    monthly = [dict(zip(['month', 'presence_rows', 'source_records', 'species', 'locations', 'days'], row))
        for row in con.execute('''SELECT month(date),count(*),sum(source_record_count),
            count(DISTINCT species_id),count(DISTINCT location_id),count(DISTINCT date)
            FROM eligible GROUP BY 1 ORDER BY 1''').fetchall()]
    write_csv(DATA / 'eligible-monthly-coverage.csv', monthly, list(monthly[0]))
    con.close()
    dump(DATA / 'model-retention-audit.json', {'valid': True, 'eligible': result,
        'all_locations_retained': True, 'all_date_location_species_combinations_identical': True,
        'common_county_concepts_identical': len(county), 'no_additional_coordinate_aggregation': True,
        'metrics': metric(start)})


def common_county():
    from bigyear_planner.travel import load_roads
    start = time.monotonic()
    county = ROOT / 'data/tuscaloosa-eod-2024'
    p, c = read(DATA / 'problem.json'), read(county / 'problem.json')
    state_roads, county_roads = load_roads(p, DATA / 'roads.matrix.json'), load_roads(c, county / 'roads.sqlite')
    state_indices = {l['id']: i for i, l in enumerate(p['locations'])}
    lookup = {i: state_indices[l['id']] for i, l in enumerate(c['locations'])}
    assert all(p['locations'][j]['road_access'] == c['locations'][i]['road_access'] for i, j in lookup.items())
    checked = identical = 0
    max_m = max_s = 0
    differences = []
    for (a, b), road in county_roads.items():
        state = state_roads[lookup[a], lookup[b]]
        dm = abs(road['miles'] - state['miles']) * 1609.344
        ds = abs(road['minutes'] - state['minutes']) * 60
        max_m, max_s = max(max_m, dm), max(max_s, ds)
        checked += 1
        identical += road['miles'] == state['miles'] and road['minutes'] == state['minutes']
        if dm > .11 or ds > .11:
            differences.append({'a': a, 'b': b, 'difference_m': dm, 'difference_s': ds})
    state_opps = {(o['date'], p['locations'][o['location']]['id']): o for o in p['opportunities']}
    for opportunity in c['opportunities']:
        state = state_opps[opportunity['date'], c['locations'][opportunity['location']]['id']]
        assert {c['species'][i]['id'] for i in opportunity['species']} == {p['species'][i]['id'] for i in state['species']}
    report = {'pairs_checked': checked, 'float64_identical_pairs': identical,
        'max_distance_difference_m': max_m, 'max_duration_difference_s': max_s,
        'all_within_0_11_meters_and_seconds': not differences, 'differences': differences,
        'all_county_626_locations_and_2203_opportunities_preserved': True,
        'wall_seconds': time.monotonic() - start}
    dump(DATA / 'county-road-equivalence.json', report)
    state_roads.close()
    county_roads.close()
    if differences:
        raise ValueError('common road costs differ materially')
    print(json.dumps(report), flush=True)


def county_replay():
    from bigyear_planner.runner import materialize
    from bigyear_planner.travel import load_roads, transitions
    from bigyear_planner.validate import validate
    start = time.monotonic()
    p = read(DATA / 'problem.json')
    county_route = read(ROOT / 'runs/tuscaloosa-eod-2024/seed-1-600s/route.json')
    lookup = {(o['date'], p['locations'][o['location']]['id']): o['id'] for o in p['opportunities']}
    selected = [lookup[d['date'], county_route['locations'][s['location']]['id']]
                for d in county_route['days'] for s in d['stops']]
    roads = load_roads(p, DATA / 'roads.matrix.json')
    route = materialize(p, roads, transitions(p, roads), selected,
        {'optimal': False, 'model': 'original_county_route_replayed_in_full_state_model',
         'species': county_route['species_total'], 'selected': selected})
    assert route['species_total'] == county_route['species_total'] == 224
    route['provenance'] = {'problem_sha256': sha256(DATA / 'problem.json'),
                           'roads_sha256': sha256(DATA / 'roads.matrix.json')}
    dump(DATA / 'county-replay-route.json', route)
    audit = validate(DATA / 'county-replay-route.json', DATA / 'problem.json', DATA / 'roads.matrix.json')
    assert audit['valid'] and audit['days_checked'] == 366
    dump(DATA / 'county-replay-validation.json', {'validation': audit, 'metrics': metric(start),
         'purpose': 'Original 224-species county route remains feasible inside the full statewide candidate model'})
    roads.close()


def matched_county():
    """Freeze all original county float64 road costs and rerun the same three budgets."""
    import math
    import struct
    from bigyear_planner.dense_roads import DenseRoads, FORMAT, location_signature
    from bigyear_planner.travel import load_roads
    county = ROOT / 'data/tuscaloosa-eod-2024'
    p = read(county / 'problem.json')
    path = county / 'roads.matrix.json'
    if not path.exists():
        start = time.monotonic()
        roads = load_roads(p, county / 'roads.sqlite')
        binary = path.with_suffix('.bin')
        n = len(p['locations'])
        with binary.open('wb') as stream:
            for a in range(n):
                for b in range(n):
                    r = roads.get((a, b))
                    stream.write(struct.pack('<dd', r['miles'], r['minutes']) if r else struct.pack('<dd', math.inf, math.inf))
        manifest = {'format': FORMAT, 'binary': binary.name, 'bytes': binary.stat().st_size,
            'sha256': sha256(binary), 'locations': [location_signature(l) for l in p['locations']],
            'anchors': [location_signature(l)[3:] for l in p['locations']], 'location_nodes': list(range(n)),
            'source': 'http://127.0.0.1:5001', 'network_release': NETWORK,
            'reachable_location_pairs': len(roads), 'reachable_anchor_pairs': len(roads),
            'source_sqlite_sha256': sha256(county / 'roads.sqlite'), 'wall_seconds': time.monotonic()-start}
        binary.chmod(0o444)
        dump(path, manifest)
        dense = DenseRoads(p, path)
        checked = 0
        for key, r in roads.items():
            assert dense[key]['miles'] == r['miles'] and dense[key]['minutes'] == r['minutes']
            checked += 1
        dump(county / 'matrix-equivalence.json', {'pairs_checked': checked,
            'all_float64_costs_identical': True, 'matrix_sha256': manifest['sha256'],
            'wall_seconds': time.monotonic()-start})
        roads.close()
        dense.close()
    pending = []
    for i in (1, 2, 3):
        output = RUNS / 'matched-tuscaloosa' / f'seed-{i}-600s'
        if (output / 'validation.json').exists() and read(output / 'validation.json')['valid']:
            continue
        command = [sys.executable, '-m', 'bigyear_planner.cli', 'search',
            '--problem', str(county / 'problem.json'), '--roads', str(path), '--output', str(output),
            '--seconds', '600', '--threads', '2', '--seed', str(20240100+i)]
        pending.append(subprocess.Popen(command))
    statuses = [job.wait() for job in pending]
    if any(statuses):
        raise ValueError('matched county search failed; retained artifacts require inspection')


def count_bound():
    """A same-date geographic conflict is an independent species-count certificate."""
    from bigyear_planner.eod import attach_dataset
    from bigyear_planner.problem import haversine
    from bigyear_planner.travel import load_roads, road_point
    start = time.monotonic()
    p = read(DATA / 'problem.json')
    species = {s['name']: i for i, s in enumerate(p['species'])}
    chosen = ['Sooty Tern', 'Pomarine Jaeger']
    records = []
    con = duckdb.connect()
    con.execute("SET memory_limit='1GB'")
    con.execute('SET threads=2')
    evidence, _ = attach_dataset(con, DATA / 'dataset', p['manifest'])
    for name in chosen:
        sp = species[name]
        opportunities = [o for o in p['opportunities'] if sp in o['species']]
        assert len(opportunities) == 1
        o = opportunities[0]
        l = p['locations'][o['location']]
        rows = con.execute(f'''SELECT date::VARCHAR,location_id,latitude_text,longitude_text,
            record_id,source_record_count FROM {evidence} WHERE species_id=?''',
            [p['species'][sp]['id']]).fetchall()
        assert len({(r[0], r[1]) for r in rows}) == 1
        assert all((r[0], r[1]) == (o['date'], l['id']) for r in rows)
        records.append({'name': name, 'species_id': p['species'][sp]['id'],
            'date': o['date'], 'opportunity_id': o['id'], 'location_index': o['location'],
            'reported_location': l, 'source_groups': [dict(zip(
                ['date', 'location_id', 'latitude_text', 'longitude_text', 'record_id', 'source_record_count'], r)) for r in rows]})
    con.close()
    assert records[0]['date'] == records[1]['date'] == '2024-09-13'
    a, b = [r['location_index'] for r in records]
    geographic_lower_bound = haversine(road_point(p['locations'][a]), road_point(p['locations'][b]))
    assert geographic_lower_bound > p['config']['local_miles'] + 1
    roads = load_roads(p, DATA / 'roads.matrix.json')
    road_costs = {'forward': roads[a, b], 'reverse': roads[b, a]}
    roads.close()
    ceiling = p['reduction']['candidate_species_ceiling']
    result = {'schema': 1, 'valid': True, 'upper_bound': ceiling-1,
        'eligible_species_ceiling': ceiling, 'problem_sha256': sha256(DATA / 'problem.json'),
        'roads_manifest_sha256': sha256(DATA / 'roads.matrix.json'),
        'records': records, 'same_date': records[0]['date'],
        'road_anchor_geodesic_lower_bound_miles': geographic_lower_bound,
        'birding_day_road_limit_miles': p['config']['local_miles'], 'road_costs': road_costs,
        'logic': 'Each species has only one eligible date/location, both September 13. Their road anchors are farther apart even in a straight line than the entire 30-mile driving budget. Any road chain visiting both would exceed that budget. The calendar cannot contain separate birding and transit days on the same date, and overnight repositioning/flights are unavailable. Therefore at least one of the 344 eligible species must remain uncredited: no route can exceed 343.',
        'scope': 'Primary species count only, for this frozen exact-coordinate Alabama 2024 presence model. No proof of minimum travel or real-world detection.',
        'metrics': metric(start)}
    dump(DATA / 'count-bound.json', result)
    dump(RUNS / 'count-bound.json', result)
    print(json.dumps({k: result[k] for k in ['upper_bound', 'same_date', 'road_anchor_geodesic_lower_bound_miles', 'metrics']}), flush=True)


def summarize():
    """Compare different geographic cohorts explicitly; generic year-compare rejects them."""
    from statistics import median
    from bigyear_planner.util import hardware

    county_data = ROOT / 'data/tuscaloosa-eod-2024'
    county_runs = ROOT / 'runs/tuscaloosa-eod-2024'
    alabama_problem = read(DATA / 'problem.json')
    county_problem = read(county_data / 'problem.json')
    assert alabama_problem['config'] == county_problem['config']
    retention = read(DATA / 'model-retention-audit.json')
    assert retention['valid'] and retention['common_county_concepts_identical'] == 228
    cost_equivalence = read(DATA / 'county-road-equivalence.json')
    assert cost_equivalence['all_within_0_11_meters_and_seconds']

    modes = read(RUNS / 'selected-road-mode-verification.json')
    ferry_counts = Counter()
    for pair in modes['results']:
        for usage in pair['usage']:
            ferry_counts[usage['route']] += len(pair['non_driving_steps'])

    def summarize_run(folder, cohort, implementation):
        route, audit, benchmark = [read(folder / f) for f in
                                    ('route.json', 'validation.json', 'benchmark.json')]
        assert audit['valid'] and audit['days_checked'] == 366
        kinds = Counter(d['kind'] for d in route['days'])
        offsets = [route['locations'][s['location']]['road_access']['offset_meters']
                   for d in route['days'] for s in d['stops']]
        assert not any(l['mode'] == 'flight' for d in route['days'] for l in d['legs'])
        history = [json.loads(line) for line in (folder / 'improvements.jsonl').read_text().splitlines()]
        bound = 343 if cohort == 'Alabama' else 224
        return {'cohort': cohort, 'implementation': implementation, 'run': folder.name,
            'seed': benchmark['seed'], 'species': route['species_total'],
            'eligible_species': route['annual_recorded_species_ceiling'],
            'road_miles': audit['road_miles'], 'driving_hours': audit['travel_minutes']/60,
            'stops': sum(len(d['stops']) for d in route['days']),
            'vehicle_ferry_crossings': ferry_counts[str((folder/'route.json').relative_to(RUNS))] if cohort == 'Alabama' else 0,
            'birding_days': kinds['birding'], 'transit_days': kinds['road_transit'],
            'rest_days': kinds['rest'], 'calendar_days_validated': audit['days_checked'],
            'evidence_rows_checked': audit['evidence_rows_checked'],
            'search_seconds': benchmark['engine']['elapsed_seconds'],
            'total_run_seconds': benchmark['wall_seconds'],
            'engine_peak_rss_mib': benchmark['engine_peak_rss_bytes']/2**20,
            'python_peak_rss_mib': benchmark['hardware']['python_peak_rss_bytes']/2**20,
            'candidate_evaluations': benchmark['engine']['candidate_evaluations'],
            'seconds_to_count_bound': next((r['elapsed_seconds'] for r in history if r['species'] == bound), None),
            'engine_cpu_seconds': benchmark['engine_cpu_seconds'],
            'model_locations': benchmark['model']['locations'],
            'model_opportunities': benchmark['model']['opportunities'],
            'directed_roads': benchmark['model']['directed_roads'],
            'engine_input_bytes': benchmark['model']['engine_input_bytes'],
            'max_visited_road_offset_meters': max(offsets),
            'visits_over_500_meters': sum(v > 500 for v in offsets),
            'engine_sha256': route['provenance']['engine_sha256'],
            'valid': True, 'route_sha256': sha256(folder / 'route.json')}

    records = [summarize_run(RUNS / 'smoke-60s', 'Alabama', 'compact')]
    for i in (1, 2, 3):
        name = f'seed-{i}-600s'
        records += [summarize_run(RUNS / name, 'Alabama', 'compact'),
                    summarize_run(RUNS / 'matched-tuscaloosa' / name, 'Tuscaloosa', 'compact'),
                    summarize_run(county_runs / name, 'Tuscaloosa', 'original SQLite/BY1')]
    state_seeds = [r for r in records if r['cohort'] == 'Alabama' and r['run'] != 'smoke-60s']
    county_seeds = [r for r in records if r['cohort'] == 'Tuscaloosa' and r['implementation'] == 'compact']
    assert len({r['engine_sha256'] for r in state_seeds + county_seeds}) == 1
    best = min(state_seeds, key=lambda r: (-r['species'], r['driving_hours'], r['road_miles']))
    nearer_access = min(state_seeds, key=lambda r: (r['max_visited_road_offset_meters'], r['driving_hours']))
    best_route = read(RUNS / best['run'] / 'route.json')
    acquired = {s for d in best_route['days'] for stop in d['stops'] for s in stop['species']}
    missing = [s for s in best_route['species'] if s['id'] not in acquired]
    write_csv(RUNS / 'unreached-species.csv', missing, ['id', 'name'])
    visited_access = [dict(date=d['date'], location=stop['name'],
                      offset_meters=best_route['locations'][stop['location']]['road_access']['offset_meters'])
                      for d in best_route['days'] for stop in d['stops']
                      if best_route['locations'][stop['location']]['road_access']['offset_meters'] > 500]
    write_csv(RUNS / 'selected-access-review.csv', visited_access, ['date', 'location', 'offset_meters'])

    def aggregate(rows):
        return {k: {'min': min(r[k] for r in rows), 'median': median(r[k] for r in rows),
                    'max': max(r[k] for r in rows)}
                for k in ('species', 'road_miles', 'driving_hours', 'search_seconds',
                          'total_run_seconds', 'engine_peak_rss_mib', 'python_peak_rss_mib',
                          'candidate_evaluations', 'seconds_to_count_bound')}

    inode_files = {}
    logical = 0
    for root in (DATA, RUNS):
        for p in root.rglob('*'):
            if p.is_file():
                st = p.stat(); logical += st.st_size
                inode_files[(st.st_dev, st.st_ino)] = (st.st_size, st.st_blocks*512)
    storage = {'logical_bytes_including_hard_links': logical,
               'unique_file_bytes': sum(x[0] for x in inode_files.values()),
               'allocated_unique_bytes': sum(x[1] for x in inode_files.values()),
               'matrix_bytes': read(DATA / 'roads.matrix.json')['bytes'],
               'excludes': 'Existing national Parquet archive and shared land/OSRM graph; completed matrix hard links counted once.'}
    limitations = [
        'Technical perfect-hindsight presence experiment, not an ABA-certified Big Year or a guarantee of future detection. EOD lacks individual approval, captive/exotic flags, effort and checklist identities.',
        'All source rows and coordinate strings remain in the verified original archive. Boundary, taxonomy, geospatial-quality and land exclusions only govern credit; no additional spatial rounding or observation thinning was performed.',
        'No scoped records have flagged or unknown geospatial issues, but reported coordinate uncertainty is missing throughout; preserving exact coordinate strings does not establish positional accuracy.',
        'Raw stateProvince=Alabama: 784,642 groups and 1,068,698 source records. The detailed 2024 polygon excludes 38 groups at 11 points (215 source records); the full source cohort is preserved. State-boundary water and offshore locations are further subject to the same terrestrial-credit rule.',
        'The fixed ABA 8.19 presence policy preserves all 228 shared Tuscaloosa concepts. Additional introduced populations and broad pre-split concepts require review; eligible species totals are conditional on this policy, not a list of every wild species that occurred in Alabama.',
        'The unchanged OSRM car profile includes the Fort Morgan–Dauphin Island vehicle ferry. Coastal routes use it; crossings and their modeled distance/time count toward the car-travel budgets. Historical ferry schedules, queues and boarding availability are not modeled. These are flight-free car-profile results, including vehicle ferries.',
        'The road graph dated January 1, 2025 is the same proxy for 2024 used in the county pilot. Historical traffic, closures and legal access are not reconstructed. Network paths can leave the observation polygon.',
        'The lowest-travel route selects an April 20 point 5.24 km from its road-access anchor. Its walking connection is unverified, so it should not be treated as a ready-to-follow itinerary. Seed 1 also reaches 343 species and keeps all selected pins within 801 m of a road anchor, at 4,954.6 car-profile miles; that remains an access proxy rather than a verified walking route.',
        'Reported pins remain exact; road-access anchors are separate. Walking and birding share the fixed two-hour stop allowance. Inland-water pins retain the authorized nearby walking/shoreline proxy; actual footpaths, walking duration and property access remain unverified. Large selected offsets are listed for review.',
        'Same-day stops follow stable opportunity-ID order. Species credit is available throughout the recorded day; the data do not establish observation time or detection probability.',
        'All six 600-second comparison jobs use the same executable, seeds, two threads, frozen travel rules and no flights. Each cohort used three concurrent jobs; county runs overlapped road preparation. These laptop measurements are indicative, not isolated hardware benchmarks. Peak RSS includes file-backed matrix pages shared across processes; process peaks occur at different times and should not be added.',
        'An independent same-day singleton conflict bounds the count at 343: Sooty Tern and Pomarine Jaeger occur only September 13 at distant locations. A validated 343-species route attains that primary-count bound, conditional on this model and policy. This does not prove minimum driving.',
        'Search results are best found within the stated budget. A gap to the eligible species ceiling alone is not proof that the missing species are infeasible. Minimum driving is not proven.',
        'Main routes pass independent 366-day calendar, network-cost and source-evidence validation. All saved display roads are checked against the same network; alternative routes are materialized but only separately validated where recorded in alternative-validation.json.',
        'The full all-pairs road matrix still grows quadratically with the number of distinct anchors; nationwide routing needs a different road-cost strategy. This tests a state-year, not nationwide routing or scientific effort-adjusted abundance. No same-year EBD reference was available for a completeness comparison.'
    ]
    report = {'scope': 'Alabama, full calendar year 2024, compared with Tuscaloosa County 2024',
        'conclusion': 'PASS for statewide technical operation; foot access and historical ferry service remain unverified',
        'best_run': best, 'route_with_nearer_road_access': nearer_access, 'runs': records, 'count_bound': read(DATA / 'count-bound.json'),
        'primary_count_bound_attained': best['species'] == read(DATA / 'count-bound.json')['upper_bound'], 'aggregate': {'Alabama': aggregate(state_seeds),
            'Tuscaloosa_matched': aggregate(county_seeds)},
        'config': alabama_problem['config'], 'retention': retention,
        'coverage': read(DATA / 'coverage-audit.json'), 'preflight': read(DATA / 'preflight.json'),
        'eligible': read(DATA / 'dataset/manifest.json')['statistics'],
        'common_county_road_comparison': cost_equivalence,
        'source_verification': read(DATA / 'source-verification.json'),
        'taxonomy_policy': read(DATA / 'taxonomy-policy.json'),
        'stages': {k: read(DATA / f) for k, f in [
            ('audit', 'coverage-audit.json'), ('land', 'land-provenance.json'),
            ('import', 'optimized-import-metrics.json'), ('candidates', 'candidate-preparation-metrics.json'),
            ('access', 'access-summary.json'), ('roads', 'roads-metrics.json')]},
        'display_verification': read(RUNS / 'display-verification.json'), 'road_mode_verification': modes,
        'viewer_verification': read(RUNS / 'viewer-verification.json'),
        'smoke_alternative_validation': read(RUNS / 'smoke-60s/alternative-validation.json'),
        'monthly_probe': read(DATA / 'monthly-probe-validation.json'),
        'county_route_replay': read(DATA / 'county-replay-validation.json'),
        'software_verification': read(RUNS / 'software-verification.json'),
        'hardware': hardware(), 'storage': storage, 'unreached_species': missing,
        'limitations': limitations,
        'code_sha256': {str(p.relative_to(ROOT)): sha256(p) for p in [Path(__file__),
            ROOT / 'src/bigyear_planner/eod.py', ROOT / 'src/bigyear_planner/travel.py',
            ROOT / 'src/bigyear_planner/dense_roads.py', ROOT / 'src/bigyear_planner/runner.py',
            ROOT / 'src/bigyear_planner/validate.py', ROOT / 'cpp/model.h', ROOT / 'cpp/search.cpp']}}
    dump(RUNS / 'evaluation-summary.json', report)
    write_csv(RUNS / 'run-summary.csv', records, list(records[0]))
    context = read(BOUNDARY)
    context['features'][0]['properties']['name'] = 'Alabama (2024)'
    dump(RUNS / 'map-context.geojson', context)
    state = report['aggregate']['Alabama']; county = report['aggregate']['Tuscaloosa_matched']
    def cell(value):
        return escape(str(value))
    comparison = [
        ('Eligible presence groups', '28,828', f"{retention['eligible']['presence_rows']:,}"),
        ('Source observations represented', '38,650', f"{retention['eligible']['source_records']:,}"),
        ('Exact eligible locations retained', '626 / 626', '17,925 / 17,925'),
        ('Location-date opportunities', '2,203', f"{retention['eligible']['opportunities']:,}"),
        ('Eligible species ceiling', '224', str(retention['eligible']['species'])),
        ('Independent species-count upper bound', '224', str(report['count_bound']['upper_bound'])),
        ('Vehicle ferries used by best-count route', '0', str(best['vehicle_ferry_crossings'])),
        ('Best species found (three 600s seeds)', str(max(r['species'] for r in county_seeds)), str(best['species'])),
        ('Median total seconds per run', f"{county['total_run_seconds']['median']:.1f}", f"{state['total_run_seconds']['median']:.1f}"),
        ('Median C++ peak RSS (MiB)', f"{county['engine_peak_rss_mib']['median']:.1f}", f"{state['engine_peak_rss_mib']['median']:.1f}"),
        ('Median Python peak RSS (MiB)', f"{county['python_peak_rss_mib']['median']:.1f}", f"{state['python_peak_rss_mib']['median']:.1f}")]
    table = ''.join('<tr>'+''.join('<td>'+cell(v)+'</td>' for v in row)+'</tr>' for row in comparison)
    details = ''.join(f"<tr><td>{cell(r['cohort'])}</td><td>{cell(r['implementation'])}</td>"
        f"<td>{cell(r['run'])}</td><td>{r['species']}</td><td>{r['road_miles']:.1f}</td>"
        f"<td>{r['driving_hours']:.2f}</td><td>{r['total_run_seconds']:.1f}</td>"
        f"<td>{r['python_peak_rss_mib']:.0f} / {r['engine_peak_rss_mib']:.0f}</td></tr>" for r in records)
    notes = ''.join('<li>'+escape(t)+'</li>' for t in limitations)
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>Alabama EOD 2024 · statewide evaluation</title><style>
    :root{{font:16px/1.55 system-ui;color:#17251c;background:#f5f7f3}}body{{max-width:1120px;margin:40px auto;padding:0 24px}}
    h1{{font-size:2.3rem;line-height:1.1}}h2{{margin-top:36px}}.eyebrow{{color:#36824b;font-weight:700;letter-spacing:.1em}}
    .card{{background:white;border:1px solid #dbe5da;border-radius:12px;padding:22px;margin:24px 0}}
    .hero{{font-size:1.3rem}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
    td,th{{text-align:left;padding:10px;border-bottom:1px solid #e1e8dd}}th{{color:#28613a}}.scroll{{overflow-x:auto}}li{{margin:12px 0}}a{{color:#28613a}}small{{color:#506354}}</style>
    <p class="eyebrow">EOD ROUTE LAB · VERIFIED STATE-YEAR TEST</p><h1>Alabama, all of 2024</h1>
    <p class="hero">Best route: <strong>{best['species']} of {retention['eligible']['species']} eligible species</strong>,
    {best['road_miles']:,.1f} car-profile miles and {best['driving_hours']:.1f} car-travel hours, including {best['vehicle_ferry_crossings']} vehicle-ferry crossings.
    All 366 days passed independent validation. Every eligible location and presence group was retained.</p>
    <p><strong>Count upper bound: {report['count_bound']['upper_bound']}.</strong> Sooty Tern and Pomarine Jaeger each have just one eligible date/location, both September 13. Their road anchors are {report['count_bound']['road_anchor_geodesic_lower_bound_miles']:.1f} miles apart in a straight line, exceeding the entire 30-mile birding-day driving budget. Both cannot be credited. A route matching the bound establishes the maximum species count in this frozen model; minimum driving remains unproven.</p>
    <div class="card"><strong>Access review required.</strong> The lowest-travel result selects a point {best['max_visited_road_offset_meters']/1000:.2f} km from road access, with no validated walking connection.
    Another 343-species run keeps every selected pin within {nearer_access['max_visited_road_offset_meters']:.1f} m of a road anchor and uses {nearer_access['road_miles']:,.1f} car-profile miles.
    <a href="/?run={nearer_access['run']}/route.json">View that route</a>. Walking paths and legal access still need verification.</div>
    <p><a href="/?run={best['run']}/route.json">Open best route map</a> · <a href="/run-summary.csv">Download run comparison CSV</a></p>
    <div class="card"><h2>Matched comparison</h2><p>Same seeds, executable, travel rules, two threads and 600-second search budget per run. Summary values use three seeds; detailed rows retain the original county baseline too.</p>
    <table><thead><tr><th>Measure</th><th>Tuscaloosa County</th><th>All Alabama</th></tr></thead><tbody>{table}</tbody></table></div>
    <div class="card"><h2>Search outcomes</h2><div class="scroll"><table><thead><tr><th>Cohort</th><th>Road model</th><th>Run</th><th>Species</th><th>Car-profile miles</th><th>Travel hours</th><th>Total seconds</th><th>Python / C++ RSS MiB</th></tr></thead><tbody>{details}</tbody></table></div></div>
    <div class="card"><h2>Storage and preparation</h2><p>The shared float64 matrix occupies {storage['matrix_bytes']/2**30:.2f} GiB.
    The full evaluation uses about {storage['allocated_unique_bytes']/2**30:.2f} GiB of additional allocated storage, counting hard links once.
    The original {read(ARCHIVE/'manifest.json')['bytes']/2**30:.2f} GiB national Parquet archive is read directly and remains unchanged.</p>
    <p>Detailed stage timings, source checks, all 228 shared concept decisions, and the county road-cost comparison are saved in evaluation-summary.json.</p></div>
    <h2>Interpretation and limits</h2><ul>{notes}</ul><small>Frozen inputs, executable snapshots, source evidence, provenance hashes and complete calendars are retained beside each run.</small></html>'''
    (RUNS / 'pilot-report.html').write_text(html)
    print(json.dumps({'best_run': best, 'aggregate': report['aggregate'], 'storage': storage}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["audit", "land", "source_check", "taxonomy", "inputs", "access", "roads", "searches", "monthly_probe", "route_geometry", "summarize", "retention_audit", "common_county", "county_replay", "matched_county", "count_bound"])
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    if args.stage in {"access", "roads", "route_geometry"}:
        globals()[args.stage](args.endpoint)
    else:
        globals()[args.stage]()
