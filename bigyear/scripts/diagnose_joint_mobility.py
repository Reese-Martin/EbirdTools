"""Describe the remaining source pool without attributing an unknown record list.

Counts are exact dated opportunity support. Proximity is a straight-line ranking
signal and never an assertion that another visit fits the routed calendar.
"""
import argparse
from collections import Counter
from datetime import date,timedelta
import json
from pathlib import Path
import time

import numpy as np

from bigyear_planner.compact_model import load
from bigyear_planner.util import dump,read,sha256,write_csv


def diagnose(folder):
    folder=Path(folder).resolve();r=read(folder/'route.json');p=read(folder/'problem.json')
    if not read(folder/'validation.json')['valid']:raise ValueError('diagnostics require a verified route')
    started=time.monotonic();model=load(p['compact_source']['path'])
    try:
        nsp=len(model.meta['species']);seen={s for d in r['days'] for stop in d['stops'] for s in stop['species']}
        missing={i for i,s in enumerate(model.meta['species']) if s['id'] not in seen}
        on_route=np.zeros(nsp,dtype=bool)
        for i,s in enumerate(model.meta['species']):on_route[i]=s['id'] in seen
        counts=np.zeros((366,nsp),dtype=np.int64);marine=np.zeros_like(counts)
        states=[*model.meta['candidate_scope']['states'],'unknown'];state_index={s:i for i,s in enumerate(states)}
        location_states=np.empty(model.meta['locations'],dtype=np.uint8)
        for idx,state in model.locations.db.execute("SELECT idx,json_extract(metadata,'$.state') FROM locations"):
            location_states[idx]=state_index.get(state,len(states)-1)
        land_states=np.zeros((nsp,len(states)),dtype=np.int64)
        catalog=read(model.meta['joint_marine']['catalog_path'])
        if any(len(t['observations'])!=1 for t in catalog['trips']):raise ValueError('proximity diagnostic expects single-pin charter variants')
        marine_pins=np.asarray([[0.,0.],*[[t['observations'][0]['lat'],t['observations'][0]['lon']] for t in catalog['trips']]])
        visited={r['locations'][s['location']]['state'] for d in r['days'] if d['kind']=='birding' for s in d['stops']}
        minimum=np.full(nsp,np.inf);within30=np.zeros(nsp,dtype=np.int32)
        position=model.coords[int(model.records[r['search']['selected_model_opportunities'][0]]['location'])].copy()
        # Calendar end-position coordinates: road anchors, airport anchors,
        # same-port boat returns. Long-road progress follows path length.
        for daynum,day in enumerate(r['days']):
            if day['legs']:
                last=day['legs'][-1]
                if last['mode']=='road' and last['end_fraction']<1:
                    coords=np.asarray(last['geometry']['coordinates'],dtype=float)
                    delta=np.diff(coords,axis=0)*[np.cos(np.radians(coords[:-1,1])).mean(),1]
                    lengths=np.hypot(delta[:,0],delta[:,1]);cumulative=np.r_[0,np.cumsum(lengths)]
                    target=last['end_fraction']*cumulative[-1];j=min(len(lengths)-1,np.searchsorted(cumulative,target,side='right')-1)
                    q=(target-cumulative[j])/lengths[j] if lengths[j] else 0
                    point=coords[j]+q*(coords[j+1]-coords[j]);position=np.asarray([point[1],point[0]])
                else:
                    loc=r['locations'][last['to']];anchor=loc.get('road_access',loc)
                    position=np.asarray([anchor['lat'],anchor['lon']])
            elif day['kind']=='birding' and day['stops']:
                loc=r['locations'][day['stops'][-1]['location']];anchor=loc.get('road_access',loc)
                position=np.asarray([anchor['lat'],anchor['lon']])
            lo,hi=np.searchsorted(model.records['day'],[daynum,daynum+1]);rows=model.records[lo:hi]
            if not len(rows):continue
            begin=int(rows[0]['offset']);end=int(rows[-1]['offset'])+int(rows[-1]['count'])
            spp=model.species[begin:end]
            counts[daynum]=np.bincount(spp,minlength=nsp)
            locations=np.repeat(rows['location'],rows['count'].astype(np.intp))
            variants=np.repeat(rows['reserved'],rows['count'].astype(np.intp));ismarine=variants>0
            marine[daynum]=np.bincount(spp[ismarine],minlength=nsp)
            indices=spp[~ismarine].astype(np.int64)*len(states)+location_states[locations[~ismarine]]
            land_states+=np.bincount(indices,minlength=nsp*len(states)).reshape(nsp,len(states))
            mask=(~on_route)[spp]
            if not mask.any():continue
            values=spp[mask];points=np.asarray(model.coords[locations[mask]]).copy()
            water=variants[mask]>0;points[water]=marine_pins[variants[mask][water]]
            lat1=np.radians(points[:,0]);lat2=np.radians(position[0]);dlon=np.radians(points[:,1]-position[1])
            h=np.sin((lat1-lat2)/2)**2+np.cos(lat1)*np.cos(lat2)*np.sin(dlon/2)**2
            distance=3958.7613*2*np.arcsin(np.sqrt(np.minimum(1,h)))
            np.minimum.at(minimum,values,distance)
            nearby=np.unique(values[distance<=30]);within30[nearby]+=1
        rows=[]
        for i in sorted(missing,key=lambda i:model.meta['species'][i]['name']):
            s=model.meta['species'][i];dates=np.flatnonzero(counts[:,i]);land=counts[:,i]-marine[:,i]
            rows.append({'species_id':s['id'],'species':s['name'],'dated_land_opportunities':int(land.sum()),
                'constructed_charter_options':int(marine[:,i].sum()),'available_dates':len(dates),
                'first_date':str(date(2024,1,1)+timedelta(days=int(dates[0]))),
                'last_date':str(date(2024,1,1)+timedelta(days=int(dates[-1]))),
                'land_states':'|'.join(states[j] for j in np.flatnonzero(land_states[i])),
                'days_within_30_air_miles_of_route_end_position':int(within30[i]),
                'closest_route_end_position_air_miles':round(float(minimum[i]),3)})
        write_csv(folder/'uncollected-species.csv',rows,list(rows[0]))
        base=read(folder.parent/'marine-charter-2024-v2/route.json')
        old={s for d in base['days'] for stop in d['stops'] for s in stop['species']}
        names={s['id']:s['name'] for s in r['species']}
        gains=[{'species_id':s,'species':names[s],'change':'gained'} for s in sorted(seen-old)]
        losses=[{'species_id':s,'species':names[s],'change':'lost'} for s in sorted(old-seen)]
        if gains or losses:write_csv(folder/'species-changes.csv',gains+losses,['species_id','species','change'])
        kinds=Counter(d['kind'] for d in r['days']);engine=read(folder/'engine.json')
        data=read(folder/'benchmark.json');resource_path=folder.parent/(folder.name+'-resources.json')
        resources=read(resource_path) if resource_path.exists() else None
        result={'route_sha256':sha256(folder/'route.json'),'species':r['species_total'],'target':759,
            'gap':759-r['species_total'],'pool':nsp,'uncollected_pool_species':len(missing),
            'uncollected_with_at_most_7_recorded_dates':sum(x['available_dates']<=7 for x in rows),
            'uncollected_with_at_least_one_date_within_30_air_miles':sum(x['days_within_30_air_miles_of_route_end_position']>0 for x in rows),
            'uncollected_marine_only':sum(x['dated_land_opportunities']==0 for x in rows),
            'uncollected_only_in_unvisited_land_states':int(sum(bool(land_states[i].any()) and
                not any(land_states[i,state_index[s]] for s in visited) and marine[:,i].sum()==0 for i in missing)),
            'remaining_land_taxa_by_state':{state:int(sum(land_states[i,j]>0 for i in missing))
                for j,state in enumerate(states) if state!='unknown'},
            'gains_vs_667':gains,'losses_vs_667':losses,'calendar_days':dict(kinds),
            'activity_hours':sum(d['activity_minutes'] for d in r['days'])/60,
            'birding_hours':sum(s.get('birding_minutes',120) for d in r['days'] for s in d['stops'])/60,
            'walking_hours':sum(d.get('walking_minutes',0) for d in r['days'])/60,
            'boarding_hours':sum(d.get('boarding_minutes',0) for d in r['days'])/60,
            'flight_hours':sum(l['minutes'] for d in r['days'] for l in d['legs'] if l['mode']=='flight')/60,
            'road_hours':sum(l['minutes'] for d in r['days'] for l in d['legs'] if l['mode']=='road')/60,
            'boat_hours':sum(l['minutes'] for d in r['days'] for l in d['legs'] if l['mode']=='boat')/60,
            'states_birded':sorted({r['locations'][s['location']]['state'] for d in r['days'] if d['kind']=='birding' for s in d['stops']}),
            'engine':{k:engine[k] for k in ['candidate_evaluations','deferred_candidates','screened_before_routing','regional_block_proposals','regional_block_feasible','elapsed_seconds']},
            'benchmark':data,'resources':resources,'diagnostic_seconds':time.monotonic()-started,
            'limitations':['The 759-species dated recordholder list is unavailable. Species-specific data absence versus recordholder search/access cannot be assigned.',
                'The pool exceeds 759 but does not establish a feasible 759-species calendar or prove that the recordholder\'s particular taxa are all present.',
                'Proximity compares exact recorded land or marine observation pins with the same day\'s route end position. Ports are not substituted for offshore pins. It is a ranking diagnostic; no road, water, walking or time feasibility is implied.',
                'Charters cover the previously constructed single-pin, incumbent-gain-focused options at nine modeled ports. Four additional port references remain held. The full offshore source archive is retained.',
                '12-hour activity, two-hour visits, full flight days with separate access/egress days, no island landings and current-snapshot roads remain modeling assumptions.',
                'All-observer coverage is a complete terrestrial cohort plus partial constructed marine options; it is not a complete ocean census.']}
        inputs=folder/'target-inputs.json'
        if inputs.exists():
            target_inputs=read(inputs);previous=Path(target_inputs['baseline_run'])
            if sha256(previous/'route.json')!=target_inputs['baseline_route_sha256']:
                raise ValueError('target campaign baseline changed')
            baseline=read(previous/'route.json')
            previous_seen={s for d in baseline['days'] for stop in d['stops'] for s in stop['species']}
            changes=[{'species_id':s,'species':names[s],'change':'gained'} for s in sorted(seen-previous_seen)]
            changes += [{'species_id':s,'species':names[s],'change':'lost'} for s in sorted(previous_seen-seen)]
            write_csv(folder/'incumbent-species-changes.csv',changes,['species_id','species','change'])
            stats={t['species_id']:t for t in read(folder/'targeted-exploration.json')['targets']}
            attempts=[json.loads(line) for line in (folder/'targeted-attempts.jsonl').read_text().splitlines()]
            target_rows=[]
            for t in target_inputs['targets']:
                sid=t['species_id'];visits=[(d,stop) for d in r['days'] for stop in d['stops'] if sid in stop['species']]
                d,stop=visits[0] if visits else (None,None)
                measured=[v for v in attempts if v['target']==sid and not v['pending']]
                feasible=[v['species'] for v in measured if v['feasible']]
                target_rows.append({'species_id':sid,'species':t['species'],'collected':bool(visits),
                    'first_collection_date':d['date'] if d else '',
                    'state':r['locations'][stop['location']]['state'] if stop else '',
                    'latitude':stop['lat'] if stop else '','longitude':stop['lon'] if stop else '',
                    'available_dates':t['available_dates'],'support_states':t['land_states'],
                    'measured_feasible_campaigns':stats[sid]['feasible_campaigns'],
                    'campaign_evaluations':stats[sid]['attempts'],'pending_campaign_evaluations':stats[sid]['pending_campaigns'],
                    'best_measured_campaign_species':max(feasible) if feasible else '',
                    'measured_infeasible_campaigns':sum(not v['feasible'] for v in measured),
                    'outcome':'Collected' if visits else 'Feasible alternative measured' if feasible else
                        'Routing unresolved in tested campaigns' if stats[sid]['pending_campaigns'] else 'Tested calendars infeasible'})
            write_csv(folder/'targeted-species-results.csv',target_rows,list(target_rows[0]))
            result.update({'target_baseline_species':baseline['species_total'],
                'gains_vs_incumbent':[v for v in changes if v['change']=='gained'],
                'losses_vs_incumbent':[v for v in changes if v['change']=='lost'],
                'targeted_species_collected':sum(t['collected'] for t in target_rows),'targeted_species':target_rows,
                'target_attempts_sha256':sha256(folder/'targeted-attempts.jsonl')})
        dump(folder/'gap-analysis.json',result)
        print(json.dumps({k:v for k,v in result.items() if k not in ['benchmark','resources','engine','gains_vs_667','losses_vs_667']}),flush=True)
        return result
    finally:model.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);args=parser.parse_args();diagnose(args.folder)
