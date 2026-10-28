#!/usr/bin/env python3
"""Benchmark lossless Parquet on bounded, distributed windows of an EOD TSV.

Requires DuckDB. Keeps all columns and sampled rows. Coordinates, identifiers,
and uncertainty remain VARCHAR; integer columns are verified against source
strings after round-trip. Never modifies the input or converts the full cohort.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time

import duckdb


INTEGER_TYPES = {
    "year": "INTEGER", "month": "SMALLINT", "day": "SMALLINT",
    "source_record_count": "UBIGINT", "geospatial_flagged_records": "UBIGINT",
    "geospatial_unknown_records": "UBIGINT",
    "records_with_reported_coordinate_uncertainty": "UBIGINT",
}
VARIANTS = (("snappy", "snappy", None), ("zstd3", "zstd", 3), ("zstd9", "zstd", 9))


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def benchmark(source, output, windows=8, window_mib=64, total_rows=None):
    stat = source.stat()
    with source.open("rb") as stream:
        header = stream.readline()
    fields = header.decode("utf-8-sig").rstrip("\r\n").split("\t")
    required = {"decimallatitude", "decimallongitude", "representative_gbifid"}
    if not required.issubset(fields) or len(fields) != len(set(fields)):
        raise ValueError("expected unique grouped-EOD headers including coordinates and source ID")
    if windows < 2 or window_mib < 1:
        raise ValueError("at least two positive-sized windows are required")
    window_bytes = window_mib * 1024 * 1024
    data_bytes = stat.st_size - len(header)
    if windows * window_bytes > data_bytes:
        raise ValueError("sampling windows would overlap; reduce the window count or size")
    if shutil.disk_usage(output.parent).free < windows * window_bytes * 5 + 2**30:
        raise ValueError("not enough free space for bounded benchmark outputs and temporary data")
    output.mkdir()
    sample_path = output / "current-window.tsv"
    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='1GB'")
    connection.execute("SET preserve_insertion_order=true")
    connection.execute("SET temp_directory=" + quote(output / "spill"))
    projections = [
        "CAST(" + identifier(field) + " AS " + INTEGER_TYPES.get(field, "VARCHAR")
        + ") AS " + identifier(field) for field in fields
    ]
    differences = " OR ".join(
        "CAST(p." + identifier(field) + " AS VARCHAR) IS DISTINCT FROM t." + identifier(field)
        for field in fields
    )
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(), "duckdb_version": duckdb.__version__,
        "source": str(source), "source_bytes": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
        "total_source_rows": total_rows, "columns": fields,
        "schema": {field: INTEGER_TYPES.get(field, "VARCHAR") for field in fields},
        "sample_design": "equal-byte contiguous windows evenly spaced from start through end",
        "requested_windows": windows, "requested_window_mib": window_mib,
        "row_group_size": 131072, "threads": 2, "memory_limit": "1GB", "windows": [],
        "estimate_limitations": [
            "A distributed-window benchmark is an estimate, not a full-file conversion.",
            "Window-to-window ranges describe observed heterogeneity, not confidence intervals.",
            "Estimates assume the same schema, codecs, row-group size, and existing row order.",
        ],
    }
    started = time.perf_counter()
    try:
        with source.open("rb") as stream:
            for index in range(windows):
                target = len(header) + round((data_bytes - window_bytes) * index / (windows - 1))
                stream.seek(target)
                if index:
                    stream.readline()  # Discard only the record crossing the sampling boundary.
                offset = stream.tell()
                block = stream.read(window_bytes)
                end = block.rfind(b"\n")
                if end < 0:
                    raise ValueError("no complete records in sampling window")
                block = block[:end + 1]
                sample_path.write_bytes(header + block)
                sample_bytes = len(block)
                expected_rows = block.count(b"\n")
                del block
                parse_started = time.perf_counter()
                connection.execute("DROP TABLE IF EXISTS sample_text")
                connection.execute("DROP TABLE IF EXISTS sample_typed")
                connection.execute(
                    "CREATE TEMP TABLE sample_text AS SELECT *, row_number() OVER () AS _sample_row "
                    "FROM read_csv(?, delim='\t', header=true, all_varchar=true, strict_mode=true)",
                    [str(sample_path)],
                )
                rows = connection.execute("SELECT count(*) FROM sample_text").fetchone()[0]
                if rows != expected_rows:
                    raise ValueError("parsed row count differs from complete TSV record count")
                connection.execute(
                    "CREATE TEMP TABLE sample_typed AS SELECT " + ", ".join(projections)
                    + " FROM sample_text ORDER BY _sample_row"
                )
                window = {
                    "index": index, "source_start_byte": offset, "tsv_data_bytes": sample_bytes,
                    "rows": rows, "parse_seconds": time.perf_counter() - parse_started,
                    "year_range": list(connection.execute(
                        'SELECT min("year"),max("year") FROM sample_typed'
                    ).fetchone()), "variants": [],
                }
                for label, codec, level in VARIANTS:
                    destination = output / f"window-{index:02d}-{label}.parquet"
                    options = f"FORMAT PARQUET, COMPRESSION {codec}, ROW_GROUP_SIZE 131072"
                    if level is not None:
                        options += f", COMPRESSION_LEVEL {level}"
                    write_started = time.perf_counter()
                    connection.execute(
                        "COPY (SELECT * FROM sample_typed) TO " + quote(destination)
                        + " (" + options + ")"
                    )
                    write_seconds = time.perf_counter() - write_started
                    connection.execute(
                        "CREATE TEMP TABLE decoded AS SELECT *, row_number() OVER () AS _sample_row "
                        "FROM read_parquet(?)", [str(destination)],
                    )
                    decoded_rows = connection.execute("SELECT count(*) FROM decoded").fetchone()[0]
                    mismatches = connection.execute(
                        "SELECT count(*) FROM sample_text t JOIN decoded p USING (_sample_row) "
                        "WHERE " + differences
                    ).fetchone()[0]
                    if decoded_rows != rows or mismatches:
                        raise ValueError(f"round-trip validation failed: {destination}")
                    connection.execute("DROP TABLE decoded")
                    result = {
                        "codec": label, "parquet_bytes": destination.stat().st_size,
                        "write_seconds": write_seconds, "round_trip_rows": decoded_rows,
                        "round_trip_mismatches": mismatches, "path": str(destination),
                    }
                    window["variants"].append(result)
                report["windows"].append(window)
                sample_path.unlink()
                print(json.dumps({
                    "window": index + 1, "of": windows, "rows": rows,
                    "years": window["year_range"],
                    "parquet_mib": {r["codec"]: round(r["parquet_bytes"] / 2**20, 2)
                                    for r in window["variants"]},
                    "round_trip_mismatches": 0,
                }), flush=True)
    finally:
        connection.close()
    if source.stat().st_size != stat.st_size or source.stat().st_mtime_ns != stat.st_mtime_ns:
        raise ValueError("source changed during the benchmark")
    report["elapsed_seconds"] = time.perf_counter() - started
    report["sample_rows"] = sum(w["rows"] for w in report["windows"])
    report["sample_tsv_data_bytes"] = sum(w["tsv_data_bytes"] for w in report["windows"])
    report["summary"] = []
    for label, _, _ in VARIANTS:
        results = [next(r for r in w["variants"] if r["codec"] == label)
                   for w in report["windows"]]
        parquet_bytes = sum(r["parquet_bytes"] for r in results)
        ratio = parquet_bytes / report["sample_tsv_data_bytes"]
        full_estimate = ratio * data_bytes
        window_estimates = [r["parquet_bytes"] / w["tsv_data_bytes"] * data_bytes
                            for w, r in zip(report["windows"], results)]
        report["summary"].append({
            "codec": label, "sample_parquet_bytes": parquet_bytes,
            "parquet_fraction_of_tsv": ratio, "estimated_full_bytes": full_estimate,
            "estimated_full_gib": full_estimate / 2**30,
            "estimated_full_bytes_by_row": (
                parquet_bytes / report["sample_rows"] * total_rows if total_rows else None
            ),
            "window_estimate_range_gib": [min(window_estimates) / 2**30,
                                           max(window_estimates) / 2**30],
            "total_write_seconds": sum(r["write_seconds"] for r in results),
        })
    (output / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"summary": report["summary"], "sample_rows": report["sample_rows"],
                      "elapsed_seconds": report["elapsed_seconds"],
                      "report": str(output / "benchmark.json")}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new output directory")
    parser.add_argument("--windows", default=8, type=int)
    parser.add_argument("--window-mib", default=64, type=int)
    parser.add_argument("--total-rows", type=int, help="previously verified full-file row count")
    args = parser.parse_args()
    benchmark(args.source.resolve(), args.output.resolve(), args.windows,
              args.window_mib, args.total_rows)


if __name__ == "__main__":
    main()
