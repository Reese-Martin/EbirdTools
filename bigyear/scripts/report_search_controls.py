"""Publish comparisons with separate search, scoring, access and missing-input limits."""
from collections import Counter
from html import escape
import json

from bigyear_planner.util import dump,read,sha256,write_csv
from evaluate_lower48 import OUT

TAG='reconciled-2024-provisional-access-v3'
CONTROL=OUT/'search-calendar-controls-v2'


def main():
    final=OUT/TAG
    result=read(final/'result.json');source=read(final/'countability-source-validation.json')
    if not result['validation']['valid'] or not source['valid']:raise ValueError('publication requires travel and publisher validation')
    surface=read(final/'surface-audit/marine-access-audit.json')
    if surface['route_sha256']!=sha256(final/'route.json') or surface['water_crossing_holds']:
        raise ValueError('selected walks still need ocean-access review')
    matrix=[]
    for name in ['old-local30','new-local30','old-local150','new-local150']:
        r=read(CONTROL/name/'result.json');engine=r['engine']
        matrix.append({'arm':name,'species':engine['species'],'active_seconds':engine['elapsed_seconds'],
           'completed_evaluations':engine['candidate_evaluations'],'deferred':engine['deferred_candidates'],
           'road_queries':r['cache'].get('successful_road_queries',0),
           'airport_plans':r['cache'].get('flight_transition_plans',0),'binary_sha256':r['binary_sha256']})
    write_csv(CONTROL/'comparison.csv',matrix,list(matrix[0]))
    summary=read(CONTROL/'summary.json');regional=read(final/'regional-exploration.json')
    primary_engine=read(CONTROL/'final/engine.json')
    attempts=[json.loads(line) for line in (final/'regional-attempts.jsonl').read_text().splitlines()]
    regions=Counter(r['region'] for r in attempts)
    marine=read(CONTROL/'rules-access/marine-cohort.json')
    resources=read(OUT/(TAG+'-resources.json'))
    route=read(final/'route.json')
    states=sorted({route['locations'][stop['location']]['state'] for d in route['days'] for stop in d['stops']})
    report={'controls':matrix,'followup_primary_species':summary['final_species'],
      'reconciled_primary':result['primary'],'reconciled_provisional':result['provisional'],
      'reconciled_total':result['species'],'target':759,'gap':759-result['species'],
      'source_records_checked':source['records_checked'],'source_postcheck':source['valid'],
      'primary_followup_feasible_block_evaluations':primary_engine['regional_block_feasible'],
      'regions_tested':dict(regions),'regional_attempts':regional['attempts'],
      'peak_combined_rss_bytes':resources['combined_peak_rss_bytes'],
      'end_to_end_wall_seconds':resources['wall_seconds'],
      'marine_scope':marine['counts'],'recordholder_replay':'pending_input',
      'native_boat_routing':'not_implemented','all_source_observations_preserved':True,
      'selected_walks_checked':surface['walks_checked'],'selected_ocean_crossings_held':surface['water_crossing_holds'],
      'walking_kmh':2.2,'route_sha256':sha256(final/'route.json')}
    dump(CONTROL/'evaluation-result.json',report)
    rows=''.join(f"<tr><td>{escape(r['arm'])}</td><td><b>{r['species']}</b></td><td>{r['completed_evaluations']:,}</td><td>{r['deferred']:,}</td><td>{r['road_queries']:,}</td></tr>" for r in matrix)
    provisional='<br>'.join(escape(s.removeprefix('aba2024:').replace('_',' ')) for s in result['provisional_species']) or 'None selected'
    scope=''.join(f"<tr><td>{escape(r['scope'].replace('_',' '))}</td><td>{r['source_groups']:,}</td><td>{r['represented_observations']:,}</td></tr>" for r in marine['counts'])
    html=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>2024 search and record comparison</title><style>
:root{{font-family:system-ui;color:#193c32;background:#f2f5ef}}body{{margin:0}}main{{max-width:1100px;margin:auto;padding:36px 24px}}h1{{font-size:38px;line-height:1.12;max-width:820px;margin:14px 0}}h2{{font-size:23px}}p{{max-width:950px;line-height:1.6}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}.card,section{{background:white;border:1px solid #d4e0d5;border-radius:12px;padding:22px;margin:20px 0}}.card b{{font-size:42px;display:block}}.card small{{display:block;line-height:1.4;color:#51685b}}a{{color:#176a52}}.button{{display:inline-block;padding:11px 16px;background:#176a52;color:white;border-radius:6px;text-decoration:none;margin-right:12px}}.muted{{color:#51685b}}.hold{{border-left:5px solid #b77625}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:12px 8px;border-bottom:1px solid #dfe7df;text-align:left}}th{{background:#edf3eb}}.scroll{{overflow:auto}}code{{overflow-wrap:anywhere}}@media(max-width:700px){{.cards{{grid-template-columns:1fr}}.card{{margin:4px 0}}h1{{font-size:30px}}}}</style>
<main><small>LOWER 48 + DC · 2024 · ALL SOURCE OBSERVATIONS PRESERVED</small>
<h1>Search repairs, a fair calendar test, and a clearer score</h1>
<p>Recorded dates are indexed at every exact source pin. Road and airport discovery have reserved workers. Regional trips are built with measured connections, and unresolved queries remain retryable. Walking remains a full round trip above 100 m at <b>2.2 km/h</b>.</p>
<div class="cards"><div class="card"><b>{result['species']}</b>reconciled modeled species<small>{result['primary']} primary + {result['provisional']} provisional · {759-result['species']} short of working target 759</small></div><div class="card"><b>783</b>eligible terrestrial species in pool<small>770 primary + 13 documented provisional additions; this union is not a demonstrated attainable score</small></div><div class="card"><b>{source['records_checked']:,}</b>selected source records checked<small>Travel/calendar and publisher checks passed · individual wildness remains an explicit proxy</small></div></div>
<p><a class="button" href="/?run={TAG}/route.json">Open reconciled route ↗</a><a href="/?run=aba2024-aligned-flights-900s-600s/route.json">Preserved original 648 route</a></p>
<section><h2>Matched search × calendar controls</h2><p>Each arm started from the same verified 648-species route and base warm road cache, with two threads, seed 20240101 and 180 active search seconds. The widened-cap seed's additional roads were resolved before timing. All other rules stayed fixed: twelve-hour activity days, eight-hour transit driving, two-hour visits and full-day flights with separate driving days.</p>
<div class="scroll"><table><thead><tr><th>Search / local daily mileage cap</th><th>Species</th><th>Completed evaluations</th><th>Deferred evaluations</th><th>New measured roads</th></tr></thead><tbody>{rows}</tbody></table></div>
<p><b>The revised short trial reduced queue churn but did not improve the score.</b> The 150-mile cap alone also did not help. One seed and a short budget are insufficient to rank stochastic algorithms; deferred counts include repeated candidates. Follow-up changes to nearby-date preference and annual regional ranking are separate from these four frozen arms.</p><a href="/search-calendar-controls-v2/comparison.csv">Download comparison CSV</a></section>
<section><h2>Longer follow-ups and remaining constraints</h2><p>Regional trip reconstruction now produces executable blocks: the primary follow-up evaluated {primary_engine['regional_block_feasible']:,} feasible macro proposals, including repetitions, versus zero in the earlier 648-species run. This measures calendar feasibility, not unique improvements.</p><p>The primary-only follow-up reached <b>{summary['final_species']}</b> after 600 seconds of regional construction and 300 seconds of native search. The separate reconciled run used 300 + 300 seconds, with provisional scoring and a known water-crossing access hold. Different budgets and scoring policies cannot be treated as controlled search gains.</p><p>The reconciled regional stage tested <b>{len(regions)} of eight regions</b>, made {regional['attempts']} constituent evaluations and retained {regional['proposal_library_size']} feasible trip blocks. It finished with birding stops in {len(states)} states, {sum(d['kind']=='flight' for d in route['days'])} flight days and {sum(d['road_miles'] for d in route['days']):,.0f} road miles. Peak combined process memory was {resources['combined_peak_rss_bytes']/2**30:.2f} GiB. Search plus endpoint/calendar validation took {resources['wall_seconds']/60:.1f} minutes; the publisher postcheck ran separately.</p><p>The daily model still requires entire flight days and separate ground-access days. Passenger-boat and pelagic search are not implemented. Canonical within-day stop order, two-hour visits, missing checklist timing and unverified private access remain material limitations. These results do not establish optimality or explain every species in the record gap.</p></section>
<section><h2>2024 provisional treatment</h2><p>The dated July 2024 ABA decision and Pyle's Tables 2a/b allow provisional species subject to the remaining rules. The separate v3 policy limits the additions to documented jurisdictions/events. Hawaii-only entries and identification-unaccepted cases remain held. California Yellow-headed Parrot provisional treatment does not revive the extirpated Florida primary population. Cattle Tyrant remains a provenance-uncertain provisional case, with TBRC non-acceptance disclosed.</p><p><b>Selected provisional scoring units:</b><br>{provisional}</p><p>Public publisher review and explicit population envelopes remain modeling assumptions, not individual ABA certification.</p></section>
<section><h2>Marine coverage and access audit</h2><div class="scroll"><table><thead><tr><th>Raw 2024 cohort scope</th><th>Source groups</th><th>Represented observations</th></tr></thead><tbody>{scope}</tbody></table></div><p>Eleven source concepts appear only among excluded marine/boundary records. They need taxonomic, jurisdiction and access review. ABA adjacent-water rules can extend to 200 miles or a jurisdictional midpoint; terrestrial state polygons are narrower. Source country/state labels do not prove nearest-land jurisdiction.</p><p>The original route's Napatree Point stop in Rhode Island assumed a walk with roughly 705 metres of open water. A real foot path may repair it; no validated path has been obtained. The reconciled search holds that exact pin on every date, preserving its source observations. A zero-length foot-routing probe was rejected.</p><p>Boat-access input validation requires dated operated service, full sailing time, port access, exact observation/access evidence and permission where necessary. A fourteen-hour pelagic trip exceeds the current twelve-hour activity rule. No marine bird receives credit from an invented straight-line boat route.</p><a href="/search-calendar-controls-v2/rules-access/marine-only-candidates.csv">Marine-only candidate ledger</a></section>
<section class="hold"><h2>Dobson's dated replay is pending input</h2><p>No verified observer ID or dated recordholder itinerary has been supplied. No other birder's route was substituted. Empty CSV and identity/hash-bound manifest templates are ready. The replay coverage audit matches exact numeric coordinates, source dates and 2024 taxonomy; missing records are reported as gaps, never replaced with nearby observations. Strict timed replay also needs checklist chronology, time zones and marine/transport evidence.</p><a href="/search-calendar-controls-v2/recordholder-replay/replay-status.json">Replay status and missing inputs</a></section>
<section><h2>Evidence and retention</h2><p><a href="https://www.aba.org/aba-checklist-committee-report-july-2024/">ABA July 2024 decision</a> · <a href="https://www.birdpop.org/docs/pubs/Pyle_2024-ABA-Checklist-in-an-evolving-world_NAB-75-1-10.pdf">Pyle 2024 Tables 2a/b/c</a> · <a href="https://www.aba.org/aba-recording-rules-and-interpretations/">Recording rules and adjacent waters</a> · <a href="https://www.texasbirdrecordscommittee.org/review-information/list-of-statements/cattle-tyrant">TBRC Cattle Tyrant statement</a></p><p>Original CSV/Parquet and all earlier verified routes remain intact. The failed model projection and successful-build temporary projection were removed. Routing caches and small result/audit files remain for reproducibility; owned router processes and process-scoped sleep assertions are released at completion.</p><small class="muted">Policy <code>{result['policy']}</code><br>Route SHA-256 <code>{sha256(final/'route.json')}</code></small></section></main></html>'''
    target=OUT/'2024-search-calendar-report.html';target.write_text(html)
    (OUT/'pilot-report.html').write_text(html)
    dump(OUT/'run-status.json',{'stage':'completed','run':TAG,'best_run':TAG,'best_species':result['species'],
        'message':f"{result['species']} modeled species ({result['primary']} primary + {result['provisional']} provisional); source and calendar audits passed. Recordholder itinerary replay awaits input; boat search remains unimplemented."})
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
