"""Check the actually served national route and all existing cumulative curves."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

from bigyear_planner.road_matching import BROUTER_POLICY, gap_m
from bigyear_planner.travel import road_point
from bigyear_planner.util import ROOT, dump, read, sha256

OUT = ROOT / "runs/lower48-eod-2024"
PATH = OUT / "regional-600s/route.json"


def main():
    global PATH
    run = read(OUT / "completion.json")["route_run"]
    PATH = OUT / run / "route.json"
    frozen = read(PATH)
    digest = sha256(PATH)
    with tempfile.TemporaryDirectory(prefix="national-viewer-") as folder:
        fixture = Path(folder) / "served.json"
        with urllib.request.urlopen(
            f"http://127.0.0.1:8773/api/route/{run}/route.json", timeout=120
        ) as stream:
            with fixture.open("wb") as target:
                shutil.copyfileobj(stream, target)
        shown = read(fixture)
        for key, value in frozen.items():
            assert shown[key] == value, key
        assert len(shown["days"]) == len(shown["observer_coverage"]["days"]) == 366
        assert shown["observer_coverage"]["annual_species"] == 764
        observer = {d["date"]: d["cumulative_species"] for d in shown["observer_coverage"]["days"]}
        for d in shown["days"]:
            assert observer[d["date"]] >= d["cumulative_species"]
        gaps = []
        legs = 0
        for d in shown["days"]:
            for leg in d["legs"]:
                assert leg["mode"] == "road" and not leg["schematic"]
                shape = leg["geometry"]
                assert shape["type"] == "LineString"
                anchors = [
                    [road_point(shown["locations"][leg[k]])[v] for v in ["lon", "lat"]]
                    for k in ["from", "to"]
                ]
                gaps.extend(
                    gap_m(a, b)
                    for a, b in zip(anchors, [shape["coordinates"][0], shape["coordinates"][-1]])
                )
                legs += 1
        assert max(gaps, default=0) <= BROUTER_POLICY["endpoint_tolerance_m"]
        job = subprocess.run(
            ["node", "--test", "tests/playback.test.mjs", "tests/activity.test.mjs"],
            cwd=ROOT,
            env={**os.environ, "BIGYEAR_PILOT_ROUTE": str(fixture)},
            text=True,
            capture_output=True,
            check=True,
        )
        (OUT / "viewer-playback-tests.txt").write_text(job.stdout)
        assert sha256(PATH) == digest
    report = {
        "valid": True,
        "days_verified": 366,
        "road_legs_verified": legs,
        "max_endpoint_gap_m": max(gaps, default=0),
        "route_sha256": digest,
        "all_observer_species": 764,
        "route_species": frozen["species_total"],
        "browser_logic_tests": int(re.search(r"# tests (\d+)", job.stdout)[1]),
        "served_coordinates_evidence_unchanged": True,
        "source_curve_not_below_route": True,
        "temporary_fixture_removed": True,
        "server_independent_of_router": read(OUT / run / "router-stopped.json")["stopped"],
    }
    dump(OUT / "viewer-verification.json", report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
