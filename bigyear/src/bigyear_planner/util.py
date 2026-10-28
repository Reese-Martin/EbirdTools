import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import sys
import subprocess
import tempfile
import shutil

ROOT = Path(__file__).resolve().parents[2]


def clean_query_spill():
    """Reclaim only this planner's marked spill directories whose owner has exited."""
    parent = ROOT / 'build/query-spill'
    parent.mkdir(parents=True, exist_ok=True)
    removed = []
    for folder in parent.glob('query-*'):
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            marker = json.loads((folder / 'owner.json').read_text())
            if marker.get('purpose') != 'bigyear-query-spill-v1' or marker.get('root') != str(ROOT):
                continue
            pid = marker['pid']
            if type(pid) is not int or pid < 1:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                size = sum(p.stat().st_blocks*512 for p in folder.rglob('*') if p.is_file())
                shutil.rmtree(folder)
                removed.append({'directory': str(folder), 'allocated_bytes': size})
        except (OSError, ValueError, KeyError):
            continue
    return removed


class ManagedDuckDB:
    """Bound spill space and remove it on normal close, failure, or collection."""
    def __init__(self):
        import duckdb

        self.con = None
        clean_query_spill()
        self.temp = tempfile.TemporaryDirectory(prefix='query-', dir=ROOT / 'build/query-spill')
        Path(self.temp.name, 'owner.json').write_text(json.dumps({
            'purpose': 'bigyear-query-spill-v1', 'root': str(ROOT), 'pid': os.getpid()}))
        self.con = duckdb.connect()
        self.con.execute('SET temp_directory=' + q(self.temp.name))
        self.con.execute("SET max_temp_directory_size='4GB'")

    def __getattr__(self, name):
        return getattr(self.con, name)

    def close(self):
        if self.con is not None:
            self.con.close()
            self.con = None
            self.temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        if getattr(self, 'con', None) is not None:
            self.close()


def dump(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2, allow_nan=False) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read(path):
    return json.loads(Path(path).read_text())


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def header(path, delimiter="\t"):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f, delimiter=delimiter))


def csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def write_csv(path, rows, names):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


def hardware():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    details = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "python": sys.version.split()[0],
        "python_peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
    }
    if platform.system() == "Darwin":
        try:
            details["cpu_model"] = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True, stderr=subprocess.DEVNULL
            ).strip()
            details["physical_ram_bytes"] = int(
                subprocess.check_output(
                    ["sysctl", "-n", "hw.memsize"], text=True, stderr=subprocess.DEVNULL
                ).strip()
            )
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
    return details
