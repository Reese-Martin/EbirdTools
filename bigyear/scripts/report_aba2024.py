"""Reviewable full-source reconciliation, kept separate from the v1 audit."""

from collections import Counter,defaultdict
import csv
from html import escape
import json

from bigyear_planner.util import ROOT,dump,read,sha256,write_csv
from align_aba2024 import DATA,POLICY,checklist
from benchmark_aba2024 import OUT,TAG


def main():
    taxa=list(csv.DictReader((DATA/'taxonomy.csv').open()))
    disposition=list(csv.DictReader((DATA/'full-source-dispositions.csv').open()))
    totals=defaultdict(Counter)
    for r in disposition:
        totals[r['source_name']][r['disposition']]+=int(r['source_groups'])
    ledger=[]
    for t in taxa:
        eligible=totals[t['source_name']]['eligible']
        ledger.append({**t,'eligible_source_groups':eligible,
            'held_or_excluded_source_groups':sum(totals[t['source_name']].values())-eligible,
            'annual_source_disposition':'eligible evidence available' if eligible else
                ('not approved / historical exclusion' if t['aba_eligible']=='0' else 'held: no resolved eligible evidence'),
            'disposition_counts':json.dumps(totals[t['source_name']],sort_keys=True)})
    exports=OUT/'aba2024-audit';exports.mkdir(exist_ok=True)
    for name in ['first-23','unresolved','approved-set']:
        (exports/name).mkdir(exist_ok=True)
    write_csv(exports/'species-audit.csv',ledger,list(ledger[0]))
    flagged=list(csv.DictReader((DATA/'first-23-decisions.csv').open()))
    write_csv(exports/'first-23/species-audit.csv',flagged,list(flagged[0]))
    held=[r for r in ledger if not r['eligible_source_groups'] and r['aba_eligible']=='1']
    write_csv(exports/'unresolved/species-audit.csv',held,list(ledger[0]))
    approved=checklist();coverage=[]
    for name,entry in approved.items():
        matching=[r for r in ledger if r['aba_2024_scientific_name']==name]
        n=sum(r['eligible_source_groups'] for r in matching)
        coverage.append({'species_id':'aba2024:'+name.replace(' ','_'),'name':entry['name'],
            'scientific_name':name,'aba_code':entry['code'],'source_concepts':'|'.join(r['source_name'] for r in matching),
            'eligible_groups':n,'status':'eligible 2024 mainland observations' if n else
                ('historical; no 2024 credit' if entry['code']=='6' else
                 ('source records held / unavailable on land' if matching else 'absent from this lower-48/DC source cohort'))})
    write_csv(exports/'approved-set/species-audit.csv',coverage,list(coverage[0]))
    stats=read(DATA/'dataset/manifest.json')['statistics'];policy=read(DATA/'policy.json')
    if len([r for r in coverage if r['eligible_groups']])!=stats['annual_recorded_species_ceiling']:
        raise ValueError('full approved-set ledger differs from independent source query')
    snapshot={'valid':True,'policy':POLICY,'policy_sha256':sha256(DATA/'policy.json'),
        'source_concepts':len(taxa),'first_23':policy['first_23'],'annual_species_pool':stats['annual_recorded_species_ceiling'],
        'eligible_source_groups':stats['eod_daily_presence_groups'],'represented_observations':stats['represented_observations'],
        'unresolved_approved_source_concepts_without_eligible_records':len(held),
        'approved_set_coverage':dict(Counter(r['status'] for r in coverage)),
        'raw_archive_unchanged':sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json')=='aa981ebaa48a8a0db9760f99a888441399c7c88b8e9faf38687b88fc1e039879',
        'exports':{str(p.relative_to(OUT)):sha256(p) for p in exports.rglob('*.csv')}}
    if not snapshot['raw_archive_unchanged']:
        raise ValueError('original source binding differs')
    dump(OUT/'aba2024-alignment-audit.json',snapshot)
    first_rows=''.join('<tr>'+''.join('<td>'+escape(str(r[k]))+'</td>' for k in
        ['name','selected_record_decision','supported_records','unresolved_records'])+'</tr>' for r in flagged)
    rows=''.join('<tr data-status="'+escape(r['annual_source_disposition'])+'">'+''.join('<td>'+escape(str(r[k]))+'</td>'
        for k in ['common_name','source_name','aba_2024_scientific_name','annual_source_disposition','eligible_source_groups'])+'</tr>' for r in ledger)
    status=read(OUT/'run-status.json')
    result_path=OUT/'aba2024-aligned-benchmark-result.json'
    if result_path.exists():
        result=read(result_path)
        run_text=f"<b>{result['species']} modeled species</b> · {result['flight_days']} flight days · {result['road_miles']:,.0f} road miles. Independent source/calendar/road audit: {result['valid']}. Publisher postcheck: {escape(str(result['publisher_source_postcheck']))}."
    else:
        run_text=escape(status.get('message','Benchmark in progress.'))
    html=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ABA 2024 alignment audit</title><style>
body{{font:16px/1.5 system-ui;background:#f4f7f3;color:#173329;margin:0}}main{{max-width:1240px;margin:auto;padding:36px 28px}}h1{{font-size:38px;line-height:1.12}}a{{color:#166853}}.cards{{display:flex;flex-wrap:wrap;gap:16px}}.card,.panel{{background:white;border:1px solid #d5e0d7;border-radius:12px;padding:20px;margin:16px 0}}.card{{flex:1;min-width:200px}}.card b{{display:block;font-size:36px}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #dfe6df;vertical-align:top}}th{{background:#e9f0e9;position:sticky;top:0}}.scroll{{overflow:auto;max-height:650px}}input{{padding:12px;width:min(90%,520px);font:inherit}}small{{color:#51665a}}code{{overflow-wrap:anywhere}}</style>
<main><small>LOWER 48 + DC · FULL YEAR 2024 · ABA 8.16</small><h1>A broader, historically aligned scoring pool</h1>
<p>All 858 source taxon concepts have an explicit disposition. Approved rare birds use the same public publisher-review proxy as common native birds; rarity code alone does not exclude them. Introduced and reintroduced populations have explicit geographic rules.</p>
<div class="cards"><div class="card"><b>770</b>eligible species in the full cohort<br><small>Previous conservative pool: 654</small></div><div class="card"><b>20 / 23</b>original held cases now supported<br><small>At least one previously selected record</small></div><div class="card"><b>3</b>original selected cases still held<br><small>TX Lilac-crowned Amazon · Graylag Goose · CA flamingo</small></div></div>
<section class="panel"><h2>Nationwide benchmark</h2><p>{run_text}</p><p><a href="/?run={TAG}/route.json">Open the new route when complete ↗</a> · <a href="/?run=countability-flights-awake-600s/route.json">Previous verified 614-species route</a></p><p>Exact coordinates, all source observations, 2.2 km/h walking, measured roads and observed 2024 airline service are retained. Up to 900 seconds of regional exploration and 600 active seconds of native search.</p></section>
<section class="panel"><h2>What changed in the audit</h2><p>The former requirement for independent corroboration of every code 3–5 rarity was an additional conservative model screen, not an ABA rule. It has been removed. Repatriated condors and cranes can qualify after their population has hatched wild young; individual wild-born provenance is not required by that rule.</p><p>Herring Gull daughter concepts collapse to one 2024 species. Broad 2024 Warbling Vireo, Yellow Warbler, Whimbrel and Herring Gull concepts are restored without inferring membership in later splits. No held record gains credit solely because its species appears on the checklist.</p><p><b>Modeled support is not individual ABA certification.</b> Public eBird review is an explicit quality and wild-occurrence proxy. Geographic envelopes operationalize named established populations; they are conservative model boundaries, not official ABA boundaries. The archive lacks individual captive/exotic flags and observer encounter/access evidence. A zoo locality by itself does not imply a captive bird.</p><p>{stats['eod_daily_presence_groups']:,} eligible source groups represent {stats['represented_observations']:,} observations at {stats['locations']:,} exact locations. Original Parquet and prior runs remain unchanged. Temporary query spill is removed on close or failure; bounded routing caches remain reusable.</p></section>
<section class="panel"><h2>Original 23 held cases</h2><p>These decisions apply to the original selected evidence. Lilac-crowned Parrot and American Flamingo have eligible evidence elsewhere; the Texas and California records below remain held.</p><a href="/aba2024-audit/first-23/species-audit.csv">Download first 23 decisions</a><div class="scroll"><table><thead><tr><th>Species</th><th>Selected record decision</th><th>Supported records</th><th>Held records</th></tr></thead><tbody>{first_rows}</tbody></table></div></section>
<section class="panel"><h2>Complete source crosswalk</h2><p><a href="/aba2024-audit/species-audit.csv">All 858 source concepts</a> · <a href="/aba2024-audit/unresolved/species-audit.csv">Approved concepts without eligible evidence</a> · <a href="/aba2024-audit/approved-set/species-audit.csv">Full approved-set coverage ledger</a></p><input id="filter" aria-label="Filter source crosswalk" placeholder="Filter species, source concept, or disposition"><p id="matches">858 source concepts</p><div class="scroll"><table><thead><tr><th>Common name</th><th>Source concept</th><th>2024 scoring identity</th><th>Disposition</th><th>Eligible source groups</th></tr></thead><tbody id="crosswalk">{rows}</tbody></table></div></section>
<section class="panel"><h2>Primary sources and limits</h2><p><a href="https://www.aba.org/wp-content/uploads/2020/03/ABA_Checklist-8.16.csv.zip">Official 2024 checklist</a> · <a href="https://www.aba.org/wp-content/uploads/2025/02/2024_ABA_CLC_Report.pdf">2024 committee report</a> · <a href="https://www.aba.org/aba-recording-rules-and-interpretations/">Recording rules</a> · <a href="https://www.aba.org/aba-area-introduced-species/">Introduced populations</a> · <a href="https://www.aba.org/aba-area-reintroduced-indigenous-species/">Repatriated populations</a> · <a href="https://wos.org/records/votingsummary/spring-2024/">Accepted Whooper Swan event</a> · <a href="https://support.ebird.org/en/support/solutions/articles/48001078113">Publisher review process</a></p><p>The official checklist CSV and PDF contain 1,151 coded species rows despite a 1,152-species header. This publisher discrepancy is retained explicitly; no extra species is invented. Species without suitable lower-48/DC 2024 observations cannot be added to a route by checklist membership alone. Historical extinct/extirpated exotics and provisional taxa are not primary-approved 2024 credits.</p><small>Policy: <code>{POLICY}</code> · <code>{sha256(DATA/'policy.json')}</code><br>The original conservative audit and prior route remain preserved separately.</small></section></main>
<script>const input=document.querySelector('#filter'),rows=[...document.querySelectorAll('#crosswalk tr')];input.addEventListener('input',()=>{{let n=0;const q=input.value.toLowerCase();for(const row of rows){{row.hidden=!row.textContent.toLowerCase().includes(q);if(!row.hidden)n++;}}document.querySelector('#matches').textContent=n+' source concepts';}});</script></html>'''
    (OUT/'aba2024-alignment-report.html').write_text(html)
    (OUT/'pilot-report.html').write_text(html)
    print(json.dumps(snapshot),flush=True)


if __name__=='__main__':
    main()
