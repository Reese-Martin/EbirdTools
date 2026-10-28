"""Lossless, resumable conversion of grouped EOD TSV into verified Parquet parts."""

import hashlib
from pathlib import Path
import shutil
import time

import duckdb

from .util import dump, ident, q, read, sha256


SCHEMA = {
    "year": "INTEGER",
    "month": "SMALLINT",
    "day": "SMALLINT",
    "stateprovince": "VARCHAR",
    "v_scientificname": "VARCHAR",
    "v_taxonconceptid": "VARCHAR",
    "decimallatitude": "VARCHAR",
    "decimallongitude": "VARCHAR",
    "representative_gbifid": "VARCHAR",
    "source_record_count": "UBIGINT",
    "geospatial_flagged_records": "UBIGINT",
    "geospatial_unknown_records": "UBIGINT",
    "max_reported_coordinate_uncertainty_m": "VARCHAR",
    "records_with_reported_coordinate_uncertainty": "UBIGINT",
}
FORMAT = "bigyear-eod-parquet-v1"


def convert(source, output, expected_rows=None, chunk_mib=64, resume=False, progress=print):
    source, output = Path(source).resolve(), Path(output).resolve()
    stat = source.stat()
    with source.open("rb") as stream:
        header = stream.readline()
    fields = header.decode("utf-8-sig").rstrip("\r\n").split("\t")
    if fields != list(SCHEMA):
        raise ValueError("input must have the 14 grouped EOD columns in the documented order")
    if chunk_mib < 1:
        raise ValueError("chunk_mib must be positive")
    if output.exists():
        if not resume or not (output / "progress.json").exists():
            raise ValueError("output exists; use a new directory or --resume a partial conversion")
        manifest = read(output / "progress.json")
        if (
            manifest["source"]["path"],
            manifest["source"]["bytes"],
            manifest["source"]["mtime_ns"],
            manifest["chunk_mib"],
        ) != (str(source), stat.st_size, stat.st_mtime_ns, chunk_mib):
            raise ValueError("resume source or chunk size differs from the original conversion")
        if (output / "manifest.json").exists():
            raise ValueError("archive is already complete and verified")
    else:
        output.mkdir(parents=True)
        manifest = {
            "format": FORMAT,
            "schema": SCHEMA,
            "status": "converting",
            "parts": [],
            "source": {"path": str(source), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
            "compression": "ZSTD",
            "compression_level": 3,
            "row_group_size": 131072,
            "chunk_mib": chunk_mib,
            "expected_rows": expected_rows,
            "duckdb": duckdb.__version__,
            "coordinate_storage": "verbatim VARCHAR",
            "verification": "every value of every row compared after Parquet round-trip",
        }
        dump(output / "progress.json", manifest)
    con = duckdb.connect()
    con.execute("SET memory_limit='1GB'")
    con.execute("SET threads=2")
    con.execute("SET preserve_insertion_order=true")
    con.execute("SET temp_directory=" + q(output / "spill"))
    projections = ",".join(f"CAST({ident(k)} AS {v}) AS {ident(k)}" for k, v in SCHEMA.items())
    difference = " OR ".join(
        f"CAST(p.{ident(k)} AS VARCHAR) IS DISTINCT FROM t.{ident(k)}" for k in fields
    )
    temporary = output / "current-window.tsv"
    source_hash = hashlib.sha256(header)
    rows = byte_count = 0
    started = last_report = time.monotonic()
    try:
        with source.open("rb") as stream:
            stream.seek(len(header))
            index = 0
            while True:
                start_byte = stream.tell()
                block = stream.read(chunk_mib * 2**20)
                if not block:
                    break
                if not block.endswith(b"\n"):
                    block += stream.readline()
                source_hash.update(block)
                block_hash = hashlib.sha256(block).hexdigest()
                expected_chunk_rows = block.count(b"\n") + int(not block.endswith(b"\n"))
                if index < len(manifest["parts"]):
                    part = manifest["parts"][index]
                    if (
                        part["source_sha256"] != block_hash
                        or sha256(output / part["name"]) != part["sha256"]
                    ):
                        raise ValueError("previously verified source/output part changed")
                else:
                    if shutil.disk_usage(output).free < 3 * 2**30:
                        raise ValueError(
                            "less than 3 GiB free; conversion is resumable after freeing space"
                        )
                    temporary.write_bytes(header + block)
                    con.execute("DROP TABLE IF EXISTS source_rows")
                    con.execute("DROP TABLE IF EXISTS decoded")
                    con.execute(
                        "CREATE TEMP TABLE source_rows AS SELECT *,row_number() OVER () AS _row "
                        "FROM read_csv(?,delim='\t',header=true,all_varchar=true,strict_mode=true)",
                        [str(temporary)],
                    )
                    n = con.execute("SELECT count(*) FROM source_rows").fetchone()[0]
                    if n != expected_chunk_rows:
                        raise ValueError("TSV parsing changed the number of complete records")
                    name = f"part-{index:05d}.parquet"
                    pending = output / (name + ".pending")
                    pending.unlink(missing_ok=True)
                    con.execute(
                        f"COPY (SELECT {projections} FROM source_rows ORDER BY _row) TO {q(pending)} "
                        "(FORMAT PARQUET,COMPRESSION ZSTD,COMPRESSION_LEVEL 3,ROW_GROUP_SIZE 131072)"
                    )
                    con.execute(
                        "CREATE TEMP TABLE decoded AS SELECT *,row_number() OVER () AS _row "
                        "FROM read_parquet(?)",
                        [str(pending)],
                    )
                    decoded_count = con.execute("SELECT count(*) FROM decoded").fetchone()[0]
                    mismatches = con.execute(
                        "SELECT count(*) FROM source_rows t JOIN decoded p USING (_row) WHERE "
                        + difference
                    ).fetchone()[0]
                    if decoded_count != n or mismatches:
                        raise ValueError(f"lossless verification failed for part {index}")
                    years = con.execute('SELECT min("year"),max("year") FROM decoded').fetchone()
                    pending.replace(output / name)
                    part = {
                        "name": name,
                        "rows": n,
                        "bytes": (output / name).stat().st_size,
                        "sha256": sha256(output / name),
                        "source_sha256": block_hash,
                        "source_start_byte": start_byte,
                        "source_end_byte": stream.tell(),
                        "year_min": years[0],
                        "year_max": years[1],
                        "mismatches": mismatches,
                    }
                    manifest["parts"].append(part)
                    dump(output / "progress.json", manifest)
                    temporary.unlink()
                rows += part["rows"]
                byte_count += part["bytes"]
                index += 1
                now = time.monotonic()
                if now - last_report >= 20:
                    progress(
                        {
                            "percent": round(100 * stream.tell() / stat.st_size, 1),
                            "verified_rows": rows,
                            "parquet_gib": round(byte_count / 2**30, 3),
                            "elapsed_seconds": round(now - started),
                        }
                    )
                    last_report = now
    finally:
        con.close()
    after = source.stat()
    if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns:
        raise ValueError("source changed during conversion")
    expected_rows = manifest["expected_rows"]
    if expected_rows is not None and rows != expected_rows:
        raise ValueError(f"expected {expected_rows} rows but verified {rows}")
    manifest.update(
        status="verified",
        rows=rows,
        bytes=byte_count,
        source_sha256=source_hash.hexdigest(),
        verified_mismatches=0,
        conversion_seconds=time.monotonic() - started,
    )
    dump(output / "manifest.json", manifest)
    (output / "progress.json").unlink()
    return manifest


def archive_files(path, verify_hashes=True):
    root = Path(path).resolve()
    manifest = read(root / "manifest.json")
    if manifest.get("format") != FORMAT or manifest.get("status") != "verified":
        raise ValueError("a fully verified EOD Parquet archive is required")
    if manifest.get("schema") != SCHEMA or manifest.get("verified_mismatches") != 0:
        raise ValueError("archive schema or lossless verification differs from the EOD contract")
    for name, digest in manifest.get("metadata_files", {}).items():
        if Path(name).name != name or sha256(root / name) != digest:
            raise ValueError("archive source metadata changed")
    files = []
    seen = set()
    for part in manifest["parts"]:
        name = part["name"]
        if part.get("mismatches") != 0:
            raise ValueError("archive contains an unverified part")
        if Path(name).name != name or not name.endswith(".parquet") or name in seen:
            raise ValueError("invalid archive part path")
        seen.add(name)
        file = root / name
        if file.stat().st_size != part["bytes"] or (
            verify_hashes and sha256(file) != part["sha256"]
        ):
            raise ValueError(f"EOD archive part changed: {name}")
        files.append(str(file))
    if not files or sum(p["rows"] for p in manifest["parts"]) != manifest["rows"]:
        raise ValueError("archive row-count manifest is inconsistent")
    return files, manifest


def archive_scan(path, verify_hashes=True):
    files, manifest = archive_files(path, verify_hashes)
    return "read_parquet([" + ",".join(q(f) for f in files) + "])", manifest
