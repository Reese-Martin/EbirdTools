"""Full West Coast rerun adding explicit access walking to the activity budget."""
import gc
import hashlib
import json
import shutil
import sys

import benchmark_west_routers as resources
import evaluate_west_endpoints as endpoints
from bigyear_planner.config import Config
from bigyear_planner.display import build_display_roads
from bigyear_planner.runner import run
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from bigyear_planner.walking import access_walk

DATA = ROOT/'data/west-coast-eod-2024'
WORK = ROOT/'data/west-coast-walking-2p2-2024'
OUT = ROOT/'runs/west-coast-walking-2p2-2024'
OLD = ROOT/'runs/west-coast-endpoints-2024'
PROVIDER = ROOT/'data/west-coast-endpoints-2024/strict.bounded.json'
ENDPOINT = 'http://127.0.0.1:5005'
SPEED = 2.2  # User-selected walking speed in km/h.


def cohort_digest(problem):
    return {key: hashlib.sha256(json.dumps(problem[key], sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
            for key in ('locations', 'species', 'opportunities')}


def prepare():
    problem_path = WORK/'problem.json'
    if problem_path.exists():
        proof = read(OUT/'cohort-verification.json')
        assert proof['derived_problem_sha256'] == sha256(problem_path)
        assert proof['changes']['walking_speed_kmh'] == SPEED
        return
    assert sha256(DATA/'problem.json') == endpoints.PROBLEM_SHA
    p = read(DATA/'problem.json')
    before = cohort_digest(p)
    config = Config(**{**p['config'], 'walking_speed_kmh': SPEED, 'walking_threshold_m': 100})
    p['config'] = config.to_dict()
    assert before == cohort_digest(p)
    dump(problem_path, p)
    problem_path.chmod(0o444)
    count = {'locations': len(p['locations']), 'opportunities': len(p['opportunities']),
             'species': len(p['species'])}
    del p
    gc.collect()
    p = read(problem_path)
    assert before == cohort_digest(p)
    dump(OUT/'cohort-verification.json', {'original_problem_sha256': endpoints.PROBLEM_SHA,
        'derived_problem_sha256': sha256(problem_path), 'unchanged_arrays': before,
        'counts': count, 'all_observations_and_exact_coordinates_retained': True,
        'changes': {'walking_speed_kmh': SPEED, 'walking_threshold_m': 100,
                    'round_trip': True, 'additional_to_birding': True},
        'source_archive_manifest_sha256': endpoints.ARCHIVE_SHA})
    old = read(OLD/'mmap-600s/route.json')
    rows = []
    for day in old['days']:
        walk = sum(access_walk(old['locations'][s['location']], p['config'])['walking_minutes']
                   for s in day['stops'])
        total = day['activity_minutes'] + walk
        rows.append({'date': day['date'], 'original_activity_minutes': day['activity_minutes'],
                     'added_round_trip_walking_minutes': walk, 'new_activity_minutes': total,
                     'exceeds_birding_day_budget': day['kind'] == 'birding' and total > p['config']['activity_minutes']+1e-5})
    write_csv(OUT/'previous-route-walking-audit.csv', rows, list(rows[0]))
    dump(OUT/'previous-route-walking-audit.json', {'route_sha256': sha256(OLD/'mmap-600s/route.json'),
         'previous_species': old['species_total'], 'walking_speed_kmh': SPEED,
         'days_exceeding_activity_budget': sum(row['exceeds_birding_day_budget'] for row in rows),
         'maximum_required_activity_hours': max(row['new_activity_minutes']/60 for row in rows),
         'scope': 'Diagnostic re-accounting only. The old route remains immutable and is not a valid result under the new walking rule.'})
    del p, old
    gc.collect()


def report():
    benchmark = read(OUT/'mmap-600s/benchmark.json')
    audit = read(OUT/'mmap-600s/validation.json')
    endpoint = read(OUT/'selected-endpoint-audit.json')
    memory = read(OUT/'walking-full-search-resources.json')
    previous = read(OLD/'mmap-600s/benchmark.json')
    old_audit = read(OUT/'previous-route-walking-audit.json')
    rows = []
    for label,b,valid,walk in [('Previous fixed stop allowance', previous, False, None),
                             ('Explicit round-trip access walking',benchmark,True,audit['walking']['minutes']/60)]:
        rows.append({'run':label,'species':b['engine']['species'],'total_seconds':b['wall_seconds'],
                     'candidate_evaluations':b['engine']['candidate_evaluations'],
                     'road_miles':b['validation']['road_miles'],'walking_hours':walk,
                     'valid_under_new_walking_rule':valid})
    write_csv(OUT/'run-summary.csv',rows,list(rows[0]))
    headers = ''.join(f'<th>{key.replace("_"," ")}</th>' for key in rows[0])
    table = ''.join('<tr>'+''.join(f'<td>{value:,.2f}</td>' if type(value) is float else
                       f'<td>{value if value is not None else "included in stop allowance"}</td>'
                       for value in row.values())+'</tr>' for row in rows)
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>West Coast 2024 — walking at {SPEED:g} km/h</title><style>
    body{{font:16px/1.6 system-ui;background:#f3f6f1;color:#20342a;max-width:1150px;margin:40px auto;padding:0 24px}}
    h1{{font-size:2rem;line-height:1.2}}section{{background:white;border:1px solid #d6e0d5;border-radius:12px;padding:24px;margin:24px 0}}
    .stats{{display:flex;gap:45px;flex-wrap:wrap}}.stats strong{{display:block;font-size:2rem}}table{{border-collapse:collapse;width:100%;font-size:14px}}
    td,th{{text-align:left;padding:10px;border-bottom:1px solid #dde5db}}.scroll{{overflow:auto}}a{{color:#196c4b}}.note{{background:#fff5db}}
    </style><h1>West Coast 2024: walking at {SPEED:g} km/h</h1>
    <p>California, Oregon and Washington · full year · all observations and exact coordinates · memory-mapped OSRM</p>
    <section><div class="stats"><div><strong>{benchmark['engine']['species']} / 570</strong>species under the walking rule</div>
    <div><strong>{benchmark['wall_seconds']/60:.1f} min</strong>search and verification</div>
    <div><strong>{memory['combined_peak_rss_bytes']/2**30:.2f} GiB</strong>sampled peak combined RSS</div></div>
    <p><a href="/?run=mmap-600s/route.json">Open the route viewer</a> · <a href="/run-summary.csv">Comparison CSV</a> ·
    <a href="/mmap-600s/itinerary.csv">Daily walking and activity</a> · <a href="/mmap-600s/validation.json">Independent audit</a></p></section>
    <section><h2>Walking rule</h2><p>For a source observation pin more than 100 m from its road anchor, add the full distance there and back at
    <strong>{audit['walking']['speed_kmh']:g} km/h</strong>. This walking time is additional to the existing two hours of on-site birding and counts against the unchanged
    12-hour birding-day activity budget. At or below 100 m, access remains within the fixed birding allowance. Each visit returns to the same anchor.</p>
    <p>The selected route includes <strong>{audit['walking']['minutes']/60:.1f} walking hours</strong> and {audit['walking']['meters']/1000:.1f} km of round-trip access walking.
    Birding-hour curves show on-site birding; stop details and activity totals show the additional walking time.</p>
    <p class="note">Walking distance uses the straight-line pin-to-anchor gap. The model assumes a walkable connection; footpaths, terrain and legal access are not verified.</p></section>
    <section><h2>Updated walking speed</h2><p>This run uses the requested 2.2 km/h. The earlier 1 mph (1.609344 km/h) search found 485 species with 27.7 walking hours;
    its result remains saved separately. Round-trip distance, the 100 m threshold, on-site birding time and all daily budgets remain unchanged.</p></section>
    <section><h2>The previous route fails the new time rule</h2><p>{old_audit['days_exceeding_activity_budget']} days in the previous 491-species route exceed the activity budget after access walking is charged.
    Its most demanding day would require {old_audit['maximum_required_activity_hours']:.1f} activity hours. That route is preserved as a result under the former assumption.</p>
    <div class="scroll"><table><thead><tr>{headers}</tr></thead><tbody>{table}</tbody></table></div>
    <p>Both searches use 600 seconds, two threads, seed 20240101, all 20,855,101 represented observations, 242,908 exact locations and 1,002,904 location/date opportunities.
    All observation, taxonomy and land rules remain unchanged. The new run reuses the valid bounded v2 road-cost cache; walking changes stop feasibility, not road costs.
    These are heuristic searches; neither establishes a global optimum.</p></section>
    <section><h2>Verification and retained files</h2><p>Independent validation checks all 366 days, source evidence, round-trip walking and daily time budgets.
    All {endpoint['unique_driving_pairs']} selected driving pairs pass strict waypoint and full geometry endpoint checks; maximum endpoint gap {endpoint['max_gap_m']:.3f} m.</p>
    <p><a href="/selected-access-review.csv">Review exact observation pins and road anchors</a>. The original source model, archive and earlier route remain unchanged.
    The router is stopped after the run. The viewer uses frozen selected-road evidence. The 8 MiB road cache remains bounded, and transient native input is removed after successful validation.</p></section></html>'''
    (OUT/'pilot-report.html').write_text(html)


def finalize():
    assert read(OUT/'mmap-600s/validation.json')['valid']
    assert read(OUT/'mmap-600s/validation.json')['walking']['speed_kmh'] == SPEED
    assert sha256(DATA/'problem.json') == endpoints.PROBLEM_SHA
    endpoints.OUT = OUT
    endpoints.audit()
    network = read(DATA/'road-network.json')
    dump(OUT/'display-verification.json',build_display_roads(OUT,ENDPOINT,network['network_release']))
    for name in ('map-context.geojson','observer-species-coverage.json'):
        shutil.copyfile(OLD/name,OUT/name)
    report()
    assert read(OUT/'mmap-walking-router-process-metrics.json')['stopped']
    dump(OUT/'completion.json',{'complete':True,'walking_speed_kmh':SPEED,'round_trip':True,
         'endpoint_and_calendar_audits_passed':True,'router_stopped':True,
         'original_problem_unchanged':True,'native_input_removed':not (OUT/'mmap-600s/problem.by').exists()})
    print(json.dumps({'complete':True,'report':str(OUT/'pilot-report.html')}),flush=True)


def main():
    WORK.mkdir(parents=True,exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)
    resources.WORK,resources.OUT = WORK,OUT
    if len(sys.argv)>1 and sys.argv[1]=='finalize':
        finalize();return
    if (OUT/'mmap-600s').exists():
        raise ValueError('preserve the existing run; use finalize for postprocessing')
    prepare()
    shutil.copyfile(__file__,OUT/'benchmark-driver.py')
    command = resources.osrm_command(True)
    command[command.index('--port')+1] = '5005'
    with resources.router('mmap-walking-router',command,
                         lambda:resources.get_json(ENDPOINT+'/nearest/v1/driving/-122.3,47.6?number=1')):
        with resources.monitor('walking-full-search'):
            print(json.dumps({'starting':'full walking-constrained search','seconds':600,
                              'walking_speed_kmh':SPEED,'cache':'existing strict v2, warm'}),flush=True)
            route = run(WORK/'problem.json',PROVIDER,OUT/'mmap-600s',seconds=600,threads=2,seed=20240101)
            print(json.dumps({'species':route['species_total'],'validation':True}),flush=True)
            del route;gc.collect()
    finalize()


if __name__ == '__main__':
    main()
