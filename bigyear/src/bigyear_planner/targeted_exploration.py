"""Source-dated regional campaigns for explicitly identified missing taxa.

The target index and proposal quotas guide search only. The original complete
catalog remains the evaluator/search input; no points, dates or observations
are removed. A feasible regional foothold may temporarily lose other species;
it is offered as a macro block without awarding artificial target points.
"""
import json
import math
import time

import numpy as np

from .regional_exploration import RegionalExplorer
from .util import dump, sha256


class TargetedExplorer(RegionalExplorer):
    def __init__(self, model, evaluator, output, targets):
        super().__init__(model, evaluator, output)
        self.targets = targets
        by_id = {s['id']:i for i,s in enumerate(model.meta['species'])}
        self.target_indices = np.asarray([by_id[t['species_id']] for t in targets])
        self.support = {int(s):[] for s in self.target_indices}
        # Stream daily slices rather than allocate an expanded 335 MiB owner
        # vector for the full species array.
        for day in range(self.days):
            lo,hi = map(int,np.searchsorted(self.records['day'],[day,day+1]))
            rows = self.records[lo:hi]
            if not len(rows):continue
            first = int(rows[0]['offset']);last = int(rows[-1]['offset'])+int(rows[-1]['count'])
            spp = self.species[first:last]
            positions = np.flatnonzero(np.isin(spp,self.target_indices))
            owners = lo+np.searchsorted(rows['offset'],positions+first,side='right')-1
            for s in self.target_indices:
                self.support[int(s)].extend(map(int,np.unique(owners[spp[positions]==s])))
        if any(not self.support[int(s)] for s in self.target_indices):
            raise ValueError('a requested target has no actual source-dated support')

    def distance(self, points, center):
        return np.hypot((points[:,0]-center[0])*69.1,
            (points[:,1]-center[1])*69.1*np.cos(np.radians(center[0])))

    def heads(self, current, target, limit=12):
        ids = np.asarray(self.support[int(target)],dtype=np.int64)
        seen = self.seen(current)
        ranked = []
        for head in ids:
            day = int(self.records[head]['day']);point = self.coords[int(self.records[head]['location'])]
            before = [i for i in current if int(self.records[i]['day'])<day]
            after = [i for i in current if int(self.records[i]['day'])>day]
            # Ranking only, never travel-time or feasibility evidence.
            cost = 0
            for neighbor in [before[-1] if before else None,after[0] if after else None]:
                if neighbor is not None:
                    gap = abs(int(self.records[neighbor]['day'])-day)
                    miles = float(self.distance(np.asarray([self.coords[int(self.records[neighbor]['location'])]]),point)[0])
                    cost += miles/(gap+1)
                    cfg=self.model.meta.get('config',{})
                    local=(min(cfg['drive_minutes'],cfg['activity_minutes']-cfg['birding_minutes'])/60*130/1.609344
                           if cfg.get('mixed_road_birding',False) else 30)
                    if gap<4 and miles>local+(gap-1)*8*130/1.609344:
                        cost += 10000
            # Prefer a free source date when an insertion would otherwise
            # evict an existing distant birding visit on that same day.
            same = [i for i in current if int(self.records[i]['day'])==day]
            if same:
                remaining=[i for i in current if i not in same]
                cost += max(0,int(seen.sum())-int(self.seen([*remaining,int(head)]).sum()))*2000
            row = self.records[head];start,count = int(row['offset']),int(row['count'])
            gain = int((~seen[self.species[start:start+count]]).sum())
            ranked.append((cost-gain*25,int(head),day,point))
        ranked.sort(key=lambda v:(v[0],v[1]))
        diverse=[];buckets=set()
        for _,head,day,point in ranked:
            bucket=(day//7,int(point[0]*4),int(point[1]*4))
            if bucket not in buckets:
                buckets.add(bucket);diverse.append(head)
        return diverse[:limit]

    def campaign(self, current, head, radius):
        day = int(self.records[head]['day'])
        begin,end = max(0,day-radius),min(self.days,day+radius+1)
        outside = [i for i in current if not begin<=int(self.records[i]['day'])<end]
        center = self.coords[int(self.records[head]['location'])]
        lo,hi = map(int,np.searchsorted(self.records['day'],[begin,end]))
        points = self.coords[self.records['location'][lo:hi]]
        distance = self.distance(points,center)
        ids = np.arange(lo,hi,dtype=np.int64)
        days = self.records['day'][lo:hi].astype(np.int32)
        margin = 3 if radius>=7 else 1
        eligible = (distance<=375)&(days>=begin+(margin if begin else 0))&(days<end-(margin if end<self.days else 0))
        chosen = [int(head)]
        # Recover incumbent regional visits before filling gaps. Charter
        # variants remain possible, but same-day exclusivity is not relaxed.
        for i in current:
            if begin<=int(self.records[i]['day'])<end and i!=head:
                p = self.coords[int(self.records[i]['location'])]
                if self.distance(np.asarray([p]),center)[0]<=375:
                    d = int(self.records[i]['day'])
                    if all(abs(d-int(self.records[v]['day']))>=(1 if self.model.meta.get('config',{}).get('mixed_road_birding',False) else 2)
                           for v in chosen):chosen.append(i)
        seen = self.seen([*outside,*chosen])
        for _ in range(max(0,8-len(chosen))):
            possible = eligible.copy()
            for i in chosen:
                d = int(self.records[i]['day']);point = self.coords[int(self.records[i]['location'])]
                nearby = self.distance(points,point)<=15
                # An intervening day for regional moves; full evaluator
                # checks actual roads, flights, walks and 12-hour activities.
                if self.model.meta.get('config',{}).get('mixed_road_birding',False):
                    possible &= (days!=d)|((self.records['reserved'][lo:hi]==0)&(self.records[i]['reserved']==0))
                else:
                    possible &= (np.abs(days-d)>=2)|((days==d)&nearby&(self.records['reserved'][lo:hi]==0)&(self.records[i]['reserved']==0))
                possible[ids==i]=False
            raw = self.novelty(seen,lo,hi).astype(float)
            weighted = self.novelty(seen,lo,hi,weighted=True)
            rank = (raw+weighted*3)/(1+distance/300)
            rank[(~possible)|(raw==0)] = -1
            at = int(np.argmax(rank)) if len(rank) else 0
            if not len(rank) or rank[at]<=0:break
            chosen.append(int(ids[at]));seen = self.seen([*outside,*chosen])
        trip = self.canonical(chosen)
        return {'head':int(head),'trip':trip,'outside':outside,'begin':begin,'end':end,
            'region':int(self.regions[head]),'ids':self.canonical([*outside,*trip])}

    def explore(self, initial, seconds=900, first_probe_seconds=2):
        if any(not math.isfinite(v) or v <= 0 for v in [seconds, first_probe_seconds]):
            raise ValueError('target campaigns require positive finite search and probe budgets')
        started=time.monotonic();deadline=started+seconds
        current=self.canonical(initial);best=self.evaluator.evaluate(current,timeout=120)
        if best['pending'] or not best['feasible']:raise ValueError('target campaigns need a measured feasible incumbent')
        baseline=best['species'];attempts=[];accepted=[];footholds=[];done=set()
        per_target={t['species_id']:{**t,'source_opportunities':len(self.support[int(s)]),
            'attempts':0,'feasible_campaigns':0,'pending_campaigns':0} for t,s in zip(self.targets,self.target_indices)}
        # Every requested species gets a turn before any target gets a second
        # expensive routing attempt. Subsequent rounds vary dates and windows.
        with (self.output/'targeted-attempts.jsonl').open('w') as log:
            round_=0
            while time.monotonic()<deadline and round_<48:
                changed=False
                for t,s in zip(self.targets,self.target_indices):
                    if time.monotonic()>=deadline:break
                    if self.seen(current)[s]:continue
                    options=self.heads(current,s)
                    head=options[(round_//2)%len(options)]
                    radius=[0,7,0,14,0,21][round_%6]
                    proposal=self.campaign(current,head,radius)
                    identity=(tuple(current),head,radius)
                    if identity in done:continue
                    done.add(identity)
                    # Measure the full campaign and then a single rare head;
                    # pending queries are revisited in a later round.
                    alternatives=[proposal['trip']]
                    if proposal['trip']!=[head]:alternatives.append([head])
                    for trip in alternatives:
                        if time.monotonic()>=deadline:break
                        candidate=self.canonical([*proposal['outside'],*trip])
                        # Give each species a short first turn before spending
                        # longer on unresolved routes in subsequent rounds.
                        # Pending is never interpreted as infeasible.
                        probe_seconds = first_probe_seconds if round_ == 0 else 16
                        score=self.evaluator.evaluate(candidate,timeout=min(probe_seconds,deadline-time.monotonic()))
                        entry={'target':t['species_id'],'name':t['species'],'head':head,
                            'begin':proposal['begin'],'end':proposal['end'],'trip':trip,
                            'source_species_bound':int(self.seen(candidate).sum()),
                            'elapsed_seconds':time.monotonic()-started,**score}
                        attempts.append(entry);log.write(json.dumps(entry)+'\n');log.flush()
                        stats=per_target[t['species_id']];stats['attempts']+=1
                        if score['pending']:
                            stats['pending_campaigns']+=1;done.discard(identity)
                        elif score['feasible']:
                            stats['feasible_campaigns']+=1
                            block={'begin':proposal['begin'],'end':proposal['end'],'ids':trip,'region':proposal['region']}
                            self.library[(block['begin'],block['end'],tuple(trip))]=block
                            if score['species']>best['species']:
                                current,best=candidate,score;accepted.append(entry);changed=True
                                dump(self.output/'targeted-checkpoint.json',current)
                                print(json.dumps({'stage':'targeted-improvement',**entry}),flush=True)
                            elif score['species']>=baseline-12:
                                footholds.append({'ids':candidate,'score':score,'target':t['species_id']})
                            # A feasible whole campaign already contains the
                            # head; do not pay again just to remove companions.
                            break
                    dump(self.output/'targeted-progress.json',{'round':round_,'species':best['species'],
                        'elapsed_seconds':time.monotonic()-started,'targets':list(per_target.values())})
                round_+=1
                if not changed and not any(not a.get('pending') for a in attempts[-18:]):
                    self.evaluator.roads.wait_for_discovery(getattr(self.evaluator.roads,'discovery_revision',0),.5)
        # Keep complete, feasible lower-score campaigns for controlled native
        # restarts and combine their source-dated blocks in the full search.
        unique={tuple(v['ids']):v for v in footholds}
        alternatives=sorted(unique.values(),key=lambda v:v['score']['species'],reverse=True)[:9]
        result={'ids':current,'score':best,'initial_species':baseline,'accepted':accepted,
            'elapsed_seconds':time.monotonic()-started,'targets':list(per_target.values()),
            'all_opportunities_available':len(self.records),'full_model_sha256':sha256(self.model.path),
            'blocks':list(self.library.values()),'feasible_alternative_calendars':alternatives,
            'policy':'source-dated-target-regional-campaigns-v2','native_joint_evaluation':True}
        dump(self.output/'targeted-exploration.json',result)
        self.save_library(self.output/'targeted-blocks.txt')
        return result
