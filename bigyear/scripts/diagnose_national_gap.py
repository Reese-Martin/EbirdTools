"""Read-only search diagnostics over the full, frozen ABA 2024 model.

Availability and proximity do not prove route feasibility. No observation,
scoring decision, route, or routing cache is changed by this analysis.
"""

from collections import Counter
import csv
from datetime import date,timedelta
import io
import json
import zipfile

import numpy as np

from bigyear_planner.compact_model import load
from bigyear_planner.util import ROOT,dump,read,sha256,write_csv

TAG='aba2024-aligned-flights-900s-600s'
OUT=ROOT/'runs/lower48-eod-2024'
DATA=ROOT/'data/lower48-aba2024-aligned'


def diagnose():
    folder=OUT/TAG;route=read(folder/'route.json');engine=read(folder/'engine.json')
    benchmark=read(folder/'benchmark.json');regional=read(folder/'regional-exploration.json')
    problem=read(folder/'problem.json')
    target=OUT/'aba2024-gap-diagnostics';target.mkdir(exist_ok=True)
    model=load(DATA/'model/model.json')
    try:
        if sha256(model.path)!=route['provenance']['compact_model_sha256']:
            raise ValueError('route and diagnostic model differ')
        if not read(folder/'validation.json')['valid']:
            raise ValueError('diagnostic requires independently validated route')
        seen={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
        missing=np.array([s['id'] not in seen for s in model.meta['species']])
        states=sorted(model.meta['candidate_scope']['states']);codes={s:i for i,s in enumerate(states)}
        location_state=np.zeros(model.meta['locations'],dtype='u1')
        for idx,state in model.locations.db.execute("SELECT idx,json_extract(metadata,'$.state') FROM locations"):
            location_state[idx]=codes[state]
        nsp=len(missing)
        support_counts=np.zeros((nsp,len(states)),dtype='u8')
        day_counts=np.zeros((nsp,366),dtype='u4')
        min_distance=np.full(nsp,np.inf)
        nearest_same_day=np.full(nsp,np.inf)
        same_pin_on_birding_day=np.zeros(nsp,dtype='u4')
        same_pin_on_rest_day=np.zeros(nsp,dtype='u4')
        by_day={};day_kinds={}
        current_location=None;rest_locations={}
        origin=date(2024,1,1)
        for day in route['days']:
            number=(date.fromisoformat(day['date'])-origin).days
            day_kinds[number]=day['kind']
            for leg in day.get('legs',[]):
                current_location=leg['to']
            for stop in day['stops']:
                loc=route['locations'][stop['location']]
                by_day.setdefault(number,[]).append([loc['lat'],loc['lon']])
                current_location=stop['location']
            if day['kind']=='rest' and current_location is not None:
                rest_locations[number]=route['locations'][current_location]['id']
        selected_locs={int(v['location']) for v in model.records[route['search']['selected_model_opportunities']]}
        dated_support=np.bincount(model.records['location'],minlength=model.meta['locations'])
        location_date_chances=1-(1-dated_support[list(selected_locs)]/len(model.records))**100
        visited=np.asarray([[l['lat'],l['lon']] for d in route['days'] for s in d['stops']
                            for l in [route['locations'][s['location']]]])
        visited=np.radians(visited)
        # Stream only uncollected supports. A species may have many source
        # records; the compact pair is an exact location/date/scoring unit.
        total_supports=0
        for begin in range(0,len(model.species),4_000_000):
            block=model.species[begin:begin+4_000_000]
            relative=np.flatnonzero(missing[block])
            if not len(relative):continue
            absolute=relative.astype('u8')+begin
            spp=block[relative]
            idx=np.searchsorted(model.records['offset'],absolute,side='right')-1
            records=model.records[idx];locs=records['location'];days=records['day']
            np.add.at(support_counts,(spp,location_state[locs]),1)
            np.add.at(day_counts,(spp,days),1)
            total_supports+=len(relative)
            for sp in np.unique(spp):
                chosen=np.flatnonzero(spp==sp);points=model.coords[locs[chosen]]
                for offset in range(0,len(points),2000):
                    chunk=np.radians(points[offset:offset+2000])
                    delta=chunk[:,None,:]-visited[None,:,:]
                    h=np.sin(delta[:,:,0]/2)**2+np.cos(chunk[:,None,0])*np.cos(visited[None,:,0])*np.sin(delta[:,:,1]/2)**2
                    nearest=float((3958.7613*2*np.arcsin(np.sqrt(np.minimum(1,h)))).min())
                    min_distance[sp]=min(min_distance[sp],nearest)
            for item,sp,loc,day in zip(idx,spp,locs,days):
                if int(loc) in selected_locs:
                    if int(day) in by_day:
                        # This is any previously visited exact pin, not
                        # necessarily the pin visited on this observation day.
                        same_pin_on_birding_day[sp]+=1
                    if int(day) in rest_locations and model.locations[int(loc)]['id']==rest_locations[int(day)]:
                        same_pin_on_rest_day[sp]+=1
                if int(day) in by_day:
                    point=np.radians(model.coords[int(loc)]);stops=np.radians(by_day[int(day)])
                    delta=stops-point
                    h=np.sin(delta[:,0]/2)**2+np.cos(stops[:,0])*np.cos(point[0])*np.sin(delta[:,1]/2)**2
                    distance=float((3958.7613*2*np.arcsin(np.sqrt(np.minimum(1,h)))).min())
                    nearest_same_day[sp]=min(nearest_same_day[sp],distance)
        rows=[]
        for sp in np.flatnonzero(missing):
            days=np.flatnonzero(day_counts[sp]);state_order=np.argsort(support_counts[sp])[::-1]
            rows.append({**model.meta['species'][sp],
                'eligible_date_location_pairs':int(support_counts[sp].sum()),'reported_days':len(days),
                'first_date':str(origin+timedelta(days=int(days[0]))),'last_date':str(origin+timedelta(days=int(days[-1]))),
                'states':'|'.join(states[i] for i in state_order if support_counts[sp,i]),
                'state_supports':json.dumps({states[i]:int(support_counts[sp,i]) for i in state_order if support_counts[sp,i]}),
                'nearest_visited_pin_miles_any_date':round(float(min_distance[sp]),3),
                'nearest_birding_pin_miles_same_date':round(float(nearest_same_day[sp]),3) if np.isfinite(nearest_same_day[sp]) else None,
                'supports_at_any_previously_visited_pin_on_a_birding_day':int(same_pin_on_birding_day[sp]),
                'supports_at_actual_overnight_pin_on_rest_day':int(same_pin_on_rest_day[sp])})
        rows.sort(key=lambda r:r['eligible_date_location_pairs'],reverse=True)
        write_csv(target/'uncollected-species.csv',rows,list(rows[0]))
        trials=[json.loads(l) for l in (folder/'regional-attempts.jsonl').read_text().splitlines()]
        modes=Counter('pending' if t['pending'] else ('feasible' if t['feasible'] else 'infeasible') for t in trials)
        rest_days=sum(d['kind']=='rest' for d in route['days'])
        activity=sum(d['activity_minutes'] for d in route['days'])
        deferred=engine['deferred_candidates'];evaluated=engine['candidate_evaluations']
        flight_plans=[v['plan'] for v in problem['window_transitions']
                      if any(s['mode']=='flight' for s in v['plan']['segments'])]
        taxonomy={r['source_name']:r for r in csv.DictReader((DATA/'taxonomy.csv').open())}
        provisional=[];section='main'
        with zipfile.ZipFile(DATA/'sources/ABA_Checklist-8.16.csv.zip') as archive:
            official=csv.reader(io.StringIO(archive.read('ABA_Checklist-8.16.csv').decode('utf-8-sig')))
            for r in official:
                if any('Provisional List A' in f for f in r):section='A'
                if any('Provisional List B' in f for f in r):section='B'
                if section!='main' and len(r)==6 and r[3] in taxonomy:
                    tax=taxonomy[r[3]]
                    provisional.append({'section':section,'source_name':r[3],'name':r[1],
                        'raw_2024_lower48_state_groups':int(tax['presence_rows']),
                        'current_scoring':tax['eod_presence_eligible'],
                        'review_status':'Needs 2024 provisional-rule and individual/population review; not automatic credit.'})
        write_csv(target/'provisional-rule-review.csv',provisional,list(provisional[0]))
        facts={'route_species':route['species_total'],'eligible_pool':len(model.meta['species']),
            'uncollected_species':len(rows),'uncollected_support_pairs':total_supports,
            'target':759,'species_shortfall':759-route['species_total'],
            'required_fraction_of_current_pool':759/len(model.meta['species']),
            'day_kinds':dict(Counter(d['kind'] for d in route['days'])),'rest_days':rest_days,
            'stops':sum(len(d['stops']) for d in route['days']),
            'total_activity_hours':activity/60,'daily_activity_capacity_hours':366*route['config']['activity_minutes']/60,
            'birding_hours':sum(len(d['stops']) for d in route['days'])*route['config']['birding_minutes']/60,
            'native_search_seconds':engine['elapsed_seconds'],'fully_evaluated_attempts':evaluated,
            'deferred_attempts':deferred,'deferred_fraction_of_evaluation_attempts':deferred/(deferred+evaluated),
            'python_road_queue_full_deferrals':benchmark['cache']['queue_full_deferrals'],
            'native_hot_entries':engine['road_cache']['hot_entries'],
            'native_hot_capacity':engine['road_cache']['hot_capacity'],
            'native_hot_evictions':engine['road_cache']['hot_evictions'],
            'successful_new_measured_road_queries':benchmark['cache']['successful_road_queries'],
            'regional_budget_seconds':900,'regional_actual_seconds':regional['elapsed_seconds'],
            'regional_attempts':regional['attempts'],'regional_attempt_outcomes':dict(modes),
            'regional_attempt_regions':dict(Counter(t['region'] for t in trials)),
            'regional_accepted_blocks':regional['accepted_blocks'],
            'native_block_proposals':engine['regional_block_proposals'],'native_feasible_blocks':engine['regional_block_feasible'],
            'date_move_probability_of_finding_any_matching_pin_in_100_global_draws':{
                'median':float(np.median(location_date_chances)),
                'mean':float(location_date_chances.mean()),'max':float(location_date_chances.max()),
                'note':'Optimistic: a matching draw can still select the unchanged current date.'},
            'flight_windows':len(flight_plans),'flight_window_calendar_days':sum(v['days'] for v in flight_plans),
            'flight_window_ground_days':sum(s['days'] for v in flight_plans for s in v['segments'] if s['mode']=='road'),
            'flight_window_travel_hours':sum(v['minutes'] for v in flight_plans)/60,
            'provisional_source_concepts_excluded_from_current_primary_only_policy':len(provisional),
            'provisional_rule_source':'https://www.aba.org/aba-checklist-committee-report-july-2024/',
            'marine_scope_rule_source':'https://www.aba.org/aba-recording-rules-and-interpretations/',
            'missing_species_with_same_date_pin_within_30_geodesic_miles':sum(r['nearest_birding_pin_miles_same_date'] is not None and r['nearest_birding_pin_miles_same_date']<=30 for r in rows),
            'missing_species_reported_at_actual_overnight_rest_pin':sum(r['supports_at_actual_overnight_pin_on_rest_day']>0 for r in rows),
            'frequent_missed_species':rows[:20],
            'provenance':{'route_sha256':sha256(folder/'route.json'),'model_sha256':sha256(model.path),
                'engine_sha256':sha256(folder/'engine.json'),'script_sha256':sha256(__file__)},
            'cautions':['Attempt counters include repeated attempts, not distinct candidates.',
                'Rest days do not prove spare feasible encounters: location, date, travel and eligible evidence still matter.',
                'Geodesic proximity is a diagnostic shortlist only; road miles, walking, budget and record validity remain unverified for these hypothetical additions.',
                'The current source/scoring pool is not the record-holder species list. This is not a decomposition of the exact 111-species difference.']}
        dump(target/'diagnosis.json',facts)
        print(json.dumps({k:v for k,v in facts.items() if k not in ['frequent_missed_species','provenance','cautions']}),flush=True)
    finally:
        model.close()


if __name__=='__main__':
    diagnose()
