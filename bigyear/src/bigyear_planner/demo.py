"""Deterministic fictional sightings, not an eBird download or ecological result."""

import csv
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import random

from .config import Config
from .ingest import prepare
from .problem import build_problem, haversine
from .runner import run
from .travel import import_roads
from .util import read, write_csv

SITES = [
    ("CA", "Monterey", 36.60, -121.89),
    ("CA", "San Diego", 32.72, -117.16),
    ("AZ", "Tucson", 32.22, -110.97),
    ("NM", "Socorro", 34.06, -106.89),
    ("TX", "McAllen", 26.20, -98.23),
    ("TX", "Galveston", 29.30, -94.80),
    ("LA", "Lafayette", 30.22, -92.02),
    ("FL", "Homestead", 25.47, -80.48),
    ("FL", "Titusville", 28.61, -80.81),
    ("NJ", "Cape May", 38.94, -74.91),
    ("MA", "Boston", 42.36, -71.06),
    ("OH", "Toledo", 41.65, -83.54),
    ("ND", "Bismarck", 46.81, -100.78),
    ("CO", "Fort Collins", 40.59, -105.08),
    ("WA", "Seattle", 47.61, -122.33),
    ("OR", "Portland", 45.52, -122.68),
]


def fixtures(root, years=(2024, 2023), small=False, nlocations=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    sites = SITES[:3] if small else SITES
    if nlocations is not None:
        if nlocations < 3 or nlocations > 2000:
            raise ValueError("synthetic locations must be between 3 and 2000")
        sites = [
            (
                SITES[i % 16][0],
                SITES[i % 16][1] + f" fixture {i}",
                round(SITES[i % 16][2] + (i // 16 % 10) * 0.025, 5),
                round(SITES[i % 16][3] + (i // 160) * 0.025, 5),
            )
            for i in range(nlocations)
        ]
    taxa = 8 if small else 400 if nlocations else 80
    mapping = [
        {
            "source_name": f"Demo taxon {s:03}",
            "species_id": f"DEMO{s:03}",
            "common_name": f"Fictional species {s:03}",
            "aba_eligible": "1",
            "eligible_states": "*",
            "reference": "SYNTHETIC mapping; no ABA claim",
        }
        for s in range(taxa)
    ]
    mapping.append(
        {
            "source_name": "Demo taxon subspecies",
            "species_id": "DEMO000",
            "common_name": "Fictional species 000",
            "aba_eligible": "1",
            "eligible_states": "*",
            "reference": "SYNTHETIC subspecies rollup",
        }
    )
    write_csv(root / "taxonomy.csv", mapping, list(mapping[0]))
    land = [
        {
            "locality_id": f"LDEMO{i}",
            "latitude": lat,
            "longitude": lon,
            "on_land": "1",
            "source": "SYNTHETIC exact-point classification",
        }
        for i, (_, _, lat, lon) in enumerate(sites)
    ]
    write_csv(root / "land.csv", land, list(land[0]))
    headers = [
        "GLOBAL UNIQUE IDENTIFIER",
        "CATEGORY",
        "SCIENTIFIC NAME",
        "OBSERVATION COUNT",
        "COUNTRY CODE",
        "STATE CODE",
        "LOCALITY ID",
        "LOCALITY",
        "LATITUDE",
        "LONGITUDE",
        "OBSERVATION DATE",
        "SAMPLING EVENT IDENTIFIER",
        "GROUP IDENTIFIER",
        "APPROVED",
        "EXOTIC CODE",
    ]
    for year in years:
        rng = random.Random(year)
        with open(root / f"ebd-{year}.tsv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
            writer.writeheader()
            count = 0
            for i, (state, name, lat, lon) in enumerate(sites):
                # Paired seasons, each discrete date reported explicitly, plus one-day rare opportunities.
                dates = (
                    [2 + i * 2, 12 + i * 2]
                    if small
                    else sorted(
                        {
                            (i * 19 + d + shift) % 365
                            for shift in [0, 105, 210]
                            for d in range(0, 19, 3)
                        }
                    )
                )
                for day in dates:
                    spp = set(range(3)) | {
                        3 + (i * 4 + j) % (taxa - 3) for j in range(6 if not small else 2)
                    }
                    if not small:
                        spp |= {3 + rng.randrange(taxa - 3) for _ in range(4)}
                    for s in sorted(spp):
                        count += 1
                        row = dict(
                            zip(
                                headers,
                                [
                                    f"DEMO:OBS{count}",
                                    "species",
                                    f"Demo taxon {s:03}",
                                    "X" if s % 3 == 0 else "1",
                                    "US",
                                    "US-" + state,
                                    f"LDEMO{i}",
                                    f"{name} · synthetic site",
                                    lat,
                                    lon,
                                    str(date(year, 1, 1) + timedelta(days=day)),
                                    f"DEMO{year}-{i}-{day}",
                                    "",
                                    "1",
                                    "",
                                ],
                            )
                        )
                        writer.writerow(row)
                        if count % 37 == 0:
                            row["GROUP IDENTIFIER"] = f"GDEMO{count}"
                            row["SAMPLING EVENT IDENTIFIER"] += "shared"
                            writer.writerow(row)
            # Explicit rejection paths, including unknown taxa, offshore and invalid records.
            for reason in ["unknown", "hybrid", "unapproved", "offshore", "subspecies"]:
                row = {h: "" for h in headers}
                row.update(
                    {
                        "GLOBAL UNIQUE IDENTIFIER": "DEMO:" + reason,
                        "CATEGORY": "species",
                        "SCIENTIFIC NAME": "Demo taxon 000",
                        "OBSERVATION COUNT": "X",
                        "COUNTRY CODE": "US",
                        "STATE CODE": "US-CA",
                        "LOCALITY ID": "LDEMO0",
                        "LOCALITY": "Monterey · synthetic site",
                        "LATITUDE": sites[0][2],
                        "LONGITUDE": sites[0][3],
                        "OBSERVATION DATE": f"{year}-01-03",
                        "SAMPLING EVENT IDENTIFIER": "DEMO-" + reason,
                        "APPROVED": "1",
                    }
                )
                if reason == "unknown":
                    row["SCIENTIFIC NAME"] = "Unmapped demo taxon"
                if reason == "hybrid":
                    row["CATEGORY"] = "hybrid"
                if reason == "unapproved":
                    row["APPROVED"] = "0"
                if reason == "offshore":
                    row["LOCALITY ID"] = "LDEMO-OFFSHORE"
                if reason == "subspecies":
                    row["CATEGORY"] = "issf"
                    row["SCIENTIFIC NAME"] = "Demo taxon subspecies"
                writer.writerow(row)
    return root


def synthetic_roads(problem_path, output_csv, output_cache):
    p = read(problem_path)

    def rows():
        for a in p["locations"]:
            for b in p["locations"]:
                miles = haversine(a, b) * 1.22
                yield {
                    "origin": a["id"],
                    "destination": b["id"],
                    "miles": miles,
                    "minutes": miles / 55 * 60,
                    "source": "synthetic-geodesic-fixture",
                    "network_release": "DEMO-NOT-ROADS",
                    "geometry": "",
                }

    write_csv(
        output_csv,
        rows(),
        ["origin", "destination", "miles", "minutes", "source", "network_release", "geometry"],
    )
    import_roads(problem_path, output_csv, output_cache, synthetic=True)


def demo(output, seconds=5, threads=2, years=(2024, 2023), nlocations=None):
    out = Path(output).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("demo output must be empty")
    raw = fixtures(out / "fixtures", years, nlocations=nlocations)
    for year in years:
        cfg = replace(
            Config(),
            year=year,
            seconds=seconds,
            threads=threads,
            target_locations=nlocations or 100,
        )
        dataset = out / f"dataset-{year}"
        prepare(
            raw / f"ebd-{year}.tsv",
            raw / "taxonomy.csv",
            raw / "land.csv",
            dataset,
            cfg,
            release="SYNTHETIC-v1",
            taxonomy_policy="SYNTHETIC-v1",
            synthetic=True,
        )
        problem = out / f"problem-{year}.json"
        build_problem(dataset, problem, cfg)
        synthetic_roads(problem, out / f"roads-{year}.csv", out / f"roads-{year}.sqlite")
        run(
            problem,
            out / f"roads-{year}.sqlite",
            out / f"run-{year}",
            seconds=seconds,
            threads=threads,
        )
    if len(years) > 1:
        from .compare import compare

        compare([out / f"run-{y}" for y in years], out / "comparison.json")
    return out
