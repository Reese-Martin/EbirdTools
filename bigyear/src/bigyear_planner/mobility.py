"""Dated nonstop flights with measured, directed ground access at both ends."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import math
from pathlib import Path
import sqlite3
import threading
import time

import numpy as np

from .bounded_roads import SLOT, HEADER
from .regional_roads import RegionalRoads, DeferredRoad
from .road_matching import gap_m
from .search_roads import SearchRoads
from .travel import road_transit_days
from .util import read, sha256


class AirportCatalog:
    def __init__(self, original, airports):
        self.original, self.airports = original, airports

    def __len__(self):
        return len(self.original) + len(self.airports)

    def __getitem__(self, index):
        return self.original[index] if index < len(self.original) else dict(self.airports[index-len(self.original)])


class AirportCoordinates:
    def __init__(self, original, airports):
        self.original = original
        self.extra = np.asarray([[a['lat'], a['lon']] for a in airports])

    def __getitem__(self, index):
        return self.original[index] if index < len(self.original) else self.extra[index-len(self.original)]


class FlightModel:
    def __init__(self, original, network_path):
        self.original = original
        self.network_path = Path(network_path).resolve()
        self.network = read(self.network_path / 'manifest.json')
        config = original.meta['config']
        if self.network.get('year',config['year']) != config['year'] or config.get('start_date') not in (None,f"{config['year']}-01-01"):
            raise ValueError('dated flight overlay requires matching years and a January 1 calendar origin')
        for file, key in [('airports.json', 'airports_sha256'), ('flights.sqlite', 'database_sha256')]:
            if sha256(self.network_path / file) != self.network[key]:
                raise ValueError('frozen flight input changed: ' + file)
        self.airports = read(self.network_path / 'airports.json')
        with sqlite3.connect('file:' + str(self.network_path/'flights.sqlite') + '?mode=ro&immutable=1', uri=True) as db:
            self.minimum_flight_minutes = float(db.execute('SELECT min(minutes) FROM flights').fetchone()[0] or 0)
        self.locations = AirportCatalog(original.locations, self.airports)
        self.coords = AirportCoordinates(original.coords, self.airports)
        self.meta = {**original.meta, 'config': {**original.meta['config'], 'flights': True}}
        self.extra_binding = {'airports_sha256': self.network['airports_sha256']}
        self.path, self.root = original.path, original.root
        self.routing_model_sha256 = getattr(original,'routing_model_sha256',sha256(original.path))
        self.opportunity = original.opportunity


class WindowTransitions:
    def __init__(self, problem):
        self.plans = {(v['from'], v['to'], v['previous'], v['target']): v['plan']
                      for v in problem['window_transitions']}

    def for_window(self, a, b, previous, target):
        plan = self.plans.get((a, b, previous, target))
        return [plan] if plan is not None else []


class MobilityRoads(SearchRoads):
    def __init__(self, *args, **kwargs):
        self.separate_discovery=kwargs.pop('separate_discovery',True)
        kwargs.setdefault('queue_capacity', 8)
        kwargs.setdefault('workers',1)
        super().__init__(*args, **kwargs)
        self.transit_capacity = 24
        model = self.model
        self.flight_db = sqlite3.connect('file:' + str(model.network_path / 'flights.sqlite') + '?mode=ro&immutable=1', uri=True, check_same_thread=False)
        self.flight_db.execute('PRAGMA cache_size=-8192')
        self.flight_lock = threading.Lock()
        self.plan_cache = OrderedDict()
        self.flight_plans = OrderedDict()
        self.transit_pending = {}
        self.base_count = model.meta['locations']
        self.airport_coords = model.coords.extra
        self.transit_executor = None

    @contextmanager
    def bridge(self):
        # Flight coordinators perform synchronous measured ground queries.
        # Sharing the road-discovery pool lets long flight proposals starve
        # stop/nearby-road discovery. Reserve one worker for each class.
        previous_workers=self.worker_count
        if not self.separate_discovery:
            self.worker_count=2
            try:
                with super().bridge():
                    self.transit_executor=self.executor
                    yield self
            finally:
                self.transit_executor=None
                self.transit_pending.clear()
                self.worker_count=previous_workers
            return
        self.transit_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='airport-discovery')
        self.worker_count=1
        try:
            with super().bridge():
                yield self
        finally:
            self.transit_executor.shutdown(wait=True,cancel_futures=True)
            self.transit_executor=None
            self.transit_pending.clear()
            self.worker_count=previous_workers

    def copy_ground_cache(self, directory):
        # Original birding indices and road-policy bindings are unchanged. New
        # airport slots have their own coordinate-bound cache identity.
        directory = Path(directory)
        old = np.memmap(directory / 'access.bin', mode='r', dtype='<f8', shape=(self.base_count, 3))
        self.anchors[:self.base_count] = old
        del old
        with (directory / 'costs.bin').open('rb') as stream:
            stream.seek(HEADER)
            while block := stream.read(SLOT.size):
                a, b, miles, minutes, stamp, checksum = SLOT.unpack(block)
                if stamp and a < self.base_count and b < self.base_count and self.store.checksum(a, b, miles, minutes) == checksum:
                    self.store.put(a, b, (miles, minutes))

    def _remember(self, key, plan):
        with self.discovery_lock:
            self.plan_cache[key] = plan
            self.plan_cache.move_to_end(key)
            if any(v['mode']=='flight' for v in plan['segments']):
                self.flight_plans[key] = plan
                self.flight_plans.move_to_end(key)
                while len(self.flight_plans)>4096:
                    self.flight_plans.popitem(last=False)
            while len(self.plan_cache) > 8192:
                self.plan_cache.popitem(last=False)
        return plan

    def ground_plan(self, a, b):
        return self._ground_plan(a,b,RegionalRoads.costs(self,a,b))

    def _ground_plan(self,a,b,cost):
        m,t = cost
        if not math.isfinite(t):
            return None
        days = road_transit_days({'miles': m, 'minutes': t}, self.model.meta['config']['drive_minutes'])
        return {'days': days, 'minutes': t, 'miles': m,
                'segments': [] if a == b else [{'mode': 'road', 'from': a, 'to': b, 'days': days}]}

    def seed_incumbent(self, route_path, location_mapping=None):
        route = read(route_path)
        if route['config'].get('flights'):
            problem_path = route_path.parent/'problem.json'
            if sha256(problem_path)!=route['provenance']['problem_sha256']:
                raise ValueError('flight incumbent problem changed')
            problem = read(problem_path)
            if problem['flight_network']['manifest_sha256']!=sha256(self.model.network_path/'manifest.json'):
                raise ValueError('flight incumbent uses a different dated network')
            routing = problem['routing_locations']
            if location_mapping is not None:
                # A new taxonomic model may have different canonical indices.
                # Never reuse the old integers without checking every identity.
                for local,old in enumerate(routing):
                    new=self.model.locations[location_mapping[old]]
                    saved=problem['locations'][local]
                    if any(new[k]!=saved[k] for k in ['id','lat','lon','state']):
                        raise ValueError('remapped incumbent location differs')
                routing=[location_mapping[i] for i in routing]
            for item in problem['window_transitions']:
                key = (routing[item['from']],routing[item['to']],item['previous'],item['target'])
                plan = {**item['plan'],'segments':[{**s,'from':routing[s['from']],'to':routing[s['to']]}
                        for s in item['plan']['segments']]}
                self._remember(key,plan)
            return
        here = None
        previous = -1
        by_day = {}
        for idx in route['search']['selected_model_opportunities']:
            o = self.model.opportunity(idx)
            by_day.setdefault(o['day'], []).append(o['location'])
        for target, locations in sorted(by_day.items()):
            if here is None:
                here = locations[0]
            # The verified driving itinerary has exactly these measured roads.
            self._remember((here, locations[0], previous, target), self.ground_plan(here, locations[0]))
            here, previous = locations[-1], target

    def nearest_airports(self, location, count=8):
        lat, lon = self.model.coords[location]
        delta = (self.airport_coords - [lat, lon]) * [1, math.cos(math.radians(lat))]
        return np.argsort(np.sum(delta*delta, axis=1))[:count]

    def _build_transition(self, a, b, previous, target):
        gap = target-previous-1
        choices = []
        stored = self.store.get(a, b) if a != b else (0., 0.)
        if stored is not None:
            direct = self.ground_plan(a, b)
            if direct and direct['days'] <= gap:
                choices.append(direct)
        if gap >= 3:
            # Airport shortlists guide a heuristic transition proposal. Every
            # observation remains available; no unmeasured access is accepted.
            candidates = []
            for x in self.nearest_airports(a):
                for y in self.nearest_airports(b):
                    if x == y:
                        continue
                    origin, dest = self.model.airports[x]['code'], self.model.airports[y]['code']
                    with self.flight_lock:
                        service = self.flight_db.execute(
                            'SELECT day,minutes FROM flights WHERE origin=? AND destination=? AND day BETWEEN ? AND ? ORDER BY minutes,day DESC LIMIT 1',
                            [origin, dest, previous+2, target-2]).fetchone()
                    if service:
                        aloc, bloc = self.model.coords[a], self.model.coords[b]
                        left, right = self.model.airports[x], self.model.airports[y]
                        ground_km = (gap_m([float(aloc[1]),float(aloc[0])],[left['lon'],left['lat']]) +
                                     gap_m([right['lon'],right['lat']],[float(bloc[1]),float(bloc[0])]))/1000
                        # Ranking only; accepted access/egress costs are always
                        # measured. Service connectivity matters as well as proximity.
                        candidates.append((service[1] + ground_km/80*60, int(x), int(y)))
            discovery_started=time.monotonic()
            for _, x, y in sorted(candidates)[:getattr(self,'airport_pair_limit',2)]:
                if time.monotonic()-discovery_started>getattr(self,'transition_discovery_seconds',float('inf')):break
                xi, yi = self.base_count+x, self.base_count+y
                try:
                    access, egress = self.ground_plan(a, xi), self.ground_plan(yi, b)
                except DeferredRoad:
                    continue
                if access is None or egress is None:
                    continue
                dx, dy = access['days'], egress['days']
                if dx+dy+1 > gap:
                    continue
                with self.flight_lock:
                    service = self.flight_db.execute(
                        'SELECT day,minutes FROM flights WHERE origin=? AND destination=? AND day BETWEEN ? AND ? ORDER BY minutes,day DESC LIMIT 1',
                        [self.model.airports[x]['code'], self.model.airports[y]['code'], previous+dx+1, target-dy-1]).fetchone()
                if service:
                    flight_day, minutes = service
                    choices.append({'days': dx+1+dy, 'minutes': access['minutes']+minutes+egress['minutes'],
                                    'miles': access['miles']+egress['miles'], 'start_day': flight_day-dx,
                                    'segments': access['segments'] + [{'mode': 'flight', 'from': xi, 'to': yi, 'days': 1, 'minutes': minutes, 'service_day': flight_day}] + egress['segments']})
                    if sum(any(s['mode']=='flight' for s in v['segments']) for v in choices)>=2:break
        if not choices:
            direct = self.ground_plan(a, b)
            if direct and direct['days'] <= gap:
                choices.append(direct)
        if not choices:
            raise DeferredRoad('No validated transition in the current airport shortlist; connectivity unknown')
        plan = min(choices, key=lambda v: (v['minutes'], v['miles']))
        self.stats['flight_transition_plans' if any(s['mode']=='flight' for s in plan['segments']) else 'ground_transition_plans'] += 1
        return self._remember((a, b, previous, target), plan)

    def transition(self, a, b, previous, target):
        key = (a, b, previous, target)
        with self.discovery_lock:
            saved = self.plan_cache.get(key) or self.flight_plans.get(key)
            if saved is not None:
                return saved
        stored = (0.,0.) if a==b else self.store.get(a,b)
        # Use the already measured tuple directly, outside discovery_lock.
        # Taking a striped road-cost lock here would invert the lock order
        # used by a worker that saves returned geometry.
        if stored is not None and stored[1]+1e-7 < self.model.minimum_flight_minutes:
            direct = self._ground_plan(a,b,stored)
            if direct and direct['days'] <= target-previous-1:
                return self._remember(key,direct)
        if self.executor is None:
            return self._build_transition(*key)
        with self.discovery_lock:
            for old, future in list(self.transit_pending.items()):
                if future.done() and old != key:
                    self.transit_pending.pop(old)
            future = self.transit_pending.get(key)
            if future and future.done():
                self.transit_pending.pop(key)
                return future.result()
            if future is None and len(self.transit_pending) < self.transit_capacity:
                self.transit_pending[key] = self.transit_executor.submit(self._build_transition, *key)
                self.transit_pending[key].add_done_callback(self._completed)
                self.stats['airport_discovery_submissions']+=1
            elif future is None:
                self.stats['airport_queue_full_deferrals']+=1
        raise DeferredRoad('dated airport transition discovery pending')

    def selection(self, problem):
        globals_ = list(problem['compact_source']['selected_locations'])
        mapping = {g: i for i, g in enumerate(globals_)}
        windows = []
        links = {}
        here, previous = None, -1
        groups = {}
        for o in problem['opportunities']:
            groups.setdefault(o['day'], []).append(o)
        for target, opps in sorted(groups.items()):
            first = globals_[opps[0]['location']]
            if here is None:
                here = first
            key = (here, first, previous, target)
            plan = self.plan_cache.get(key) or self.flight_plans.get(key)
            if plan is None:
                # Native evaluation can use a complete local drive on the
                # birding day, with zero intervening transit days. It need not
                # have requested any whole-day transition for that window.
                # Keep the measured ground option; materialization separately
                # checks the local mileage/activity budget and rejects a
                # whole-day option that does not fit the intervening gap.
                plan = (self.ground_plan(here,first) if self.cached_costs(here,first) is not None
                        else self._build_transition(*key))
            selected_plan = {**plan, 'segments': []}
            for segment in plan['segments']:
                for endpoint in [segment['from'], segment['to']]:
                    if endpoint not in mapping:
                        mapping[endpoint] = len(globals_)
                        globals_.append(endpoint)
                        problem['locations'].append(self.location(endpoint))
                saved = {**segment, 'from': mapping[segment['from']], 'to': mapping[segment['to']]}
                selected_plan['segments'].append(saved)
                if segment['mode'] == 'flight':
                    links[(saved['from'], saved['to'], saved['service_day'])] = {
                        'from': saved['from'], 'to': saved['to'], 'minutes': saved['minutes'],
                        'service_day': saved['service_day'], 'source': self.model.network['source']}
            windows.append({'from': mapping[here], 'to': mapping[first], 'previous': previous, 'target': target, 'plan': selected_plan})
            here, previous = globals_[opps[-1]['location']], target
        problem.update(routing_locations=globals_, window_transitions=windows, flight_links=list(links.values()),
                       flight_network={'path': str(self.model.network_path), 'manifest_sha256': sha256(self.model.network_path/'manifest.json')})

    def cached_costs(self, a, b):
        return (0., 0.) if a == b else self.store.get(a, b)

    def close(self):
        self.flight_db.close()
        super().close()

    def metrics(self):
        return {**super().metrics(), 'airport_transition_queue_capacity': self.transit_capacity,
                'discovery_workers':1 if self.separate_discovery else 2,
                'total_discovery_queue_capacity': self.queue_capacity+self.transit_capacity,
                'scheduling_policy':('independent-road-and-airport-workers-v2' if self.separate_discovery else 'shared-two-worker-control'),
                'airport_discovery_workers':1 if self.separate_discovery else 2,
                'minimum_flight_minutes': self.model.minimum_flight_minutes}
