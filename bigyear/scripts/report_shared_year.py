"""Source-bound comparison of the shared-12h benchmark and verified 699 route."""

import argparse
import csv
from pathlib import Path

from bigyear_planner.compact_model import load
from bigyear_planner.config import Config
from bigyear_planner.rest_exploration import RestExplorer
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from benchmark_shared_year import metrics, verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag',default='shared-12h-full-2024-v1')
    parser.add_argument('--baseline',type=Path,
                        default=ROOT/'runs/lower48-eod-2024/rest-opportunities-2024-v1')
    args = parser.parse_args()
    if Path(args.tag).name!=args.tag:
        raise ValueError('tag must be a single directory name')
    out = ROOT/'runs/lower48-eod-2024'/args.tag
    baseline = args.baseline.resolve()
    before, old_problem = verified(baseline)
    route, problem = verified(out)
    result, audit = read(out/'result.json'), read(out/'validation.json')
    if not result['valid'] or result['route_sha256']!=sha256(out/'route.json') or audit['days_checked']!=366:
        raise ValueError('report requires a complete, independently validated saved result')
    old_cfg,new_cfg = Config(**before['config']).to_dict(),Config(**route['config']).to_dict()
    changed = {k for k in old_cfg if old_cfg[k]!=new_cfg[k]}
    if changed!={'mixed_road_birding','drive_minutes'} or new_cfg['drive_minutes']!=720:
        raise ValueError('comparison changed unexpected scientific rules')
    if before['taxonomy_policy']!=route['taxonomy_policy']:
        raise ValueError('scoring policy changed')
    old_meta = read(old_problem['compact_source']['path'])
    new_meta = read(problem['compact_source']['path'])
    if old_meta['components']!=new_meta['components'] or any(old_meta[k]!=new_meta[k] for k in
        ['locations','opportunities','species','species_entries','dataset','manifest_sha256']):
        raise ValueError('comparison changed observations, exact coordinates or countability')
    if before['provenance']['road_network_sha256']!=route['provenance']['road_network_sha256']:
        raise ValueError('comparison changed the physical routing network')
    names = {s['id']:s['name'] for s in route['species']}
    taxonomy = {r['species_id']:r for r in csv.DictReader((Path(problem['dataset'])/'taxonomy.csv').open())}

    def first_visits(saved):
        values = {}
        for day in saved['days']:
            for stop in day['stops']:
                for species in stop['species']:
                    values.setdefault(species,(day['date'],saved['locations'][stop['location']]['state']))
        return values

    old_visits,new_visits = first_visits(before),first_visits(route)
    rows = []
    for label,subset,visits in [('added',new_visits.keys()-old_visits.keys(),new_visits),
                                ('lost',old_visits.keys()-new_visits.keys(),old_visits)]:
        for s in sorted(subset,key=lambda s:names[s]):
            when,state = visits[s]
            rows.append({'change':label,'species_id':s,'species':names[s],
                         'tier':taxonomy[s]['score_tier'],'first_date':when,'state':state})
    write_csv(out/'species-changes.csv',rows,['change','species_id','species','tier','first_date','state'])
    provisionals = [{**p,'collected':p['id'] in new_visits,
                    'first_date':new_visits.get(p['id'],('',''))[0],
                    'state':new_visits.get(p['id'],('',''))[1]} for p in result['provisional_pool']]
    write_csv(out/'provisional-species.csv',provisionals,
              ['id','name','tier','collected','first_date','state'])
    model = load(problem['compact_source']['path'])
    try:
        helper = RestExplorer(model,None,out/'remaining-rest-opportunities',route)
        _,_,inventory = helper.index(route['search']['selected_model_opportunities'])
        dump(out/'remaining-rest-opportunities/inventory.json',inventory)
        rest_rows = [{'date':r['date'],'opportunities':r['opportunities'],
                      'boat_opportunities':r['boat_opportunities'],'missing_species_count':len(r['missing_species']),
                      'missing_species':'|'.join(names[s] for s in r['missing_species'])} for r in inventory['days']]
        write_csv(out/'remaining-rest-opportunities.csv',rest_rows,
                  ['date','opportunities','boat_opportunities','missing_species_count','missing_species'])
    finally:
        model.close()
    a,b = metrics(before),metrics(route)
    resource_file = out.parent/(args.tag+'-resources.json')
    resource = read(resource_file)
    for folder in [out,out.parent/(args.tag+'-audit')]:
        if not read(folder/'router-stopped.json').get('stopped'):
            raise ValueError('managed routing worker has not stopped: '+str(folder))
    native = [read(out/label/'binding.json') for label in ['calendar-search','joint-calendar-polish']]
    native_peaks = ', '.join(format(r['peak_rss_bytes']/2**30,'.2f') for r in native)
    rest_phase = next(p for p in result['phases'] if p['label']=='national-rest')
    lines = ['# Verified shared twelve-hour 2024 benchmark','',
        f"The final route has **{b['species']} species ({b['primary']} primary + {b['provisional']} provisional)**, "
        f"a gain of **{b['species']-a['species']}** over the 699-species baseline. "
        f"There are {len(new_visits.keys()-old_visits.keys())} additions and {len(old_visits.keys()-new_visits.keys())} losses.",'',
        '| Measure | Original 699 route | Shared twelve-hour result |','|---|---:|---:|']
    for title,left,right in [('Total species',a['species'],b['species']),
        ('Primary species',a['primary'],b['primary']),('Provisional species',a['provisional'],b['provisional']),
        ('Road miles',f"{a['road_miles']:,.1f}",f"{b['road_miles']:,.1f}"),
        ('Travel hours',f"{a['travel_hours']:,.1f}",f"{b['travel_hours']:,.1f}"),
        ('Birding hours',f"{a['birding_hours']:,.1f}",f"{b['birding_hours']:,.1f}"),
        ('Walking hours',f"{a['walking_hours']:,.1f}",f"{b['walking_hours']:,.1f}"),
        ('Rest days',a['day_counts'].get('rest',0),b['day_counts'].get('rest',0)),
        ('Road-only days',a['day_counts'].get('road_transit',0),b['day_counts'].get('road_transit',0)),
        ('Flight days',a['day_counts'].get('flight',0),b['day_counts'].get('flight',0)),
        ('Pelagic charter days',a['day_counts'].get('pelagic',0),b['day_counts'].get('pelagic',0)),
        ('Island landing days',a['day_counts'].get('island',0),b['day_counts'].get('island',0))]:
        lines.append(f'| {title} | {left} | {right} |')
    for title,key in [('Birding days above 30 road miles','birding_days_over_30_road_miles'),
                      ('Birding days above eight driving hours','birding_days_over_8_driving_hours')]:
        lines.append(f'| {title} | {a[key]} | {b[key]} |')
    lines += ['', '## Rules and comparability','',
        'Both results use the identical complete 2024 lower-48/DC observation catalog, exact source coordinates, '
        '2.2 km/h walking, two-hour birding visits, dated nonstop flights, existing modeled charters/island access '
        'and current primary/provisional scoring policy. The shared configuration removes the ordinary birding-day '
        'road-mile cap and uses one twelve-hour daily budget for driving, birding and walking. '
        'Flights still occupy separate nonbirding days; boat access assumptions are unchanged. '
        'All observations and coordinates are retained. The warm start is not a mandatory calendar.','',
        'This is a warm-cache, finite search with a longer search allocation than the earlier rest study. '
        'It records a feasible result under the new rules; timing and score differences do not isolate '
        'the budget change from additional search or cache warming. Global optimality is not established. '
        'A full ABA-area record has different geographic coverage.','',
        '## Search phases','', '| Phase | Species | Search seconds |','|---|---:|---:|']
    for phase in result['phases']:
        duration=phase.get('seconds')
        lines.append(f"| {phase['label']} | {phase['score']['species']} | {duration:.1f} |" if duration is not None
                     else f"| {phase['label']} | {phase['score']['species']} | preparation |")
    lines += ['', 'Regional reconstructions and the rest sweep feed the final joint driving/flight/boat calendar search. '
        'The rest sweep uses actual free dates reconstructed under the new budget. Pending road/flight requests '
        'remain unresolved opportunities, rather than infeasibility proofs.', '',
        f"The rest sweep made {rest_phase['attempts']} evaluation attempts across "
        f"{rest_phase['rest_dates_attempted']} dates and {rest_phase['target_species_attempted']} target species; "
        f"{rest_phase['pending_attempts']} attempts returned pending. Attempts can include retries. "
        'The final short native replay materializes the selected calendar for audit; the timed search '
        'phases are the separate artifacts listed above.','',
        '## Species changes','', '| Change | Species | Tier | Date | State |','|---|---|---|---|---|']
    lines += [f"| {r['change']} | {r['species']} | {r['tier']} | {r['first_date']} | {r['state']} |" for r in rows]
    lines += ['', 'The complete provisional pool and its selected/unselected status are in `provisional-species.csv`. '
        'The approved provisional credits remain separate from primary-list totals.','',
        '## Resources and retention','',
        f"End-to-end wall time: {result['wall_seconds']/60:.1f} minutes. "
        f"Peak sampled combined process RSS: {resource['combined_peak_rss_bytes']/2**30:.2f} GiB. "
        f"Minimum sampled free disk: {resource['minimum_free_bytes']/2**30:.1f} GiB. "
        f"Native search peak RSS values: {native_peaks} GiB. "
        'The run retained its bounded warm road cache, selected road geometry, checkpoints, source/audit evidence '
        'and saved route. Source observation/model components are shared. Temporary DuckDB spills and bridge '
        'credentials are removed; both managed routing workers were stopped on completion.','',
        '## Independent verification and remaining opportunities','',
        f"All {audit['days_checked']} dates and {result['publisher_records_checked']:,} publisher records passed. "
        'The audit checks source identity, scoring, directed road endpoints/costs, access walking, dated flight '
        'service, boat/landing access, daily budgets, continuity and unique species totals.','',
        f"The final actual calendar has {inventory['rest_dates']} rest dates; "
        f"{inventory['opportunity_dates']} have source supports for missing annual species. "
        f"The inventory contains {inventory['dated_opportunities']:,} dated opportunities for "
        f"{inventory['missing_species']} missing species. These are opportunities to test, not proven reachable trips.",'',
        f"Baseline route SHA-256: `{sha256(baseline/'route.json')}`.",
        f"Final route SHA-256: `{sha256(out/'route.json')}`.",
        f"Complete model SHA-256: `{sha256(problem['compact_source']['path'])}`.",'']
    (out/'report.md').write_text('\n'.join(lines))
    dump(out/'report-verification.json',{'valid':True,'baseline_sha256':sha256(baseline/'route.json'),
        'route_sha256':sha256(out/'route.json'),'report_sha256':sha256(out/'report.md'),
        'model_sha256':sha256(problem['compact_source']['path']),
        'species_changes_sha256':sha256(out/'species-changes.csv'),
        'rest_inventory_sha256':sha256(out/'remaining-rest-opportunities/inventory.json'),
        'expected_config_changes':sorted(changed),'all_observations_and_exact_coordinates_preserved':True})
    print({'species':b['species'],'gain_over_699':b['species']-699,'report':str(out/'report.md')})


if __name__=='__main__':
    main()
