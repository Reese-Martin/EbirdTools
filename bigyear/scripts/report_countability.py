"""Publish the complete 626-species decision ledger and audited rerun."""

from collections import Counter
import csv
from html import escape
import json

from bigyear_planner.util import dump, read, sha256, write_csv
from audit_countability import BASE, DATA, OUT, POLICY, RULES, INTRODUCED, REVIEW
from benchmark_countability import TAG


def main():
    result=read(OUT/'countability-benchmark-result.json')
    verified=read(OUT/'countability-viewer-verification.json')
    folder=OUT/TAG
    if not result['valid'] or not verified['valid'] or verified['route_sha256']!=sha256(folder/'route.json'):
        raise ValueError('publish only independently validated and served-viewer checked results')
    route=read(folder/'route.json');policy=read(DATA/'policy.json')
    source_check=read(folder/'countability-source-validation.json')
    if not source_check['valid'] or source_check['route_sha256']!=sha256(folder/'route.json'):
        raise ValueError('selected publisher evidence has not passed the post-search policy check')
    engine=read(folder/'engine.json');audit=read(folder/'validation.json')
    construction=read(folder/'regional-exploration.json')
    resources=read(OUT/(TAG+'-resources.json'))
    active_total=resources['wall_seconds']
    peak_rss=resources['combined_peak_rss_bytes']
    timing_note=''
    if result.get('resumed_from'):
        previous_resources=read(OUT/(result['resumed_from']+'-resources.json'))
        active_total+=previous_resources['wall_seconds']
        peak_rss=max(peak_rss,previous_resources['combined_peak_rss_bytes'])
        timing_note='<p>A recorded 903-second idle sleep exhausted the first attempt’s libc++ clock budget. Its 610-species result passed both source and travel audits. The final run resumes that verified incumbent for a fresh 600 awake seconds using macOS mach_absolute_time. A process-scoped idle-sleep assertion is released after completion. Persistent power settings and display sleep are unchanged. Active elapsed figures exclude system suspension; source-metadata retrieval and model preparation are separate.</p>'
    species=list(csv.DictReader((DATA/'species-decisions.csv').open()))
    credited=[r for r in species if r['credited_in_baseline']=='True']
    unresolved=[r for r in credited if r['decision']=='unresolved']
    excluded=[r for r in credited if r['decision']=='excluded']
    evidence=list(csv.DictReader((DATA/'evidence-decisions.csv').open()))
    totals=Counter(r['decision'] for r in credited)
    assert len(credited)==626 and dict(totals)==policy['baseline_decisions']
    export=OUT/'countability-audit';export.mkdir(exist_ok=True)
    write_csv(export/'species-audit.csv',credited,list(credited[0]))
    for name,rows in [('unresolved',unresolved),('excluded',excluded)]:
        (export/name).mkdir(exist_ok=True)
        write_csv(export/name/'species-audit.csv',rows,list(credited[0]))
    (export/'all-evidence').mkdir(exist_ok=True)
    write_csv(export/'all-evidence/species-audit.csv',evidence,list(evidence[0]))
    supported_before={r['species_id'] for r in credited if r['decision']=='supported'}
    scored_after={sp for d in route['days'] for s in d['stops'] for sp in s['species']}
    names={r['id']:r['name'] for r in route['species']}
    gains=sorted(names[v] for v in scored_after-supported_before)
    losses=sorted(names[v] for v in supported_before-scored_after)
    days=Counter(d['kind'] for d in route['days'])
    comparison={**result,'baseline_audit':dict(totals),'same_policy_gain':route['species_total']-len(supported_before),
         'gained_supported_species':gains,'baseline_supported_species_not_in_rerun':losses,
         'final_continuation_active_seconds':resources['wall_seconds'],
         'completed_routing_attempts_active_seconds':active_total,'peak_process_rss_bytes':peak_rss,
         'reporting_scope':'Supported under documented publisher-review proxy; individual wild status and actual encounter are not certified.'}
    dump(OUT/'countability-comparison.json',comparison)
    rows=[]
    for item in sorted(credited,key=lambda v:v['name']):
        records=[r for r in evidence if r['species_id']==item['species_id']]
        details=[]
        for r in records:
            source='https://www.gbif.org/occurrence/'+r['representative_gbifid']
            references=' '.join(f'<a href="{escape(ref,quote=True)}">Rule/event source ↗</a>' for ref in r['reference'].split('; ') if ref.startswith('https://'))
            details.append(f"<li>{escape(r['date'])} · {escape(r['state'])} · {escape(r['locality'])}: <b>{escape(r['decision'])}</b> — {escape(r['reason'])}. <a href='{source}'>GBIF {escape(r['representative_gbifid'])} ↗</a> {references}</li>")
        summary=f"{item['supported_records']} supported / {item['unresolved_records']} unresolved supporting records"
        excluded_records=int(item['selected_records'])-int(item['supported_records'])-int(item['unresolved_records'])
        if excluded_records:
            summary+=f' / {excluded_records} excluded'
        hidden='' if item['decision']=='unresolved' else ' hidden'
        rows.append(f"<tr data-status='{item['decision']}'{hidden}><td><b>{escape(item['name'])}</b><br><small>{escape(item['source_name'])}</small></td><td>{escape(item['decision'])}</td><td><details><summary>{escape(summary)}</summary><p>{escape(item['selected_reasons'])}</p><ul>{''.join(details)}</ul></details></td></tr>")
    states=', '.join(v.removeprefix('US-') for v in result['birding_states'])
    html=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>2024 countability audit and rerun</title>
<style>body{{font:16px/1.6 system-ui;max-width:1160px;margin:32px auto;padding:0 24px;color:#15251b;background:#f6f8f6}}section{{background:white;padding:24px;margin:20px 0;border-radius:10px}}h1,h2{{line-height:1.25}}a{{color:#24683c}}.note{{border-left:4px solid #36824b}}.cards{{display:flex;gap:30px;flex-wrap:wrap}}.cards strong{{display:block;font-size:32px}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;vertical-align:top;border-bottom:1px solid #dbe9df;padding:12px}}small{{color:#526159}}input,select{{padding:10px;border:1px solid #bdcbbf;border-radius:5px;font:inherit}}.controls{{display:flex;gap:16px;align-items:center;flex-wrap:wrap}}summary{{cursor:pointer}}li{{margin-bottom:12px}}td:first-child{{width:25%}}td:nth-child(2){{width:14%}}[hidden]{{display:none!important}}</style>
<h1>2024 countability audit and rerun</h1><p><a href="/?run={TAG}/route.json">Open the audited route and cumulative curves ↗</a> · <a href="/?run={BASE.name}/route.json">Original 626-species route ↗</a></p>
<section class="note"><h2>{route['species_total']} supported modeled species · {len(result['birding_states'])} birding states · {result['flight_days']} flights</h2>
<div class="cards"><div><strong>{totals['supported']}</strong>original route: supported</div><div><strong>{totals['unresolved']}</strong>original route: unresolved</div><div><strong>{totals['excluded']}</strong>original route: excluded</div><div><strong>{route['species_total']-totals['supported']:+}</strong>rerun gain under the same audited policy</div></div>
<p>All 626 originally credited species and all 3,525 supporting public records were audited. Their published species, concept, date and coordinates match the saved original route. Historical exclusions are {escape(', '.join(r['name'] for r in excluded))}. Unresolved cases receive zero credit in both the rerun and its all-observer comparison.</p>
<p><b>What “supported” means:</b> the score uses the documented public eBird review process as an explicit proxy for native wild occurrences. Individual approval, captive/exotic status and actual encounter details are not supplied by EOD and remain unverified. This is a reproducible, conservative modeled score under the rules below; it is not an official ABA count or a certified lower bound on what a birder could see.</p></section>
<section><h2>The scoring policy</h2><p>Policy: <code>{POLICY}</code>. Native ABA code-1/2 records qualify under the publisher-review proxy. Three species have explicit ABA-Area-wide established-population support: House Sparrow, European Starling and Eurasian Collared-Dove. Other introduced taxa require a reviewed population match for the selected source record. Code-6 extinct/extirpated taxa receive no 2024 credit.</p>
<p>Code-3/4/5 records need independently corroborated events under this additional conservative screen. Matching accepted Texas, California and Rhode Island records restore credit only to the specific audited source occurrences at the matching place and date. This screen is our model policy; ABA rarity codes alone do not make a bird uncountable. Condor/crane reintroduction provenance and potentially captive-site records remain unresolved when the required evidence is absent.</p>
<p>The frozen ABA 8.19 identities were checked against the 2024 ABA 8.16 list, including genus/name changes. Vega Gull remains unresolved under the historical mapping screen. Existing unresolved broad taxon concepts were not silently promoted.</p>
<p><a href="{RULES}">ABA recording rules ↗</a> · <a href="{INTRODUCED}">Introduced-population guidance ↗</a> · <a href="{REVIEW}">Cornell public-review documentation ↗</a></p></section>
<section><h2>Complete cohort retained</h2><p>All 127,097,446 represented observations remain bound through the original archive. The scoring derivative preserves all 1,936,106 exact locations and all 6,858,225 canonical location/date opportunities, including opportunities with zero supported credit. Coordinates and road-routing identities are unchanged. The scoring layer currently admits {result['scoring_eligible_observations']:,} represented observations; all remaining original observations remain available for review.</p>
<p>The all-observer curve uses the same audited policy and has a {result['all_observer_supported_species']}-species annual pool. This is a data ceiling; no feasible itinerary achieving the whole pool has been established. A supported score of 705 is impossible under this frozen policy until additional held evidence is resolved. This ceiling reflects the conservative evidence screen, not proof that other species were absent in 2024. The original 626-species route and its original 764-species presence-only comparison remain available with their original policy.</p></section>
<section><h2>Rerun and validation</h2><p>The rerun birds {states}. It uses {result['road_miles']:,.0f} road miles, {days['birding']} birding days, {days['road_transit']} road-transit days, {days['flight']} flight days and {days['rest']} rest days. Walking remains the full round trip above 100 m at 2.2 km/h. The original dated 2024 operated-service flight network and road-endpoint policy are unchanged.</p>
<table><tr><th>Measure</th><th>Result</th></tr><tr><td>Regional exploration / final native search</td><td>{construction['elapsed_seconds']/60:.2f} / {engine['elapsed_seconds']/60:.2f} awake minutes</td></tr><tr><td>Final run including independent validation</td><td>{resources['wall_seconds']/60:.2f} active minutes</td></tr><tr><td>Completed routing attempts combined</td><td>{active_total/60:.2f} active minutes</td></tr><tr><td>Peak combined process RSS</td><td>{peak_rss/2**30:.2f} GiB</td></tr><tr><td>Completed final native candidate evaluations</td><td>{engine['candidate_evaluations']:,}</td></tr><tr><td>Independent source/calendar audit</td><td>{audit['days_checked']} days; {audit['evidence_rows_checked']:,} scored species-evidence rows</td></tr><tr><td>Road endpoint and flight checks</td><td>{audit['endpoint_matching']['pairs_checked']} directed pairs; {audit['flight_service']['links_checked']} dated flights</td></tr></table>{timing_note}
<p>Bounded caches and normal temporary-file cleanup remain active. Discovery and validation routers are stopped. Auditable source metadata, decisions and completed results are retained. This run does not establish global optimality.</p>
<p>Supported species gained over the rescored original itinerary: {escape(', '.join(gains)) or 'None'}. Original supported species absent from the new itinerary: {escape(', '.join(losses)) or 'None'}.</p></section>
<section><h2>Post-search source check</h2><p>All {source_check['records_checked']:,} scored publisher records in the new itinerary pass the same countability decision rules, including newly selected occurrences. No unresolved or excluded source record contributes to the score. Source metadata snapshots and hashes are retained alongside the original 626-species audit.</p></section>
<section><h2>Review all 626 decisions</h2><p><a href="/countability-audit/species-audit.csv">Download all 626 species decisions</a> · <a href="/countability-audit/unresolved/species-audit.csv">Unresolved cases</a> · <a href="/countability-audit/excluded/species-audit.csv">Excluded cases</a> · <a href="/countability-audit/all-evidence/species-audit.csv">All 3,525 evidence decisions</a></p>
<div class="controls"><input id="query" type="search" aria-label="Search species" placeholder="Search species or evidence"><select id="status" aria-label="Decision status"><option value="all">All decisions</option><option value="unresolved" selected>Unresolved</option><option value="excluded">Excluded</option><option value="supported">Supported under policy</option></select><output id="shown" role="status">23 of 626 species shown</output></div>
<table><thead><tr><th>Species</th><th>Decision</th><th>Supporting records and sources</th></tr></thead><tbody id="species-rows">{''.join(rows)}</tbody></table></section>
<script>const query=document.getElementById('query'),status=document.getElementById('status'),rows=Array.from(document.querySelectorAll('#species-rows tr'));function filter(){{let n=0;for(const row of rows){{row.hidden=(status.value!=='all'&&row.dataset.status!==status.value)||!row.textContent.toLowerCase().includes(query.value.toLowerCase());if(!row.hidden)n++;}}document.getElementById('shown').textContent=n+' of 626 species shown';}}query.addEventListener('input',filter);status.addEventListener('change',filter);</script></html>'''
    (OUT/'countability-report.html').write_text(html)
    (OUT/'pilot-report.html').write_text(html)
    print(json.dumps({'baseline_decisions':dict(totals),'rerun_species':route['species_total'],'gained':len(gains),'lost':len(losses)}),flush=True)


if __name__=='__main__':
    main()
