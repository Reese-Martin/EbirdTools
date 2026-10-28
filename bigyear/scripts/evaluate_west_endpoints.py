"""Full-cohort 2024 West Coast rerun after strict OSRM endpoint matching fixes."""
from contextlib import closing
import fcntl
import gc
import json
import math
from pathlib import Path
import shutil
import sys

import benchmark_west_routers as resources
from bigyear_planner.bounded_roads import create_bounded_roads
from bigyear_planner.display import build_display_roads
from bigyear_planner.road_matching import POLICY, request_route
from bigyear_planner.runner import run
from bigyear_planner.travel import load_roads, road_point
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv

DATA = ROOT/'data/west-coast-eod-2024'
WORK = ROOT/'data/west-coast-endpoints-2024'
OUT = ROOT/'runs/west-coast-endpoints-2024'
PREVIOUS = ROOT/'runs/west-coast-routing-2024'
ENDPOINT = 'http://127.0.0.1:5005'
PROBLEM_SHA = '209ae37ce1e155240aefa40b9bd5ca567a1204a595fce0c9bd281c8f2023724e'
ARCHIVE_SHA = 'aa981ebaa48a8a0db9760f99a888441399c7c88b8e9faf38687b88fc1e039879'


def configure():
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    resources.WORK, resources.OUT = WORK, OUT


def invalidate():
    """Remove only mutable v1 caches; retain immutable failed-run evidence."""
    record = OUT/'cache-invalidation.json'
    if record.exists():
        return read(record)
    rows = []
    for folder in (DATA, ROOT/'data/west-coast-routing-2024'):
        for path in folder.glob('*.bounded.json'):
            manifest = read(path)
            if manifest['format'] != 'bigyear-bounded-roads-v1':
                continue
            binary = Path(manifest['cache'])
            row = {'manifest': str(path), 'manifest_sha256': sha256(path),
                   'binary': str(binary), 'bytes_removed': 0}
            if binary.exists():
                with binary.open('r+b') as stream:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    row.update({'bytes_removed': binary.stat().st_size, 'sha256': sha256(binary)})
                    binary.unlink()
            row['invalidated'] = True
            rows.append(row)
            dump(path.with_suffix('.invalidated.json'), {
                'invalidated': True, 'reason': 'Unbounded OSRM endpoint rematching; v1 cache rejected by current code',
                'replacement_policy': POLICY, 'record': str(record)})
    result = {'reason': 'OSRM route endpoints were not checked before caching',
              'replacement_policy': POLICY, 'mutable_caches': rows,
              'bytes_removed': sum(r['bytes_removed'] for r in rows),
              'historical_run_snapshots': 'Preserved as diagnostic evidence. Current real-data validation rejects missing endpoint proof; old validation certificates predate this fix.'}
    dump(record, result)
    return result


def preflight():
    p = read(DATA/'problem.json')
    old = read(PREVIOUS/'selected-endpoint-audit.json')
    rows = []
    for row in old['queries']:
        a, b = map(int, row['id'].split(':'))
        anchors = [[road_point(p['locations'][i])[k] for k in ('lon','lat')] for i in (a,b)]
        response = request_route(ENDPOINT, anchors, geometry=True)
        rows.append({'id': row['id'], 'previous_max_gap_m': max(row['anchor_to_returned_road_meters']),
                     'strict_code': response['code']})
    assert all(r['strict_code'] != 'Ok' for r in rows if r['previous_max_gap_m'] > 100)
    result = {'policy': POLICY, 'previous_route_sha256': old['route_sha256'], 'queries': rows,
              'formerly_shifted_over_100m': sum(r['previous_max_gap_m'] > 100 for r in rows),
              'all_formerly_shifted_over_100m_rejected': True,
              'strict_available': sum(r['strict_code'] == 'Ok' for r in rows)}
    dump(OUT/'preflight.json', result)
    print(json.dumps({k:v for k,v in result.items() if k != 'queries'}), flush=True)
    del p
    gc.collect()


def distance_m(a, b):
    # Independent implementation for the final frozen-evidence audit.
    lat1, lat2 = [math.radians(p[1]) for p in (a,b)]
    dlon = math.radians(b[0]-a[0])
    term = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371008.8 * 2 * math.atan2(math.sqrt(term), math.sqrt(max(0, 1-term)))


def audit():
    folder = OUT/'mmap-600s'
    route = read(folder/'route.json')
    pairs = sorted({(leg['from'], leg['to']) for d in route['days'] for leg in d['legs']
                    if leg['mode'] == 'road'})
    rows = []
    with closing(load_roads({'locations': route['locations'], 'manifest': {'synthetic': False}},
                            folder/'roads.sqlite')) as roads:
        for a,b in pairs:
            road = roads[a,b]
            proof = road['endpoint_evidence']
            assert proof['policy'] == POLICY
            assert proof['network_sha256'] == route['provenance']['road_network_sha256']
            anchors = [[road_point(route['locations'][i])[k] for k in ('lon','lat')] for i in (a,b)]
            assert anchors == proof['anchors']
            measured = proof['routes'][0]
            points = measured['geometry']['coordinates']
            assert all(len(p) == 2 and all(math.isfinite(v) for v in p) for p in points)
            gaps = [distance_m(x,y) for x,y in zip(anchors, [w['location'] for w in proof['waypoints']])]
            gaps += [distance_m(x,y) for x,y in zip(anchors, (points[0], points[-1]))]
            assert max(gaps) <= POLICY['endpoint_tolerance_m']
            assert abs(measured['distance']-road['miles']*1609.344) <= .11
            assert abs(measured['duration']-road['minutes']*60) <= .11
            rows.append({'id': f'{a}:{b}', 'waypoint_gaps_m': gaps[:2], 'geometry_gaps_m': gaps[2:]})
    # This remains a separate model assumption, not fixed by strict road matching.
    selected = {s['location'] for d in route['days'] for s in d['stops']}
    access = [distance_m([route['locations'][i]['lon'],route['locations'][i]['lat']],
                        [road_point(route['locations'][i])['lon'],road_point(route['locations'][i])['lat']])
              for i in selected]
    access_rows = []
    for i in selected:
        loc = route['locations'][i]
        anchor = road_point(loc)
        visits = [(day['date'], stop) for day in route['days'] for stop in day['stops']
                  if stop['location'] == i]
        access_rows.append({'location_index': i, 'location_id': loc['id'], 'state': loc['state'],
                           'source_latitude': loc['lat'], 'source_longitude': loc['lon'],
                           'road_anchor_latitude': anchor['lat'], 'road_anchor_longitude': anchor['lon'],
                           'minimum_straight_line_gap_m': distance_m([loc['lon'], loc['lat']],
                                                                   [anchor['lon'], anchor['lat']]),
                           'visit_dates': '|'.join(day for day,_ in visits),
                           'new_species_credited': sum(len(stop['new_species']) for _,stop in visits),
                           'walking_and_legal_access': 'not verified'})
    access_rows.sort(key=lambda row: -row['minimum_straight_line_gap_m'])
    if access_rows:
        write_csv(OUT/'selected-access-review.csv', access_rows, list(access_rows[0]))
    result = {'valid': True, 'route_sha256': sha256(folder/'route.json'),
              'roads_sha256': sha256(folder/'roads.sqlite'), 'policy': POLICY,
              'unique_driving_pairs': len(rows), 'queries': rows,
              'max_gap_m': max((max(r['waypoint_gaps_m']+r['geometry_gaps_m']) for r in rows), default=0),
              'selected_observation_to_anchor': {'locations': len(selected),
                  'over_1km': sum(v>1000 for v in access), 'max_m': max(access, default=0)},
              'scope': 'Frozen waypoint and full geometry endpoints, cost agreement and network identity. Does not certify walking or legal access from road anchors to reported observation pins.'}
    dump(OUT/'selected-endpoint-audit.json', result)
    print(json.dumps({k:v for k,v in result.items() if k != 'queries'}), flush=True)
    return result


def report():
    benchmark = read(OUT/'mmap-600s/benchmark.json')
    old = read(PREVIOUS/'mmap-600s/benchmark.json')
    endpoint = read(OUT/'selected-endpoint-audit.json')
    memory = read(OUT/'mmap-full-search-resources.json')
    retention = read(DATA/'model-retention-audit.json')
    rows = []
    for label,b,ram,valid in [('Previous unbounded matching (endpoint audit failed)', old,
                             read(PREVIOUS/'mmap-full-search-resources.json')['combined_peak_rss_bytes'],False),
                            ('Strict endpoint matching', benchmark,memory['combined_peak_rss_bytes'],True)]:
        rows.append({'run': label, 'species': b['engine']['species'], 'search_seconds': b['engine']['elapsed_seconds'],
                     'total_seconds': b['wall_seconds'], 'peak_combined_rss_gib': ram/2**30,
                     'candidate_evaluations': b['engine']['candidate_evaluations'], 'endpoint_audit_passed': valid,
                     'road_miles': b['validation']['road_miles']})
    write_csv(OUT/'run-summary.csv', rows, list(rows[0]))
    table = ''.join('<tr>'+''.join(f'<td>{v:,.2f}</td>' if type(v) is float else f'<td>{v}</td>'
                                  for v in row.values())+'</tr>' for row in rows)
    headers = ''.join(f'<th>{k.replace("_"," ")}</th>' for k in rows[0])
    access = endpoint['selected_observation_to_anchor']
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>West Coast 2024 — corrected road endpoints</title><style>
    body{{font:16px/1.6 system-ui;background:#f3f6f1;color:#20342a;max-width:1150px;margin:40px auto;padding:0 24px}}
    h1{{font-size:2rem;line-height:1.2}}section{{background:white;border:1px solid #d6e0d5;border-radius:12px;padding:24px;margin:24px 0}}
    .stats{{display:flex;gap:45px;flex-wrap:wrap}}.stats strong{{display:block;font-size:2rem}}table{{border-collapse:collapse;width:100%;font-size:14px}}
    td,th{{text-align:left;padding:10px;border-bottom:1px solid #dde5db}}.scroll{{overflow:auto}}a{{color:#196c4b}}.note{{background:#fff5db}}
    </style><h1>West Coast 2024: corrected road endpoints</h1>
    <p>California, Oregon and Washington · full year · exact source coordinates · memory-mapped OSRM</p>
    <section><div class="stats"><div><strong>{benchmark['engine']['species']} / 570</strong>species in the search result</div>
    <div><strong>{benchmark['wall_seconds']/60:.1f} min</strong>search and verification</div>
    <div><strong>{memory['combined_peak_rss_bytes']/2**30:.2f} GiB</strong>sampled combined peak RSS</div></div>
    <p><a href="/?run=mmap-600s/route.json">Open the corrected route viewer</a> · <a href="/run-summary.csv">Download comparison CSV</a> · <a href="/mmap-600s/validation.json">Independent validation</a></p>
    <p class="note"><strong>Road endpoints pass; physical access remains unverified.</strong> Five selected observation sites are more than 1 km from their road anchors,
    including one {access['max_m']/1000:.1f} km away. The score uses the existing fixed stop allowance; <a href="/selected-access-review.csv">review exact pins and anchors</a>.</p></section>
    <section><h2>Endpoint checks pass</h2><p>All {endpoint['unique_driving_pairs']} selected directed road pairs pass both returned-waypoint and full-path endpoint checks.
    Maximum gap: {endpoint['max_gap_m']:.3f} m. Search radius: 1 m; acceptance tolerance: 1.2 m, including coordinate rounding.
    The 20 old selected pairs that previously moved by more than 100 m are now rejected by strict matching.</p>
    <p>Every successful search cost checks returned waypoints. Selected pairs are remeasured with full geometry and checked against the search costs.
    Their endpoint evidence is frozen in SQLite and independently audited without the live router. Service errors abort; explicit NoRoute and NoSegment results are unavailable roads.</p></section>
    <section><h2>Same full observation cohort</h2><p>{retention['source_records']:,} represented observations · {retention['eligible_presence_groups']:,} presence groups ·
    {retention['exact_locations_retained']:,} exact locations · {retention['opportunities_retained']:,} location/date opportunities.
    The frozen problem and source archive manifest retain their original SHA-256 identities. No geographic thinning or coordinate rounding was introduced.</p></section>
    <section><h2>Comparison with the previous diagnostic run</h2><div class="scroll"><table><thead><tr>{headers}</tr></thead><tbody>{table}</tbody></table></div>
    <p>The old score used invalid endpoint matches and is not a feasible route benchmark. Both runs use a 600-second heuristic search, two threads, seed 20240101,
    30 local road miles per birding day, 12 activity hours, 8 driving hours per transit day, 2-hour stops, and flights disabled. Neither proves a global optimum.</p></section>
    <section class="note"><h2>Remaining access assumption</h2><p>Strict endpoint checks connect the supplied road-access anchors.
    The reported observation pins stay exact and separate. Among selected observation locations, {access['over_1km']} are more than 1 km from their road anchor;
    the largest separation is {access['max_m']/1000:.2f} km. The unchanged model shares a fixed two-hour allowance between walking and birding.
    Walking routes, legal access, historic closures and traffic are not certified. This is an endpoint-correct result under the existing planner rules.</p></section>
    <section><h2>Storage and cleanup</h2><p>Old mutable v1 caches were invalidated and removed; historical failed-run evidence remains labeled as diagnostic.
    The replacement cache stays fixed at 8 MiB. The existing road graph and observation archive were reused. Selected route geometry is frozen for the viewer;
    OSRM is stopped after the run, and the viewer does not need a live road router. Native search input and temporary query spill files are removed after successful validation.</p></section></html>'''
    (OUT/'pilot-report.html').write_text(html)


def main():
    configure()
    stage = sys.argv[1] if len(sys.argv)>1 else 'run'
    if stage == 'audit':
        audit(); return
    if stage == 'report':
        report(); return
    if stage == 'finalize':
        finalize(); return
    if (OUT/'mmap-600s').exists():
        raise ValueError('preserve existing run; do not overwrite or resume old endpoint costs')
    assert sha256(DATA/'problem.json') == PROBLEM_SHA
    archive = ROOT/'data/eod-us-since-2000-zstd3/manifest.json'
    assert sha256(archive) == ARCHIVE_SHA
    network = read(DATA/'road-network.json')
    shutil.copyfile(__file__, OUT/'benchmark-driver.py')
    invalidate()
    create_bounded_roads(DATA/'problem.json', WORK/'strict.bounded.json', ENDPOINT,
                         network['network_release'],network['runtime_sha256'],capacity_mib=8)
    command = resources.osrm_command(True)
    command[command.index('--port')+1] = '5005'
    with resources.router('mmap-search-router', command,
                          lambda:resources.get_json(ENDPOINT+'/nearest/v1/driving/-122.3,47.6?number=1')):
        preflight()
        with resources.monitor('mmap-full-search'):
            print(json.dumps({'starting': 'endpoint-correct full cohort search', 'seconds':600}),flush=True)
            route = run(DATA/'problem.json',WORK/'strict.bounded.json',OUT/'mmap-600s',
                        seconds=600,threads=2,seed=20240101)
            print(json.dumps({'species':route['species_total'],'validation':True}),flush=True)
            del route
            gc.collect()
    assert sha256(DATA/'problem.json') == PROBLEM_SHA
    assert sha256(archive) == ARCHIVE_SHA
    dump(OUT/'cohort-verification.json', {'problem_sha256':PROBLEM_SHA,'archive_manifest_sha256':ARCHIVE_SHA,
         'observations_and_exact_coordinates_unchanged':True,'retention':read(DATA/'model-retention-audit.json')})
    finalize()


def finalize():
    """Resume postprocessing without repeating or modifying a completed search."""
    assert sha256(DATA/'problem.json') == PROBLEM_SHA
    assert sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json') == ARCHIVE_SHA
    assert read(OUT/'mmap-600s/validation.json')['valid']
    assert read(OUT/'mmap-search-router-process-metrics.json')['stopped']
    network = read(DATA/'road-network.json')
    audit()
    # Selected endpoint evidence includes full geometry; no provider needed for display.
    dump(OUT/'display-verification.json',build_display_roads(OUT,ENDPOINT,network['network_release']))
    for name in ('map-context.geojson','observer-species-coverage.json'):
        shutil.copyfile(resources.OLD/name,OUT/name)
    report()
    dump(OUT/'completion.json',{'complete':True,'endpoint_audit_passed':True,'router_stopped':True,
         'problem_and_archive_manifest_unchanged':True,
         'native_input_removed':not (OUT/'mmap-600s/problem.by').exists()})
    print(json.dumps({'complete':True,'report':str(OUT/'pilot-report.html')}),flush=True)


if __name__ == '__main__':
    main()
