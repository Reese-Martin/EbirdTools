"""One-shot national continuation with live status and automatic final auditing."""

import json
import os
import signal
import subprocess
import time

from bigyear_planner.util import ROOT, dump, read

OUT = ROOT / "runs/lower48-eod-2024"
TAG = "national-3600s"


def progress():
    path = OUT / TAG / "improvements.jsonl"
    if not path.exists():
        return {}
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        for line in reversed(stream.read().splitlines()):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def main():
    if not read(OUT / "regional-seeded-600s/validation.json")["valid"]:
        raise ValueError("starting national itinerary has not passed independent validation")
    if (OUT / TAG).exists():
        raise ValueError("continuation folder already exists; preserve its run")
    started = time.monotonic()
    child = None
    cancelled = False
    command = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/evaluate_lower48.py"),
        "search",
        "--seconds",
        "3600",
        "--folder",
        TAG,
        "--road-seconds",
        "120",
        "--seed-file",
        str(OUT / "continuation-seed.json"),
    ]

    def status(stage, **fields):
        dump(
            OUT / "run-status.json",
            {
                "stage": stage,
                "run": TAG,
                "requested_seconds": 3600,
                "road_query_seconds": 120,
                "elapsed_seconds": time.monotonic() - started,
                "manager_pid": os.getpid(),
                "driver_pid": child.pid if child else None,
                **fields,
            },
        )

    def stop(signum, frame):
        nonlocal cancelled
        cancelled = True
        if child and child.poll() is None:
            child.send_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with (OUT / "extended-national.log").open("w") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            dump(
                OUT / "extended-run-process.json",
                {"manager_pid": os.getpid(), "driver_pid": child.pid, "command": command},
            )
            while child.poll() is None:
                item = progress()
                score = max(346, item.get("species", 346))
                stage = "validation" if (OUT / TAG / "engine.json").exists() else "search"
                status(
                    stage,
                    best_species=score,
                    message=(
                        f"Extended national run: validating the {score}-species candidate."
                        if stage == "validation"
                        else f"Extended national search running · best {score} species · {int((time.monotonic() - started) / 60)} minutes elapsed of a 60-minute search budget."
                    ),
                )
                time.sleep(10)
            if child.returncode:
                raise RuntimeError(
                    "national driver exited "
                    + str(child.returncode)
                    + "; see extended-national.log"
                )
        status(
            "validation",
            message="National search finished; verifying saved viewer and final reports.",
        )
        subprocess.run(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/finalize_lower48.py")], check=True
        )
        subprocess.run(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/verify_lower48_viewer.py")],
            check=True,
        )
        completed = read(OUT / "completion.json")
        status(
            "completed",
            best_run=completed["route_run"],
            best_species=completed["species_found"],
            message=f"National run complete and independently verified · best {completed['species_found']} species. Router stopped; temporary files cleaned.",
        )
    except BaseException as error:
        if child and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=90)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=30)
        stage = "cancelled" if cancelled else "failed"
        status(
            stage,
            message=(
                "National continuation cancelled; completed results preserved."
                if cancelled
                else "National continuation needs attention; completed results preserved. See extended-national.log."
            ),
            error=repr(error),
        )
        raise


if __name__ == "__main__":
    main()
