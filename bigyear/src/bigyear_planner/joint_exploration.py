"""Boat-led calendar replacements, verified by the joint native evaluator."""
import json
import time

import numpy as np

from .regional_exploration import RegionalExplorer
from .util import dump


def explore_boats(model,evaluator,output,initial,seconds=240):
    helper=RegionalExplorer(model,evaluator,output)
    joint=model.meta['joint_marine'];current=helper.canonical(initial)
    score=evaluator.evaluate(current,timeout=120)
    if score['pending'] or not score['feasible']:raise ValueError('joint boat exploration needs a feasible seed')
    started=time.monotonic();deadline=started+seconds;attempted=set();accepted=[];blocks={};attempts=0;pending=set()
    options=[];byport={}
    for trip,i in joint['trip_opportunities'].items():
        d=int(helper.records[i]['day']);port=int(helper.records[i]['location'])
        entry=(i,d,port,trip);options.append(entry);byport.setdefault(port,[]).append(entry)
    with (output/'joint-boat-attempts.jsonl').open('w') as log:
        while time.monotonic()<deadline:
            proposals=[];seen=helper.seen(current);unique=set();portfolio_cache={};outside_cache={}
            for i,d,port,trip in options:
                if time.monotonic()>=deadline:break
                novel=tuple(np.flatnonzero(helper.seen([i])&~seen))
                if not novel:continue
                for radius in [0,1,3,7,14]:
                    begin=max(0,d-radius);end=min(helper.days,d+radius+1)
                    if (begin,end) not in outside_cache:
                        outside_cache[begin,end]=[v for v in current if not begin<=int(helper.records[v]['day'])<end]
                    outside=outside_cache[begin,end]
                    portfolios=[[i]]
                    if radius>=3:
                        # Whole multi-day port visits can pay for flights that
                        # no isolated rare bird could justify. Every chosen
                        # date/pin remains an original frozen charter option.
                        cachekey=(port,begin,end)
                        if cachekey not in portfolio_cache:
                            pool=[v[0] for v in byport[port] if begin<=v[1]<end]
                            chosen=[];covered=helper.seen(outside);used=set()
                            for _ in range(8):
                                ranked=[(int((helper.seen([v])&~covered).sum()),v) for v in pool
                                        if int(helper.records[v]['day']) not in used]
                                if not ranked:break
                                gain,v=max(ranked)
                                if not gain:break
                                chosen.append(v);covered|=helper.seen([v]);used.add(int(helper.records[v]['day']))
                            portfolio_cache[cachekey]=helper.canonical(chosen)
                        if len(portfolio_cache[cachekey])>1:portfolios.append(portfolio_cache[cachekey])
                    for visits in portfolios:
                        key=(tuple(current),tuple(visits),begin,end)
                        identity=(tuple(visits),begin,end)
                        if key in attempted or identity in unique:continue
                        unique.add(identity);candidate=helper.canonical([*outside,*visits])
                        count=int(helper.seen(candidate).sum())
                        if count<=score['species']:continue
                        proposals.append({'bound':count,'radius':radius,'trip':trip,'visits':visits,
                            'begin':begin,'end':end,'port':port,'month':d//31,'novel':novel,
                            'ids':candidate,'key':key})
            proposals.sort(reverse=True,key=lambda v:(v['bound'],-v['radius'],v['trip']))
            chosen=[];buckets=set()
            for p in proposals:
                bucket=(p['port'],p['month'],p['novel'],p['radius'],len(p['visits'])>1)
                if bucket not in buckets:buckets.add(bucket);chosen.append(p)
            changed=False;pending_this_round=0
            for p in chosen:
                if time.monotonic()>=deadline:break
                attempts+=1;result=evaluator.evaluate(p['ids'],timeout=min(6,deadline-time.monotonic()))
                log.write(json.dumps({'trip':p['trip'],'charters':len(p['visits']),'begin':p['begin'],'end':p['end'],
                    'potential_species':p['bound'],'elapsed_seconds':time.monotonic()-started,**result})+'\n');log.flush()
                if result['pending']:
                    pending.add((tuple(p['visits']),p['begin'],p['end']));pending_this_round+=1
                else:attempted.add(p['key'])
                if not result['pending'] and result['feasible']:
                    blocks[(p['begin'],p['end'],tuple(p['visits']))]={'begin':p['begin'],'end':p['end'],'ids':p['visits']}
                    if result['species']>score['species']:
                        current,score=p['ids'],result;accepted.append({'trip':p['trip'],'charters':len(p['visits']),**result})
                        print(json.dumps({'stage':'joint-boat-improvement','elapsed_seconds':time.monotonic()-started,**result}),flush=True)
                        changed=True;break
            if not chosen or not changed and not pending_this_round:break
            if not changed:evaluator.roads.wait_for_discovery(getattr(evaluator.roads,'discovery_revision',0),.5)
    result={'ids':current,'score':score,'elapsed_seconds':time.monotonic()-started,'attempts':attempts,
        'accepted':accepted,'pending_options':len(pending),'native_joint_evaluation':True,'blocks':list(blocks.values())}
    dump(output/'joint-boat-exploration.json',result)
    return result
