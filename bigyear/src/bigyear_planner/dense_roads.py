"""Immutable, disk-backed OSRM matrix; exact observation nodes are never reduced.

Identical road anchors share a matrix index. Costs are little-endian float64
(miles, minutes), preserving the same OSRM table values as the SQLite cache.
"""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from collections import Counter
from collections.abc import Mapping
from functools import lru_cache
import json
import math
import mmap
import os
from pathlib import Path
import struct
import time
import urllib.parse
import urllib.request

from .util import dump, read, sha256

FORMAT = "bigyear-road-matrix-v1"


def location_signature(location):
    from .travel import road_point

    anchor = road_point(location)
    return [location['id'], location['lat'], location['lon'], anchor['lat'], anchor['lon']]


@lru_cache(maxsize=16)
def verified_digest(path, size, mtime_ns, expected):
    if sha256(path) != expected:
        raise ValueError('road matrix checksum mismatch')
    return True


class DenseRoads(Mapping):
    def __init__(self, problem, path):
        self.path = Path(path).resolve()
        self.manifest = read(self.path)
        m = self.manifest
        if m['format'] != FORMAT or m['locations'] != [location_signature(l) for l in problem['locations']]:
            raise ValueError('road matrix locations or access anchors differ from problem')
        if m['source'].startswith('synthetic') and not problem['manifest']['synthetic']:
            raise ValueError('synthetic roads cannot validate historical observations')
        self.binary = self.path.parent / m['binary']
        if self.binary.parent != self.path.parent or self.binary.name != m['binary']:
            raise ValueError('road binary must be beside its manifest')
        self.n = len(m['anchors'])
        self.nodes = m['location_nodes']
        self.locs = problem['locations']
        if len(self.nodes) != len(self.locs) or any(not 0 <= x < self.n for x in self.nodes):
            raise ValueError('invalid road matrix node index')
        if any(m['anchors'][i] != sig[3:] for i, sig in zip(self.nodes, m['locations'])):
            raise ValueError('matrix node does not match exact road access anchor')
        stat = self.binary.stat()
        if stat.st_size != self.n * self.n * 16 or stat.st_size != m['bytes']:
            raise ValueError('truncated road matrix')
        verified_digest(str(self.binary), stat.st_size, stat.st_mtime_ns, m['sha256'])
        self.file = self.binary.open('rb')
        self.mapped = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        self.lookup = lru_cache(maxsize=8192)(self.lookup)

    def lookup(self, key):
        a, b = key
        if not (0 <= a < len(self.nodes) and 0 <= b < len(self.nodes)):
            raise KeyError(key)
        miles, minutes = struct.unpack_from('<dd', self.mapped,
                                            (self.nodes[a] * self.n + self.nodes[b]) * 16)
        if math.isinf(miles) and math.isinf(minutes):
            raise KeyError(key)
        if not all(math.isfinite(v) and v >= 0 for v in (miles, minutes)):
            raise ValueError('invalid road matrix costs')
        return {'miles': miles, 'minutes': minutes, 'geometry': None,
                'source': self.manifest['source'], 'network_release': self.manifest['network_release']}

    def __getitem__(self, key):
        return self.lookup(key)

    def __len__(self):
        return self.manifest['reachable_location_pairs']

    def __iter__(self):
        for a in range(len(self.nodes)):
            for b in range(len(self.nodes)):
                if self.get((a, b)) is not None:
                    yield a, b

    def close(self):
        self.lookup.cache_clear()
        self.mapped.close()
        self.file.close()


def freeze_matrix(source, output):
    """Hard-link completed, read-only binary; each run gets its own frozen manifest."""
    source, output = Path(source).resolve(), Path(output).resolve()
    m = read(source)
    binary = source.parent / m['binary']
    if binary.stat().st_mode & 0o222:
        raise ValueError('only a completed read-only matrix can be shared')
    target = output.parent / m['binary']
    os.link(binary, target)
    output.write_bytes(source.read_bytes())
    return output


def osrm_matrix(problem_path, endpoint, manifest_path, network_release, batch=3000):
    """Resumable exact all-pairs costs, bounded request/write buffers, no geodesic fallback."""
    from .travel import road_point
    import numpy as np

    if not network_release or not 1 <= batch <= 3000:
        raise ValueError('network release and batch between 1 and 3000 are required')
    started = time.monotonic()
    path = Path(manifest_path).resolve()
    if path.exists():
        if read(path)['network_release'] != network_release:
            raise ValueError('completed matrix belongs to a different network release')
        result = DenseRoads(read(problem_path), path)
        result.close()
        return read(path)
    problem = read(problem_path)
    locs = problem['locations']
    anchors, nodes, index = [], [], {}
    for loc in locs:
        point = road_point(loc)
        key = point['lat'], point['lon']
        if key not in index:
            index[key] = len(anchors)
            anchors.append(list(key))
        nodes.append(index[key])
    n = len(anchors)
    binary = path.with_suffix('.bin')
    progress = path.with_suffix('.progress.json')
    signature = {'locations': [location_signature(l) for l in locs], 'anchors': anchors,
                 'location_nodes': nodes, 'network_release': network_release,
                 'source': endpoint, 'batch': batch}
    state = read(progress) if progress.exists() else {**signature, 'completed': [],
        'unreachable_location_pairs': 0, 'unreachable_anchor_pairs': 0, 'repaired_pairs': [],
        'previous_wall_seconds': 0}
    if any(state[k] != v for k, v in signature.items()):
        raise ValueError('cannot resume matrix with different coordinates, network, or batch')
    if not binary.exists():
        with binary.open('wb') as f:
            f.truncate(n * n * 16)
    if binary.stat().st_size != n * n * 16:
        raise ValueError('partial matrix has wrong size')
    weights = Counter(nodes)
    state['previous_wall_seconds'] = state.get('wall_seconds', 0)
    completed = {tuple(block) for block in state['completed']}

    def request(service, points, params):
        coords = ';'.join(f'{lon},{lat}' for lat, lon in points)
        url = f"{endpoint.rstrip('/')}/{service}/v1/driving/{coords}?{urllib.parse.urlencode(params)}"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=300) as response:
                    data = json.load(response)
                if data.get('code') != 'Ok':
                    raise ValueError(f"OSRM error: {data.get('code')}")
                return data
            except (OSError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)

    blocks = iter((ai, bi) for ai in range(0, n, batch) for bi in range(0, n, batch)
                  if (ai, bi) not in completed)

    def fetch_block(ai, bi):
        aa, bb = anchors[ai:ai + batch], anchors[bi:bi + batch]
        data = request('table', aa + bb, {
            'sources': ';'.join(map(str, range(len(aa)))),
            'destinations': ';'.join(map(str, range(len(aa), len(aa) + len(bb)))),
            'annotations': 'duration,distance'})
        return data

    # Two in-flight HTTP buffers; consume/write one block before submitting another.
    with binary.open('r+b') as f, ThreadPoolExecutor(max_workers=2) as executor:
        pending = {}
        for _ in range(2):
            block = next(blocks, None)
            if block is not None:
                pending[executor.submit(fetch_block, *block)] = block
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                ai, bi = pending.pop(future)
                data = future.result()
                seconds = np.asarray(data['durations'], dtype=np.float64)
                meters = np.asarray(data['distances'], dtype=np.float64)
                del data
                na, nb = seconds.shape
                if meters.shape != (na, nb) or (na, nb) != (min(batch, n-ai), min(batch, n-bi)):
                    raise ValueError('truncated OSRM matrix block')
                missing = np.isnan(seconds) | np.isnan(meters)
                invalid = (~np.isfinite(seconds) | ~np.isfinite(meters) |
                           (seconds < 0) | (meters < 0)) & ~missing
                for i, j in zip(*np.nonzero(invalid)):
                    r = request('route', [anchors[ai+int(i)], anchors[bi+int(j)]],
                                {'overview': 'false'})['routes'][0]
                    sec, distance = r['duration'], r['distance']
                    if not all(math.isfinite(v) and v >= 0 for v in (sec, distance)):
                        raise ValueError('OSRM route service also returned invalid costs')
                    seconds[i, j], meters[i, j] = sec, distance
                    state['repaired_pairs'].append([ai+int(i), bi+int(j), distance, sec])
                state['unreachable_anchor_pairs'] += int(np.count_nonzero(missing))
                wa = np.asarray([weights[i] for i in range(ai, ai+na)], dtype=np.int64)
                wb = np.asarray([weights[i] for i in range(bi, bi+nb)], dtype=np.int64)
                state['unreachable_location_pairs'] += int(wa @ missing.astype(np.int64) @ wb)
                values = np.empty((na, nb, 2), dtype='<f8')
                values[:, :, 0] = meters / 1609.344
                values[:, :, 1] = seconds / 60
                values[missing] = math.inf
                # Identical road anchors follow the original cache identity policy.
                for node in range(max(ai, bi), min(ai+na, bi+nb)):
                    values[node-ai, node-bi] = 0
                for i, row in enumerate(values):
                    f.seek(((ai+i)*n+bi)*16)
                    f.write(row.tobytes())
                f.flush()
                del seconds, meters, values, missing, invalid
                state['completed'].append([ai, bi])
                state['wall_seconds'] = state['previous_wall_seconds'] + time.monotonic() - started
                dump(progress, state)
                print(json.dumps({'matrix_blocks': len(state['completed']),
                    'total_blocks': math.ceil(n / batch)**2, 'anchors': n,
                    'wall_seconds': state['wall_seconds']}), flush=True)
                block = next(blocks, None)
                if block is not None:
                    pending[executor.submit(fetch_block, *block)] = block
    result = {**signature, 'format': FORMAT, 'binary': binary.name,
              'bytes': binary.stat().st_size, 'sha256': sha256(binary),
              'reachable_location_pairs': len(locs)**2 - state['unreachable_location_pairs'],
              'reachable_anchor_pairs': n**2 - state['unreachable_anchor_pairs'],
              'repaired_pairs': state['repaired_pairs'], 'wall_seconds': state['wall_seconds'],
              'precision': 'little-endian float64 miles/minutes; no coordinate rounding',
              'unreachable': 'Infinity costs; never substituted or interpolated'}
    binary.chmod(0o444)
    dump(path, result)
    return result
