"""Fixed-size measured road-cost cache and local native-engine query bridge.

Four-way associative slots are reused in place. There is no growing database,
WAL, or per-miss file. Only explicit OSRM NoRoute/NoSegment responses are unavailable;
transport/service failures abort instead of manufacturing an unavailable road.
"""

from collections import Counter
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import mmap
from pathlib import Path
import secrets
import socketserver
import socket
import struct
import threading

from .travel import cache, road_point
from .road_matching import POLICY, evidence, request_route, verify_evidence
from .util import dump, read, sha256

FORMAT = 'bigyear-bounded-roads-v2'
HEADER = 4096
SLOT = struct.Struct('<IIddQQ')
WAYS = 4


def location_digest(problem):
    h = hashlib.sha256()
    for loc in problem['locations']:
        point = road_point(loc)
        h.update(json.dumps([loc['id'], loc['lat'], loc['lon'], point['lat'], point['lon']],
                            separators=(',', ':')).encode() + b'\n')
    return h.hexdigest()


class FixedCostCache:
    def __init__(self, path, binding, capacity_bytes):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sets = (int(capacity_bytes) - HEADER) // (WAYS * SLOT.size)
        if self.sets < 1:
            raise ValueError('road cache needs at least one complete set')
        self.bytes = HEADER + self.sets * WAYS * SLOT.size
        signature = json.dumps({'format': FORMAT, 'binding': binding, 'bytes': self.bytes},
                               sort_keys=True).encode()
        if len(signature) > HEADER:
            raise ValueError('cache identity exceeds header')
        expected = signature.ljust(HEADER, b'\0')
        if self.path.exists():
            with self.path.open('rb') as stream:
                if stream.read(HEADER) != expected or self.path.stat().st_size != self.bytes:
                    raise ValueError('bounded road cache binding/size changed')
        else:
            with self.path.open('xb') as stream:
                stream.write(expected)
                stream.truncate(self.bytes)
        self.file = self.path.open('r+b')
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise ValueError('bounded cache already has an active owner; share its provider or run sequentially') from None
        self.data = mmap.mmap(self.file.fileno(), 0)
        self.lock = threading.Lock()
        self.clock = 1
        self.stats = Counter()
        self.pinned = {}
        self.pin_capacity = 8192

    def offsets(self, a, b):
        # Mix both directed indices; reverse costs have a different key.
        value = (((a << 32) | b) * 0x9e3779b97f4a7c15) & ((1 << 64) - 1)
        value ^= value >> 32
        start = HEADER + (value % self.sets) * WAYS * SLOT.size
        return range(start, start + WAYS * SLOT.size, SLOT.size)

    def get(self, a, b):
        with self.lock:
            if (a,b) in self.pinned:
                self.stats['pinned_hits'] += 1
                return self.pinned[a,b]
            for offset in self.offsets(a, b):
                x, y, miles, minutes, stamp, checksum = SLOT.unpack_from(self.data, offset)
                if stamp and (x, y) == (a, b) and checksum == self.checksum(x,y,miles,minutes):
                    self.clock = max(self.clock, stamp) + 1
                    SLOT.pack_into(self.data, offset, a, b, miles, minutes, self.clock, checksum)
                    self.stats['hits'] += 1
                    return miles, minutes
            self.stats['misses'] += 1
        return None

    def pin_current(self,a,b):
        """Protect a measured selected cost during finalization, bounded in RAM."""
        costs=self.get(a,b)
        if costs is None or not all(math.isfinite(v) and v>=0 for v in costs):return False
        with self.lock:
            if (a,b) not in self.pinned and len(self.pinned)>=self.pin_capacity:
                raise ValueError('selected-road pin capacity exceeded')
            self.pinned[a,b]=costs
            self.stats['selected_pins']=len(self.pinned)
        return True

    def put(self, a, b, costs):
        with self.lock:
            slots = [(SLOT.unpack_from(self.data, o)[4], o) for o in self.offsets(a, b)]
            stamp, offset = min(slots)
            if stamp:
                self.stats['evictions'] += 1
            self.clock = max(self.clock, *(s for s, _ in slots)) + 1
            SLOT.pack_into(self.data, offset, a, b, *costs, self.clock, self.checksum(a,b,*costs))
            self.stats['writes'] += 1

    @staticmethod
    def checksum(a,b,miles,minutes):
        return int.from_bytes(hashlib.blake2b(struct.pack('<IIdd',a,b,miles,minutes),digest_size=8).digest(),'little')

    def close(self):
        self.data.flush()
        self.data.close()
        self.file.close()


def create_bounded_roads(problem_path, output, endpoint, network_release, network_sha256,
                         capacity_mib=128, hot_entries=65536):
    problem = read(problem_path)
    if problem['config']['flights']:
        raise ValueError('bounded native roads require flights=false')
    if not network_release or not network_sha256 or not 1 <= capacity_mib <= 4096 or hot_entries < 1:
        raise ValueError('bounded roads need explicit network identity and positive bounded capacities')
    output = Path(output).resolve()
    manifest = {'format': FORMAT, 'locations_sha256': location_digest(problem),
                'source': endpoint.rstrip('/'), 'network_release': network_release,
                'network_sha256': network_sha256, 'capacity_bytes': int(capacity_mib * 2**20),
                'native_hot_entries': hot_entries, 'cache': str(output.with_suffix('.bin')),
                'matching_policy': POLICY,
                'cost_service': 'OSRM fastest car route, double-precision miles/minutes',
                'missing': 'Only explicit NoRoute or NoSegment is unavailable; service and endpoint validation failures abort'}
    if output.exists() and read(output) != manifest:
        raise ValueError('use a new manifest for different bounded cache inputs')
    dump(output, manifest)
    return manifest


class BoundedRoads(Mapping):
    def __init__(self, problem, path, fetch=None):
        self.path = Path(path).resolve()
        self.manifest = read(path)
        m = self.manifest
        if m['format'] != FORMAT or m['locations_sha256'] != location_digest(problem):
            raise ValueError('bounded road cache locations or access anchors differ')
        if m.get('matching_policy') != POLICY:
            raise ValueError('bounded road cache endpoint matching policy differs; rebuild costs')
        self.synthetic = problem['manifest']['synthetic']
        if problem['config']['flights']:
            raise ValueError('bounded roads require flights=false')
        self.location_count = len(problem['locations'])
        self.points, self.nodes, indices = [], [], {}
        for loc in problem['locations']:
            point = road_point(loc)
            key = point['lat'], point['lon']
            if key not in indices:
                indices[key] = len(self.points)
                self.points.append(key)
            self.nodes.append(indices[key])
        binding = {k: m[k] for k in ('locations_sha256', 'network_release', 'network_sha256', 'source', 'matching_policy')}
        self.store = FixedCostCache(m['cache'], binding, m['capacity_bytes'])
        self.fetch = fetch or self.fetch_osrm
        self.lock, self.pending, self.stats = threading.Lock(), {}, Counter()
        self.errors = []

    def fetch_osrm(self, a, b):
        anchors = [[lon, lat] for lat, lon in (self.points[a], self.points[b])]
        data = request_route(self.manifest['source'], anchors)
        if data.get('code') in {'NoRoute', 'NoSegment'}:
            return math.inf, math.inf
        route = data['routes'][0]
        return route['distance'] / 1609.344, route['duration'] / 60

    def endpoint_evidence(self, x, y, road):
        a, b = self.nodes[x], self.nodes[y]
        anchors = [[lon, lat] for lat, lon in (self.points[a], self.points[b])]
        if a == b:
            data = {'waypoints': [{'location': p} for p in anchors], 'routes': [
                {'distance': 0, 'duration': 0, 'geometry': {'type': 'LineString', 'coordinates': anchors}}]}
        else:
            data = request_route(self.manifest['source'], anchors, geometry=True)
            if data.get('code') != 'Ok':
                raise ValueError('selected road is no longer available under strict endpoint matching')
        proof = evidence(data, anchors, self.manifest['network_sha256'])
        verify_evidence(proof, anchors, road)
        return proof

    def costs(self, x, y):
        if not 0 <= x < self.location_count or not 0 <= y < self.location_count:
            raise IndexError('unknown road endpoint')
        a, b = self.nodes[x], self.nodes[y]
        if a == b:
            return 0., 0.
        if (costs := self.store.get(a, b)) is not None:
            return costs
        with self.lock:
            future = self.pending.get((a, b))
            owner = future is None
            if owner:
                future = self.pending[a, b] = Future()
        if owner:
            try:
                result = self.fetch(a, b)
                if not (all(math.isinf(v) and v > 0 for v in result)
                        or all(math.isfinite(v) and v >= 0 for v in result)):
                    raise ValueError('invalid provider road costs')
                self.store.put(a, b, result)
                with self.lock:
                    self.stats['provider_queries'] += 1
                    self.stats['unreachable_queries'] += int(math.isinf(result[0]))
                future.set_result(result)
            except Exception as error:
                with self.lock:
                    self.stats['provider_errors'] += 1
                    if len(self.errors) < 8:
                        self.errors.append(type(error).__name__ + ': ' + str(error)[:400])
                future.set_exception(error)
            finally:
                with self.lock:
                    self.pending.pop((a, b), None)
        return future.result()

    def __getitem__(self, key):
        miles, minutes = self.costs(*key)
        if math.isinf(miles):
            raise KeyError(key)
        return {'miles': miles, 'minutes': minutes, 'geometry': None,
                'source': self.manifest['source'], 'network_release': self.manifest['network_release']}

    def __len__(self):
        return self.store.stats['writes']

    def __iter__(self):
        raise TypeError('bounded roads are lazy; all-pairs iteration is prohibited')

    def metrics(self):
        return {**self.stats, **self.store.stats, 'capacity_bytes': self.store.bytes,
                'logical_bytes': self.store.path.stat().st_size,
                'allocated_bytes': self.store.path.stat().st_blocks * 512,
                'native_hot_entries_cap': self.manifest['native_hot_entries'], 'errors': self.errors}

    @contextmanager
    def bridge(self):
        owner, token = self, secrets.token_hex(16)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.connection.settimeout(10)
                if self.rfile.readline(128).strip().decode() != token:
                    return
                self.connection.settimeout(None)
                for line in self.rfile:
                    try:
                        a, b = map(int, line.split())
                        miles, minutes = owner.costs(a, b)
                        value = 'NO\n' if math.isinf(miles) else f'{miles:.17g} {minutes:.17g}\n'
                    except Exception:
                        # Transport failures are errors, never unreachable roads.
                        value = 'ERROR\n'
                    self.wfile.write(value.encode())
                    self.wfile.flush()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        server = Server(('127.0.0.1', 0), Handler)
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
        self.store.close()


def freeze_used_roads(problem, roads, routes, output):
    """Retain immutable measured evidence for selected legs; no whole-cache copies."""
    pairs = {(leg['from'], leg['to']) for route in routes for day in route['days']
             for leg in day['legs'] if leg['mode'] == 'road'}
    with cache(output) as con:
        for a, b in sorted(pairs):
            r = roads[a, b]
            x, y = problem['locations'][a], problem['locations'][b]
            ar, br = road_point(x), road_point(y)
            con.execute('INSERT OR REPLACE INTO roads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (x['id'], y['id'], r['miles'], r['minutes'], r['source'], r['network_release'],
                         None, x['lat'], x['lon'], y['lat'], y['lon'],
                         ar['lat'], ar['lon'], br['lat'], br['lon']))
            if not roads.synthetic:
                proof = roads.endpoint_evidence(a, b, r)
                con.execute('INSERT OR REPLACE INTO road_endpoints VALUES (?,?,?)',
                            (x['id'], y['id'], json.dumps(proof, separators=(',', ':'))))
    return {'pairs': len(pairs), 'sha256': sha256(output)}
