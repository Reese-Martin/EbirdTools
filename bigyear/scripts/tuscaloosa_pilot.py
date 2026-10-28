"""Reproducible preparation of the authorized March 2025 Tuscaloosa sample."""

import argparse
from collections import Counter
import csv
from pathlib import Path
import urllib.request
import json
import zipfile

from bigyear_planner.config import Config
from bigyear_planner.ingest import classify_land, prepare
from bigyear_planner.problem import build_problem
from bigyear_planner.util import dump, read, sha256, write_csv

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/tuscaloosa-2025-03"
SAMPLE = ROOT / "data/ebd-sample"
EBD = SAMPLE / "ebd_US-AL-125_202503_202503_smp_relMar-2025.txt"
SED = SAMPLE / "ebd_US-AL-125_202503_202503_smp_relMar-2025_sampling.txt"
TAX = ROOT / "data/crosswalk-review-2026-10-15/outputs/aba-crosswalk-review/taxonomy-approved.csv"
CONFIG = Config(
    year=2025,
    start_date="2025-03-01",
    end_date="2025-03-31",
    activity_minutes=720,
    drive_minutes=480,
    birding_minutes=120,
    target_locations=1000,
    threads=4,
    seconds=600,
    seed=20250301,
)
POLICY = "tuscaloosa-ebd-mar2025-aba8.19-fixed-v1"
NETWORK = "Geofabrik-AL-2025-01-01_OSRM-26.10.0_car-v1"


def land():
    import shapefile
    from shapely.geometry import box, mapping, shape
    from shapely.ops import unary_union

    records = list(csv.DictReader(EBD.open(), delimiter="\t"))
    assert {r["COUNTY CODE"] for r in records} == {"US-AL-125"}
    assert {r["STATE CODE"] for r in records} == {"US-AL"}
    xs, ys = [float(r["LONGITUDE"]) for r in records], [float(r["LATITUDE"]) for r in records]
    bounds = (min(xs) - 0.1, min(ys) - 0.1, max(xs) + 0.1, max(ys) + 0.1)
    archive = DATA / "sources/land-polygons-split-4326.zip"
    folder = ROOT / "build/land-mask"
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for n in z.namelist():
            if n.endswith((".shp", ".shx", ".prj", ".cpg", "README.txt")):
                if not (folder / n).exists():
                    z.extract(n, folder)
        source_readme = z.read("land-polygons-split-4326/README.txt").decode()
    reader = shapefile.Reader(
        shp=str(folder / "land-polygons-split-4326/land_polygons.shp"),
        shx=str(folder / "land-polygons-split-4326/land_polygons.shx"),
    )
    clip = box(*bounds)
    polygons = [
        shape(s.__geo_interface__).intersection(clip) for s in reader.iterShapes(bbox=bounds)
    ]
    reader.close()
    mask = unary_union(polygons)
    assert not mask.is_empty and mask.is_valid
    mask_path = DATA / "terrestrial-mask.geojson"
    dump(mask_path, mapping(mask))
    classify_land(EBD, mask_path, DATA / "land.csv")
    rows = list(csv.DictReader((DATA / "land.csv").open()))
    dump(
        DATA / "land-provenance.json",
        {
            "source": "https://osmdata.openstreetmap.de/data/land-polygons.html",
            "download": "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip",
            "archive_sha256": sha256(archive),
            "readme": source_readme,
            "clipped_bounds": bounds,
            "intersecting_source_polygons": len(polygons),
            "classification": dict(Counter(r["on_land"] for r in rows)),
            "inland_water_policy": "Inland water pins represent nearby walking/shoreline/forest access, per user instruction. The coastline mask does not remove inland lakes.",
        },
    )
    print("Land classification:", dict(Counter(r["on_land"] for r in rows)), flush=True)


def inputs():
    dump(DATA / "config.json", CONFIG.to_dict())
    if not (DATA / "dataset/manifest.json").exists():
        result = prepare(
            EBD, TAX, DATA / "land.csv", DATA / "dataset", CONFIG, "EBD_relMar-2025", POLICY, SED
        )
        print("Import:", result["statistics"], flush=True)
    p = build_problem(DATA / "dataset", DATA / "problem-unanchored.json", CONFIG)
    print(
        "Full candidates:",
        len(p["locations"]),
        len(p["opportunities"]),
        len(p["species"]),
        flush=True,
    )


def access(endpoint):
    p = read(DATA / "problem-unanchored.json")
    rows, waypoints = [], []
    for loc in p["locations"]:
        url = f"{endpoint}/nearest/v1/driving/{loc['lon']},{loc['lat']}?number=1"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
        if data.get("code") != "Ok" or not data.get("waypoints"):
            raise ValueError(f"No documented road access for {loc['id']}")
        waypoint = data["waypoints"][0]
        row = {
            "locality_id": loc["locality_id"],
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "road_latitude": waypoint["location"][1],
            "road_longitude": waypoint["location"][0],
            "offset_meters": waypoint["distance"],
            "source": NETWORK,
            "walking_policy": "Walking and birding share the 120-minute stop allowance; reported pins can proxy nearby shoreline or forest. No boat travel inferred.",
        }
        rows.append(row)
        waypoints.append(
            {
                "id": loc["id"],
                "name": loc["name"],
                "reported": [loc["lon"], loc["lat"]],
                "response": data,
            }
        )
    write_csv(DATA / "road-access.csv", rows, list(rows[0]))
    dump(DATA / "road-access-evidence.json", waypoints)
    p = build_problem(
        DATA / "dataset", DATA / "problem.json", CONFIG, access_catalog=DATA / "road-access.csv"
    )
    offsets = sorted(float(r["offset_meters"]) for r in rows)
    dump(
        DATA / "access-summary.json",
        {
            "locations": len(rows),
            "maximum_offset_meters": max(offsets),
            "median_offset_meters": offsets[len(offsets) // 2],
            "over_100_meters": sum(x > 100 for x in offsets),
            "over_500_meters": sum(x > 500 for x in offsets),
            "policy": rows[0]["walking_policy"],
            "review": sorted(
                (
                    {"location": w["name"], "offset_meters": r["offset_meters"]}
                    for w, r in zip(waypoints, rows)
                ),
                key=lambda x: -x["offset_meters"],
            ),
        },
    )
    print(
        "Road-access offsets: maximum",
        max(offsets),
        "meters; candidates",
        len(p["locations"]),
        flush=True,
    )


def context():
    import shapefile

    archive = DATA / "sources/cb_2024_us_county_500k.zip"
    reader = shapefile.Reader(str(archive))
    feature = None
    for index, record in enumerate(reader.iterRecords()):
        if record.as_dict()["GEOID"] == "01125":
            feature = {
                "type": "Feature",
                "properties": {"name": "Tuscaloosa County, Alabama"},
                "geometry": reader.shape(index).__geo_interface__,
            }
            break
    reader.close()
    assert feature is not None
    folder = ROOT / "runs/tuscaloosa-2025-03"
    dump(folder / "map-context.geojson", {"type": "FeatureCollection", "features": [feature]})
    dump(
        folder / "map-context-source.json",
        {
            "source": "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_county_500k.zip",
            "sha256": sha256(archive),
            "use": "Display context only. Not a land mask or road constraint.",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["land", "inputs", "access", "context"])
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    if args.phase == "land":
        land()
    elif args.phase == "inputs":
        inputs()
    elif args.phase == "context":
        context()
    else:
        access(args.endpoint.rstrip("/"))
