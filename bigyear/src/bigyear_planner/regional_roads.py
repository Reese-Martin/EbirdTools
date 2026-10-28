"""Cross-tile car routing, fixed-size caches, and on-demand access matching.

Unknown/time-limited queries defer a proposal. They never enter the cache as an
unreachable road. Selected routes require full geometry and endpoint validation.
"""

from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import secrets
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

from .bounded_roads import FixedCostCache
from .config import Config
from .road_matching import BROUTER_POLICY, check_endpoints, evidence, gap_m
from .util import dump, read, sha256


class DeferredRoad(RuntimeError):
    pass


ACCESS_POLICY = {
    "id": "brouter-local-car-probe-v1",
    "probe_longitude_offset": 0.02,
    "method": "Use actual start of a locally routed car path, not an unvalidated geometric nearest-way projection. Source pin stays exact; charge its actual anchor gap.",
}


def normalize_route(data, anchors):
    features = [
        f for f in data.get("features", []) if f.get("geometry", {}).get("type") == "LineString"
    ]
    if len(features) != 1:
        raise ValueError("BRouter did not return exactly one road path")
    feature = features[0]
    props = feature["properties"]
    shape = {
        "type": "LineString",
        "coordinates": [v[:2] for v in feature["geometry"]["coordinates"]],
    }
    meters = float(props["track-length"])
    seconds = float(props["total-time"])
    if not all(math.isfinite(v) and v >= 0 for v in [meters, seconds]):
        raise ValueError("invalid car costs")
    # Actual full-path endpoints, not echoed requested waypoints.
    route = {"distance": meters, "duration": seconds, "geometry": shape, "legs": []}
    result = {
        "waypoints": [{"location": shape["coordinates"][i]} for i in (0, -1)],
        "routes": [route],
    }
    check_endpoints(result, anchors, geometry=True, policy=BROUTER_POLICY)
    if meters + 0.25 < gap_m(*anchors):
        raise ValueError("car distance shorter than endpoint gap")
    return result


class RegionalRoads(Mapping):
    def __init__(
        self,
        model,
        release_path,
        work,
        endpoint="http://127.0.0.1:17779",
        capacity_mib=16,
        query_timeout_seconds=45,
        access_holds_path=None,
        access_policy_extension=None,
    ):
        self.model = model
        self.release = read(release_path)
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.endpoint = endpoint
        self.query_timeout_seconds = query_timeout_seconds
        self.synthetic = False
        self.lock = threading.RLock()
        self.stats = Counter()
        self.errors = []
        self.access_holds=set()
        access_policy=dict(ACCESS_POLICY)
        if access_policy_extension is not None:
            access_policy['extension']=access_policy_extension
        if access_holds_path is not None:
            hold=read(access_holds_path)
            self.access_holds={tuple(p) for p in hold['exact_coordinate_holds']}
            access_policy['surface_access_holds']={'path':str(Path(access_holds_path).resolve()),
                'sha256':sha256(access_holds_path),'reason':hold['reason']}
        binding = {
            # A scoring-only derivative may reuse road evidence only after its
            # complete coordinates, indices and calendar have been verified.
            "model_sha256": getattr(model,'routing_model_sha256',sha256(model.path)),
            "road_release_sha256": sha256(release_path),
            "matching_policy": BROUTER_POLICY,
            "access_policy": access_policy,
        }
        network_digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        if getattr(model, 'extra_binding', None):
            binding['additional_locations'] = model.extra_binding
        self.manifest = {
            "format": "bigyear-regional-roads-v1",
            **binding,
            "source": endpoint,
            "network_release": "BRouter-1.7.10-car-vario-national-regional-tiles",
            "network_sha256": network_digest,
            "native_hot_entries": 65536,
            "capacity_bytes": capacity_mib * 2**20,
            "unknown_queries": "Deferred, not cached as unreachable. Best found route uses only validated successful queries.",
        }
        manifest = self.work / "provider.json"
        if manifest.exists() and read(manifest) != self.manifest:
            raise ValueError("regional provider binding differs")
        dump(manifest, self.manifest)
        self.store = FixedCostCache(self.work / "costs.bin", binding, capacity_mib * 2**20)
        anchor_path = self.work / "access.bin"
        size = len(model.locations) * 24
        if not anchor_path.exists():
            arr = np.memmap(anchor_path, mode="w+", dtype="<f8", shape=(len(model.locations), 3))
            arr[:] = np.nan
            arr.flush()
            del arr
        if anchor_path.stat().st_size != size:
            raise ValueError("access cache size differs")
        self.anchors = np.memmap(
            anchor_path, mode="r+", dtype="<f8", shape=(len(model.locations), 3)
        )
        self.locks = [threading.Lock() for _ in range(256)]
        self.cost_locks = [threading.Lock() for _ in range(256)]

    def request(self, coords, match=False, radius=1):
        params = {
            "lonlats": "|".join(f"{lon:.17g},{lat:.17g}" for lon, lat in coords),
            "profile": "car-vario",
            "alternativeidx": 0,
            "format": "geojson",
            **{
                "profile:" + k: str(v)
                for k, v in self.release["parameters"].items()
                if k != "profile"
            },
            "profile:waypointCatchingRange": str(radius),
        }
        if match:
            params["engineMode"] = 5
        try:
            with urllib.request.urlopen(
                self.endpoint + "/brouter?" + urllib.parse.urlencode(params),
                timeout=self.query_timeout_seconds,
            ) as stream:
                return json.load(stream)
        except urllib.error.HTTPError as error:
            message = error.read().decode(errors="replace")[:800]
            # Matching-only does no connectivity search. This is a finite
            # profile-aware nearest-road search, never an invented route cost.
            if match and error.code == 400 and "position not mapped" in message:
                return None
            # BRouter's island heuristics, retracing errors, and timeouts do not
            # prove no route. Defer all of these, preserving the source cohort.
            raise DeferredRoad(message) from error
        except (OSError, ValueError) as error:
            raise DeferredRoad(str(error)) from error

    def access(self, index):
        if self.access_holds and tuple(map(float,self.model.coords[index])) in self.access_holds:
            # An explicit access-policy hold is not a cached disconnected road.
            # Keep the observation in the model and refuse unsupported access.
            self.stats['surface_access_policy_holds']+=1
            lat,lon=map(float,self.model.coords[index])
            return lat,lon,math.inf
        with self.locks[index % len(self.locks)]:
            value = self.anchors[index]
            if not math.isnan(float(value[2])):
                return tuple(float(v) for v in value)
            lat, lon = map(float, self.model.coords[index])
            cfg = self.model.meta["config"]
            max_gap = (
                (cfg["activity_minutes"] - cfg["birding_minutes"])
                * cfg["walking_speed_kmh"]
                * 1000
                / 120
            )
            failure = None
            for radius in [min(1000, max_gap + 1), max_gap + 1]:
                started = time.monotonic()
                try:
                    raw = self.request(
                        [[lon, lat], [lon + ACCESS_POLICY["probe_longitude_offset"], lat]],
                        radius=radius,
                    )
                    with self.lock:
                        self.stats["anchor_queries"] += 1
                    paths = [
                        f
                        for f in raw.get("features", [])
                        if f.get("geometry", {}).get("type") == "LineString"
                    ]
                    if len(paths) != 1:
                        raise DeferredRoad("local access probe lacks one complete car path")
                    vertices = paths[0]["geometry"]["coordinates"]
                    if len(vertices) < 2:
                        raise DeferredRoad("local access probe lacks endpoints")
                    for vertex in vertices:
                        gap_m(vertex[:2], vertex[:2])
                    point = vertices[0][:2]
                except DeferredRoad as error:
                    failure = error
                    # One bridge operation must remain below its socket timeout.
                    if time.monotonic() - started > 5 or "timeout" in str(error).lower():
                        raise
                    continue
                gap = gap_m([lon, lat], point)
                if gap > radius + 1:
                    raise DeferredRoad("car access anchor lies outside requested matching radius")
                walk = (
                    2 * gap / (cfg["walking_speed_kmh"] * 1000) * 60
                    if gap > cfg["walking_threshold_m"]
                    else 0
                )
                self.anchors[index] = [point[1], point[0], cfg["birding_minutes"] + walk]
                return point[1], point[0], cfg["birding_minutes"] + walk
            # A failed local route alone does not establish inaccessible land.
            # Only an explicit nearest-way miss across the full feasible radius
            # can establish that no same-day car access is available.
            nearest = self.request([[lon, lat]], match=True, radius=max_gap + 1)
            if nearest is not None:
                raise failure or DeferredRoad("local car access remains unresolved")
            # A point with no car anchor within the entire same-day walking
            # radius cannot be visited under the activity rule. Keep its model
            # opportunities. This finite-radius access result has no road cost.
            self.anchors[index] = [lat, lon, math.inf]
            with self.lock:
                self.stats["walking_radius_unmatched_locations"] += 1
            return lat, lon, math.inf

    def location(self, index):
        loc = self.model.locations[index]
        lat, lon, duration = self.access(index)
        if not math.isfinite(duration):
            raise ValueError("selected location has no feasible road access")
        loc["road_access"] = {
            "lat": lat,
            "lon": lon,
            "offset_meters": gap_m([loc["lon"], loc["lat"]], [lon, lat]),
            "source": self.manifest["network_release"] + " " + self.manifest["network_sha256"],
            "walking_policy": "Full round trip above 100 m at 2.2 km/h; straight-line walking assumed.",
            "matching_policy": ACCESS_POLICY["id"] + "; source coordinate unchanged",
        }
        return loc

    def query(self, a, b):
        alat, alon, at = self.access(a)
        blat, blon, bt = self.access(b)
        if not all(math.isfinite(v) for v in [at, bt]):
            return None
        anchors = [[alon, alat], [blon, blat]]
        if anchors[0] == anchors[1]:
            return {
                "waypoints": [{"location": p} for p in anchors],
                "routes": [
                    {
                        "distance": 0,
                        "duration": 0,
                        "geometry": {"type": "LineString", "coordinates": anchors},
                        "legs": [],
                    }
                ],
            }
        return normalize_route(self.request(anchors), anchors)

    def costs(self, a, b):
        if a == -1:
            return 0.0, self.access(b)[2]
        if a == b:
            return 0.0, 0.0
        key = (int(a), int(b))
        with self.cost_locks[hash(key) % len(self.cost_locks)]:
            stored = self.store.get(*key)
            if stored is not None:
                return stored
            data = self.query(a, b)
            if data is None:
                return math.inf, math.inf
            route = data["routes"][0]
            cost = (route["distance"] / 1609.344, route["duration"] / 60)
            self.store.put(a, b, cost)
            with self.lock:
                self.stats["successful_road_queries"] += 1
            return cost

    def __getitem__(self, key):
        m, t = self.costs(*key)
        if math.isinf(m):
            raise KeyError(key)
        return {
            "miles": m,
            "minutes": t,
            "geometry": None,
            "source": self.manifest["network_release"],
            "network_release": self.manifest["network_release"],
        }

    def __len__(self):
        return self.store.stats["writes"]

    def __iter__(self):
        raise TypeError("no all-pairs materialization")

    def endpoint_evidence(self, a, b, road):
        data = self.query(a, b)
        if data is None:
            raise ValueError("selected road access unavailable")
        coords = [[self.access(i)[1], self.access(i)[0]] for i in (a, b)]
        return evidence(data, coords, self.manifest["network_sha256"], policy=BROUTER_POLICY)

    def metrics(self):
        return {
            **self.stats,
            **self.store.stats,
            "road_cache_bytes": self.store.bytes,
            "access_cache_bytes": self.anchors.nbytes,
            "errors": self.errors,
            "native_hot_entries_cap": 65536,
            "query_timeout_seconds": self.query_timeout_seconds,
        }

    @contextmanager
    def bridge(self):
        owner = self
        token = secrets.token_hex(16)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if self.rfile.readline(128).strip().decode() != token:
                    return
                for line in self.rfile:
                    try:
                        fields = line.split()
                        transit = fields[0] == b'T'
                        a, b = map(int, fields[1:3] if transit else fields)
                        if not (
                            -1 <= a < len(owner.model.locations)
                            and 0 <= b < len(owner.model.locations)
                        ):
                            raise ValueError("unknown location")
                        if transit:
                            previous, target = map(int, fields[3:])
                            if not -1 <= previous < target < Config(**owner.model.meta['config']).days:
                                raise ValueError('invalid transition calendar window')
                            plan = owner.transition(a, b, previous, target)
                            value = f"{plan['days']} {plan['minutes']:.17g} {plan['miles']:.17g}\n"
                        else:
                            m, t = owner.costs(a, b)
                            value = "NO\n" if math.isinf(t) else f"{m:.17g} {t:.17g}\n"
                    except (DeferredRoad, ValueError) as error:
                        with owner.lock:
                            owner.stats["deferred_queries"] += 1
                            if len(owner.errors) < 12:
                                owner.errors.append(str(error)[:400])
                        value = "DEFER\n"
                    except Exception as error:
                        with owner.lock:
                            if len(owner.errors) < 12:
                                owner.errors.append(repr(error))
                        value = "ERROR\n"
                    self.wfile.write(value.encode())
                    self.wfile.flush()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.address, self.token = server.server_address, token
            yield self
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def close(self):
        self.anchors.flush()
        self.anchors._mmap.close()
        self.store.close()
