"""Bounded asynchronous road discovery; only measured successful costs are usable."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import threading
import time

import numpy as np

from .regional_roads import DeferredRoad, RegionalRoads
from .road_matching import verify_evidence
from .util import read, sha256


class SearchRoads(RegionalRoads):
    def __init__(self, *args, queue_capacity=32, workers=2, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue_capacity = queue_capacity
        self.worker_count = workers
        self.pending = {}
        self.cooldown = OrderedDict()
        self.discovery_lock = threading.RLock()
        self.executor = None
        self.latencies = []
        self.geometry = OrderedDict()
        self.geometry_bytes = 0
        self.geometry_capacity = 32 * 2**20
        self.evidence_databases = []
        self.discovery_condition = threading.Condition(self.discovery_lock)
        self.discovery_revision = 0

    def _completed(self, future):
        # Native/Python evaluators can wait for actual discovery progress rather
        # than flooding the loopback bridge while a bounded queue is occupied.
        with self.discovery_condition:
            self.discovery_revision += 1
            self.stats['discovery_completions'] += 1
            self.discovery_condition.notify_all()

    def wait_for_discovery(self, revision, timeout=.25):
        with self.discovery_condition:
            self.discovery_condition.wait_for(lambda:self.discovery_revision!=revision,timeout)
            return self.discovery_revision

    def begin_phase(self,label):
        """Drop queued speculation from the old phase, never measured costs.

        A running HTTP/car query completes normally. Cancelled unknown queries
        leave no unreachable value and may be proposed again in the new phase.
        """
        result={'phase':label,'cancelled_road':0,'cancelled_flight':0,
                'running_road':0,'running_flight':0}
        with self.discovery_lock:
            for attribute,kind in [('pending','road'),('transit_pending','flight')]:
                pending=getattr(self,attribute,{})
                for key,future in list(pending.items()):
                    if future.cancel():
                        pending.pop(key,None);result['cancelled_'+kind]+=1
                    elif future.done():pending.pop(key,None)
                    else:result['running_'+kind]+=1
                self.stats['phase_cancelled_'+kind]+=result['cancelled_'+kind]
            self.stats['phase_transitions']+=1
        return result

    def seed_evidence(self, route_path):
        route = read(route_path)
        path = route_path.parent / route['provenance']['roads_file']
        if sha256(path) != route['provenance']['roads_sha256']:
            raise ValueError('verified seed road evidence changed')
        if route['provenance']['road_network_sha256'] != self.manifest['network_sha256']:
            raise ValueError('verified seed belongs to a different road network')
        self.evidence_databases.append(sqlite3.connect(
            'file:' + str(path) + '?mode=ro&immutable=1', uri=True, check_same_thread=False))

    def query(self, a, b):
        data = super().query(a, b)
        if data is not None:
            payload = json.dumps(data, separators=(',', ':')).encode()
            with self.discovery_lock:
                if len(payload) <= self.geometry_capacity:
                    old = self.geometry.pop((a, b), None)
                    if old:
                        self.geometry_bytes -= len(old)
                    while self.geometry and self.geometry_bytes + len(payload) > self.geometry_capacity:
                        self.geometry_bytes -= len(self.geometry.popitem(last=False)[1])
                    self.geometry[a, b] = payload
                    self.geometry_bytes += len(payload)
        return data

    def endpoint_evidence(self, a, b, road):
        from .road_matching import BROUTER_POLICY, evidence

        anchors = [[self.access(i)[1], self.access(i)[0]] for i in (a, b)]
        with self.discovery_lock:
            payload = self.geometry.get((a, b))
        if payload:
            result = evidence(json.loads(payload), anchors, self.manifest['network_sha256'], policy=BROUTER_POLICY)
            verify_evidence(result, anchors, road)
            return result
        ids = [self.model.locations[i]['id'] for i in (a, b)]
        for db in self.evidence_databases:
            row = db.execute('SELECT evidence FROM road_endpoints WHERE origin=? AND destination=?', ids).fetchone()
            if row:
                result = json.loads(row[0])
                if result['network_sha256'] != self.manifest['network_sha256']:
                    raise ValueError('seed endpoint evidence network changed')
                verify_evidence(result, anchors, road)
                self.stats['reused_endpoint_proofs'] += 1
                return result
        return super().endpoint_evidence(a, b, road)

    def _discover(self, key):
        started = time.monotonic()
        try:
            result = RegionalRoads.costs(self, *key)
            with self.discovery_lock:
                self.stats['discovery_successes'] += 1
            return result
        except DeferredRoad:
            with self.discovery_lock:
                self.cooldown[key] = time.monotonic() + 60
                while len(self.cooldown) > 2048:
                    self.cooldown.popitem(last=False)
                self.stats['discovery_failures'] += 1
            raise
        finally:
            with self.discovery_lock:
                if len(self.latencies) < 10000:
                    self.latencies.append(time.monotonic() - started)

    def costs(self, a, b):
        if self.executor is None:
            return RegionalRoads.costs(self, a, b)
        if a == b:
            return 0., 0.
        if a == -1:
            if self.access_holds and tuple(map(float,self.model.coords[b])) in self.access_holds:
                return 0.,float('inf')
            value = float(self.anchors[b, 2])
            if not np.isnan(value):
                return 0., value
        else:
            stored = self.store.get(a, b)
            if stored is not None:
                return stored
        key = (a, b)
        with self.discovery_lock:
            future = self.pending.get(key)
            if future is not None and future.done():
                del self.pending[key]
                return future.result()
            if self.cooldown.get(key, 0) > time.monotonic():
                self.stats['cooldown_deferrals'] += 1
                raise DeferredRoad('temporary discovery cooldown; connectivity unknown')
            if future is None:
                # Collect completed tasks so one-off proposals do not occupy
                # the queue forever. Successful costs already live in cache.
                for old, done in list(self.pending.items()):
                    if done.done():
                        self.pending.pop(old)
                if len(self.pending) < self.queue_capacity:
                    self.pending[key] = self.executor.submit(self._discover, key)
                    self.pending[key].add_done_callback(self._completed)
                    self.stats['discovery_submissions'] += 1
                    self.stats['peak_pending'] = max(self.stats['peak_pending'], len(self.pending))
                else:
                    self.stats['queue_full_deferrals'] += 1
        raise DeferredRoad('road discovery pending; no cost substituted')

    @contextmanager
    def bridge(self):
        self.executor = ThreadPoolExecutor(max_workers=self.worker_count, thread_name_prefix='road-discovery')
        try:
            with super().bridge():
                yield self
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
            self.pending.clear()

    def metrics(self):
        return {**super().metrics(), 'queue_capacity': self.queue_capacity,
                'discovery_workers': self.worker_count,
                'discovery_latency_seconds': {
                    'samples': len(self.latencies),
                    'median': float(np.median(self.latencies)) if self.latencies else None,
                    'p95': float(np.percentile(self.latencies, 95)) if self.latencies else None},
                'geometry_cache_bytes': self.geometry_bytes,
                'geometry_cache_capacity': self.geometry_capacity}

    def close(self):
        for db in self.evidence_databases:
            db.close()
        super().close()
