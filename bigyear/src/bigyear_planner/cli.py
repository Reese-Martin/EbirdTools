import argparse
import json
from pathlib import Path
import subprocess
import sys

import duckdb

from .config import Config


def parser():
    p = argparse.ArgumentParser(
        description="Standalone perfect-hindsight historical big-year experiments"
    )
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("build", help="compile the C++ engine")
    s.add_argument("--ortools-prefix")
    s = sub.add_parser("import", help="stream local EBD/SED into audited yearly Parquet")
    for arg in ("ebd", "taxonomy", "land", "output", "release", "taxonomy-policy"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--sed")
    s.add_argument("--config")
    s = sub.add_parser("convert-ebd", help="stream EBD/SED tar, ZIP, gzip or TSV to lossless 2024 lower-48/DC Parquet")
    for arg in ("source", "output", "release"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--kind", choices=["ebd", "sed"], default="ebd")
    s.add_argument("--member", help="explicit TSV/gzip member when tar/ZIP contains multiple candidates")
    s.add_argument("--chunk-mib", type=int, default=32)
    s.add_argument("--resume", action="store_true")
    s = sub.add_parser("inspect-ebd", help="verify an EBD/SED archive and retained original")
    s.add_argument("--archive", required=True)
    s.add_argument("--source")
    s = sub.add_parser("classify-land-ebd", help="classify archived exact EBD points without discarding marine rows")
    for arg in ("archive", "geojson", "output"):
        s.add_argument("--" + arg, required=True)
    s = sub.add_parser("import-ebd", help="bind EBD Parquet to the frozen national 2024 scoring policy")
    for arg in ("archive", "land", "output", "release", "config"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--policy-dir")
    s.add_argument("--sed", help="verified SED Parquet archive from the same release")
    s = sub.add_parser("prepare-complete", help="build every eligible date/location in a memory-mapped model")
    for arg in ("dataset", "output", "config"):
        s.add_argument("--" + arg, required=True)
    s = sub.add_parser("import-eod", help="bind a verified EOD Parquet archive for direct planning")
    for arg in ("archive", "taxonomy", "land", "output", "release", "taxonomy-policy"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--config")
    s.add_argument("--scope-geojson", help="EPSG:4326 polygon restricting observation points")
    s.add_argument("--scope-label")
    s = sub.add_parser("convert-eod", help="losslessly convert grouped EOD TSV to verified ZSTD-3 Parquet")
    for arg in ("source", "output"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--expected-rows", type=int)
    s.add_argument("--resume", action="store_true")
    s = sub.add_parser("inspect-eod", help="verify and query the compressed EOD archive directly")
    s.add_argument("--archive", required=True)
    s.add_argument("--source", help="also verify the retained original against its full SHA-256")
    s = sub.add_parser("classify-land-eod", help="classify exact EOD Parquet points using a supplied land mask")
    for arg in ("archive", "geojson", "output"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--year", required=True, type=int)
    s.add_argument("--scope-geojson")
    s = sub.add_parser(
        "classify-land", help="classify exact EBD points with a terrestrial GeoJSON polygon mask"
    )
    for arg in ("ebd", "geojson", "output"):
        s.add_argument("--" + arg, required=True)
    s = sub.add_parser("prepare", help="select locations and export a frozen search problem")
    for arg in ("dataset", "output"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--config")
    s.add_argument("--keep", action="append", default=[])
    s.add_argument("--expand-route")
    s.add_argument("--expand-miles", type=float, default=50)
    s.add_argument("--airports")
    s.add_argument("--flights")
    s.add_argument("--from-date")
    s.add_argument("--to-date")
    s.add_argument("--state", action="append", default=[])
    s.add_argument(
        "--access-catalog", help="exact-point reported coordinates and road-access anchors"
    )
    s = sub.add_parser(
        "roads-import", help="import directed road distances/times, never geodesic substitutes"
    )
    for arg in ("problem", "csv", "cache"):
        s.add_argument("--" + arg, required=True)
    s = sub.add_parser(
        "roads-osrm", help="populate a resumable cache using an authorized OSRM endpoint"
    )
    for arg in ("problem", "endpoint", "cache", "network-release"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--batch", type=int, default=40)
    s.add_argument("--geometries", action="store_true")
    s = sub.add_parser('roads-matrix', help='build a compact shared float64 OSRM road matrix')
    for arg in ('problem', 'endpoint', 'cache', 'network-release'):
        s.add_argument('--' + arg, required=True)
    s.add_argument('--batch', type=int, default=3000)
    s = sub.add_parser("viewer-roads", help="attach checked driving shapes for viewing without changing frozen runs")
    for arg in ("runs", "endpoint", "network-release"):
        s.add_argument("--" + arg, required=True)
    s = sub.add_parser("viewer-coverage", help="build all-observer year-to-date counts for the full source cohort")
    for arg in ("dataset", "runs"):
        s.add_argument("--" + arg, required=True)
    s = sub.add_parser('roads-bounded', help='configure a fixed-size on-demand measured-road cache')
    for arg in ('problem','output','endpoint','network-release','network-sha256'):
        s.add_argument('--'+arg,required=True)
    s.add_argument('--capacity-mib',type=int,default=128)
    s.add_argument('--hot-entries',type=int,default=65536)
    s = sub.add_parser("search", help="run a separate checkpointed C++ optimization job")
    for arg in ("problem", "roads", "output"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--seconds", type=float)
    s.add_argument("--threads", type=int)
    s.add_argument("--seed", type=int)
    s.add_argument("--exact", choices=["exhaustive", "ortools"])
    s.add_argument("--engine")
    s.add_argument("--resume")
    s = sub.add_parser(
        "validate", help="independently audit every calendar day and credited species"
    )
    s.add_argument("run")
    s.add_argument("--route", default="route.json")
    s = sub.add_parser(
        "review-detours", help="independently test single-stop removal counterfactuals"
    )
    s.add_argument("run")
    s.add_argument("--output")
    s = sub.add_parser("serve", help="display saved routes only; never launches optimization")
    s.add_argument("--runs", required=True)
    s.add_argument("--port", type=int, default=8765)
    s = sub.add_parser(
        "demo", help="build clearly labeled fictional 2024/2023 full-calendar routes"
    )
    s.add_argument("--output", default="runs/demo")
    s.add_argument("--seconds", type=float, default=5)
    s.add_argument("--threads", type=int, default=2)
    s.add_argument("--year", type=int, action="append")
    s.add_argument(
        "--locations", type=int, help="generate a larger synthetic benchmark (3–2000 locations)"
    )
    s = sub.add_parser("compare", help="compare validated runs with identical scientific rules")
    s.add_argument("runs", nargs="+")
    s.add_argument("--output", required=True)
    s = sub.add_parser("benchmark", help="run staged search budgets on an identical frozen problem")
    for arg in ("problem", "roads", "output"):
        s.add_argument("--" + arg, required=True)
    s.add_argument("--budgets", type=float, nargs="+", default=[600, 3600, 21600])
    s.add_argument("--threads", type=int, default=4)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "build":
            from .runner import compile_engine

            print(compile_engine(args.ortools_prefix))
        elif args.command == "import":
            from .ingest import prepare

            result = prepare(
                args.ebd,
                args.taxonomy,
                args.land,
                args.output,
                Config.read(args.config),
                args.release,
                args.taxonomy_policy,
                args.sed,
            )
            print(json.dumps(result["statistics"], indent=2))
        elif args.command == "classify-land":
            from .ingest import classify_land

            classify_land(args.ebd, args.geojson, args.output)
        elif args.command == "convert-ebd":
            from .ebd_archive import convert

            result = convert(args.source, args.output, args.release, kind=args.kind,
                             member=args.member, chunk_mib=args.chunk_mib, resume=args.resume,
                             progress=lambda value: print(json.dumps(value), flush=True))
            print(json.dumps({k: result[k] for k in ("status", "rows", "input_rows", "bytes")}, indent=2))
        elif args.command == "inspect-ebd":
            from .ebd_archive import archive_scan
            from .util import ManagedDuckDB, sha256

            source, result = archive_scan(args.archive)
            with ManagedDuckDB() as con:
                count = con.execute(f"SELECT count(*) FROM {source}").fetchone()[0]
            if count != result["rows"]:
                raise ValueError("archive row-count verification failed")
            if args.source and sha256(args.source) != result["source_sha256"]:
                raise ValueError("retained original changed")
            print(json.dumps({"rows": count, "parts": len(result["parts"]), "bytes": result["bytes"],
                              "original_verified": bool(args.source)}, indent=2))
        elif args.command == "classify-land-ebd":
            from .ebd import classify_land

            classify_land(args.archive, args.geojson, args.output)
        elif args.command == "import-ebd":
            from .ebd import DEFAULT_POLICY, prepare

            result = prepare(args.archive, args.land, args.output, args.release,
                             Config.read(args.config), args.policy_dir or DEFAULT_POLICY, args.sed)
            print(json.dumps(result["statistics"], indent=2))
        elif args.command == "prepare-complete":
            from .compact_model import build

            model = build(args.dataset, args.output, Config.read(args.config))
            try:
                print(json.dumps({k: model.meta[k] for k in ("locations", "opportunities", "represented_observations")}, indent=2))
            finally:
                model.close()
        elif args.command == "convert-eod":
            from .eod_archive import convert

            result = convert(args.source, args.output, args.expected_rows, resume=args.resume,
                             progress=lambda value: print(json.dumps(value), flush=True))
            print(json.dumps({k: result[k] for k in ("status", "rows", "bytes", "source_sha256")}, indent=2))
        elif args.command == "import-eod":
            from .eod import prepare_eod

            result = prepare_eod(args.archive, args.taxonomy, args.land, args.output,
                                 Config.read(args.config), args.release, args.taxonomy_policy,
                                 scope_geojson=args.scope_geojson, scope_label=args.scope_label)
            print(json.dumps(result["statistics"], indent=2))
        elif args.command == "inspect-eod":
            from .eod_archive import archive_scan
            from .util import sha256

            source, manifest = archive_scan(args.archive)
            con = duckdb.connect()
            count, first, last = con.execute(
                f'SELECT count(*),min("year"),max("year") FROM {source}'
            ).fetchone()
            con.close()
            if count != manifest["rows"]:
                raise ValueError("archive metadata and actual Parquet row counts differ")
            if args.source:
                if (Path(args.source).stat().st_size != manifest["source"]["bytes"]
                        or sha256(args.source) != manifest["source_sha256"]):
                    raise ValueError("retained original differs from the verified conversion source")
            print(json.dumps({"rows": count, "first_year": first, "last_year": last,
                              "parquet_bytes": manifest["bytes"], "parts": len(manifest["parts"]),
                              "coordinate_storage": manifest["coordinate_storage"],
                              "original_source_verified": bool(args.source)}, indent=2))
        elif args.command == "classify-land-eod":
            from .eod import classify_land_eod

            classify_land_eod(args.archive, args.year, args.geojson, args.output, args.scope_geojson)
        elif args.command == "prepare":
            from .problem import build_problem

            result = build_problem(
                args.dataset,
                args.output,
                Config.read(args.config),
                args.keep,
                args.expand_route,
                args.expand_miles,
                args.airports,
                args.flights,
                args.from_date,
                args.to_date,
                args.state,
                args.access_catalog,
            )
            print(json.dumps(result["reduction"], indent=2))
        elif args.command == "roads-import":
            from .travel import import_roads

            print("Imported directed roads:", import_roads(args.problem, args.csv, args.cache))
        elif args.command == "roads-osrm":
            from .travel import osrm_roads

            osrm_roads(
                args.problem,
                args.endpoint,
                args.cache,
                args.network_release,
                args.batch,
                args.geometries,
            )
        elif args.command == 'roads-matrix':
            from .dense_roads import osrm_matrix

            result = osrm_matrix(args.problem, args.endpoint, args.cache,
                                 args.network_release, args.batch)
            print(json.dumps({k: result[k] for k in ('bytes', 'reachable_location_pairs', 'wall_seconds')}))
        elif args.command == "viewer-roads":
            from .display import build_display_roads

            print(json.dumps(build_display_roads(args.runs, args.endpoint, args.network_release), indent=2))
        elif args.command == "viewer-coverage":
            from .coverage import build_observer_coverage

            result = build_observer_coverage(args.dataset, args.runs)
            print(json.dumps({k: v for k, v in result.items() if k not in {"days", "first_seen"}}, indent=2))
        elif args.command == 'roads-bounded':
            from .bounded_roads import create_bounded_roads

            print(json.dumps(create_bounded_roads(args.problem,args.output,args.endpoint,
                args.network_release,args.network_sha256,args.capacity_mib,args.hot_entries),indent=2))
        elif args.command == "search":
            from .runner import run

            result = run(
                args.problem,
                args.roads,
                args.output,
                args.seconds,
                args.threads,
                args.seed,
                {None: 0, "exhaustive": 1, "ortools": 2}[args.exact],
                args.resume,
                args.engine,
            )
            print(
                f"{result['status']}: {result['species_total']} species; validation passed; {Path(args.output).resolve()}"
            )
        elif args.command == "review-detours":
            from .review import review_route

            report = review_route(args.run, args.output)
            print(json.dumps({k: v for k, v in report.items() if k != "stops"}, indent=2))
        elif args.command == "validate":
            from .validate import validate

            root = Path(args.run)
            route = json.loads((root / args.route).read_text())
            roads_file = route.get('provenance', {}).get('roads_file', 'roads.sqlite')
            result = validate(root / args.route, root / "problem.json", root / roads_file)
            print(json.dumps(result, indent=2))
            return 0 if result["valid"] else 1
        elif args.command == "serve":
            from .server import serve

            serve(args.runs, args.port)
        elif args.command == "demo":
            from .demo import demo

            print(
                demo(
                    args.output,
                    args.seconds,
                    args.threads,
                    tuple(args.year or [2024, 2023]),
                    args.locations,
                )
            )
        elif args.command == "compare":
            from .compare import compare

            compare(args.runs, args.output)
            print(Path(args.output).resolve())
        elif args.command == "benchmark":
            from .runner import run

            for budget in args.budgets:
                run(
                    args.problem,
                    args.roads,
                    Path(args.output) / f"{budget:g}s",
                    seconds=budget,
                    threads=args.threads,
                )
            print(Path(args.output).resolve())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, duckdb.Error) as e:
        print(f"bigyear: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
