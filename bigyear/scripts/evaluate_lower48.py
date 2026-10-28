"""Resource-guarded complete lower-48/DC 2024 model and national search."""

import argparse
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import threading
import time

from bigyear_planner.compact_model import build, load
from bigyear_planner.config import Config
from bigyear_planner.national_runner import run
from bigyear_planner.regional_roads import RegionalRoads
from bigyear_planner.search_roads import SearchRoads
from bigyear_planner.util import ROOT, dump, read, sha256, hardware

DATA = ROOT / "data/lower48-eod-2024"
OUT = ROOT / "runs/lower48-eod-2024"


@contextmanager
def guarded(stage):
    stop = threading.Event()
    started = time.monotonic()
    started_wall = time.time()
    metrics = {
        "stage": stage,
        "combined_peak_rss_bytes": 0,
        "minimum_free_bytes": shutil.disk_usage(DATA).free,
        "rss_limit_bytes": 18 * 2**30,
        "free_space_floor_bytes": 30 * 2**30,
        "samples": 0,
    }

    def watch():
        while not stop.is_set():
            rows = [
                list(map(int, line.split()))
                for line in subprocess.check_output(
                    ["ps", "-axo", "pid,ppid,rss"], text=True
                ).splitlines()[1:]
            ]
            owned = {os.getpid()}
            for _ in range(6):
                owned |= {pid for pid, parent, rss in rows if parent in owned}
            rss = sum(rss * 1024 for pid, parent, rss in rows if pid in owned)
            free = shutil.disk_usage(DATA).free
            metrics["combined_peak_rss_bytes"] = max(metrics["combined_peak_rss_bytes"], rss)
            metrics["minimum_free_bytes"] = min(metrics["minimum_free_bytes"], free)
            metrics["samples"] += 1
            if rss > metrics["rss_limit_bytes"] or free < metrics["free_space_floor_bytes"]:
                metrics["guard_triggered"] = True
                os.kill(os.getpid(), signal.SIGINT)
                return
            stop.wait(0.5)

    worker = threading.Thread(target=watch, daemon=True)
    worker.start()
    try:
        yield metrics
    finally:
        stop.set()
        worker.join()
        metrics["wall_seconds"] = time.monotonic() - started
        metrics['wall_clock_seconds'] = time.time()-started_wall
        metrics['suspend_seconds_estimate'] = max(0.,metrics['wall_clock_seconds']-metrics['wall_seconds'])
        metrics["hardware"] = hardware()
        dump(OUT / (stage + "-resources.json"), metrics)


@contextmanager
def router(tag="national", road_seconds=35):
    folder = OUT / tag
    folder.mkdir(parents=True, exist_ok=True)
    release = read(DATA / "roads/release.json")
    # The routing snapshot is frozen before any anchor or route query.
    for path, digest in [
        (Path(release["jar"]), release["jar_sha256"]),
        (Path(release["profiles"]) / "car-vario.brf", release["profile_sha256"]),
        (Path(release["profiles"]) / "lookups.dat", release["lookups_sha256"]),
    ]:
        if sha256(path) != digest:
            raise ValueError("router executable/profile binding changed")
    for tile in release["tiles"]:
        if sha256(Path(release["tile_directory"]) / tile["file"]) != tile["sha256"]:
            raise ValueError("regional tile changed")
    cmd = [
        release["java"],
        "-Xms64m",
        "-Xmx3g",
        f"-DmaxRunningTime={road_seconds}",
        "-cp",
        release["jar"],
        "btools.server.RouteServer",
        release["tile_directory"],
        release["profiles"],
        release["custom_profiles"],
        "17779",
        "2",
        "127.0.0.1",
    ]
    with (folder / "router.log").open("w+b") as log:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        rotations = [0]

        def drain():
            for block in iter(lambda: process.stdout.readline(65536), b""):
                if log.tell() + len(block) > 2 * 2**20:
                    log.seek(0)
                    log.truncate()
                    rotations[0] += 1
                log.write(block)
                log.flush()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        dump(folder / "router-process.json", {"pid": process.pid, "command": cmd})
        try:
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError("regional router exited during startup")
                try:
                    with socket.create_connection(("127.0.0.1", 17779), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.25)
            else:
                raise RuntimeError("regional router did not become ready")
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=60)
            reader.join(timeout=5)
            process.stdout.close()
            dump(
                folder / "router-log-retention.json",
                {
                    "capacity_bytes": 2 * 2**20,
                    "rotations": rotations[0],
                    "retained_bytes": log.tell(),
                },
            )
            dump(
                folder / "router-stopped.json",
                {"pid": process.pid, "returncode": process.returncode, "stopped": True},
            )


def compact():
    cfg = Config(**read(DATA / "config.json"))
    with guarded("compact-preparation"):
        model = build(DATA / "dataset", DATA / "model", cfg)
        summary = {
            k: model.meta[k]
            for k in [
                "locations",
                "opportunities",
                "eligible_presence_groups",
                "represented_observations",
                "species_entries",
            ]
        }
        model.close()
        for name in [*read(DATA / "model/model.json")["components"], "model.json"]:
            (DATA / "model" / name).chmod(0o444)
        dump(
            DATA / "model-retention-audit.json",
            {
                "valid": True,
                **summary,
                "model_sha256": sha256(DATA / "model/model.json"),
                "source_archive_manifest_sha256": sha256(
                    ROOT / "data/eod-us-since-2000-zstd3/manifest.json"
                ),
                "retention": "All eligible observations represented, every exact source locality identity, every observed date/species combination; no candidate reduction.",
                "coordinates": "Source coordinate strings preserved in the original verified archive. Distinct locality IDs include their exact strings; numeric coordinates use unchanged DOUBLE.",
            },
        )
        print(json.dumps({"stage": "compact-ready", **summary}), flush=True)


def prepare_audit(stack, process, roads, folder):
    # This Popen instance is the exact child created by this routing context.
    # Release the fast discovery server before opening the audit server.
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=60)
    stack.enter_context(router(folder + '-audit', 120))
    roads.query_timeout_seconds = 135


def search(seconds=600, folder="regional-600s", seed_file=None, road_seconds=35, resume=None, discovery=False, cache_dir=None):
    if not 1 <= road_seconds <= 150:
        raise ValueError("road query budget must be 1–150 seconds")
    model = load(DATA / "model/model.json")
    try:
        with guarded(folder):
            with ExitStack() as routing:
                process = routing.enter_context(router(folder, road_seconds))
                roads = (SearchRoads if discovery else RegionalRoads)(
                    model,
                    DATA / "roads/release.json",
                    cache_dir or DATA / "routing-cache-connected",
                    query_timeout_seconds=road_seconds + 15,
                )
                try:
                    if discovery:
                        roads.prepare_audit = lambda: prepare_audit(routing, process, roads, folder)
                    if discovery and resume:
                        roads.seed_evidence(Path(resume).resolve().parent / 'route.json')
                    route = run(
                        model.path, roads, OUT / folder, seconds=seconds, seed_file=seed_file, resume=resume
                    )
                    print(
                        json.dumps(
                            {
                                "stage": "completed",
                                "species": route["species_total"],
                                "route": str(OUT / folder / "route.json"),
                            }
                        ),
                        flush=True,
                    )
                finally:
                    roads.close()
    finally:
        model.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["compact", "search", "all"])
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--folder", default="regional-600s")
    parser.add_argument("--seed-file", type=Path)
    parser.add_argument("--road-seconds", type=int, default=35)
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--discovery', action='store_true')
    args = parser.parse_args()
    if args.stage in {"compact", "all"}:
        compact()
    if args.stage in {"search", "all"}:
        search(args.seconds, args.folder, args.seed_file, args.road_seconds, args.resume, args.discovery)
