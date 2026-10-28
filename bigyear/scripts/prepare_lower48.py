"""Resumable national inputs. Exact source strings and all 2024 rows stay in Parquet."""

import argparse
import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from pathlib import Path
import shutil
import subprocess
import urllib.parse
import zipfile

from bigyear_planner.config import Config, STATES
from bigyear_planner.eod import STATE_NAMES, locality_id, scoped_points, prepare_eod
from bigyear_planner.eod_archive import archive_scan
from bigyear_planner.util import ROOT, ManagedDuckDB, dump, q, read, sha256, write_csv

DATA = ROOT / "data/lower48-eod-2024"
ARCHIVE = ROOT / "data/eod-us-since-2000-zstd3"
BOUNDARY = DATA / "sources/lower48-dc-acs2024.geojson"
POLICY = "ABA8.19-national-explicit-EOD-presence-only-v1"
CONFIG = Config(
    year=2024,
    activity_minutes=720,
    birding_minutes=120,
    drive_minutes=480,
    target_locations=10000000,
    seconds=600,
    threads=2,
    seed=20240101,
    alternatives=1,
    memory_limit="3GB",
    walking_speed_kmh=2.2,
    walking_threshold_m=100,
)


def download(url, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    subprocess.run(
        ["/usr/bin/curl", "-fLsS", "--retry", "3", "--max-time", "180", "-o", str(tmp), url],
        check=True,
    )
    tmp.replace(path)


def audit():
    DATA.mkdir(parents=True, exist_ok=True)
    if not BOUNDARY.exists() or BOUNDARY.read_text().startswith("<"):
        codes = "01 04 05 06 08 09 10 11 12 13 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56".split()
        features = []
        for offset in range(0, len(codes), 6):
            where = "GEOID IN (" + ",".join(q(c) for c in codes[offset : offset + 6]) + ")"
            params = {
                "where": where,
                "outFields": "GEOID,NAME",
                "outSR": 4326,
                "returnGeometry": "true",
                "f": "geojson",
            }
            path = DATA / "sources" / f"states-{offset}.geojson"
            if not path.exists():
                download(
                    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_ACS2024/MapServer/80/query?"
                    + urllib.parse.urlencode(params),
                    path,
                )
            features.extend(read(path)["features"])
        dump(BOUNDARY, {"type": "FeatureCollection", "features": features})
    assert {f["properties"]["NAME"] for f in read(BOUNDARY)["features"]} == {
        STATE_NAMES[s[3:]] for s in STATES
    }
    source, _ = archive_scan(ARCHIVE)
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'")
        con.execute("SET threads=2")
        if not (DATA / "scope-points.csv").exists():
            write_csv(
                DATA / "scope-points.csv.part",
                scoped_points(con, source, CONFIG.year, BOUNDARY),
                ["state", "latitude", "longitude"],
            )
            (DATA / "scope-points.csv.part").replace(DATA / "scope-points.csv")
        names = ",".join(q(STATE_NAMES[s[3:]]) for s in sorted(STATES))
        if not (DATA / "taxa.csv").exists():
            rows = con.execute(f"""SELECT v_scientificname,v_taxonconceptid,count(*),sum(source_record_count),
                min(representative_gbifid) FROM {source} WHERE year={CONFIG.year} AND stateprovince IN ({names})
                GROUP BY 1,2 ORDER BY 1,2""").fetchall()
            fields = [
                "source_name",
                "source_taxon_id",
                "presence_rows",
                "source_records",
                "example_gbif_id",
            ]
            write_csv(DATA / "taxa.csv", [dict(zip(fields, r)) for r in rows], fields)
    dump(DATA / "config.json", CONFIG.to_dict())
    print(
        json.dumps(
            {
                "stage": "audit",
                "taxa": sum(1 for _ in csv.DictReader((DATA / "taxa.csv").open())),
                "scope_points_sha256": sha256(DATA / "scope-points.csv"),
            }
        ),
        flush=True,
    )


def taxonomy():
    taxa = list(csv.DictReader((DATA / "taxa.csv").open()))
    dest = DATA / "sources/gbif-verbatim"
    dest.mkdir(exist_ok=True)
    prefix = "http://rs.tdwg.org/dwc/terms/"

    def fetch(item):
        name = item["example_gbif_id"] + ".json"
        target = dest / name
        if not target.exists():
            existing = next((ROOT / "data").glob("*/sources/gbif-verbatim/" + name), None)
            if existing:
                shutil.copyfile(existing, target)
            else:
                download(
                    "https://api.gbif.org/v1/occurrence/" + item["example_gbif_id"] + "/verbatim",
                    target,
                )
        raw = read(target)
        record = {
            k: raw.get(k, raw.get(prefix + k))
            for k in [
                "scientificName",
                "taxonConceptID",
                "vernacularName",
                "basisOfRecord",
                "occurrenceStatus",
                "datasetKey",
            ]
        }
        if (
            record["scientificName"] != item["source_name"]
            or record["taxonConceptID"] != item["source_taxon_id"]
            or record["basisOfRecord"] != "HumanObservation"
            or record["occurrenceStatus"] != "PRESENT"
            or record["datasetKey"] != "4fa7b334-ce0d-4e88-aaae-2e0c138d049e"
        ):
            raise ValueError("publisher verification differs: " + item["source_name"])
        return record

    with ThreadPoolExecutor(max_workers=4) as pool:
        verified = list(pool.map(fetch, taxa))
    aba_zip = ROOT / "data/crosswalk-review-2026-10-15/sources/ABA_Checklist-8.19.csv.zip"
    with zipfile.ZipFile(aba_zip) as z:
        aba = {
            r[3]: r
            for r in csv.reader(io.StringIO(z.read("ABA_Checklist-8.19.csv").decode("utf-8-sig")))
            if len(r) > 5 and r[5] in "123456" and len(r[5]) == 1
        }
    broad = {"Setophaga petechia", "Vireo gilvus", "Numenius phaeopus", "Larus argentatus"}
    population = {
        "Cairina moschata",
        "Columba livia",
        "Alopochen aegyptiaca",
        "Cygnus olor",
        "Gallus gallus",
        "Myiopsitta monachus",
        "Lonchura punctulata",
    }
    rows = []
    for item, source in zip(taxa, verified):
        name = item["source_name"]
        a = aba.get(name)
        decision = "1" if a else "0"
        reason = "Exact publisher name/concept and fixed ABA 8.19 membership. Experimental presence-only policy; approval/captive/exotic metadata unavailable."
        if name in broad:
            decision = "review"
            reason = "Broad historical concept needs supported split assignment; no geographic inference."
        elif name in population:
            decision = "review"
            reason = "Cannot establish wild/established population or distinguish domestic forms."
        rows.append(
            {
                "source_name": name,
                "source_taxon_id": item["source_taxon_id"],
                "species_id": "aba8.19:" + name.replace(" ", "_"),
                "common_name": a[1] if a else source["vernacularName"],
                "aba_eligible": decision,
                "eod_presence_eligible": decision,
                "eligible_states": "|".join(sorted(STATES)),
                "reference": "https://www.aba.org/aba-checklist/; https://api.gbif.org/v1/occurrence/"
                + item["example_gbif_id"]
                + "/verbatim",
                "review_basis": reason,
                "presence_rows": item["presence_rows"],
                "policy_id": POLICY,
            }
        )
    write_csv(DATA / "taxonomy.csv", rows, list(rows[0]))
    dump(
        DATA / "taxonomy-policy.json",
        {
            "policy": POLICY,
            "aba_sha256": sha256(aba_zip),
            "concepts": len(rows),
            "dispositions": dict(Counter(r["eod_presence_eligible"] for r in rows)),
            "publisher_files": {p.name: sha256(p) for p in sorted(dest.glob("*.json"))},
            "scope": "Same conservative experimental presence-only rules extended explicitly to lower 48/DC, not ABA-certified historical eligibility.",
        },
    )
    print(
        json.dumps(
            {
                "stage": "taxonomy",
                "dispositions": dict(Counter(r["eod_presence_eligible"] for r in rows)),
            }
        ),
        flush=True,
    )


def land():
    import numpy as np
    import shapefile
    from shapely import intersects_xy, prepare
    from shapely.geometry import shape, box
    from shapely.ops import unary_union

    fields = ["locality_id", "latitude", "longitude", "on_land", "source"]
    folder = ROOT / "build/land-mask/land-polygons-split-4326"
    source_hash = sha256(folder / "land_polygons.shp")
    # At most one 5-degree tile of geometry/points in memory. Tiles are resumable
    # and contain exact source strings, not rounded observation coordinates.
    tiles = DATA / "land-staging"
    tiles.mkdir(exist_ok=True)
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'")
        con.execute("SET threads=2")
        source = f"read_csv({q(DATA / 'scope-points.csv')},all_varchar=true)"
        cells = con.execute(f"""SELECT DISTINCT floor(try_cast(longitude AS DOUBLE)/5)*5 x,
            floor(try_cast(latitude AS DOUBLE)/5)*5 y FROM {source} ORDER BY 1,2""").fetchall()
        for x, y in cells:
            target = tiles / f"{int(x)}-{int(y)}.csv"
            if target.exists():
                continue
            rows = con.execute(
                f"""SELECT state,latitude,longitude FROM {source}
               WHERE try_cast(longitude AS DOUBLE)>=? AND try_cast(longitude AS DOUBLE)<?
                 AND try_cast(latitude AS DOUBLE)>=? AND try_cast(latitude AS DOUBLE)<? ORDER BY 1,2,3""",
                [x, x + 5, y, y + 5],
            ).fetchall()
            bounds = (x - 1e-7, y - 1e-7, x + 5 + 1e-7, y + 5 + 1e-7)
            with shapefile.Reader(
                shp=str(folder / "land_polygons.shp"), shx=str(folder / "land_polygons.shx")
            ) as reader:
                clip = box(*bounds)
                polygons = [
                    shape(s.__geo_interface__).intersection(clip)
                    for s in reader.iterShapes(bbox=bounds)
                ]
            mask = unary_union(polygons)
            prepare(mask)
            inside = intersects_xy(
                mask, np.array([float(r[2]) for r in rows]), np.array([float(r[1]) for r in rows])
            )
            write_csv(
                target.with_suffix(".part"),
                (
                    {
                        "locality_id": locality_id(state, lat, lon),
                        "latitude": lat,
                        "longitude": lon,
                        "on_land": int(v),
                        "source": "OSM-land-shp-sha256:" + source_hash,
                    }
                    for (state, lat, lon), v in zip(rows, inside)
                ),
                fields,
            )
            target.with_suffix(".part").replace(target)
            print(
                json.dumps(
                    {
                        "stage": "land",
                        "cell": [x, y],
                        "points": len(rows),
                        "land": int(inside.sum()),
                    }
                ),
                flush=True,
            )
    write_csv(
        DATA / "land.csv.part",
        (r for p in sorted(tiles.glob("*.csv")) for r in csv.DictReader(p.open())),
        fields,
    )
    (DATA / "land.csv.part").replace(DATA / "land.csv")
    dump(
        DATA / "land-provenance.json",
        {
            "source": "https://osmdata.openstreetmap.de/data/land-polygons.html",
            "shp_sha256": source_hash,
            "classification": "Exact polygon covers point. Same OSM coastline/inland-water shoreline proxy as West Coast; footpaths and legal access assumed.",
            "land_csv_sha256": sha256(DATA / "land.csv"),
            "scope_sha256": sha256(BOUNDARY),
        },
    )
    shutil.rmtree(tiles)


def dataset():
    dump(DATA / "config.json", CONFIG.to_dict())
    if (DATA / "dataset").exists() and not (DATA / "dataset/manifest.json").exists():
        folder = DATA / "dataset"
        known = {
            "taxonomy.csv",
            "land.csv",
            "scope.geojson",
            "scope-points.csv",
            "review.csv",
            "manifest.json.tmp",
        }
        if folder.is_symlink() or any(
            p.name not in known or not p.is_file() for p in folder.iterdir()
        ):
            raise ValueError("unexpected contents in incomplete dataset; preserve for inspection")
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()
    # prepare_eod now streams the scope catalog; never collect two million dicts.
    if not (DATA / "dataset/manifest.json").exists():
        prepare_eod(
            ARCHIVE,
            DATA / "taxonomy.csv",
            DATA / "land.csv",
            DATA / "dataset",
            CONFIG,
            "GBIF-0031401-260928105237408",
            POLICY,
            scope_geojson=BOUNDARY,
            scope_label="Lower 48 states and DC",
        )
    print(
        json.dumps(
            {"stage": "dataset", "statistics": read(DATA / "dataset/manifest.json")["statistics"]}
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["audit", "taxonomy", "land", "dataset", "all"])
    args = parser.parse_args()
    for stage in ["audit", "taxonomy", "land", "dataset"] if args.stage == "all" else [args.stage]:
        globals()[stage]()
