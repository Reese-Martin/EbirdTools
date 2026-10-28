"""Directed road cache, with no straight-line feasibility fallback."""

import csv
import json
import math
from pathlib import Path
import sqlite3
import urllib.parse
import urllib.request
from collections.abc import Mapping
from functools import lru_cache

from .problem import haversine
from .util import read


def road_point(location):
    """Road-access coordinates never replace the reported observation point."""
    return location.get("road_access", location)


def cache(path):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE IF NOT EXISTS road_endpoints '
                '(origin TEXT, destination TEXT, evidence TEXT NOT NULL, PRIMARY KEY(origin,destination))')
    con.execute("""CREATE TABLE IF NOT EXISTS roads (
        origin TEXT, destination TEXT, miles REAL NOT NULL, minutes REAL NOT NULL,
        source TEXT NOT NULL, network_release TEXT NOT NULL, geometry TEXT,
        origin_lat REAL, origin_lon REAL, destination_lat REAL, destination_lon REAL,
        PRIMARY KEY(origin,destination))""")
    names = {r[1] for r in con.execute("PRAGMA table_info(roads)")}
    for name in (
        "origin_road_lat",
        "origin_road_lon",
        "destination_road_lat",
        "destination_road_lon",
    ):
        if name not in names:
            con.execute(f"ALTER TABLE roads ADD COLUMN {name} REAL")
    return con


def import_roads(problem_path, csv_path, cache_path, synthetic=False):
    p = read(problem_path)
    locs = {l["id"]: l for l in p["locations"]}
    con = cache(cache_path)
    count = 0
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            a, b = locs[r["origin"]], locs[r["destination"]]
            ar, br = road_point(a), road_point(b)
            miles, minutes = float(r["miles"]), float(r["minutes"])
            if not all(math.isfinite(v) and v >= 0 for v in (miles, minutes)) or (
                (ar["lat"], ar["lon"]) != (br["lat"], br["lon"]) and (miles == 0 or minutes == 0)
            ):
                raise ValueError("road costs must be finite and positive for distinct locations")
            if not r["source"] or not r["network_release"]:
                raise ValueError("road costs require source and network_release")
            if r["source"].startswith("synthetic") and not (
                synthetic and p["manifest"]["synthetic"]
            ):
                raise ValueError("synthetic road costs are allowed only in synthetic experiments")
            if miles + 0.25 < haversine(ar, br) and not synthetic:
                raise ValueError("road miles shorter than geodesic distance; check units")
            geom = r.get("geometry") or None
            if geom:
                g = json.loads(geom)
                if g.get("type") != "LineString" or len(g.get("coordinates", [])) < 2:
                    raise ValueError("geometry must be a GeoJSON LineString")
            con.execute(
                "INSERT OR REPLACE INTO roads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    a["id"],
                    b["id"],
                    miles,
                    minutes,
                    r["source"],
                    r["network_release"],
                    geom,
                    a["lat"],
                    a["lon"],
                    b["lat"],
                    b["lon"],
                    ar["lat"],
                    ar["lon"],
                    br["lat"],
                    br["lon"],
                ),
            )
            count += 1
    con.commit()
    con.close()
    return count


def osrm_roads(problem_path, endpoint, cache_path, network_release, batch=40, geometries=False):
    """Use a self-hosted/authorized OSRM endpoint, in bounded table blocks; resumable."""
    if not network_release:
        raise ValueError("label the road network/profile release")
    if batch < 1 or batch > 100:
        raise ValueError("OSRM batch size must be between 1 and 100")
    p = read(problem_path)
    locs = p["locations"]
    con = cache(cache_path)

    def request(service, points, params):
        coords = ";".join(f"{road_point(l)['lon']},{road_point(l)['lat']}" for l in points)
        url = (
            f"{endpoint.rstrip('/')}/{service}/v1/driving/{coords}?{urllib.parse.urlencode(params)}"
        )
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = json.load(resp)
        if data.get("code") != "Ok":
            raise ValueError(f"OSRM error: {data.get('code')}")
        return data

    for ai in range(0, len(locs), batch):
        aa = locs[ai : ai + batch]
        for bi in range(0, len(locs), batch):
            bb = locs[bi : bi + batch]
            def valid_cached(a, b):
                costs = con.execute(
                    "SELECT miles,minutes FROM roads WHERE origin=? AND destination=?",
                    (a["id"], b["id"]),
                ).fetchone()
                return costs is not None and all(math.isfinite(v) and v >= 0 for v in costs)

            if all(valid_cached(a, b) for a in aa for b in bb):
                continue
            data = request(
                "table",
                aa + bb,
                {
                    "sources": ";".join(map(str, range(len(aa)))),
                    "destinations": ";".join(map(str, range(len(aa), len(aa) + len(bb)))),
                    "annotations": "duration,distance",
                },
            )
            for i, a in enumerate(aa):
                for j, b in enumerate(bb):
                    sec, meters = data["durations"][i][j], data["distances"][i][j]
                    if sec is None or meters is None:
                        continue  # Unreachable pairs remain absent, never estimated.
                    geom = None
                    invalid = not all(math.isfinite(v) and v >= 0 for v in (sec, meters))
                    if (geometries and a != b) or invalid:
                        r = request("route", [a, b], {"geometries": "geojson", "overview": "full"})[
                            "routes"
                        ][0]
                        # Use the same route service result for both cost and displayed geometry.
                        sec, meters, geom = r["duration"], r["distance"], json.dumps(r["geometry"])
                    if not all(math.isfinite(v) and v >= 0 for v in (sec, meters)):
                        raise ValueError("OSRM returned invalid costs in both table and route services")
                    con.execute(
                        "INSERT OR REPLACE INTO roads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            a["id"],
                            b["id"],
                            meters / 1609.344,
                            sec / 60,
                            endpoint,
                            network_release,
                            geom,
                            a["lat"],
                            a["lon"],
                            b["lat"],
                            b["lon"],
                            road_point(a)["lat"],
                            road_point(a)["lon"],
                            road_point(b)["lat"],
                            road_point(b)["lon"],
                        ),
                    )
            con.commit()
    con.close()


class RoadCache(Mapping):
    """SQLite-backed directed costs with a bounded hot-pair cache, not millions of Python dicts."""

    def __init__(self, problem, path):
        self.problem = problem
        self.locs = problem["locations"]
        self.indices = {l["id"]: i for i, l in enumerate(self.locs)}
        self.con = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
        self.has_endpoint_evidence = self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='road_endpoints'").fetchone() is not None
        self.con.execute("CREATE TEMP TABLE selected(id TEXT PRIMARY KEY)")
        self.con.executemany("INSERT INTO selected VALUES (?)", [(l["id"],) for l in self.locs])
        self.query = """SELECT r.* FROM roads r JOIN selected a ON r.origin=a.id
            JOIN selected b ON r.destination=b.id WHERE r.origin!=r.destination"""
        self.lookup = lru_cache(maxsize=8192)(self.lookup)

    def decode(self, row):
        a, b, miles, minutes, source, release, geom, alat, alon, blat, blon = row[:11]
        x, y = self.indices[a], self.indices[b]
        if (alat, alon, blat, blon) != (
            self.locs[x]["lat"],
            self.locs[x]["lon"],
            self.locs[y]["lat"],
            self.locs[y]["lon"],
        ):
            raise ValueError("road cache coordinates differ from problem")
        anchors = row[11:15] if len(row) >= 15 else (None,) * 4
        access = (road_point(self.locs[x]), road_point(self.locs[y]))
        expected = tuple(v for point in access for v in (point["lat"], point["lon"]))
        if any(v is not None for v in anchors):
            if tuple(anchors) != expected:
                raise ValueError("road cache access anchors differ from problem")
        elif any("road_access" in self.locs[i] for i in (x, y)):
            raise ValueError("road cache lacks evidence for the specified access anchors")
        if source.startswith("synthetic") and not self.problem["manifest"]["synthetic"]:
            raise ValueError("synthetic roads cannot validate historical observations")
        if not all(math.isfinite(v) and v >= 0 for v in (miles, minutes)):
            raise ValueError("invalid cached road costs")
        proof = self.con.execute('SELECT evidence FROM road_endpoints WHERE origin=? AND destination=?',
                                 (a, b)).fetchone() if self.has_endpoint_evidence else None
        return {
            "miles": miles,
            "minutes": minutes,
            "source": source,
            "network_release": release,
            "geometry": json.loads(geom) if geom else None,
            "endpoint_evidence": json.loads(proof[0]) if proof else None,
        }

    def __getitem__(self, key):
        return self.lookup(key)

    def lookup(self, key):
        a, b = key
        if a == b:
            return {
                "miles": 0,
                "minutes": 0,
                "source": "identity",
                "network_release": "identity",
                "geometry": None,
            }
        row = self.con.execute(
            "SELECT * FROM roads WHERE origin=? AND destination=?",
            (self.locs[a]["id"], self.locs[b]["id"]),
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return self.decode(row)

    def __len__(self):
        return self.con.execute("SELECT count(*) FROM (" + self.query + ")").fetchone()[0] + len(
            self.locs
        )

    def items(self):
        for row in self.con.execute(self.query + " ORDER BY r.origin,r.destination"):
            yield (self.indices[row[0]], self.indices[row[1]]), self.decode(row)
        for i in range(len(self.locs)):
            yield (i, i), self[i, i]

    def __iter__(self):
        return (key for key, value in self.items())

    def close(self):
        self.lookup.cache_clear()
        self.con.close()


def load_roads(problem, path):
    if str(path).endswith('.bounded.json'):
        from .bounded_roads import BoundedRoads

        return BoundedRoads(problem, path)
    if str(path).endswith('.matrix.json'):
        from .dense_roads import DenseRoads

        return DenseRoads(problem, path)
    return RoadCache(problem, path)


def road_transit_days(road, budget):
    # A provider may round a very short duration to zero. Positive distance
    # still needs a calendar day when it is scheduled as separate transit.
    return max(int(road['miles'] > 0), math.ceil(road['minutes'] / budget))


def pair_transitions(problem, roads, a, b):
    """Cache conservative direct-road or airport-access + flight + egress plans."""
    budget = problem["config"]["drive_minutes"]
    plans = []
    r = roads.get((a, b))
    if r is not None:
        days = road_transit_days(r, budget)
        plans.append(
            {
                "days": days,
                "minutes": r["minutes"],
                "miles": r["miles"],
                "segments": [] if a == b else [{"mode": "road", "from": a, "to": b, "days": days}],
            }
        )
    if problem["config"]["flights"]:
        for f in problem["flight_links"]:
            x, y = roads.get((a, f["from"])), roads.get((f["to"], b))
            if x is None or y is None:
                continue
            dx, dy = road_transit_days(x, budget), road_transit_days(y, budget)
            plans.append(
                {
                    "days": dx + 1 + dy,
                    "minutes": x["minutes"] + f["minutes"] + y["minutes"],
                    "miles": x["miles"] + y["miles"],
                    "segments": (
                        [{"mode": "road", "from": a, "to": f["from"], "days": dx}] if dx else []
                    )
                    + [
                        {
                            "mode": "flight",
                            "from": f["from"],
                            "to": f["to"],
                            "days": 1,
                            "minutes": f["minutes"],
                        }
                    ]
                    + ([{"mode": "road", "from": f["to"], "to": b, "days": dy}] if dy else []),
                }
            )
    return sorted(plans, key=lambda z: (z["minutes"], z["miles"], z["days"]))


class Transitions:
    def __init__(self, problem, roads):
        self.problem, self.roads = problem, roads

    def get(self, key, default=None):
        return pair_transitions(self.problem, self.roads, *key) or default

    def items(self):
        if not self.problem["config"]["flights"]:
            budget = self.problem["config"]["drive_minutes"]
            for (a, b), r in self.roads.items():
                days = road_transit_days(r, budget)
                yield (a, b), [{"days": days, "minutes": r["minutes"], "miles": r["miles"]}]
        else:
            n = len(self.problem["locations"])
            for a in range(n):
                for b in range(n):
                    if pp := pair_transitions(self.problem, self.roads, a, b):
                        yield (a, b), pp

    def values(self):
        return (v for k, v in self.items())


def transitions(problem, roads):
    return Transitions(problem, roads)
