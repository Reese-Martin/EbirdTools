"""Freeze BRouter's regional tiles and a bounded car server with match-only API.

The small source patch exposes the upstream car-profile waypoint matcher without
running an artificial route. Routing itself remains upstream BRouter 1.7.10.
"""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from bigyear_planner.util import ROOT, dump, read, sha256

BUILD = ROOT / "build/regional-brouter"
DATA = ROOT / "data/lower48-eod-2024/roads"
ORIGINAL = ROOT / "build/brouter-benchmark/brouter-1.7.10"


def download(url, path, size=None):
    if path.exists() and (size is None or path.stat().st_size == size):
        return
    tmp = path.with_suffix(path.suffix + ".part")
    subprocess.run(
        ["/usr/bin/curl", "-fLsS", "--retry", "3", "--max-time", "900", "-o", str(tmp), url],
        check=True,
    )
    if size is not None and tmp.stat().st_size != size:
        raise ValueError("tile changed during snapshot: " + path.name)
    tmp.replace(path)


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    if (DATA / "release.json").exists():
        frozen = read(DATA / "release.json")
        files = [
            (Path(frozen["jar"]), frozen["jar_sha256"]),
            (Path(frozen["profiles"]) / "car-vario.brf", frozen["profile_sha256"]),
            (Path(frozen["profiles"]) / "lookups.dat", frozen["lookups_sha256"]),
        ]
        files += [
            (Path(frozen["tile_directory"]) / t["file"], t["sha256"]) for t in frozen["tiles"]
        ]
        if not all(p.is_file() and sha256(p) == digest for p, digest in files):
            raise ValueError(
                "frozen regional road snapshot changed; do not overwrite an existing release"
            )
        print(
            json.dumps(
                {"ready": True, "reused_verified_snapshot": True, "tiles": len(frozen["tiles"])}
            ),
            flush=True,
        )
        return
    tiles = DATA / "tiles"
    tiles.mkdir(exist_ok=True)
    index = DATA / "download-index.html"
    download("https://brouter.de/brouter/segments4/", index)
    entries = []
    for line in index.read_text().splitlines():
        m = re.search(r">(W(\d+)_N(\d+)\.rd5)</a>\s+([^<]+?)\s+(\d+)\s*$", line)
        if not m:
            continue
        name, west, north, modified, size = m.groups()
        # Include the surrounding Canadian/Mexican halo: roads may leave the
        # observation boundary. Each 5-degree tile is opened on demand by BRouter.
        if -130 <= -int(west) < -60 and 20 <= int(north) < 55:
            download("https://brouter.de/brouter/segments4/" + name, tiles / name, int(size))
            entries.append(
                {
                    "file": name,
                    "bytes": int(size),
                    "sha256": sha256(tiles / name),
                    "modified": modified.strip(),
                }
            )
            print(json.dumps({"tile": name, "bytes": int(size)}), flush=True)
    if not entries:
        raise ValueError("no national tiles")
    src = BUILD / "source"
    if not src.exists():
        shutil.copytree(ORIGINAL, src)
    profiles = src / "misc/profiles2"
    download("https://brouter.de/brouter/profiles2/lookups.dat", profiles / "lookups-national.dat")
    shutil.copyfile(profiles / "lookups-national.dat", profiles / "lookups.dat")
    engine = src / "brouter-core/src/main/java/btools/router/RoutingEngine.java"
    text = engine.read_text()
    method = """
  // Planner extension: expose the same profile-aware upstream nearest matcher.
  private void doMatchOnly() {
    try {
      java.util.List<MatchedWaypoint> matched = new java.util.ArrayList<>();
      for (int i=0;i<waypoints.size();i++) {
        MatchedWaypoint w = new MatchedWaypoint(); w.waypoint=waypoints.get(i); w.name="p"+i; matched.add(w);
      }
      matchWaypointsToNodes(matched);
      StringBuilder out=new StringBuilder("{\\"anchors\\":[");
      for(int i=0;i<matched.size();i++) {
        if(i>0)out.append(','); MatchedWaypoint w=matched.get(i);
        if(w.crosspoint==null)throw new IllegalArgumentException("unmatched source point");
        out.append('[').append(w.crosspoint.ilon/1000000.0-180.0).append(',')
           .append(w.crosspoint.ilat/1000000.0-90.0).append(']');
      }
      outputMessage=out.append("]}").toString();
    } catch(Exception e) { logException(e); }
    finally { ProfileCache.releaseProfile(routingContext);if(nodesCache!=null){nodesCache.close();nodesCache=null;}finished=true; }
  }
"""
    # Recreate from the pinned original every time, avoiding repeated patches.
    text = (ORIGINAL / "brouter-core/src/main/java/btools/router/RoutingEngine.java").read_text()
    text = text.replace(
        "switch (engineMode) {", "switch (engineMode) {\n      case 5: doMatchOnly(); break;", 1
    )
    text = text.replace(
        "  public void doRouting(long maxRunningTime) {",
        method + "\n  public void doRouting(long maxRunningTime) {",
        1,
    )
    engine.write_text(text)
    server = src / "brouter-server/src/main/java/btools/server/RouteServer.java"
    text = (ORIGINAL / "brouter-server/src/main/java/btools/server/RouteServer.java").read_text()
    text = text.replace(
        "engineMode == RoutingEngine.BROUTER_ENGINEMODE_GETINFO)",
        "engineMode == RoutingEngine.BROUTER_ENGINEMODE_GETINFO || engineMode == 5)",
    )
    server.write_text(text)
    handler = src / "brouter-server/src/main/java/btools/server/request/ServerHandler.java"
    text = (
        (ORIGINAL / "brouter-server/src/main/java/btools/server/request/ServerHandler.java")
        .read_text()
        .replace("rc.memoryclass = 128;", "rc.memoryclass = 512;")
    )
    handler.write_text(text)
    old = read(ROOT / "runs/west-coast-routing-2024/brouter-release.json")
    java = Path(old["java"])
    javac = java.with_name("javac")
    jarcmd = java.with_name("jar")
    classes = BUILD / "classes"
    classes.mkdir(exist_ok=True)
    modules = [
        "brouter-codec",
        "brouter-core",
        "brouter-expressions",
        "brouter-mapaccess",
        "brouter-util",
        "brouter-server",
    ]
    sources = [
        p
        for module in modules
        for p in (src / module / "src/main/java").rglob("*.java")
        if p.name != "BRouter.java"
    ]
    subprocess.run(
        [str(javac), "--release", "17", "-d", str(classes), *map(str, sources)], check=True
    )
    jar = BUILD / "regional-routing.jar"
    subprocess.run(
        [str(jarcmd), "--create", "--file", str(jar), "-C", str(classes), "."], check=True
    )
    custom = DATA / "custom-profiles"
    custom.mkdir(exist_ok=True)
    release = {
        "format": "bigyear-regional-brouter-v1",
        "upstream_tag": "v1.7.10",
        "jar": str(jar),
        "jar_sha256": sha256(jar),
        "source_zip_sha256": old["source_zip_sha256"],
        "tiles": entries,
        "tile_bytes": sum(e["bytes"] for e in entries),
        "tiles_sha256": hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
        "profiles": str(profiles),
        "profile_sha256": sha256(profiles / "car-vario.brf"),
        "lookups_sha256": sha256(profiles / "lookups.dat"),
        "java": str(java),
        "tile_directory": str(tiles),
        "custom_profiles": str(custom),
        "source_patches": {str(p.relative_to(src)): sha256(p) for p in [engine, server, handler]},
        "parameters": {
            "profile": "car-vario",
            "vmax": 130,
            "considerTurnRestrictions": 1,
            "add_beeline": 0,
            "use_offroad": 0,
            "correctMisplacedViaPoints": 0,
            "use_dynamic_range": 0,
        },
        "memory": "3 GiB Java heap, 512 MiB engine memory class, two server workers; tile files read regionally on demand",
        "snapshot_caveat": "Weekly current OSM tiles; not reconstructed 2024 closures/traffic. Car profile uses modeled speed and route preference, not OSRM fastest-route semantics.",
    }
    dump(DATA / "release.json", release)
    shutil.rmtree(classes)
    print(
        json.dumps({"ready": True, "tiles": len(entries), "tile_bytes": release["tile_bytes"]}),
        flush=True,
    )


if __name__ == "__main__":
    main()
