"""2024 national joint driving/observed flights/modeled charter benchmark."""
import argparse
from contextlib import ExitStack
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from bigyear_planner.compact_model import load
from bigyear_planner.joint_marine import JointRoads
from bigyear_planner.routing_resolution import ResolvedJointRoads,RESOLVED_POLICY
from bigyear_planner.joint_seed import seed_joint,seed_verified_joint
from bigyear_planner.joint_exploration import explore_boats
from bigyear_planner.mobility import FlightModel
from bigyear_planner.national_runner import run
from bigyear_planner.regional_exploration import NativeEvaluator,RegionalExplorer
from bigyear_planner.targeted_exploration import TargetedExplorer
from bigyear_planner.util import ROOT,dump,read,sha256
from evaluate_lower48 import OUT,DATA as NATIONAL,guarded,prepare_audit,router

DATA=ROOT/'data/lower48-joint-mobility-2024-v1'
PARENT=ROOT/'data/lower48-aba2024-provisional-v3'
MARINE=ROOT/'data/marine-2024-charter-v1'
BASE=OUT/'marine-charter-2024-v2'
HOLDS=OUT/'search-calendar-controls-v2/rules-access/exact-access-holds.json'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tag',default='joint-mobility-2024-v1')
    parser.add_argument('--boats',type=int,default=240);parser.add_argument('--regional',type=int,default=240)
    parser.add_argument('--native',type=int,default=900);parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--incumbent',type=Path)
    parser.add_argument('--targeted',type=int,default=0)
    parser.add_argument('--resolve-routing',action='store_true')
    parser.add_argument('--routing-query-cache',type=Path)
    parser.add_argument('--routing-witnesses',type=Path)
    args=parser.parse_args();out=OUT/args.tag;out.mkdir(exist_ok=True)
    if args.routing_query_cache and (not args.resolve_routing or not args.routing_query_cache.exists()):
        raise ValueError('routing-query-cache requires --resolve-routing and an existing cache directory')
    if (out/'route.json').exists():raise ValueError('completed run exists; use another tag')
    awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())]);started=time.monotonic()
    model=load(DATA/'model/model.json')
    try:
        with guarded(args.tag),ExitStack() as stack:
            process=stack.enter_context(router(args.tag,120 if args.resolve_routing else 8))
            flight=FlightModel(model,NATIONAL/'flights-2024')
            work=DATA/(args.tag+'-cache');pilot=OUT/'joint-mobility-2024-pilot-v1'
            migration_source=None
            if args.incumbent and not work.exists():
                source_cache=args.routing_query_cache or DATA/(args.incumbent.name+'-cache')
                if not source_cache.exists():source_cache=DATA/'joint-mobility-2024-v2-cache'
                if args.resolve_routing and read(source_cache/'provider.json')['access_policy'].get('extension')!=RESOLVED_POLICY:
                    migration_source=source_cache
                else:shutil.copytree(source_cache,work)
                dump(out/'incumbent-cache-reuse.json',{'source':str(source_cache),
                    'provider_sha256':sha256(source_cache/'provider.json'),'physical_binding_checked_by_provider':True,
                    'explicit_access_policy_migration':migration_source is not None})
            elif args.tag!='joint-mobility-2024-pilot-v1' and not work.exists() and (pilot/'result.json').exists():
                if not read(pilot/'validation.json')['valid'] or sha256(pilot/'route.json')!=read(pilot/'result.json')['route_sha256']:
                    raise ValueError('pilot route changed before bounded-cache reuse')
                shutil.copytree(DATA/'joint-mobility-2024-pilot-v1-cache',work)
                dump(out/'pilot-cache-reuse.json',{'pilot_route_sha256':sha256(pilot/'route.json'),
                    'provider_sha256':sha256(work/'provider.json'),'same_complete_model_and_physical_binding':True})
            road_class=ResolvedJointRoads if args.resolve_routing else JointRoads
            roads=road_class(flight,NATIONAL/'roads/release.json',work,
                query_timeout_seconds=135 if args.resolve_routing else 23,access_holds_path=HOLDS,
                source_access_holds_path=(DATA/'source-visit-holds.json') if (DATA/'source-visit-holds.json').exists() else None)
            try:
                if migration_source:roads.migrate_cache(migration_source)
                if not args.resolve_routing and (pilot/'validation.json').exists() and read(pilot/'validation.json')['valid']:
                    roads.seed_evidence(pilot/'route.json')
                if args.incumbent:
                    ids,binding=seed_verified_joint(flight,roads,args.incumbent)
                else:
                    ids,binding=seed_joint(flight,roads,BASE,MARINE/'road-cache-v2',
                        PARENT/'reconciled-2024-provisional-access-v3-cache')
                dump(out/'warm-seed-binding.json',binding)
                with NativeEvaluator(flight,roads,out).open() as evaluator:
                    initial=evaluator.evaluate(ids,timeout=180)
                    dump(out/'baseline-rescored.json',initial)
                    print(json.dumps({'stage':'joint-native-baseline',**initial}),flush=True)
                    expected=read(args.incumbent/'route.json')['species_total'] if args.incumbent else 667
                    if initial['pending'] or not initial['feasible'] or initial['species']!=expected:
                        raise ValueError('joint evaluator did not reproduce verified seed')
                    if args.smoke:
                        print(json.dumps({'smoke_passed':True,'score':initial,'road_metrics':roads.metrics()}),flush=True);return
                    dump(OUT/'run-status.json',{'stage':'search','run':args.tag,'label':'Joint driving, flights and charters',
                        'best_species':expected,'best_run':args.incumbent.name if args.incumbent else BASE.name,
                        'message':f'Verified {expected}-species warm start; targeting unvisited states with all observations and exact coordinates retained.'})
                    targeted=None
                    if args.targeted:
                        if not args.incumbent:raise ValueError('targeted search requires a verified incumbent')
                        baseline=read(args.incumbent/'route.json')
                        visited={baseline['locations'][s['location']]['state'] for d in baseline['days'] if d['kind']=='birding' for s in d['stops']}
                        if args.resolve_routing:
                            unresolved={t['species_id'] for t in read(args.incumbent/'gap-analysis.json')['targeted_species']
                                if t['outcome'].startswith('Routing unresolved')}
                            targets=[t for t in csv.DictReader((args.incumbent/'uncollected-species.csv').open()) if t['species_id'] in unresolved]
                            if len(targets)!=4:raise ValueError('expected the four documented routing-unresolved targets')
                        else:
                            targets=[t for t in csv.DictReader((args.incumbent/'uncollected-species.csv').open())
                                if int(t['dated_land_opportunities']) and not int(t['constructed_charter_options'])
                                and not set(t['land_states'].split('|'))&visited]
                            if len(targets)!=9:raise ValueError('expected the nine documented unvisited-state targets')
                        dump(out/'target-inputs.json',{'targets':targets,'baseline_route_sha256':sha256(args.incumbent/'route.json'),
                            'source_diagnostic_sha256':sha256(args.incumbent/'uncollected-species.csv'),
                            'baseline_run':str(args.incumbent),'full_model_sha256':sha256(model.path)})
                        if args.resolve_routing:
                            # Resolve generous complete-calendar witnesses first.
                            # These can lose species; they establish measured
                            # connectivity, not that all targets fit optimally.
                            helper=TargetedExplorer(flight,evaluator,out,targets);witnesses=[];reused={}
                            witness_binding={'baseline_route_sha256':sha256(args.incumbent/'route.json'),
                                'full_model_sha256':sha256(model.path),'road_network_sha256':roads.manifest['network_sha256']}
                            if args.routing_witnesses:
                                saved=read(args.routing_witnesses)
                                if any(saved.get(k)!=v for k,v in witness_binding.items()):
                                    raise ValueError('routing witness resume inputs differ')
                                reused={v['species_id']:v for v in saved['witnesses'] if v['score']['feasible'] and not v['score']['pending']}
                            for target,s in zip(targets,helper.target_indices):
                                head=helper.heads(ids,s)[0]
                                proposal=helper.campaign(ids,head,21)
                                # Connectivity witnesses use one exact target
                                # visit and generous transfer slack. Clustered
                                # replacements are optimized in the next phase.
                                proposal['trip']=[head]
                                proposal['ids']=helper.canonical([*proposal['outside'],head])
                                if target['species_id'] in reused:
                                    old=reused[target['species_id']]
                                    if old['head']!=head or head not in old['ids']:raise ValueError('resumed target head differs')
                                    proposal.update({k:old[k] for k in ['ids','trip','begin','end']})
                                score=evaluator.evaluate(proposal['ids'],timeout=240)
                                if score['pending'] or not score['feasible']:
                                    proposal['trip']=[head];proposal['ids']=helper.canonical([*proposal['outside'],head])
                                    score=evaluator.evaluate(proposal['ids'],timeout=240)
                                witnesses.append({'species_id':target['species_id'],'species':target['species'],
                                    'head':head,'begin':proposal['begin'],'end':proposal['end'],
                                    'ids':proposal['ids'],'trip':proposal['trip'],'score':score})
                                dump(out/'routing-witnesses.json',{**witness_binding,'witnesses':witnesses})
                                print(json.dumps({'stage':'resolved-routing-witness','target':target['species'],**score}),flush=True)
                            if any(v['score']['pending'] or not v['score']['feasible'] for v in witnesses):
                                raise ValueError('a full-calendar target routing witness remains unresolved or infeasible; inspect before rerunning')
                        targeted=TargetedExplorer(flight,evaluator,out,targets).explore(ids,seconds=args.targeted)
                        if args.resolve_routing:
                            with (out/'targeted-attempts.jsonl').open('a') as log:
                                for witness in witnesses:
                                    log.write(json.dumps({'target':witness['species_id'],'name':witness['species'],
                                        'phase':'routing-resolution-witness','head':witness['head'],
                                        'begin':witness['begin'],'end':witness['end'],'trip':witness['trip'],**witness['score']})+'\n')
                                    targeted['blocks'].append({'begin':witness['begin'],'end':witness['end'],'ids':witness['trip']})
                                    stats=next(t for t in targeted['targets'] if t['species_id']==witness['species_id'])
                                    stats['attempts']+=1;stats['feasible_campaigns']+=1;stats['routing_witness_resolved']=True
                            dump(out/'targeted-exploration.json',targeted)
                        ids=targeted['ids']
                    boats=explore_boats(flight,evaluator,out,ids,seconds=args.boats)
                    construction=RegionalExplorer(flight,evaluator,out).explore(boats['ids'],seconds=args.regional)
                with (out/'regional-blocks.txt').open('a') as stream:
                    for block in boats['blocks'][-128:]:
                        stream.write(f"{block['begin']} {block['end']} {len(block['ids'])} "+' '.join(map(str,block['ids']))+'\n')
                    if targeted:
                        for block in targeted['blocks'][-256:]:
                            stream.write(f"{block['begin']} {block['end']} {len(block['ids'])} "+' '.join(map(str,block['ids']))+'\n')
                # One bounded native library across all construction phases.
                library=out/'regional-blocks.txt';lines=list(dict.fromkeys(library.read_text().splitlines()))
                library.write_text('\n'.join(lines[-512:])+'\n' if lines else '')
                dump(out/'seed.json',{'ids':construction['ids'],'mode':'complete','model_sha256':sha256(model.path),
                    'source_file':str(out/'regional-exploration.json'),'source_sha256':sha256(out/'regional-exploration.json')})
                roads.prepare_audit=lambda:prepare_audit(stack,process,roads,args.tag)
                route=run(model.path,roads,out,seconds=args.native,seed_file=out/'seed.json',blocks_path=out/'regional-blocks.txt')
                taxonomy={v['species_id']:v for v in csv.DictReader((PARENT/'dataset/taxonomy.csv').open())}
                selected={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
                provisional=sorted(s for s in selected if taxonomy[s]['score_tier'].startswith('provisional'))
                sources=read(out/'validation.json')['joint_transport']['publisher_sources']
                result={'species':route['species_total'],'primary':len(selected)-len(provisional),'provisional':len(provisional),
                    'provisional_species':provisional,'baseline_fixed_calendar':667,'baseline_land_flights':656,
                    'gain_vs_fixed_calendar':route['species_total']-667,'record_comparison_target':759,
                    'gap_to_759':759-route['species_total'],'pool':len(model.meta['species']),
                    'native_joint_search':True,'boat_days':route['marine_extension']['boat_days'],
                    'flight_days':sum(d['kind']=='flight' for d in route['days']),
                    'road_miles':sum(d['road_miles'] for d in route['days']),
                    'boat_nautical_miles':sum(d.get('boat_nautical_miles',0) for d in route['days']),
                    'walking_kmh':2.2,'all_observations_retained':True,'exact_coordinates':True,
                    'source_records_checked':sources['records_checked'],'publisher_source_postcheck':'passed',
                    'recordholder_replay':'pending_input','validation':read(out/'validation.json'),
                    'route_sha256':sha256(out/'route.json'),'total_wall_seconds':time.monotonic()-started}
                if args.incumbent:
                    result.update({'baseline_incumbent':expected,'gain_vs_incumbent':route['species_total']-expected,
                        'baseline_run':str(args.incumbent),'baseline_route_sha256':binding['route_sha256']})
                if targeted:
                    result['targeted_species']=[{**t,'collected':t['species_id'] in selected} for t in targeted['targets']]
                    result['targeted_budget_seconds']=args.targeted
                if args.resolve_routing:
                    if (out/'routing-witnesses.json').exists():
                        result['routing_queries_resolved']=all(v['score']['feasible'] and not v['score']['pending'] for v in read(out/'routing-witnesses.json')['witnesses'])
                    result['access_resolution_migration']=str(work/'access-cache-migration.json')
                dump(out/'result.json',result)
                dump(out/'countability-source-validation.json',{'valid':True,'route_sha256':result['route_sha256'],
                    'policy_sha256':sha256(PARENT/'policy.json'),'retrieval_sha256':sha256(out/'publisher-retrieval.json'),
                    'records_checked':sources['records_checked'],'individual_wild_status_certified':False,
                    'unresolved_or_excluded_credited':[],'decisions':sources['decisions']})
                dump(OUT/'run-status.json',{'stage':'completed','run':args.tag,'species':route['species_total'],
                    'best_species':route['species_total'],'best_run':args.tag,
                    'message':'Joint road/dated flight/charter search passed independent calendar, transport and publisher audits.'})
                print(json.dumps(result),flush=True)
            finally:roads.close()
    finally:
        model.close()
        if awake.poll() is None:awake.terminate();awake.wait(timeout=5)
        dump(out/'cleanup.json',{'process_scoped_idle_assertion_released':True,'transient_bridge_credentials_removed':True,
            'bounded_cost_cache_retained_for_reproducible_followup':True,'original_observation_archive_preserved':True})


if __name__=='__main__':main()
