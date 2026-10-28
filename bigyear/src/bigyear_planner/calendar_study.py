"""Controlled nearby polishing and hard-anchor calendar reconstruction.

Proposal scores never change the one-species/one-point final objective. Every
accepted calendar uses the ordinary full-model native feasibility evaluator.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import numpy as np

from .national_runner import export_native,selected_problem
from .regional_roads import DeferredRoad
from .regional_exploration import NativeEvaluator,RegionalExplorer
from .runner import compile_engine
from .targeted_exploration import TargetedExplorer
from .util import dump,read,sha256


def superior(a,b):
    if a.get('pending') or not a.get('feasible'):return False
    if b.get('pending') or not b.get('feasible'):return True
    if a['species']!=b['species']:return a['species']>b['species']
    if abs(a['minutes']-b['minutes'])>1e-7:return a['minutes']<b['minutes']
    return a['miles']+1e-7<b['miles']


def verify_fresh_calendar(model,roads,output,ids,timeout=300):
    """Recheck without the old native client's hot costs or transit entries.

    This is preparation, not a second search. Bounded provider entries can be
    evicted while the long-lived evaluator still has their verified costs.
    A fresh client forces any missing selected costs to be measured before the
    complete-seed materializer starts its intentionally tiny native run.
    """
    started=time.monotonic()
    with NativeEvaluator(model,roads,output).open() as evaluator:
        score=evaluator.evaluate(ids,timeout=timeout)
        if score['pending'] or not score['feasible']:
            raise ValueError('selected calendar did not pass fresh-client preparation')
        problem=selected_problem(model,roads,ids)
        routing=problem.get('routing_locations',problem['compact_source']['selected_locations'])
        pairs=set();groups={}
        for o in problem['opportunities']:groups.setdefault(o['day'],[]).append(o['location'])
        previous=None
        for locations in groups.values():
            pairs.update((routing[a],routing[b]) for a,b in zip(locations,locations[1:]) if a!=b)
            if previous is not None and previous!=locations[0]:
                a,b=routing[previous],routing[locations[0]]
                if roads.cached_costs(a,b) is not None:pairs.add((a,b))
            previous=locations[-1]
        for window in problem.get('window_transitions',[]):
            for segment in window['plan']['segments']:
                if segment['mode']=='road' and segment['from']!=segment['to']:
                    pairs.add((routing[segment['from']],routing[segment['to']]))
        deadline=time.monotonic()+timeout
        for a,b in sorted(pairs):
            while True:
                revision=roads.discovery_revision
                try:
                    roads.costs(a,b)
                    if not roads.store.pin_current(a,b):raise ValueError('selected car cost is not measured finite')
                    break
                except DeferredRoad:
                    if time.monotonic()>=deadline:raise ValueError('selected car cost preparation timed out')
                    roads.wait_for_discovery(revision,.25)
        handoff=roads.begin_phase('fresh-calendar-prepared')
    dump(Path(output)/'fresh-calendar.json',{'score':score,'ids':list(map(int,ids)),
        'wall_seconds':time.monotonic()-started,'model_sha256':sha256(model.path),
        'network_sha256':roads.manifest['network_sha256'],'handoff':handoff,
        'search_performed':False,'selected_costs_pinned':len(roads.store.pinned),
        'pin_capacity':roads.store.pin_capacity,
        'selected_road_costs':[[a,b,*roads.store.pinned[a,b]] for a,b in sorted(pairs)]})
    return score


def native_session(model,roads,output,initial,seconds,required=(),seed=20240101,blocks=()):
    """Search while the caller owns an open bridge; freeze its exact inputs."""
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    executable=compile_engine();snapshot=output/'engine-snapshot';shutil.copyfile(executable,snapshot)
    snapshot.chmod(0o755)
    native=output/'problem.by';export_native(model,roads,native)
    dump(output/'warm-start.json',list(map(int,initial)))
    env=dict(os.environ)
    for key in ['BIGYEAR_BLOCK_PROPOSALS','BIGYEAR_REQUIRED_OPPORTUNITIES']:env.pop(key,None)
    if required:
        path=output/'required.txt';path.write_text('\n'.join(map(str,required))+'\n')
        env['BIGYEAR_REQUIRED_OPPORTUNITIES']=str(path.resolve())
    if blocks:
        path=output/'blocks.txt'
        path.write_text(''.join(f'{b["begin"]} {b["end"]} {len(b["ids"])} '+
            ' '.join(map(str,b['ids']))+'\n' for b in blocks[-512:]))
        env['BIGYEAR_BLOCK_PROPOSALS']=str(path.resolve())
    started=time.monotonic()
    try:
        with (output/'engine.log').open('w') as log:
            process=subprocess.Popen([str(snapshot),str(native),str(output),str(max(.01,seconds)),
                '2',str(seed),'3','12','0',str(output/'warm-start.json')],env=env,stdout=log,stderr=log)
            try:
                _,status,usage=os.wait4(process.pid,0);process.returncode=os.waitstatus_to_exitcode(status)
            except BaseException:
                process.terminate();process.wait(timeout=10)
                raise
        if process.returncode:raise RuntimeError('calendar study native search failed: '+str(output))
        result=read(output/'engine.json')
        if not set(required)<=set(result['selected']):raise ValueError('tentpole was lost in native search')
        dump(output/'binding.json',{'model_sha256':sha256(model.path),'network_sha256':roads.manifest['network_sha256'],
            'engine_sha256':sha256(snapshot),'input_sha256':sha256(native),'seconds':seconds,'seed':seed,
            'threads':2,'required':list(required),'wall_seconds':time.monotonic()-started,
            'peak_rss_bytes':usage.ru_maxrss,'cpu_seconds':usage.ru_utime+usage.ru_stime,
            'expired_bridge_credentials_removed':True})
        return result
    finally:native.unlink(missing_ok=True)


class NearbyPolisher(RegionalExplorer):
    def __init__(self,*args,reference_route=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.reference_lines={};self.reference_binding=None
        self.corridor_distance=None;self.corridor_computed=None
        if reference_route is None:return
        from shapely.geometry import LineString
        path=Path(reference_route);route=read(path)
        self.reference_binding={'path':str(path.resolve()),'sha256':sha256(path)}
        previous=-1
        def line(leg):
            coordinates=np.asarray(leg['geometry']['coordinates'],dtype=float)
            cosine=float(np.cos(np.radians(coordinates[:,1].mean())))
            shape=LineString(np.column_stack([coordinates[:,0]*69.1*cosine,coordinates[:,1]*69.1]))
            return shape,cosine
        for day,entry in enumerate(route['days']):
            if not entry['stops']:continue
            # The whole transfer gap is available for rescheduling drives.
            # Source records need not fall on the old late-scheduled drive day.
            transfers={}
            for d in range(previous+1,day):
                for leg in route['days'][d]['legs']:
                    if leg['mode']=='road' and leg.get('geometry'):
                        transfers.setdefault((leg['from'],leg['to']),leg)
            lines=[line(leg) for leg in transfers.values()]
            for d in range(previous+1,day):self.reference_lines.setdefault(d,[]).extend(lines)
            for leg in entry['legs']:
                if leg['mode']=='road' and leg.get('geometry'):
                    self.reference_lines.setdefault(day,[]).append(line(leg))
            previous=day
        # A linear, fixed-size proposal-distance index, not a road matrix.
        self.corridor_distance=np.full(len(self.records),np.inf)
        self.corridor_computed=np.zeros(len(self.records),dtype=bool)

    def corridor_distances(self,day,ids,points):
        if self.corridor_distance is None:return np.full(len(ids),np.inf)
        import shapely
        pending=~self.corridor_computed[ids]
        if pending.any():
            source=points[pending];distance=np.full(len(source),np.inf)
            for line,cosine in self.reference_lines.get(day,[]):
                x,y=source[:,1]*69.1*cosine,source[:,0]*69.1
                a,b,c,d=line.bounds
                at=np.flatnonzero((x>=a-30)&(x<=c+30)&(y>=b-30)&(y<=d+30))
                if len(at):
                    distance[at]=np.minimum(distance[at],shapely.distance(shapely.points(x[at],y[at]),line))
            self.corridor_distance[ids[pending]]=distance;self.corridor_computed[ids[pending]]=True
        return self.corridor_distance[ids]

    def proposals(self,current):
        seen=self.seen(current);novelty=self.novelty(seen)
        candidates=np.flatnonzero((novelty>0)&(self.records['reserved']==0))
        groups={}
        for i in current:groups.setdefault(int(self.records[i]['day']),[]).append(i)
        dates=sorted(groups)
        proposals=[]
        for day in sorted(set(map(int,self.records['day'][candidates]))):
            ids=candidates[self.records['day'][candidates]==day]
            before=[d for d in dates if d<=day];after=[d for d in dates if d>=day]
            anchors=list(groups.get(day,[]))
            if before:anchors.append(groups[before[-1]][-1])
            if after:anchors.append(groups[after[0]][0])
            if not anchors:continue
            points=self.coords[self.records['location'][ids]]
            distance=np.full(len(ids),np.inf)
            for a in set(anchors):
                center=self.coords[int(self.records[a]['location'])]
                dist=np.hypot((points[:,0]-center[0])*69.1,
                    (points[:,1]-center[1])*69.1*np.cos(np.radians(center[0])))
                distance=np.minimum(distance,dist)
            distance=np.minimum(distance,self.corridor_distances(day,ids,points))
            for at in np.flatnonzero(distance<=30):
                i=int(ids[at]);proposals.append((int(novelty[i]),float(distance[at]),i))
        # Source frequency is not used: prioritize marginal distinct coverage,
        # then smaller geometric detour. Full roads decide actual feasibility.
        return sorted(proposals,key=lambda p:(-p[0],p[1],int(self.records[p[2]]['day']),p[2]))

    def polish(self,initial,seconds=300,required=()):
        started=time.monotonic();deadline=started+seconds
        current=self.canonical(initial);best=self.evaluator.evaluate(current,timeout=min(30,seconds))
        if best['pending'] or not best['feasible']:raise ValueError('nearby polish requires feasible seed')
        attempts=[];accepted=[];tested=set();round_=0
        with (self.output/'nearby-attempts.jsonl').open('w') as log:
            while time.monotonic()<deadline:
                round_+=1;changed=False;pending=False;proposals=self.proposals(current)
                for gain,distance,head in proposals:
                    if time.monotonic()>=deadline:break
                    candidate=self.canonical([*current,head]);key=tuple(candidate)
                    if key in tested:continue
                    score=self.evaluator.evaluate(candidate,timeout=min(2,max(.01,deadline-time.monotonic())))
                    entry={'head':head,'potential_gain':gain,'air_miles':distance,'round':round_,**score}
                    attempts.append(entry);log.write(json.dumps(entry)+'\n');log.flush()
                    if score['pending']:pending=True
                    else:tested.add(key)
                    if superior(score,best):
                        if not set(required)<=set(candidate):raise ValueError('nearby polish lost anchor')
                        current,best=candidate,score;accepted.append(entry);changed=True
                        dump(self.output/'nearby-checkpoint.json',current)
                        print(json.dumps({'stage':'nearby-improvement',**entry}),flush=True)
                        break
                if not changed:
                    if not pending:break
                    self.evaluator.roads.wait_for_discovery(getattr(self.evaluator.roads,'discovery_revision',0),.25)
        result={'ids':current,'score':best,'attempts':len(attempts),'pending_attempts':sum(a['pending'] for a in attempts),
            'accepted':accepted,'rounds':round_,'elapsed_seconds':time.monotonic()-started,
            'reference_corridors':self.reference_binding,
            'scope':'Missing-species source dates within approximately30 miles of that day/bracketing exact visits or measured baseline ground corridors throughout their transfer gaps. Corridor geometry guides proposals only; each entire current calendar is rechecked with measured roads.'}
        dump(self.output/'nearby-result.json',result)
        return result


class TentpoleExplorer(TargetedExplorer):
    def footholds(self,initial,seconds=180,limit=4,island_only=False):
        started=time.monotonic();deadline=started+seconds
        baseline=self.evaluator.evaluate(initial,timeout=min(30,seconds));attempts=[];feasible=[]
        plans=[]
        for target,s in zip(self.targets,self.target_indices):
            heads=self.heads(initial,s,limit=12)
            if island_only:heads=[i for i in heads if int(self.records[i]['reserved'])>0]
            # Actual dated alternatives are selected once per scenario, then
            # stay hard requirements throughout its own native reconstruction.
            for rank,head in enumerate(heads):
                for radius in [0,3,7,14,21,28]:plans.append((rank,radius,target,int(s),head))
        # Test all targets and distinct dates before expanding replacement
        # windows. Penalized current-score footholds are retained, not dropped.
        plans.sort(key=lambda x:(x[0],x[1],self.targets.index(x[2])))
        with (self.output/'tentpole-construction.jsonl').open('w') as log:
            for rank,radius,target,s,head in plans:
                if time.monotonic()>=deadline:break
                proposal=self.campaign(initial,head,radius)
                alternatives=[('regional',proposal['ids'])]
                minimal=self.canonical([*proposal['outside'],head])
                if minimal!=proposal['ids']:alternatives.append(('anchor_only',minimal))
                for construction,calendar in alternatives:
                    if time.monotonic()>=deadline:break
                    score=self.evaluator.evaluate(calendar,timeout=min(2,max(.01,deadline-time.monotonic())))
                    entry={'target':target['species_id'],'name':target['species'],'head':head,
                        'begin':proposal['begin'],'end':proposal['end'],'radius':radius,
                        'construction':construction,**score}
                    attempts.append(entry);log.write(json.dumps(entry)+'\n');log.flush()
                    if score['feasible'] and not score['pending']:
                        feasible.append({**entry,'ids':calendar,'required':[head]})
                        break
        # Prefer diverse target species. Low-score scenarios still receive a
        # reserved recovery budget instead of requiring an immediate gain.
        feasible.sort(key=lambda p:(-p['species'],p['minutes'],p['head']))
        selected=[];used=set()
        for p in feasible:
            if p['target'] not in used:selected.append(p);used.add(p['target'])
            if len(selected)==limit:break
        for p in feasible:
            if len(selected)>=limit:break
            if p not in selected:selected.append(p)
        result={'baseline':baseline,'attempts':len(attempts),'feasible_calendars':len(feasible),
            'scenarios':selected,'elapsed_seconds':time.monotonic()-started,
            'policy':'Hard exact dated visit for each alternative scenario; ordinary unique-species objective. No credit for unsupported dates, no artificial rarity points.'}
        dump(self.output/'tentpole-footholds.json',result)
        return result
