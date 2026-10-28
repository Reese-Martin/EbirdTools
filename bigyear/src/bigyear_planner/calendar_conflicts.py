"""Conservative pair conflicts under the current native day-based calendar.

This bounds the frozen candidate model, not a real ABA big year. It allows all
flights when three transit days exist, ignores access failures, and gives both
birding dates their entire local-mile allowance plus maximum walking slack.
"""
from datetime import date,timedelta
import math
from pathlib import Path

import numpy as np

from .compact_model import load
from .util import dump,read,sha256,write_csv


def pair_conflict(a,b,config):
    """Support rows: (opportunity_id, day, lat, lon, charter_variant)."""
    if set(a[:,0].astype(int))&set(b[:,0].astype(int)):return None
    # Any pair separated by >=4 dates is optimistically allowed a full flight
    # day plus separate ground-access days. Actual service is not required.
    if max(abs(a[:,1].max()-b[:,1].min()),abs(b[:,1].max()-a[:,1].min()))>=4:return None
    maximum_walk=(config['activity_minutes']-config['birding_minutes'])*config['walking_speed_kmh']/120
    # A relaxed road/birding date can use its driving-time allowance rather
    # than the legacy 30-mile radius. Never carry the old conflict certificate
    # into this larger feasible set.
    road_slack=(min(config['drive_minutes'],config['activity_minutes']-config['birding_minutes'])
                /60*200 if config.get('mixed_road_birding',False) else config['local_miles']*1.609344)
    local_slack=2*road_slack+2*maximum_walk
    minimum=math.inf;checked=0;exclusive=0
    for begin in range(0,len(a),64):
        left=a[begin:begin+64];delta=np.abs(left[:,None,1]-b[None,:,1])
        forbidden=(delta==0)&((left[:,None,4]>0)|(b[None,:,4]>0))
        lat1=np.radians(left[:,None,2]);lat2=np.radians(b[None,:,2]);dlon=np.radians(left[:,None,3]-b[None,:,3])
        h=np.sin((lat1-lat2)/2)**2+np.cos(lat1)*np.cos(lat2)*np.sin(dlon/2)**2
        distance=6371.0088*2*np.arcsin(np.sqrt(np.minimum(1,h)))
        # 200 km/h is deliberately far above the frozen 130 km/h car profile,
        # with generous slack for reported time rounding. Car travel on both birding
        # dates is also allowed optimistically, even for exclusive charters.
        allowed=np.maximum(0,delta-1)*config['drive_minutes']/60*200+local_slack
        margin=(distance-allowed)[~forbidden]
        if len(margin) and margin.min()<=0:return None
        if len(margin):minimum=min(minimum,float(margin.min()))
        checked+=int(distance.size);exclusive+=int(forbidden.sum())
    return {'all_source_pairs_incompatible':True,'source_pairs_checked':checked,
        'same_date_charter_exclusive_pairs':exclusive,
        'minimum_car_distance_margin_km':minimum if math.isfinite(minimum) else None}


def clique_cover(adjacency,seed=2024,attempts=500):
    """Disjoint cliques are a directly checkable species-count certificate."""
    rng=np.random.default_rng(seed);nodes=set(adjacency);best=[[v] for v in sorted(nodes)]
    for _ in range(attempts):
        remaining=set(nodes);groups=[]
        while remaining:
            ranked=sorted(remaining,key=lambda v:(len(adjacency[v]&remaining),rng.random()),reverse=True)
            v=ranked[0];clique=[v];candidates=adjacency[v]&remaining
            while candidates:
                v=max(sorted(candidates),key=lambda u:(len(adjacency[u]&candidates),rng.random()))
                clique.append(v);candidates&=adjacency[v]
            groups.append(sorted(clique));remaining-=set(clique)
        if len(groups)<len(best):best=groups
    return [g for g in best if len(g)>1]


def analyze(model_path,release_path,output):
    model=load(model_path);output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    try:
        config=model.meta['config'];release=read(release_path)
        if config['year']!=2024 or release['parameters']['vmax']!=130:
            raise ValueError('calendar certificate requires the frozen 2024 / 130-km/h model')
        nspp=len(model.meta['species']);first=np.full(nspp,9999);last=np.full(nspp,-1)
        for d in range(366):
            lo,hi=np.searchsorted(model.records['day'],[d,d+1]);rows=model.records[lo:hi]
            if not len(rows):continue
            start=int(rows[0]['offset']);end=int(rows[-1]['offset'])+int(rows[-1]['count'])
            present=np.unique(model.species[start:end]);first[present]=np.minimum(first[present],d);last[present]=d
        narrow=(last-first<=6)&(last>=0);supports={int(i):[] for i in np.flatnonzero(narrow)}
        for d in range(366):
            lo,hi=np.searchsorted(model.records['day'],[d,d+1]);rows=model.records[lo:hi]
            if not len(rows):continue
            start=int(rows[0]['offset']);end=int(rows[-1]['offset'])+int(rows[-1]['count'])
            spp=model.species[start:end];mask=narrow[spp]
            if not mask.any():continue
            ids=np.repeat(np.arange(lo,hi,dtype=np.uint32),rows['count'].astype(np.intp))[mask]
            for species in np.unique(spp[mask]):supports[int(species)].extend(map(int,ids[spp[mask]==species]))
        arrays={}
        for s,ids in supports.items():
            arrays[s]=np.asarray([[i,int(model.records[i]['day']),*model.coords[int(model.records[i]['location'])],
                int(model.records[i]['reserved'])] for i in ids],dtype=float)
        adjacency={s:set() for s in arrays};edges=[]
        for a in sorted(arrays):
            for b in sorted(arrays):
                if b<=a:continue
                proof=pair_conflict(arrays[a],arrays[b],config)
                if proof:
                    adjacency[a].add(b);adjacency[b].add(a)
                    edges.append({'a':model.meta['species'][a]['id'],'b':model.meta['species'][b]['id'],
                        'a_name':model.meta['species'][a]['name'],'b_name':model.meta['species'][b]['name'],**proof})
        cover=clique_cover(adjacency);loss=sum(len(g)-1 for g in cover)
        # Verify disjointness and every clique edge independently of the
        # greedy cover's order; no matching optimality is claimed.
        used=set()
        for group in cover:
            if used&set(group) or any(b not in adjacency[a] for a in group for b in group if a!=b):
                raise ValueError('invalid count-bound clique certificate')
            used.update(group)
        rows=[]
        for i,group in enumerate(cover,1):
            for s in group:
                item=model.meta['species'][s]
                rows.append({'group':i,'species_id':item['id'],'species':item['name'],
                    'first_date':str(date(2024,1,1)+timedelta(days=int(first[s]))),
                    'last_date':str(date(2024,1,1)+timedelta(days=int(last[s]))),
                    'source_opportunities':len(arrays[s]),'maximum_species_from_group':1})
        if rows:write_csv(output/'calendar-conflict-groups.csv',rows,list(rows[0]))
        result={'schema':1,'valid':True,'model_path':str(model.path),'model_sha256':sha256(model.path),
            'components':model.meta['components'],'road_release_sha256':sha256(release_path),
            'analysis_source_sha256':sha256(__file__),'effective_flights_enabled':True,
            'species_pool':nspp,'conservative_upper_bound':nspp-loss,'unavoidable_omissions_at_least':loss,
            'narrow_calendar_taxa_examined':len(arrays),'conflict_edges':edges,
            'cliques':[[model.meta['species'][s]['id'] for s in g] for g in cover],
            'support_opportunities':{model.meta['species'][s]['id']:supports[s] for s in supports},
            'policy':{'maximum_car_speed_kmh':200,'frozen_car_vmax':130,
                'both_birding_days_get_full_local_road_allowance':not config.get('mixed_road_birding',False),
                'both_birding_days_get_full_driving_time_allowance':config.get('mixed_road_birding',False),
                'full_maximum_anchor_walk_slack_at_both_ends':True,'three_intervening_days_allow_any_flight':True,
                'exclusive_charter_dates':True},
            'scope':'Upper bound for the frozen complete land plus partial constructed marine candidate model under its current day-based flight/drive/charter calendar. It is not a real-world ABA count bound. Other route, quality and access constraints are relaxed. All species not in the disjoint cliques are optimistically counted.',
            'target_759_ruled_out':nspp-loss<759}
        dump(output/'calendar-conflicts.json',result)
        print({k:result[k] for k in ['species_pool','conservative_upper_bound','unavoidable_omissions_at_least','narrow_calendar_taxa_examined','target_759_ruled_out']},flush=True)
        return result
    finally:model.close()
