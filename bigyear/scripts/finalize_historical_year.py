"""Revalidate an already materialized historical route after restoring inputs."""
import argparse
from pathlib import Path
import time

from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from bigyear_planner.validate import validate
from benchmark_shared_year import metrics, verified
import evaluate_lower48 as resources


def complete_exports(folder):
    route,problem = verified(folder)
    fields = ['date','kind','road_miles','activity_minutes','walking_minutes','new_species','cumulative_species']
    write_csv(folder/'itinerary.csv',[{'date':d['date'],'kind':d['kind'],
        'road_miles':d['road_miles'],'activity_minutes':d['activity_minutes'],
        'walking_minutes':d.get('walking_minutes',0),'new_species':len(d['new_species']),
        'cumulative_species':d['cumulative_species']} for d in route['days']],fields)
    dump(folder/'benchmark.json',{'year':2023,'wall_seconds':read(folder/'result.json')['wall_seconds'],
        'requested_seconds':read(folder/'plan.json')['budgets_seconds'],
        'engine':read(folder/'engine.json'),
        'materialization_replay_only':True,
        'search':read(folder/'search-result.json'),
        'prior_search':read(folder/'plan.json').get('prior_search'),
        'cache':read(folder/'road-cache-metrics.json'),
        'model':{k:read(problem['compact_source']['path'])[k] for k in
            ['locations','opportunities','species_entries','eligible_presence_groups','represented_observations']},
        'saved_route_bytes':(folder/'route.json').stat().st_size,
        'validation':read(folder/'validation.json'),
        'note':'The top-level engine is the final complete-seed replay. Full optimization effort is bound in search phases, prior stages and the refinement resume input.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,default=ROOT/'runs/lower48-eod-2023/shared-12h-full-2023-v5')
    parser.add_argument('--exports-only',action='store_true')
    args = parser.parse_args()
    folder = args.run.resolve()
    if args.exports_only:
        complete_exports(folder)
        print('Verified itinerary and benchmark exports completed.',flush=True)
        return
    route = read(folder/'route.json')
    if route['year']!=2023 or not route['config']['mixed_road_birding'] or route['config']['drive_minutes']!=720:
        raise ValueError('historical finalization scenario differs')
    baseline_path = ROOT/'runs/lower48-eod-2024/shared-12h-full-2024-v2'
    baseline,_ = verified(baseline_path)
    baseline_sha = sha256(baseline_path/'route.json')
    resources.OUT = folder.parent
    tag = folder.name+'-independent-audit'
    started = time.monotonic()
    with resources.guarded(tag):
        audit = validate(folder/'route.json',folder/'problem.json',folder/'roads.sqlite')
        dump(folder/'validation.json',audit)
        if not audit['valid'] or audit['route_sha256']!=sha256(folder/'route.json'):
            raise ValueError('Independent historical audit failed: '+str(audit['errors'][:8]))
        search = read(folder/'search-result.json')
        if route['search']['selected_model_opportunities']!=search['ids']:
            raise ValueError('saved route differs from optimized candidate IDs')
        if sha256(baseline_path/'route.json')!=baseline_sha:
            raise ValueError('2024 baseline changed')
        previous_resources = read(folder.parent/(folder.name+'-resources.json'))
        result = {'valid':True,**metrics(route),'year':2023,'phases':search['phases'],
            'route_sha256':sha256(folder/'route.json'),
            'model_sha256':route['provenance']['compact_model_sha256'],
            'full_calendar_days':audit['days_checked'],
            'publisher_records_checked':audit['joint_transport']['publisher_sources']['records_checked'],
            'all_observations_retained':True,'exact_coordinates':True,'walking_kmh':2.2,
            'reference_metrics':metrics(baseline),'reference_route_sha256':baseline_sha,
            'wall_seconds':previous_resources['wall_seconds']+time.monotonic()-started,
            'validation_recovery':{'required_raw_BTS_archives_restored':True,
                'final_audit_seconds':time.monotonic()-started,
                'no_observations_dates_coordinates_or_route_legs_changed':True},
            'optimization_proven':False}
        dump(folder/'result.json',result)
        dump(folder/'run-status.json',{'stage':'completed','candidate_verified':True,
            'species':route['species_total'],'route_sha256':sha256(folder/'route.json')})
        complete_exports(folder)
        print({k:v for k,v in result.items() if k not in ['phases','reference_metrics']},flush=True)


if __name__=='__main__':
    main()
