"""Compare audited historical routes without treating data years as identical."""
import argparse
from collections import Counter, defaultdict
import calendar
import csv
import hashlib
import json
import os
from pathlib import Path

from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from benchmark_shared_year import metrics, verified


def summarize(folder):
    route,problem = verified(folder)
    seen = {}
    states = defaultdict(lambda:{'visits':0,'birding_days':set(),'species':set(),'first':None,'last':None})
    months = []
    for month in range(1,13):
        days = [d for d in route['days'] if int(d['date'][5:7])==month]
        visited = []
        for day in days:
            for stop in day['stops']:
                loc = route['locations'][stop['location']]
                state = loc['state'] or 'Offshore'
                if state not in visited:
                    visited.append(state)
                entry = states[state]
                entry['visits'] += 1
                entry['birding_days'].add(day['date'])
                entry['species'].update(stop['species'])
                entry['first'] = entry['first'] or day['date']
                entry['last'] = day['date']
                for s in stop['species']:
                    seen.setdefault(s,{'date':day['date'],'state':state})
        months.append({'month':month,'cumulative_species':days[-1]['cumulative_species'],
            'new_species':sum(len(d['new_species']) for d in days),
            'road_miles':sum(d['road_miles'] for d in days),
            'states':' → '.join(s.removeprefix('US-') for s in visited),
            **dict(Counter(d['kind'] for d in days))})
    source = problem['compact_source']
    if sha256(source['path'])!=source['sha256'] or source['sha256']!=route['provenance']['compact_model_sha256']:
        raise ValueError('audited complete model metadata changed')
    dataset = Path(problem['dataset'])
    if sha256(dataset/'manifest.json')!=problem['manifest_sha256']:
        raise ValueError('audited source dataset changed')
    manifest = read(dataset/'manifest.json')
    if sha256(dataset/'taxonomy.csv')!=manifest['outputs']['taxonomy.csv']:
        raise ValueError('audited taxonomy changed')
    model = read(source['path'])
    tiers = {r['species_id']:r['score_tier'] for r in
        csv.DictReader((Path(problem['dataset'])/'taxonomy.csv').open())
        if r['eod_presence_eligible']=='1'}
    paths = []
    stops = []
    for day in route['days']:
        for leg in day['legs']:
            geom = leg.get('geometry')
            if leg['mode']=='flight' and not geom:
                start,end = [route['locations'][leg[k]] for k in ['from','to']]
                geom = {'type':'LineString','coordinates':[
                    [start['lon'],start['lat']],[end['lon'],end['lat']]]}
            if geom and geom.get('type')=='LineString' and leg['end_fraction']==1:
                coords = geom['coordinates']
                # Display-only overview simplification. The original route,
                # observations, endpoints and coordinates stay untouched.
                paths.append({'month':int(day['date'][5:7]),'mode':leg['mode'],
                    'points':coords[::16]+[coords[-1]]})
        for stop in day['stops']:
            loc = route['locations'][stop['location']]
            stops.append({'month':int(day['date'][5:7]),'date':day['date'],
                'lat':loc['lat'],'lon':loc['lon'],'name':loc['name'],
                'new_species':len(stop['new_species'])})
    plan = read(folder/'plan.json')
    effort = dict(plan['budgets_seconds'])
    inherited_seconds = 0
    if plan.get('prior_search'):
        prior_plan = read(Path(plan['prior_search']['path'])/'plan.json')
        inherited_seconds = sum(prior_plan['budgets_seconds'][k] for k in ['calendar','regional','rest'])
    partial_seconds = 0
    if plan.get('resume_input'):
        partial_seconds = read(plan['resume_input']['path']).get('completed_polish_seconds_approx',0)
    return {'year':route['year'],'metrics':{**metrics(route),
        'supported_cohort_species':len(model['first_seen']),
        'states_with_birding':sum(s.startswith('US-') for s in states)},'months':months,
        'search_effort':{'current_declared_phase_seconds':effort,
            'inherited_completed_phase_seconds':inherited_seconds,
            'earlier_polish_seconds_approx':partial_seconds,
            'total_search_allocation_minutes_approx':(sum(effort.values())+inherited_seconds+partial_seconds)/60,
            'initialization_and_validation_excluded':True},
        'states':{s:{**v,'birding_days':len(v['birding_days']),'species':len(v['species'])}
            for s,v in sorted(states.items())},'seen':seen,
        'available':sorted({v['species'] for v in model['first_seen']}),
        'species_names':{s['id']:s['name'] for s in route['species']},
        'species_tiers':tiers,
        'source_groups':model['eligible_presence_groups'],
        'represented_observations':model['represented_observations'],
        'locations':model['locations'],'opportunities':model['opportunities'],
        'route_sha256':sha256(folder/'route.json'),'source_manifest_sha256':model['manifest_sha256'],
        'days_checked':read(folder/'validation.json')['days_checked'],
        'paths':paths,'stops':stops}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--earlier',type=Path,default=ROOT/'runs/lower48-eod-2023/shared-12h-full-2023-v5')
    parser.add_argument('--later',type=Path,default=ROOT/'runs/lower48-eod-2024/shared-12h-full-2024-v2')
    parser.add_argument('--output',type=Path,default=ROOT/'runs/year-comparison-2023-2024')
    args = parser.parse_args()
    output = args.output
    output.mkdir(exist_ok=True)
    routes = [verified(p) for p in [args.earlier,args.later]]
    rules = [{k:v for k,v in r['config'].items() if k not in
        ['year','seed','seconds','start_date','end_date']} for r,p in routes]
    if rules[0]!=rules[1]:
        raise ValueError('travel/search scenario rules differ')
    providers = []
    for folder,(route,problem) in zip([args.earlier,args.later],routes):
        provider = read(folder/'search-binding.json')['road_provider']
        binding = {k:provider[k] for k in ['model_sha256','road_release_sha256','matching_policy','access_policy']}
        if hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()!=route['provenance']['road_network_sha256']:
            raise ValueError('provider does not reproduce audited binding')
        providers.append({k:v for k,v in binding.items() if k!='model_sha256'})
    if providers[0]!=providers[1]:
        raise ValueError('physical road release/matching/access rules differ')
    policy = read(Path(routes[0][1]['dataset'])/'countability-policy.json')
    if policy['parent_policy_sha256']!=sha256(Path(routes[1][1]['dataset'])/'countability-policy.json'):
        raise ValueError('historical scoring is not derived from the reference policy')
    years = [summarize(p) for p in [args.earlier,args.later]]
    a,b = years
    common = set(a['seen'])&set(b['seen'])
    names = {**a['species_names'],**b['species_names']}
    tiers = {**a['species_tiers'],**b['species_tiers']}
    differences = []
    for sid in sorted(set(a['seen'])^set(b['seen']),key=lambda s:names[s]):
        differences.append({'species_id':sid,'common_name':names[sid],
            'score_tier':tiers[sid],
            'collected_in':a['year'] if sid in a['seen'] else b['year'],
            'available_in_both_cohorts':sid in a['available'] and sid in b['available'],
            'earlier_first_date':a['seen'].get(sid,{}).get('date',''),
            'later_first_date':b['seen'].get(sid,{}).get('date','')})
    if differences:
        write_csv(output/'species-differences.csv',differences,list(differences[0]))
    monthly = [{'year':y['year'],**m} for y in years for m in y['months']]
    fields = list(dict.fromkeys(k for row in monthly for k in row))
    write_csv(output/'monthly-comparison.csv',monthly,fields)
    state_rows = [{'year':y['year'],'state':s,**v} for y in years for s,v in y['states'].items()]
    write_csv(output/'state-comparison.csv',state_rows,list(state_rows[0]))
    result = {'valid':True,'years':[{k:v for k,v in y.items() if k not in
        ['paths','stops','species_names']} for y in years],
        'shared_collected_species':len(common),
        'earlier_only_species':len(set(a['seen'])-set(b['seen'])),
        'later_only_species':len(set(b['seen'])-set(a['seen'])),
        'species_differences':differences,
        'limitations':['Both routes are best found, not proven optimal.',
            'Common Waxbill and Red Avadavat mainland supports are held pending established-population or natural-dispersal evidence; the documented Hawaii introductions do not establish mainland countability. Original records and exact coordinates remain archived.',
            '2024 benefited from many preceding searches; matching final phase budgets does not establish equal search convergence.',
            'Fixed 2024 taxonomic concepts and approved provisional tiers are applied retrospectively; annual population envelopes are shared modeling assumptions, and exceptional dated events are not moved between years.',
            '2024 has 366 dates; 2023 has 365. Observation coverage and actual reported bird occurrences differ.',
            'Support in both annual cohorts does not establish that the dated visits can be combined into one feasible itinerary; seasonality and travel windows still matter.',
            'Flights use each year’s reported operated dates. Roads/coastlines are the same frozen modern physical snapshot, not historical traffic reconstruction.',
            'Marine/island access construction remains partial and modeled; historical sailings and weather are not certified.'],
        'rules':rules[0],'shared_road_release_sha256':providers[0]['road_release_sha256'],
        'provider_binding_sha256':{str(r['year']):r['provenance']['road_network_sha256'] for r,p in routes}}
    dump(output/'comparison.json',result)
    labels = [('Total species','species'),('Primary species','primary'),('Provisional species','provisional'),
        ('Supported cohort species','supported_cohort_species'),('States with birding','states_with_birding'),
        ('Driving miles','road_miles'),('Travel hours','travel_hours'),('Birding hours','birding_hours'),('Walking hours','walking_hours')]
    lines = ['# 2023 and 2024 route comparison','',
        '| Metric | 2023 | 2024 |','|---|---:|---:|']
    for label,key in labels:
        digits = 0 if key in {'species','primary','provisional','supported_cohort_species','states_with_birding'} else 1
        lines.append(f'| {label} | {a["metrics"][key]:,.{digits}f} | {b["metrics"][key]:,.{digits}f} |')
    for kind in ['birding','road_transit','flight','pelagic','island','rest']:
        lines.append(f'| {kind.replace("_"," ").title()} days | {a["metrics"]["day_counts"].get(kind,0)} | {b["metrics"]["day_counts"].get(kind,0)} |')
    lines.extend(['',f'{len(common)} species collected in both routes; {result["earlier_only_species"]} only in 2023 and {result["later_only_species"]} only in 2024. These are route differences, not necessarily differences in national availability.','',
        '| Month | 2023 cumulative | 2023 regions visited | 2024 cumulative | 2024 regions visited |',
        '|---|---:|---|---:|---|'])
    for x,y in zip(a['months'],b['months']):
        lines.append(f'| {calendar.month_abbr[x["month"]]} | {x["cumulative_species"]} | {x["states"]} | {y["cumulative_species"]} | {y["states"]} |')
    missed_earlier = sum(r['collected_in']==b['year'] and r['available_in_both_cohorts'] for r in differences)
    missed_later = sum(r['collected_in']==a['year'] and r['available_in_both_cohorts'] for r in differences)
    lines.extend(['',f'{missed_earlier} species collected only by the 2024 route have scored 2023 support; {missed_later} species collected only by the 2023 route also have scored 2024 support. These are concrete trip-construction targets. Their actual dates and travel still need feasibility testing; annual support alone does not establish a reachable combined itinerary.'])
    lines.extend(['',f'Search allocation after initialization: approximately {a["search_effort"]["total_search_allocation_minutes_approx"]:.1f} minutes for 2023 and {b["search_effort"]["total_search_allocation_minutes_approx"]:.1f} minutes for 2024. The 2023 population review and refinement handoffs preserve their source-stage bindings; preparation and independent validation are measured separately.','',
        'Assumptions and interpretation:','',*['- '+s for s in result['limitations']]])
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    for year,source in zip(years,[args.earlier,args.later]):
        target = output/str(year['year'])
        target.mkdir(exist_ok=True)
        for name in ['route.json','problem.json','validation.json','roads.sqlite']:
            dest = target/name
            if dest.exists():
                if sha256(dest)!=sha256(source/name):
                    raise ValueError('comparison view artifact changed')
            else:
                os.link(source/name,dest)
        for name in ['itinerary.csv','benchmark.json','result.json','source-decisions.csv']:
            if (source/name).exists() and not (target/name).exists():
                os.link(source/name,target/name)
    payload = {'years':years,'shared':len(common),'differences':differences,
        'limitations':result['limitations'],
        'states':read(ROOT/'web/states.geojson')}
    template = (ROOT/'web/year-comparison-template.html').read_text()
    content = template.replace('__COMPARISON_DATA__',json.dumps(payload).replace('<',chr(92)+'u003c'))
    (output/'pilot-report.html').write_text(content)
    dump(output/'report-verification.json',{'valid':True,
        'routes':{str(y['year']):y['route_sha256'] for y in years},
        'report_sha256':sha256(output/'report.md'),'html_sha256':sha256(output/'pilot-report.html'),
        'comparison_sha256':sha256(output/'comparison.json')})
    print(json.dumps({'report':str(output/'report.md'),'html':str(output/'pilot-report.html'),
        'totals':{str(y['year']):y['metrics']['species'] for y in years},
        'shared_species':len(common)}))


if __name__=='__main__':
    main()
