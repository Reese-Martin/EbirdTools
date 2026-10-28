"""Regional trip replacements over the complete exact-point candidate catalog.

The proposal shortlist is a search heuristic, never a reduced observation model.
Every accepted itinerary is evaluated by the same native calendar evaluator used
by the full search. Unknown road/flight queries remain deferred.
"""

from contextlib import contextmanager
import json
from pathlib import Path
import select
import shutil
import subprocess
import time

import numpy as np

from .national_runner import export_native
from .config import Config
from .util import ROOT, dump, sha256


def region_codes(coordinates):
    lon,lat = coordinates[:,1],coordinates[:,0]
    return (np.searchsorted([-115.,-100.,-85.],lon,side='right')*2+(lat>=37)).astype('u1')


class NativeEvaluator:
    def __init__(self,model,roads,output):
        self.model,self.roads,self.output = model,roads,Path(output)
        self.calls = self.deferred = 0

    @contextmanager
    def open(self):
        self.output.mkdir(parents=True,exist_ok=True)
        binary = ROOT/'build/bigyear-evaluate'
        sources = [ROOT/'cpp'/name for name in ['evaluate.cpp','model.h','bounded_roads.h','compact_catalog.h']]
        if not binary.exists() or any(p.stat().st_mtime>binary.stat().st_mtime for p in sources):
            subprocess.run([shutil.which('c++'),'-std=c++17','-O3','-pthread',str(sources[0]),'-o',str(binary)],check=True)
        snapshot = self.output/'regional-evaluator'
        shutil.copyfile(binary,snapshot)
        snapshot.chmod(0o755)
        native = self.output/'regional-evaluation.by'
        with self.roads.bridge(), (self.output/'regional-evaluator.log').open('w') as log:
            export_native(self.model,self.roads,native)
            self.process = subprocess.Popen([str(snapshot),str(native)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,bufsize=1)
            try:
                yield self
            finally:
                if self.process.poll() is None:
                    try:
                        self.process.stdin.write('QUIT\n');self.process.stdin.flush()
                        self.process.wait(timeout=5)
                    except (BrokenPipeError,subprocess.TimeoutExpired):
                        self.process.terminate();self.process.wait(timeout=5)
                self.process.stdin.close();self.process.stdout.close()
                if native.exists():
                    dump(self.output/'regional-evaluator-provenance.json',{'binary_sha256':sha256(snapshot),
                         'input_sha256':sha256(native),'expired_bridge_credentials_removed':True})
                    native.unlink()

    def evaluate(self,ids,timeout=30):
        deadline = time.monotonic()+timeout
        while True:
            revision=getattr(self.roads,'discovery_revision',0)
            self.process.stdin.write(json.dumps(ids)+'\n');self.process.stdin.flush()
            if not select.select([self.process.stdout],[],[],10)[0]:
                raise RuntimeError('native regional evaluator stopped responding')
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError('native regional evaluator exited; see its log')
            score = json.loads(line);self.calls += 1
            if not score['pending']:
                return score
            self.deferred += 1
            if time.monotonic()>=deadline:
                return score
            if hasattr(self.roads,'wait_for_discovery'):
                self.roads.wait_for_discovery(revision,min(.5,max(0,deadline-time.monotonic())))
            else:
                time.sleep(.1)


class RegionalExplorer:
    def __init__(self,model,evaluator,output,seed=20240101):
        self.model,self.evaluator,self.output = model,evaluator,Path(output)
        self.records = model.original.records if hasattr(model,'original') else model.records
        self.species = model.original.species if hasattr(model,'original') else model.species
        self.coords = model.original.coords if hasattr(model,'original') else model.coords
        self.regions = region_codes(self.coords)[self.records['location']]
        self.nspecies = len(model.meta['species'])
        self.days=Config(**model.meta.get('config',{})).days
        self.rng = np.random.default_rng(seed)
        self.library = {}
        self.attempts = self.accepted = 0
        self.round = 0
        self.attempted_heads = set()
        self.retry_heads = {}
        counts=np.bincount(self.species,minlength=self.nspecies)
        self.rarity=1/np.sqrt(np.maximum(1,counts))

    def canonical(self,ids):
        return sorted(set(map(int,ids)),key=lambda i:(int(self.records[i]['day']),i))

    def seen(self,ids):
        result = np.zeros(self.nspecies,dtype=bool)
        for i in ids:
            row = self.records[i]
            offset,count = int(row['offset']),int(row['count'])
            result[self.species[offset:offset+count]] = True
        return result

    def novelty(self,seen,lo=0,hi=None,weighted=False):
        hi = len(self.records) if hi is None else hi
        rows = self.records[lo:hi]
        if not len(rows):
            return np.zeros(0,dtype=np.int32)
        first = int(rows[0]['offset'])
        last = int(rows[-1]['offset'])+int(rows[-1]['count'])
        weights = (~seen)[self.species[first:last]]
        if weighted:
            weights=weights*self.rarity[self.species[first:last]]
        nonempty = np.flatnonzero(rows['count'])
        result = np.zeros(len(rows),dtype=np.float64 if weighted else np.int32)
        if len(nonempty):
            result[nonempty] = np.add.reduceat(weights,(rows['offset'][nonempty]-first).astype(np.intp),dtype=result.dtype)
        return result

    def _trip(self,head,begin,end,region,outside):
        margin=1 if end-begin<=14 else 3
        lo,hi = np.searchsorted(self.records['day'],[
            begin+(margin if begin else 0),end-(margin if end<self.days else 0)])
        ids = np.arange(lo,hi,dtype=np.int64)
        locations = self.records['location'][lo:hi]
        points = self.coords[locations]
        center = self.coords[int(self.records[head]['location'])]
        # Ranking only. A broad region can contain far-apart sites; keep a
        # proposed trip clustered while leaving every source point available.
        distance = np.hypot((points[:,0]-center[0])*69.1,
                            (points[:,1]-center[1])*69.1*np.cos(np.radians(center[0])))
        eligible = (self.regions[lo:hi]==region)&(distance<=150)
        chosen = [int(head)]
        seen = self.seen([*outside,head])
        days = self.records['day'][lo:hi].astype(np.int32)
        for _ in range(5):
            possible = eligible.copy()
            for item in chosen:
                day=int(self.records[item]['day'])
                point=self.coords[int(self.records[item]['location'])]
                nearby=np.hypot((points[:,0]-point[0])*69.1,
                    (points[:,1]-point[1])*69.1*np.cos(np.radians(point[0])))<=25
                # Nearby same-day stops can share a birding day. Longer moves
                # reserve an intervening transit day; measured evaluation still
                # decides whether the exact stop order and walking costs fit.
                if self.model.meta.get('config',{}).get('mixed_road_birding',False):
                    # A regional drive may now share a birding date, or arrive
                    # on the following birding date. Measured evaluation, not
                    # the old local-radius/transit-day heuristic, decides.
                    possible &= (days!=day)|((self.records['reserved'][lo:hi]==0)&
                                               (self.records[item]['reserved']==0))
                else:
                    possible &= (np.abs(days-day)>=2)|((days==day)&nearby)
                possible[ids==item]=False
            gain = self.novelty(seen,int(lo),int(hi),weighted=True)
            ranked = gain/(1+distance/75)
            ranked[~possible] = -1
            at = int(np.argmax(ranked))
            if ranked[at]<=0:
                break
            item = int(ids[at]);chosen.append(item)
            row = self.records[item];offset,count = int(row['offset']),int(row['count'])
            seen[self.species[offset:offset+count]] = True
        return self.canonical(chosen)

    def proposals(self,current):
        seen = self.seen(current)
        novelty = self.novelty(seen)
        rare=self.novelty(seen,weighted=True)
        novelty_scale=max(novelty.max(),1)
        rare_scale=max(rare.max(),1e-12)
        heads = [[] for _ in range(8)]
        # Overlapping four-week replacements reserve time for the measured
        # access/flight/egress chain at both ends. They are proposals, not a
        # restriction on dates or geography in the full native search.
        width=min(self.days,[14,28,42][self.round%3])
        start=(self.round//3)%min(7,self.days-width+1)
        starts=sorted({*range(start,self.days-width+1,7),self.days-width})
        for begin in starts:
            end = min(self.days,begin+width)
            margin=1 if width<=14 else 3
            lo,hi = np.searchsorted(self.records['day'],[
                begin+(margin if begin else 0),end-(margin if end<self.days else 0)])
            for region in range(8):
                options = np.flatnonzero((self.regions[lo:hi]==region)&(novelty[lo:hi]>0))
                if not len(options):
                    continue
                # Both multi-species hotspots and rare singletons get heads.
                for values in [novelty[lo:hi][options],rare[lo:hi][options]]:
                    k = min(4,len(options))
                    for at in np.argpartition(values,-k)[-k:]:
                        head = int(lo+options[at])
                        key=(begin,end,head)
                        if key not in self.attempted_heads and self.retry_heads.get(key,0)<=time.monotonic():
                            # Normalize against the criterion's annual scale,
                            # not each window's best. Otherwise every season
                            # ties at 1 and late-year integer IDs win the sort.
                            scale=novelty_scale if np.issubdtype(values.dtype,np.integer) else rare_scale
                            heads[region].append((float(values[at]/scale),head,begin,end,region))
        unique = set()
        # Rank every opportunity before bounding expensive trip construction.
        # Different source dates and locations can be tried in later rounds.
        # Round-robin quotas prevent high-novelty Florida/Arizona heads from
        # consuming the entire expensive-discovery budget.
        balanced=[]
        for region in range(8):
            entries=sorted(set(heads[region]),reverse=True)
            # Different months/cells first, then fill the quota with remaining
            # exact opportunities. This guides proposals, never filters data.
            diverse=[];buckets=set()
            for entry in entries:
                _,head,begin,end,_=entry
                lat,lon=self.coords[int(self.records[head]['location'])]
                bucket=(begin//28,int(lat//2),int(lon//2))
                if bucket not in buckets:
                    buckets.add(bucket);diverse.append(entry)
            balanced.append((diverse+[v for v in entries if v not in diverse])[:10])
        ordered=[]
        for rank in range(10):
            for offset in range(8):
                region=(self.round+offset)%8
                if rank<len(balanced[region]):ordered.append(balanced[region][rank])
        self.round+=1
        for _,head,begin,end,region in ordered:
            outside = [i for i in current if not begin<=int(self.records[i]['day'])<end]
            trip = self._trip(head,begin,end,region,outside)
            identity = tuple(trip)
            if identity in unique:
                continue
            unique.add(identity)
            candidate = self.canonical([*outside,*trip])
            species = int(self.seen(candidate).sum())
            # Also measure lower-scoring regional footholds. Their complete
            # feasible blocks can cross barriers in native reconstruction;
            # acceptance of the full itinerary still requires improvement.
            # Let the first head reach the router before constructing dozens
            # of other multi-stop proposals over the full source catalog.
            yield {'ids':candidate,'trip':trip,'head':head,'outside':outside,
                'begin':begin,'end':end,'region':region,'species_bound':species}

    def construct(self,proposal,deadline):
        """Keep only measured feasible partial trips, with the outer route fixed."""
        head=proposal['head'];trip=[head];outside=proposal['outside']
        key=(proposal['begin'],proposal['end'],head)
        def measure(ids):
            remaining=deadline-time.monotonic()
            if remaining<=0:return None
            self.attempts+=1
            return self.evaluator.evaluate(self.canonical([*outside,*ids]),timeout=min(8,remaining))
        # A complete block may be feasible when its isolated head cannot reach
        # the following fixed stop. Keep this macro-move possibility.
        score=measure(proposal['trip'])
        unresolved=score is None or score['pending']
        if score is not None and not score['pending'] and score['feasible']:
            trip=list(proposal['trip'])
        else:
            score=measure(trip)
            if score is None or score['pending']:
                self.retry_heads[key]=time.monotonic()+10
                while len(self.retry_heads)>1024:self.retry_heads.pop(next(iter(self.retry_heads)))
                return None,score
            if not score['feasible']:
                self.attempted_heads.add(key)
                return None,score
        options=[i for i in proposal['trip'] if i not in trip]
        options.sort(key=lambda i:int(self.seen([*outside,*trip,i]).sum()),reverse=True)
        for item in options:
            measured=measure([*trip,item])
            if measured is None:
                unresolved=True
                break
            if measured['pending']:unresolved=True
            if not measured['pending'] and measured['feasible']:
                trip.append(item);score=measured
        trip=self.canonical(trip)
        if unresolved:
            self.retry_heads[key]=time.monotonic()+10
            while len(self.retry_heads)>1024:self.retry_heads.pop(next(iter(self.retry_heads)))
        else:
            self.attempted_heads.add(key)
            self.retry_heads.pop(key,None)
        self.library[(proposal['begin'],proposal['end'],tuple(trip))]={
            'begin':proposal['begin'],'end':proposal['end'],'ids':trip,'region':proposal['region']}
        while len(self.library)>512:self.library.pop(next(iter(self.library)))
        return self.canonical([*outside,*trip]),score

    def explore(self,initial,seconds=900):
        started = time.monotonic();deadline = started+seconds
        current = self.canonical(initial)
        score = self.evaluator.evaluate(current,timeout=30)
        if score['pending'] or not score['feasible']:
            raise ValueError('regional exploration needs a fully measured feasible incumbent')
        history = self.output/'regional-improvements.jsonl'
        diagnostics = self.output/'regional-attempts.jsonl'
        with history.open('w') as improvements,diagnostics.open('w') as attempts:
            generation=0
            while time.monotonic()<deadline:
                changed = False
                for proposal in self.proposals(current):
                    if time.monotonic()>=deadline:
                        break
                    candidate,measured=self.construct(proposal,min(deadline,time.monotonic()+24))
                    if measured is not None:
                        attempts.write(json.dumps({'generation':generation,'region':proposal['region'],
                           'begin':proposal['begin'],'end':proposal['end'],
                           'elapsed_seconds':time.monotonic()-started,**measured})+'\n');attempts.flush()
                        if candidate is not None and not measured['pending'] and measured['feasible'] and measured['species']>score['species']:
                            current,score = candidate,measured
                            self.accepted += 1;changed = True
                            event = {'elapsed_seconds':time.monotonic()-started,'species':score['species'],
                                     'region':proposal['region'],'selected':current,'score':score}
                            improvements.write(json.dumps(event)+'\n');improvements.flush()
                            dump(self.output/'regional-checkpoint.json',current)
                            dump(self.output/'regional-progress.json',event)
                            break
                    if changed:
                        break
                generation+=1
        summary = {'ids':current,'score':score,'accepted_blocks':self.accepted,'attempts':self.attempts,
                   'elapsed_seconds':time.monotonic()-started,'native_calls':self.evaluator.calls,
                   'pending_native_calls':self.evaluator.deferred,'all_opportunities_ranked':len(self.records),
                   'proposal_library_size':len(self.library),'model_sha256':sha256(self.model.path)}
        summary['construction_policy']='balanced-regions-annual-ranking-measured-trips-v3'
        dump(self.output/'regional-exploration.json',summary)
        self.save_library(self.output/'regional-blocks.txt')
        return summary

    def save_library(self,path):
        # Retain a bounded, reproducible macro-move library for native polish.
        blocks = list(self.library.values())[-512:]
        with Path(path).open('w') as stream:
            for block in blocks:
                stream.write(f"{block['begin']} {block['end']} {len(block['ids'])} "+' '.join(map(str,block['ids']))+'\n')
