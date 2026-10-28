"""Bounded, lossless EBD/SED TSV -> ZSTD-3 Parquet, without unpacking the download.

Only year/country/state select the archival cohort. Quality, taxonomy, protocol,
effort, duplicate checklists, and access are deliberately not archival filters.
"""

from contextlib import contextmanager
import gzip
import hashlib
from pathlib import Path
import shutil
import tarfile
import time
import zipfile

import duckdb

from .config import STATES
from .util import ManagedDuckDB, dump, ident, q, read, sha256

FORMAT = "bigyear-ebd-parquet-v1"
COMMON = {"OBSERVATION DATE", "COUNTRY CODE", "STATE CODE", "LATITUDE", "LONGITUDE",
          "LOCALITY ID", "SAMPLING EVENT IDENTIFIER"}
REQUIRED = COMMON | {"SCIENTIFIC NAME", "CATEGORY", "OBSERVATION COUNT", "APPROVED",
                     "GLOBAL UNIQUE IDENTIFIER", "TAXON CONCEPT ID"}


def data_member(name):
    return name.lower().endswith(('.txt', '.tsv', '.txt.gz', '.tsv.gz'))


@contextmanager
def member_stream(raw, name):
    """Decode a nested gzip member in memory, never into an extracted file."""
    if name.lower().endswith('.gz'):
        with gzip.GzipFile(fileobj=raw, mode='rb') as stream:
            yield stream
    else:
        yield raw


def matches_kind(raw, name, kind):
    with member_stream(raw, name) as stream:
        try:
            fields = set(stream.readline(2**20).decode('utf-8-sig').rstrip('\r\n').split('\t'))
        except UnicodeDecodeError:
            return False  # A text README may use a different encoding.
    required = REQUIRED if kind == 'ebd' else COMMON
    return required <= fields and (kind == 'ebd' or 'SCIENTIFIC NAME' not in fields)


@contextmanager
def open_source(path, kind="ebd", member=None):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if member is None:
                candidates = []
                for name in archive.namelist():
                    if not data_member(name):
                        continue
                    with archive.open(name) as candidate:
                        if matches_kind(candidate, name, kind):
                            candidates.append(name)
                if len(candidates) != 1:
                    raise ValueError(f"select --member explicitly; candidate {kind} members: {candidates}")
                member = candidates[0]
            if archive.namelist().count(member) != 1:
                raise ValueError('ZIP member is missing or duplicated: ' + member)
            with archive.open(member) as raw, member_stream(raw, member) as stream:
                yield stream, member
    elif tarfile.is_tarfile(path):
        # Local uncompressed tar can skip member bodies by seeking. For an
        # outer-compressed tar, discovery/reopening can require another scan;
        # both modes bound RAM and never call extract/extractall.
        with tarfile.open(path, mode='r:*') as archive:
            candidates = []
            for index, info in enumerate(archive):
                if index >= 10000:
                    raise ValueError('too many tar members; inspect this archive layout before importing')
                if member is not None:
                    if info.name == member:
                        if not info.isfile():
                            raise ValueError('tar data member must be a regular file, not a link/device')
                        candidates.append(info)
                elif info.isfile() and data_member(info.name):
                    with archive.extractfile(info) as candidate:
                        if matches_kind(candidate, info.name, kind):
                            candidates.append(info)
            if len(candidates) != 1:
                raise ValueError(f'select one unique --member explicitly; candidate {kind} tar members: '
                                 + str([info.name for info in candidates]))
            selected = candidates[0]
            with archive.extractfile(selected) as raw, member_stream(raw, selected.name) as stream:
                yield stream, selected.name
    else:
        if member:
            raise ValueError("--member applies only to ZIP/tar input")
        opener = gzip.open if path.suffix.lower() == ".gz" else open
        with opener(path, "rb") as stream:
            yield stream, None


def convert(source, output, release, *, kind="ebd", member=None, year=2024,
            chunk_mib=32, resume=False, progress=print, min_free_bytes=3 * 2**30):
    """Resume by replaying/hashing bounded source windows, including excluded rows."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if kind not in {"ebd", "sed"} or year != 2024 or chunk_mib < 1 or not release.strip():
        raise ValueError("require kind ebd/sed, year 2024, positive chunk size and release label")
    stat = source.stat()
    started = last_report = time.monotonic()
    with open_source(source, kind, member) as (stream, selected):
        header = stream.readline()
        original = header.decode("utf-8-sig").rstrip("\r\n").split("\t")
        names = [n if n else "_ebd_trailing_blank" for n in original]
        if (not header.endswith(b"\n") or len({n.lower() for n in names}) != len(names)
                or any(not n for n in original[:-1]) or "_source_row" in {n.lower() for n in names}):
            raise ValueError("invalid/duplicate TSV headers or reserved _source_row column")
        missing = (REQUIRED if kind == "ebd" else COMMON) - set(names)
        if missing:
            raise ValueError(f"missing {kind.upper()} headers: {sorted(missing)}")
        binding = {"path": str(source), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                   "member": selected, "release": release, "kind": kind, "year": year,
                   "chunk_mib": chunk_mib, "original_headers": original}
        if output.exists():
            if not resume or not (output / "progress.json").exists():
                raise ValueError("output exists; choose a new output or --resume a partial archive")
            manifest = read(output / "progress.json")
            if manifest["source"] != binding or (output / "manifest.json").exists():
                raise ValueError("resume inputs differ or archive is already complete")
        else:
            output.mkdir(parents=True)
            manifest = {"format": FORMAT, "status": "converting", "source": binding,
                        "columns": names, "parts": [], "windows": [], "year": year,
                        "states": sorted(STATES), "kind": kind,
                        "compression": "ZSTD", "compression_level": 3,
                        "coordinate_storage": "verbatim VARCHAR; numeric projection only in model",
                        "filters": "2024, COUNTRY CODE=US, STATE CODE in lower 48 + DC only",
                        "verification": "every retained field and row compared after Parquet round-trip"}
            dump(output / "progress.json", manifest)
        temporary = output / "current-window.tsv"
        pending = output / "current-part.pending"
        source_hash = hashlib.sha256(header)
        total = retained = index = 0
        states = ",".join(q(s) for s in sorted(STATES))
        us_jurisdictions = ','.join(q(s) for s in sorted(STATES | {
            'US-AK', 'US-HI', 'US-AS', 'US-GU', 'US-MP', 'US-PR', 'US-VI', 'US-UM'}))
        columns = "{" + ",".join(q(n) + ":'VARCHAR'" for n in names) + "}"
        forced = "[" + ",".join(q(n) for n in names) + "]"
        with ManagedDuckDB() as con:
            con.execute("SET memory_limit='1GB'")
            con.execute("SET threads=1")
            con.execute("SET preserve_insertion_order=true")
            try:
                while True:
                    block = stream.read(chunk_mib * 2**20)
                    if not block:
                        break
                    if not block.endswith(b"\n"):
                        # Reject pathological records rather than unbounded readline memory.
                        tail = stream.readline(16 * 2**20)
                        if len(tail) == 16 * 2**20 and not tail.endswith(b"\n"):
                            raise ValueError("TSV record exceeds 16 MiB; source retained for review")
                        block += tail
                    source_hash.update(block)
                    count = block.count(b"\n") + int(not block.endswith(b"\n"))
                    digest = hashlib.sha256(block).hexdigest()
                    if index < len(manifest["windows"]):
                        window = manifest["windows"][index]
                        if window["source_sha256"] != digest or window["input_rows"] != count:
                            raise ValueError("previously verified source window changed")
                        if window["part"] is not None:
                            part = manifest["parts"][window["part"]]
                            if sha256(output / part["name"]) != part["sha256"]:
                                raise ValueError("previously verified Parquet part changed")
                    else:
                        if shutil.disk_usage(output).free < min_free_bytes:
                            raise ValueError("less than 3 GiB free; free space and resume this conversion")
                        temporary.write_bytes(header + block)
                        con.execute("DROP TABLE IF EXISTS source_rows")
                        con.execute("DROP TABLE IF EXISTS scoped_rows")
                        con.execute(f"""CREATE TEMP TABLE source_rows AS SELECT *,
                            row_number() OVER ()+{total} AS _source_row FROM read_csv({q(temporary)},
                            delim='\t',header=true,auto_detect=false,columns={columns},quote='',escape='',
                            force_not_null={forced},strict_mode=true,parallel=false,
                            max_line_size=16777216)""")
                        if con.execute("SELECT count(*) FROM source_rows").fetchone()[0] != count:
                            raise ValueError("TSV parser changed source row count (blank/malformed row?)")
                        if con.execute(f'''SELECT count(*) FROM source_rows WHERE
                            NOT regexp_full_match("OBSERVATION DATE",'[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}')
                            OR try_cast("OBSERVATION DATE" AS DATE) IS NULL
                            OR (left("OBSERVATION DATE",4)='2024' AND
                            (("COUNTRY CODE"='US' AND "STATE CODE" NOT IN ({us_jurisdictions}))
                            OR ("COUNTRY CODE"!='US' AND "STATE CODE" IN ({states}))))''').fetchone()[0]:
                            raise ValueError("invalid date or unresolved US jurisdiction; no rows silently dropped")
                        con.execute(f'''CREATE TEMP TABLE scoped_rows AS SELECT * FROM source_rows
                            WHERE year(CAST("OBSERVATION DATE" AS DATE))={year}
                            AND "COUNTRY CODE"='US' AND "STATE CODE" IN ({states})''')
                        n = con.execute("SELECT count(*) FROM scoped_rows").fetchone()[0]
                        part_index = None
                        if n:
                            pending.unlink(missing_ok=True)
                            con.execute(f"COPY scoped_rows TO {q(pending)} (FORMAT PARQUET,COMPRESSION ZSTD,COMPRESSION_LEVEL 3)")
                            diff = " OR ".join(f"s.{ident(n)} IS DISTINCT FROM p.{ident(n)}" for n in names)
                            mismatches = con.execute(f"""SELECT count(*) FROM scoped_rows s FULL JOIN
                                read_parquet({q(pending)}) p USING (_source_row)
                                WHERE s._source_row IS NULL OR p._source_row IS NULL OR {diff}""").fetchone()[0]
                            if mismatches:
                                raise ValueError("lossless Parquet round-trip verification failed")
                            part_index = len(manifest["parts"])
                            name = f"part-{part_index:05d}.parquet"
                            pending.replace(output / name)
                            manifest["parts"].append({"name": name, "rows": n,
                                "bytes": (output / name).stat().st_size, "sha256": sha256(output / name)})
                        window = {"input_rows": count, "retained_rows": n, "source_sha256": digest,
                                  "part": part_index}
                        manifest["windows"].append(window)
                        dump(output / "progress.json", manifest)
                    total += count
                    retained += window["retained_rows"]
                    index += 1
                    if time.monotonic() - last_report >= 20:
                        progress({"input_rows": total, "retained_rows": retained,
                                  "seconds": round(time.monotonic() - started)})
                        last_report = time.monotonic()
            finally:
                temporary.unlink(missing_ok=True)
                pending.unlink(missing_ok=True)
        if index != len(manifest["windows"]):
            raise ValueError("source ended before the checkpoint boundary")
    container_hash = sha256(source)
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        raise ValueError("source changed during conversion")
    manifest.update(status="verified", rows=retained, input_rows=total, outside_scope_rows=total-retained,
                    bytes=sum(p["bytes"] for p in manifest["parts"]), verified_mismatches=0,
                    source_stream_sha256=source_hash.hexdigest(), source_sha256=container_hash,
                    seconds=time.monotonic() - started, duckdb=duckdb.__version__)
    dump(output / "manifest.json", manifest)
    (output / "progress.json").unlink()
    return manifest


def archive_scan(path, verify_hashes=True):
    root = Path(path).resolve()
    manifest = read(root / "manifest.json")
    if (manifest.get("format") != FORMAT or manifest.get("status") != "verified"
            or manifest.get("verified_mismatches") != 0 or manifest.get("year") != 2024
            or manifest.get("states") != sorted(STATES)):
        raise ValueError("a verified 2024 lower-48/DC EBD/SED archive is required")
    files, seen = [], set()
    for part in manifest["parts"]:
        name = part["name"]
        if Path(name).name != name or not name.endswith(".parquet") or name in seen:
            raise ValueError("invalid or duplicate archive part path")
        seen.add(name)
        file = root / name
        if file.stat().st_size != part["bytes"] or (verify_hashes and sha256(file) != part["sha256"]):
            raise ValueError("EBD archive part changed: " + name)
        files.append(q(file))
    if sum(p["rows"] for p in manifest["parts"]) != manifest["rows"]:
        raise ValueError("archive row-count manifest is inconsistent")
    if sum(w['input_rows'] for w in manifest['windows']) != manifest['input_rows'] or sum(
            w['retained_rows'] for w in manifest['windows']) != manifest['rows']:
        raise ValueError("archive window accounting is inconsistent")
    if not files:
        return '(SELECT ' + ','.join("''::VARCHAR AS " + ident(n) for n in manifest['columns']) + ',0::BIGINT AS _source_row WHERE false)', manifest
    return "read_parquet([" + ",".join(files) + "])", manifest
