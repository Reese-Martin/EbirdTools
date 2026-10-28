import copy
import json
import math
import socketserver
import struct
import subprocess
import threading

import numpy as np
import pytest

from bigyear_planner.compact_model import build, RECORD
from bigyear_planner.config import Config
from bigyear_planner.eod import prepare_eod
from bigyear_planner.problem import build_problem
from bigyear_planner.regional_roads import normalize_route
from bigyear_planner.road_matching import BROUTER_POLICY, evidence, verify_evidence
from bigyear_planner.runner import compile_engine, export_engine_problem
from bigyear_planner.travel import transitions
from bigyear_planner.util import ROOT
from test_eod_archive import eod_input  # noqa: F401
from test_dense_roads import fixture


def test_full_eod_compact_catalog_matches_legacy_exact_points(eod_input, tmp_path):  # noqa: F811
    _, archive, taxonomy, land, rows = eod_input
    config = Config(
        target_locations=10000000, threads=1, memory_limit="256MB", walking_speed_kmh=2.2
    )
    dataset = tmp_path / "dataset"
    prepare_eod(archive, taxonomy, land, dataset, config, "fixture", "fixture", synthetic=True)
    legacy = build_problem(dataset, tmp_path / "legacy.json", config)
    model = build(dataset, tmp_path / "compact", config)
    try:
        assert model.meta["locations"] == len(legacy["locations"]) == 2
        assert model.meta["eligible_presence_groups"] == 2
        assert model.meta["represented_observations"] == 6
        assert model.meta["species_entries"] == 2
        assert [
            {k: model.locations[i][k] for k in legacy["locations"][i]} for i in range(2)
        ] == legacy["locations"]
        assert {model.locations[i]["latitude_text"] for i in range(2)} == {
            r["decimallatitude"] for r in rows
        }
        assert [model.opportunity(i) for i in range(2)] == legacy["opportunities"]
        assert np.array_equal(
            model.coords, np.array([[l["lat"], l["lon"]] for l in legacy["locations"]])
        )
        # Adjacent verbatim precision values must not merge into one location.
        assert model.locations[0]["id"] != model.locations[1]["id"]
        assert not (model.root / "rows.parquet").exists()
        same = build(dataset, tmp_path / "compact", config)
        same.close()
        from dataclasses import replace

        with pytest.raises(ValueError, match="different inputs/configuration"):
            build(dataset, tmp_path / "compact", replace(config, walking_speed_kmh=1.6))
    finally:
        model.close()


def test_native_mapped_all_subsets_match_directed_legacy_walking(tmp_path):
    p, _, roads = fixture(tmp_path)
    p["config"].update(walking_speed_kmh=2.2, walking_threshold_m=100, activity_minutes=300)
    durations = [120.0, 250.0, 301.0]
    # Match actual stop durations by independently positioning the anchors.
    from bigyear_planner.walking import stop_minutes

    for index, l in enumerate(p["locations"]):
        gap = (durations[index] - 120) * 2200 / 120
        l["road_access"] = {"lat": l["lat"] + math.degrees(gap / 6371008.8), "lon": l["lon"]}
        durations[index] = stop_minutes(l, p["config"])
    legacy = tmp_path / "legacy.by"
    export_engine_problem(p, roads, transitions(p, roads), legacy)
    records = tmp_path / "opportunities.bin"
    species = tmp_path / "species.bin"
    coords = tmp_path / "coords.bin"
    records.write_bytes(
        b"".join(
            RECORD.pack(o["day"], o["location"], i, 1, 0) for i, o in enumerate(p["opportunities"])
        )
    )
    species.write_bytes(struct.pack("<III", 0, 1, 2))
    coords.write_bytes(
        np.array([[l["lat"], l["lon"]] for l in p["locations"]], dtype="<f8").tobytes()
    )

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            assert self.rfile.readline() == b"test\n"
            for line in self.rfile:
                a, b = map(int, line.split())
                r = roads.get((a, b))
                result = (
                    f"0 {durations[b]:.17g}"
                    if a == -1
                    else (f"{r['miles']} {r['minutes']}" if r else "NO")
                )
                self.wfile.write((result + "\n").encode())
                self.wfile.flush()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mapped = tmp_path / "compact.by"
        mapped.write_text(
            f'BY4W 366 3 3 3 -1 30 300 480 120\n{json.dumps(str(records))} {json.dumps(str(species))} 3 {json.dumps(str(coords))} {server.server_address[1]} "test" 8\n'
        )
        source = tmp_path / "equivalence.cpp"
        source.write_text("""#include "model.h"
int main(int argc,char**argv){Problem a(argv[1]),b(argv[2]);for(int mask=0;mask<8;++mask){std::vector<int>r;
for(int i=0;i<3;++i)if(mask&(1<<i))r.push_back(i);auto x=a.evaluate(r),y=b.evaluate(r);
if(x.feasible!=y.feasible||x.species!=y.species||std::abs(x.minutes-y.minutes)>1e-7||std::abs(x.miles-y.miles)>1e-7)return 1;}return 0;}""")
        subprocess.run(
            [
                "c++",
                "-std=c++17",
                "-O2",
                "-pthread",
                "-I",
                str(ROOT / "cpp"),
                str(source),
                "-o",
                str(tmp_path / "evaluate"),
            ],
            check=True,
        )
        subprocess.run([str(tmp_path / "evaluate"), str(legacy), str(mapped)], check=True)
        # Truncation must fail before search rather than read past the mapping.
        records.write_bytes(records.read_bytes()[:-1])
        result = subprocess.run(
            [
                str(compile_engine()),
                str(mapped),
                str(tmp_path / "bad"),
                "1",
                "1",
                "1",
                "1",
                "2",
                "0",
            ],
            capture_output=True,
        )
        assert result.returncode != 0 and b"truncated" in result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_brouter_actual_geometry_endpoints_and_portable_proof():
    anchors = [[-87.0, 33.0], [-87.001, 33.001]]
    raw = {
        "features": [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[*anchors[0], 0], [*anchors[1], 0]],
                },
                "properties": {"track-length": "200", "total-time": "25"},
            }
        ]
    }
    data = normalize_route(raw, anchors)
    proof = evidence(data, anchors, "frozen-network", policy=BROUTER_POLICY)
    assert max(verify_evidence(proof, anchors, {"miles": 200 / 1609.344, "minutes": 25 / 60})) == 0
    bad = copy.deepcopy(raw)
    bad["features"][0]["geometry"]["coordinates"][0][0] += 0.01
    with pytest.raises(ValueError, match="endpoint mismatch"):
        normalize_route(bad, anchors)
    bad = copy.deepcopy(raw)
    bad["features"][0]["properties"]["total-time"] = "NaN"
    with pytest.raises(ValueError):
        normalize_route(bad, anchors)


def test_regional_unknown_cost_not_cached_and_access_is_round_trip(tmp_path):
    from types import SimpleNamespace
    from bigyear_planner.regional_roads import RegionalRoads, DeferredRoad
    from bigyear_planner.road_matching import gap_m
    from bigyear_planner.util import dump

    path = tmp_path / "model.json"
    path.write_text("{}")
    coords = np.array([[33.0, -87.0], [33.01, -87.01]])
    locations = [
        {"id": str(i), "lat": float(v[0]), "lon": float(v[1])} for i, v in enumerate(coords)
    ]
    model = SimpleNamespace(
        path=path,
        coords=coords,
        locations=locations,
        meta={"config": Config(activity_minutes=720, walking_speed_kmh=2.2).to_dict()},
    )
    release = tmp_path / "release.json"
    dump(release, {"parameters": {}})
    roads = RegionalRoads(model, release, tmp_path / "cache", capacity_mib=1)
    attempts = []

    def request(points, match=False, radius=1):
        if match:
            return {"anchors": [[points[0][0], points[0][1] + 0.002]]}
        if points[0][1] == points[1][1]:
            return {
                "features": [
                    {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[v[0], v[1] + 0.002] for v in points],
                        },
                        "properties": {"track-length": "2000", "total-time": "120"},
                    }
                ]
            }
        attempts.append(1)
        if len(attempts) == 1:
            raise DeferredRoad("timeout")
        return {
            "features": [
                {
                    "geometry": {"type": "LineString", "coordinates": points},
                    "properties": {"track-length": "2000", "total-time": "120"},
                }
            ]
        }

    roads.request = request
    try:
        with pytest.raises(DeferredRoad):
            roads.costs(0, 1)
        assert roads.store.stats["writes"] == 0
        assert roads.costs(0, 1) == pytest.approx((2000 / 1609.344, 2))
        assert len(attempts) == 2
        assert roads.costs(0, 1) == pytest.approx((2000 / 1609.344, 2)) and len(attempts) == 2
        expected = 120 + 2 * gap_m([-87.0, 33.0], [-87.0, 33.002]) / 2200 * 60
        assert roads.costs(-1, 0)[1] == pytest.approx(expected)
        assert roads.location(0)["lat"] == 33.0  # Exact observation pin stays unchanged.
        assert roads.location(0)["road_access"]["lat"] == 33.002
    finally:
        roads.close()


def test_native_deferred_road_is_retried_instead_of_cached_unreachable(tmp_path):
    requests = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            assert self.rfile.readline() == b"test\n"
            for line in self.rfile:
                requests.append(line)
                self.wfile.write(b"DEFER\n" if len(requests) == 1 else b"10 20\n")
                self.wfile.flush()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = tmp_path / "deferred.cpp"
        source.write_text("""#include "bounded_roads.h"
#include <thread>
int main(int argc,char**argv){BoundedRoadClient roads(std::stoi(argv[1]),"test",8);bool deferred=false;
try{roads.get(0,1);}catch(const DeferredRoad&){deferred=true;}
bool pending=false;try{roads.get(0,1);}catch(const DeferredRoad&){pending=true;}
std::this_thread::sleep_for(std::chrono::milliseconds(120));
auto a=roads.get(0,1),b=roads.get(0,1);
return !deferred||!pending||a.miles!=10||b.minutes!=20||roads.requests!=2||roads.hits!=1||roads.pending_hits!=1;}
""")
        subprocess.run(
            [
                "c++",
                "-std=c++17",
                "-O2",
                "-pthread",
                "-I",
                str(ROOT / "cpp"),
                str(source),
                "-o",
                str(tmp_path / "deferred"),
            ],
            check=True,
        )
        subprocess.run([str(tmp_path / "deferred"), str(server.server_address[1])], check=True)
        assert len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_compact_run_source_binding_and_independent_tamper_rejection(eod_input, tmp_path):  # noqa: F811
    from bigyear_planner.national_runner import run
    from bigyear_planner.regional_roads import RegionalRoads
    from bigyear_planner.util import dump, read
    from bigyear_planner.validate import validate

    _, archive, taxonomy, land, _ = eod_input
    config = Config(
        target_locations=10000000, threads=1, memory_limit="256MB", walking_speed_kmh=2.2
    )
    dataset = tmp_path / "dataset"
    prepare_eod(archive, taxonomy, land, dataset, config, "fixture", "fixture", synthetic=True)
    model = build(dataset, tmp_path / "compact", config)
    release = tmp_path / "release.json"
    dump(release, {"parameters": {}, "snapshot_caveat": "Synthetic test only"})
    roads = RegionalRoads(model, release, tmp_path / "cache", capacity_mib=1)

    def request(points, match=False, radius=1):
        if match:
            return {"anchors": points}
        return {
            "features": [
                {
                    "geometry": {"type": "LineString", "coordinates": points},
                    "properties": {"track-length": "0", "total-time": "0"},
                }
            ]
        }

    roads.request = request
    try:
        route = run(model.path, roads, tmp_path / "run", seconds=0.1, threads=1)
        assert route["species_total"] == 1 and len(route["days"]) == 366
        assert route["search"]["rare_proposal_index_bytes"] == 8
        assert read(tmp_path / "run/validation.json")["valid"]
        p = read(tmp_path / "run/problem.json")
        assert len(p["locations"]) <= model.meta["locations"]
        assert p["reduction"]["input_locations"] == model.meta["locations"]
        p["compact_source"]["selected_opportunities"][0] = (
            1 - p["compact_source"]["selected_opportunities"][0]
        )
        dump(tmp_path / "run/problem.json", p)
        audit = validate(
            tmp_path / "run/route.json",
            tmp_path / "run/problem.json",
            tmp_path / "run/roads.sqlite",
        )
        assert not audit["valid"]
        assert any("complete model" in e or "identity differs" in e for e in audit["errors"])
        assert not (tmp_path / "run/problem.by").exists()
    finally:
        roads.close()
        model.close()


def test_source_seed_is_rechecked_under_current_walking_rules(tmp_path):
    p, _, roads = fixture(tmp_path)
    p["config"].update(walking_speed_kmh=2.2, walking_threshold_m=100, activity_minutes=300)
    p["locations"][2]["road_access"] = {
        "lat": p["locations"][2]["lat"] + 1,
        "lon": p["locations"][2]["lon"],
    }
    native = tmp_path / "problem.by"
    export_engine_problem(p, roads, transitions(p, roads), native)
    seed = tmp_path / "seed.json"
    seed.write_text("[2,0,1]")
    subprocess.run(
        [
            str(compile_engine()),
            str(native),
            str(tmp_path / "result"),
            ".03",
            "1",
            "42",
            "1",
            "2",
            "0",
            str(seed),
            "greedy",
        ],
        check=True,
    )
    from bigyear_planner.util import read

    result = read(tmp_path / "result/engine.json")
    assert result["warm_start"]["attempted"] == 3 and result["warm_start"]["accepted"] == 2
    assert result["species"] == 2 and 2 not in result["selected"]


def test_failed_local_probe_with_existing_nearby_way_is_unknown_not_inaccessible(tmp_path):
    from types import SimpleNamespace
    from bigyear_planner.regional_roads import RegionalRoads, DeferredRoad
    from bigyear_planner.util import dump

    path = tmp_path / "model.json"
    path.write_text("{}")
    model = SimpleNamespace(
        path=path,
        coords=np.array([[33.0, -87.0]]),
        locations=[{"id": "point", "lat": 33.0, "lon": -87.0}],
        meta={"config": Config(activity_minutes=720, walking_speed_kmh=2.2).to_dict()},
    )
    release = tmp_path / "release.json"
    dump(release, {"parameters": {}})
    roads = RegionalRoads(model, release, tmp_path / "cache", capacity_mib=1)

    def request(points, match=False, radius=1):
        if match:
            return {"anchors": points}
        raise DeferredRoad("from-position not mapped in existing datafile")

    roads.request = request
    try:
        with pytest.raises(DeferredRoad):
            roads.access(0)
        assert math.isnan(roads.anchors[0, 2])
        assert roads.store.stats["writes"] == 0
    finally:
        roads.close()


def test_partial_calendar_opportunity_dates_use_actual_calendar_origin(tmp_path):
    from types import SimpleNamespace
    from bigyear_planner.compact_model import CompactModel
    owner=SimpleNamespace(records=np.array([(0,0,0,1,0)],dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')]),
        species=np.array([0]),meta={'config':Config(start_date='2024-02-29').to_dict()})
    assert CompactModel.opportunity(owner,0)['date']=='2024-02-29'
