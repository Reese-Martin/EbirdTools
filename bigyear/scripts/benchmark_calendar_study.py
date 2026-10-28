"""Equal-budget 2024 nearby / tentpole / island / combined study arms."""
import argparse
from contextlib import ExitStack
import csv
import gc
import json
import shutil
import subprocess
import sys
import time

import numpy as np

from bigyear_planner.calendar_study import NearbyPolisher,TentpoleExplorer,native_session,superior,verify_fresh_calendar
from bigyear_planner.compact_model import load
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.util import ROOT,dump,read,sha256
from evaluate_lower48 import OUT,DATA as NATIONAL,guarded,router,prepare_audit


BASE=OUT/'resolved-routing-2024-v3'
JOINT=ROOT/'data/lower48-joint-mobility-2024-v1'
STUDY=ROOT/'data/calendar-strategy-study-2024-v1'
HOLDS=OUT/'search-calendar-controls-v2/rules-access/exact-access-holds.json'
TARGET_NAMES={'Golden-cheeked Warbler','Black-capped Vireo','Colima Warbler',
    'Rufous-capped Warbler','White-eared Hummingbird','American Flamingo','Brown Noddy','White-winged Tern'}


def refresh_progress(tag,arm):
    best=(682,BASE.name)
    for name in ['control-2024-v1','nearby-2024-v2','tentpole-2024-v1','island-2024-v1','combined-2024-v1']:
        folder=OUT/('calendar-study-'+name)
        if not (folder/'result.json').exists() or not (folder/'validation.json').exists():continue
        result,audit=read(folder/'result.json'),read(folder/'validation.json')
        if (audit.get('valid') and result.get('route_sha256')==audit.get('route_sha256')==sha256(folder/'route.json')
            and result.get('species',0)>best[0]):best=(result['species'],folder.name)
    dump(OUT/'run-status.json',{'stage':'search','run':tag,'label':'2024 calendar strategy study: '+arm,
        'best_species':best[0],'best_run':best[1],'verified_best':True,
        'message':'Independent15minute study arms; candidate scores appear only after full validation.'})
    subprocess.run([sys.executable,str(ROOT/'scripts/report_calendar_study.py'),'--partial'],
        check=True,stdout=subprocess.DEVNULL)


def remap(source,target,ids):
    result=[]
    for i in ids:
        row=source.records[i];day=int(row['day']);variant=int(row['reserved'])
        if variant:
            trip=source.meta['joint_marine']['trip_ids'][variant-1]
            j=target.meta['joint_marine']['trip_opportunities'][trip]
        else:
            lo,hi=map(int,np.searchsorted(target.records['day'],[day,day+1]))
            options=np.flatnonzero((target.records['location'][lo:hi]==row['location'])&
                (target.records['reserved'][lo:hi]==0))
            if len(options)!=1:raise ValueError('study seed exact date/location not unique')
            j=lo+int(options[0])
        a,b=source.opportunity(i),target.opportunity(j)
        for key in ['day','date','location','species','marine_trip']:
            if a.get(key)!=b.get(key):raise ValueError('study seed differs: '+key)
        result.append(int(j))
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--arm',choices=['nearby','tentpole','island','combined'],required=True)
    parser.add_argument('--seconds',type=float,default=900);parser.add_argument('--tag')
    parser.add_argument('--smoke',action='store_true');args=parser.parse_args()
    tag=args.tag or f'calendar-study-{args.arm}-2024-v1';out=OUT/tag;out.mkdir(exist_ok=True)
    if (out/'route.json').exists():raise ValueError('completed study arm exists')
    island=args.arm in {'island','combined'};nearby=args.arm in {'nearby','combined'};tentpole=args.arm in {'tentpole','combined'}
    original=load(JOINT/'model/model.json')
    model=load(STUDY/'island-model/model.json') if island else original
    baseflight=FlightModel(original,NATIONAL/'flights-2024');flight=FlightModel(model,NATIONAL/'flights-2024')
    awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(__import__('os').getpid())])
    wall=time.monotonic()
    try:
        with guarded(tag),ExitStack() as stack:
            process=stack.enter_context(router(tag,120));work=STUDY/(tag+'-cache')
            plan=read(STUDY/'study-plan.json');cache_binding=plan['starting_cache']
            for name,digest in cache_binding['sha256'].items():
                if sha256(JOINT/'resolved-routing-2024-v3-cache'/name)!=digest:raise ValueError('common starting cache changed')
            if not work.exists():
                shutil.copytree(JOINT/'resolved-routing-2024-v3-cache',work)
                if any(sha256(work/name)!=digest for name,digest in cache_binding['sha256'].items()):
                    raise ValueError('study cache copy differs from common starting cache')
                dump(out/'starting-cache-binding.json',cache_binding)
            elif not (out/'starting-cache-binding.json').exists():raise ValueError('existing study cache has no initial binding')
            roads=ResolvedJointRoads(flight,NATIONAL/'roads/release.json',work,query_timeout_seconds=135,
                access_holds_path=HOLDS,source_access_holds_path=JOINT/'source-visit-holds.json')
            try:
                ids,binding=seed_verified_joint(baseflight,roads,BASE)
                if island:ids=remap(original,model,ids)
                binding.update(complete_model_sha256=sha256(model.path),same_routing_coordinates_verified=True)
                dump(out/'baseline-binding.json',binding)
                # Preparation is separate from the equal search budget. Index
                # construction during an actual strategy remains inside it.
                with NativeEvaluator(flight,roads,out/'evaluator').open() as evaluator:
                    baseline=evaluator.evaluate(ids,timeout=180)
                    if baseline['pending'] or not baseline['feasible'] or baseline['species']!=682:
                        raise ValueError('study did not reproduce the frozen682 seed')
                    print(json.dumps({'stage':'study-baseline','arm':args.arm,**baseline}),flush=True)
                    if args.smoke:return
                    refresh_progress(tag,args.arm)
                    started=time.monotonic();deadline=started+args.seconds;stages=[];best_ids=ids;best=baseline;best_required=[]
                    candidates=list(csv.DictReader((BASE/'uncollected-species.csv').open()))
                    targets=[t for t in candidates if t['species'] in TARGET_NAMES]

                    def phase(label):
                        entry=roads.begin_phase(label)
                        with (out/'phase-transitions.jsonl').open('a') as log:log.write(json.dumps(entry)+'\n')

                    def search(initial,budget,label,required=(),blocks=()):
                        nonlocal best,best_ids,best_required
                        phase(label)
                        budget=min(budget,max(.01,deadline-time.monotonic()))
                        result=native_session(flight,roads,out/label,initial,budget,required=required,blocks=blocks)
                        score={'pending':False,'feasible':True,'species':result['species'],
                               'minutes':result['travel_minutes'],'miles':result['road_miles']}
                        checked=evaluator.evaluate(result['selected'],timeout=30)
                        if checked['pending'] or not checked['feasible'] or checked['species']!=score['species']:
                            raise ValueError('native scenario failed final re-evaluation')
                        stages.append({'stage':label,'budget_seconds':budget,'required':list(required),
                            'species':score['species'],'candidate_evaluations':result['candidate_evaluations']})
                        if superior(score,best):best,best_ids,best_required=score,result['selected'],list(required)
                        return result,score

                    # Island access alone uses the same unconstrained final
                    # objective. Guided new-boat proposals can seed it only
                    # when they improve the complete calendar immediately.
                    if island:
                        phase('island-construction')
                        island_targets=[t for t in candidates if t['species']=='Island Scrub-Jay']
                        helper=TentpoleExplorer(flight,evaluator,out/'island-construction',island_targets)
                        helper.output.mkdir(exist_ok=True)
                        for s in helper.support:
                            helper.support[s]=[i for i in helper.support[s] if int(helper.records[i]['reserved'])>0]
                        islands=helper.footholds(best_ids,seconds=min(150,args.seconds/6),limit=2,island_only=True)
                        for scenario in islands['scenarios']:
                            if superior(scenario,best):best,best_ids=scenario,scenario['ids']
                        stages.append({'stage':'island-construction','elapsed_seconds':islands['elapsed_seconds'],
                            'feasible_calendars':islands['feasible_calendars'],'species':best['species']})
                        del helper;gc.collect()
                    if tentpole:
                        phase('tentpole-construction')
                        helper=TentpoleExplorer(flight,evaluator,out/'tentpole-construction',targets)
                        helper.output.mkdir(exist_ok=True)
                        constructions=helper.footholds(best_ids,seconds=min(180,max(.01,deadline-time.monotonic())*.3),limit=3)
                        scenarios=constructions['scenarios']
                        stages.append({'stage':'tentpole-construction','elapsed_seconds':constructions['elapsed_seconds'],
                            'feasible_calendars':constructions['feasible_calendars'],
                            'selected_scenarios':len(scenarios)})
                        del helper;gc.collect()
                        reserve=180 if nearby else 90
                        for j,scenario in enumerate(scenarios):
                            remaining=max(0,deadline-time.monotonic()-reserve)
                            if remaining<1:break
                            budget=remaining/(len(scenarios)-j)
                            search(scenario['ids'],budget,f'anchored-{j+1}',scenario['required'])
                    # Keep a global refinement phase in every arm, including
                    # those whose anchored alternatives did not beat682.
                    reserve=300 if args.arm=='nearby' else 180 if nearby else 0
                    remaining=deadline-time.monotonic()-reserve
                    if remaining>1:search(best_ids,remaining,'global-refinement',best_required)
                    if nearby and deadline-time.monotonic()>1:
                        phase('nearby-polish')
                        helper=NearbyPolisher(flight,evaluator,out/'nearby-polish',reference_route=BASE/'route.json');helper.output.mkdir(exist_ok=True)
                        polished=helper.polish(best_ids,seconds=max(.01,deadline-time.monotonic()),required=best_required)
                        if superior(polished['score'],best):best,best_ids=polished['score'],polished['ids']
                        stages.append({'stage':'nearby-polish','elapsed_seconds':polished['elapsed_seconds'],
                            'accepted':len(polished['accepted']),'species':best['species']})
                        del helper;gc.collect()
                    # Converged polish/construction returns unused time to the
                    # ordinary search, so every arm consumes the same budget.
                    if deadline-time.monotonic()>1:search(best_ids,deadline-time.monotonic(),'budget-remainder',best_required)
                    search_elapsed=time.monotonic()-started
                    frozen=evaluator.evaluate(best_ids,timeout=180)
                    if frozen['pending'] or not frozen['feasible'] or frozen['species']!=best['species']:
                        raise ValueError('study final seed not measured feasible')
                    phase('finalize-measured-calendar')
                    dump(out/'study-search.json',{'arm':args.arm,'allocated_seconds':args.seconds,
                        'elapsed_seconds':search_elapsed,'baseline':baseline,'score':frozen,'ids':best_ids,
                        'stages':stages,'required_final_visits':best_required,'same_seed':20240101,'threads':2,'shared_future_discoveries':False,
                        'source_archive_unchanged':True,'sensitive_species_policy_unchanged':True})
                # Save and independently audit the winning calendar. A0.01s
                # complete-seed native run materializes it without adding a
                # hidden second optimization budget.
                prepared=verify_fresh_calendar(flight,roads,out/'fresh-preparation',best_ids,timeout=300)
                if prepared['species']!=best['species']:raise ValueError('fresh calendar species differ')
                dump(out/'seed.json',{'ids':best_ids,'mode':'complete','model_sha256':sha256(model.path),
                    'source_file':str((out/'study-search.json').resolve()),'source_sha256':sha256(out/'study-search.json')})
                roads.study_context={'arm':args.arm,'allocated_seconds':args.seconds,'elapsed_seconds':search_elapsed,
                    'path':str((out/'study-search.json').resolve()),'sha256':sha256(out/'study-search.json'),
                    'candidate_evaluations':sum(s.get('candidate_evaluations',0) for s in stages),
                    'required_final_visits':best_required}
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,tag)
                route=run(model.path,roads,out,seconds=.01,seed_file=out/'seed.json')
                audit=read(out/'validation.json')
                selected={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
                before={s for d in read(BASE/'route.json')['days'] for stop in d['stops'] for s in stop['species']}
                result={'arm':args.arm,'species':route['species_total'],**route['scoring_tiers'],
                    'gain_vs_682':route['species_total']-682,'gap_to_759':759-route['species_total'],
                    'added_species':sorted(selected-before),'lost_species':sorted(before-selected),
                    'allocated_search_seconds':args.seconds,'search_seconds':search_elapsed,
                    'wall_seconds':time.monotonic()-wall,'road_miles':sum(d['road_miles'] for d in route['days']),
                    'flight_days':sum(d['kind']=='flight' for d in route['days']),
                    'charter_days':sum(d['kind']=='pelagic' for d in route['days']),
                    'island_days':sum(d['kind']=='island' for d in route['days']),
                    'boat_nautical_miles':sum(d.get('boat_nautical_miles',0) for d in route['days']),
                    'birding_hours':sum(s.get('birding_minutes',120) for d in route['days'] for s in d['stops'])/60,
                    'walking_hours':audit['walking']['minutes']/60,'rest_days':sum(d['kind']=='rest' for d in route['days']),
                    'valid':audit['valid'],'route_sha256':sha256(out/'route.json'),
                    'exact_coordinates':True,'all_observations_retained':True,'walking_kmh':2.2,
                    'sensitive_species_policy_unchanged':True,'cache':roads.metrics(),'stages':stages}
                dump(out/'result.json',result)
                dump(out/'countability-source-validation.json',{'valid':audit['valid'],'route_sha256':sha256(out/'route.json'),
                    'records_checked':audit['joint_transport']['publisher_sources']['records_checked'],
                    'unresolved_or_excluded_credited':[],'individual_wild_status_certified':False})
                refresh_progress(tag,args.arm+' verified')
                print(json.dumps({'stage':'study-arm-completed',**result}),flush=True)
            finally:roads.close()
    finally:
        if model is not original:model.close()
        original.close()
        if awake.poll() is None:awake.terminate();awake.wait(timeout=5)


if __name__=='__main__':main()
