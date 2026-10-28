"""Standalone comparison and gap report for an independently verified joint run."""
import argparse
import csv
from html import escape
import json
from pathlib import Path
import shutil

from bigyear_planner.eod import STATE_NAMES
from bigyear_planner.util import dump,read,write_csv,sha256


def report(folder,publish=True):
    folder=Path(folder).resolve();r=read(folder/'route.json');result=read(folder/'result.json');gap=read(folder/'gap-analysis.json')
    audit=read(folder/'validation.json')
    if not audit['valid'] or audit.get('route_sha256')!=sha256(folder/'route.json') or result['route_sha256']!=sha256(folder/'route.json'):
        raise ValueError('report publication requires the route-bound positive independent audit')
    resources=read(folder.parent/(folder.name+'-resources.json'));benchmark=read(folder/'benchmark.json')
    lineage=result.get('search_lineage');prior_resources=None
    if lineage:
        previous=Path(lineage['prior_run'])
        prior_resources=read(previous.parent/(previous.name+'-resources.json'))
    native_minutes=(benchmark['requested_seconds']+(lineage['prior_native_seconds'] if lineage else 0))/60
    workflow_minutes=(resources['wall_seconds']+(prior_resources['wall_seconds'] if prior_resources else 0))/60
    workflow_peak=max(resources['combined_peak_rss_bytes'],prior_resources['combined_peak_rss_bytes'] if prior_resources else 0)/2**30
    workflow_free=min(resources['minimum_free_bytes'],prior_resources['minimum_free_bytes'] if prior_resources else resources['minimum_free_bytes'])/2**30
    names={s['id']:s['name'] for s in r['species']};audit=[]
    for d in r['days']:
        for stop in d['stops']:
            for s in stop['new_species']:audit.append({'species_id':s,'species':names[s],'first_date':d['date'],
                'mode':d['kind'],'latitude':stop['lat'],'longitude':stop['lon'],
                'source_records':'|'.join(e['record'] for e in stop['evidence'][s])})
    write_csv(folder/'species-audit.csv',audit,list(audit[0]))
    baselines=[('Land + flights','reconciled-2024-provisional-access-v3'),('Boats around fixed calendar','marine-charter-2024-v2')]
    if result.get('baseline_run'):
        baselines.append(('Previous joint route',Path(result['baseline_run']).name))
    current_label=('Resolved routing + regional trips' if result.get('routing_queries_resolved') else
        'Targeted regional trips' if 'targeted_species' in result else 'Joint driving, flights + boats')
    baselines.append((current_label,folder.name))
    table=[];series=[]
    for label,tag in baselines:
        route=read(folder.parent/tag/'route.json');summary=read(folder.parent/tag/'result.json')
        resource=folder.parent/(tag+'-resources.json');rss=read(resource)['combined_peak_rss_bytes']/2**30 if resource.exists() else None
        if summary.get('search_lineage'):
            prior=Path(summary['search_lineage']['prior_run'])
            prior_rss=read(prior.parent/(prior.name+'-resources.json'))['combined_peak_rss_bytes']/2**30
            rss=max(rss or 0,prior_rss)
        if tag==folder.name:rss=workflow_peak
        table.append({'experiment':label,'species':route['species_total'],'primary':summary.get('primary'),
            'provisional':summary.get('provisional'),'flight_days':sum(d['kind']=='flight' for d in route['days']),
            'charter_days':sum(d['kind']=='pelagic' for d in route['days']),
            'road_miles':sum(d['road_miles'] for d in route['days']),
            'rest_days':sum(d['kind']=='rest' for d in route['days']),'peak_gib':rss})
        series.append({'name':label,'values':[d['cumulative_species'] for d in route['days']]})
    write_csv(folder/'comparison.csv',table,list(table[0]))
    missing=list(csv.DictReader((folder/'uncollected-species.csv').open()))
    points=lambda values:' '.join(f'{50+i*850/365:.2f},{310-v*280/800:.2f}' for i,v in enumerate(values))
    colors=['#96a9b6','#e1a347',*(['#7d6b9d'] if len(series)>3 else []),'#087e9b']
    chart=''.join(f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{points(s["values"])}"/>' for color,s in zip(colors,series))
    observer=r['marine_observer_coverage']['days'];obs=[d['cumulative_species'] for d in observer]
    chart+=f'<polyline fill="none" stroke="#6a8b42" stroke-dasharray="5 4" stroke-width="1.5" points="{points(obs)}"/>'
    chart+=f'<line x1="50" x2="900" y1="{310-759*280/800}" y2="{310-759*280/800}" stroke="#9e423e" stroke-dasharray="4 3"/>'
    for y in [0,200,400,600,800]:chart+=f'<text x="38" y="{315-y*280/800}" text-anchor="end" font-size="12" fill="#5b686f">{y}</text>'
    chart+='<text x="50" y="338" font-size="12">Jan 1</text><text x="860" y="338" font-size="12">Dec 31</text>'
    rows=''.join('<tr>'+''.join(f'<td>{escape(str(v))}</td>' for v in [t['experiment'],t['species'],f"{t['primary']} + {t['provisional']}",
        t['flight_days'],t['charter_days'],f"{t['road_miles']:,.0f}",t['rest_days'],f"{t['peak_gib']:.2f}" if t['peak_gib'] else '—'])+'</tr>' for t in table)
    gains=', '.join(v['species'] for v in gap['gains_vs_667']) or 'None'
    losses=', '.join(v['species'] for v in gap['losses_vs_667']) or 'None'
    repair_note=('The first 673 candidate failed its access audit because the assumed Curlew Sandpiper walk crossed 571 m of open water. That exact pin remains held. The final route uses another real observation on July 22 at its original exact coordinate, then passed a fresh complete audit. The 15-minute joint search and one-minute corrective search are recorded separately.' if lineage else '')
    target_section=''
    if 'targeted_species' in gap:
        target_count=len(gap['targeted_species'])
        target_title='The four routing targets' if result.get('routing_queries_resolved') and target_count==4 else f'The {target_count} regional targets'
        resolution_note=''
        if result.get('routing_queries_resolved'):
            witnesses=read(folder/'routing-witnesses.json')['witnesses']
            if len(witnesses)!=target_count or any(v['score']['pending'] or not v['score']['feasible'] for v in witnesses):
                raise ValueError('routing resolution report requires a measured feasible calendar for every target')
            resolution_note='<p class="note"><b>All four routing gaps resolved:</b> each target now has a measured feasible complete-calendar alternative. Alternate local car probes resolve failed directional matches; airport approach probes resolve disconnected centroid matches. The observation and airport reference coordinates remain unchanged, and observation access keeps the full round-trip walking charge. Airport centroids identify airports; the ground transfer ends at a separately recorded nearby road approach, with the existing full flight day and separate ground-access days. The search considers 24 airports per side and measures up to six pairs with a longer routing-query budget. Minneapolis–St. Paul was ninth nearest to Fieldfare, outside the old eight-airport cutoff. Existing source-visit holds now also precede the cached stop-duration path. Cached costs from unchanged finite anchors carry an explicit migration record; selected road paths are freshly measured under the new binding.</p>'
        target_rows=''.join('<tr>'+''.join(f'<td>{escape(str(v))}</td>' for v in [t['species'],
            t['outcome'],t['first_collection_date'] or '—',
            t['state'] or t['support_states'],t['available_dates'],t['best_measured_campaign_species'] or 'Unresolved'])+'</tr>' for t in gap['targeted_species'])
        new_gains=', '.join(v['species'] for v in gap['gains_vs_incumbent']) or 'None'
        new_losses=', '.join(v['species'] for v in gap['losses_vs_incumbent']) or 'None'
        target_section=f'''<h2>{target_title}</h2><section>{resolution_note}<p><b>{gap['targeted_species_collected']} of {target_count}</b> targeted species were collected. The verified total changed by <b>{result['gain_vs_incumbent']:+d}</b> from {gap['target_baseline_species']}. Targeted construction tries actual recorded dates, single-date insertions, and regional calendar replacements; the full search can still move all land, flight and boat visits.</p><div class="scroll"><table><thead><tr><th>Species</th><th>Outcome</th><th>First date</th><th>State / support</th><th>Source dates</th><th>Best measured campaign total</th></tr></thead><tbody>{target_rows}</tbody></table></div><p class="muted">Campaign totals refer to the complete calendar at that construction step, including the targeted species. A feasible campaign may sacrifice other species. Uncollected targets and unresolved routing queries are not declarations of global infeasibility.</p><p><b>Gained relative to {gap['target_baseline_species']}:</b> {escape(new_gains)}.</p><p><b>Lost relative to {gap['target_baseline_species']}:</b> {escape(new_losses)}.</p><p><a href="/{folder.name}/targeted-species-results.csv">Target dates, exact coordinates and outcome CSV</a> · <a href="/{folder.name}/incumbent-species-changes.csv">Species changes</a></p></section>'''
    regional_note=''
    if 'uncollected_only_in_unvisited_land_states' in gap:
        top=sorted(((s,n) for s,n in gap['remaining_land_taxa_by_state'].items() if s not in gap['states_birded']),key=lambda v:v[1],reverse=True)[:2]
        examples=', '.join(f"{STATE_NAMES[s.removeprefix('US-')]} ({n})" for s,n in top)
        regional_note=f" {gap['uncollected_only_in_unvisited_land_states']} uncollected taxa have terrestrial support only in states this route does not bird. Leading unvisited states by remaining source taxa: {examples}. These counts overlap and do not imply that one trip can collect them all."
    bound=read(folder/'calendar-conflicts.json') if (folder/'calendar-conflicts.json').exists() else None
    if bound and bound['model_sha256']!=read(folder/'problem.json')['compact_source']['sha256']:
        raise ValueError('calendar conflict certificate belongs to another model')
    conflict_note=(f'''<p class="note"><b>Calendar-conflict check:</b> a conservative certificate limits this frozen candidate model to at most {bound['conservative_upper_bound']} species. It optimistically allows 200 km/h road travel, maximum walking/local-driving slack, and any flight when three intervening days exist. Even with that slack, the recorded options for Common Swift and Great Knot cannot both fit. The {bound['conservative_upper_bound']} bound does <b>not</b> rule out 759, and does not establish that 759 is achievable. <a href="/{folder.name}/calendar-conflict-groups.csv">Conflict groups</a> · <a href="/{folder.name}/calendar-conflicts.json">Source-bound certificate</a>.</p>''' if bound else '')
    delta=(f"{result['gain_vs_incumbent']:+d}",f"vs previous {result['baseline_incumbent']} joint route") if 'gain_vs_incumbent' in result else (f"{result['gain_vs_fixed_calendar']:+d}",'vs fixed-calendar boats')
    cards=[(str(result['species']),'modeled species'),(f"{result['primary']} + {result['provisional']}",'primary + provisional'),
        delta,(str(result['gap_to_759']),'short of the 759 target')]
    hero=''.join(f'<div class="card"><strong>{escape(a)}</strong><span>{escape(b)}</span></div>' for a,b in cards)
    datajson=json.dumps(missing).replace('</','<\\/')
    body=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>2024 joint travel benchmark</title><style>
body{{margin:0;background:#f4f7f8;color:#172d39;font:16px/1.55 system-ui,sans-serif}}main{{max-width:1140px;margin:auto;padding:42px 28px}}h1{{font-size:38px;line-height:1.15;margin:8px 0 18px}}h2{{font-size:22px;margin:30px 0 12px}}p{{max-width:1000px}}.eyebrow{{color:#087e9b;font-weight:700;letter-spacing:.12em;font-size:12px}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:25px 0}}.card,section{{background:white;border:1px solid #dbe4e8;border-radius:12px;padding:20px}}.card strong{{font-size:34px;display:block;line-height:1.25}}.card span,.muted{{font-size:13px;color:#536975}}a{{color:#006f8b}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:11px 10px;border-bottom:1px solid #e1e8eb}}th{{background:#eef4f5}}.scroll{{overflow:auto}}.note{{background:#eaf2f3;border-left:4px solid #087e9b;padding:15px 18px}}.legend{{font-size:13px;display:flex;gap:20px;flex-wrap:wrap}}.dot{{width:11px;height:11px;display:inline-block;margin-right:7px;border-radius:50%}}input{{width:95%;font:inherit;padding:10px;border:1px solid #bbccd4;border-radius:7px;margin:10px 0}}li{{margin-bottom:12px}}@media(max-width:750px){{.cards{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:30px}}main{{padding:25px 16px}}}}
</style><main><div class="eyebrow">LOWER 48 + DC · ALL OF 2024 · EXACT OBSERVATION COORDINATES</div>
<h1>Driving, flights and boats<br>in one calendar search</h1>
<p>The search can move land visits, choose dated nonstop flights, and schedule modeled same-port charters together. It retains the original observation archive and complete terrestrial candidate cohort, with 2.2 km/h round-trip walking above 100 m.</p>
<div class="cards">{hero}</div>
<p><a href="/?run={folder.name}/route.json">Open the verified animated route →</a> &nbsp; · &nbsp; <a href="/{folder.name}/species-audit.csv">Species and source audit</a> &nbsp; · &nbsp; <a href="/{folder.name}/validation.json">Independent validation</a></p>
<section><h2 style="margin-top:0">Comparison with the earlier routes</h2><div class="scroll"><table><thead><tr><th>Experiment</th><th>Species</th><th>Primary + provisional</th><th>Flight days</th><th>Charter days</th><th>Road miles</th><th>Rest days</th><th>Peak GiB</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="muted">Descriptive comparisons. Search budgets and available modes differ; this is not a matched experiment proving an algorithm effect. Primary/provisional scores use the same v3 public-review proxy, which is not individual ABA certification.</p>
<svg viewBox="0 0 950 355" role="img" aria-label="Cumulative species by benchmark route compared with source pool and working record target">{chart}</svg>
<div class="legend">{''.join(f'<span><i class="dot" style="background:{c}"></i>{escape(s["name"])}</span>' for c,s in zip(colors,series))}<span>Green dashed: partial marine + terrestrial observer pool</span><span>Red dashed: 759 target</span></div></section>
{target_section}<h2>What the gap tells us</h2><section><ol>
<li><b>The source pool is larger than the target, but that is not a feasible-route proof.</b> The modeled pool contains {gap['pool']} species; {gap['uncollected_pool_species']} remain uncollected. Without Dobson's dated list, we cannot say which of his particular 759 are absent from these inputs.</li>
<li><b>Search and calendar construction remain relevant.</b> {gap['uncollected_with_at_least_one_date_within_30_air_miles']} uncollected species have at least one reported date within 30 straight-line miles of the modeled traveler's end position that day. This is a search ranking signal only: road distance, time and access have not been shown feasible for those additions. The calendar still has {gap['calendar_days'].get('rest',0)} rest days.{escape(regional_note)}</li>
<li><b>Some targets need tightly dated regional trips.</b> {gap['uncollected_with_at_most_7_recorded_dates']} uncollected species occur on at most seven modeled dates. A globally feasible combination may require replacing several existing visits, not inserting one bird into the current calendar.</li>
<li><b>Marine access is still incomplete.</b> {gap['uncollected_marine_only']} uncollected species are represented only by constructed marine options. This benchmark uses 2,001 previously constructed single-pin charters at nine modeled ports. Four additional port references remain held; island landings and longer overnight pelagics are outside the scenario. All offshore source rows are retained.</li>
<li><b>The time assumptions are substantial.</b> Every exact site-date requires two hours of birding; flights take a full day plus separate ground-access days; activity is capped at 12 hours. Timed eBird checklists and a documented itinerary would let us test these assumptions against what happened, rather than relax them without evidence.</li>
</ol>{conflict_note}<p class="note">The next priorities are species-directed multi-day replacements, more complete port/island access, and a timed calendar comparison with the same observation and scoring policy. The recordholder replay remains <b>pending input</b>. Waiting for eBird is useful for resolving coverage and chronology, but it is not evidence that further search gains are exhausted.</p></section>
<h2>What changed in the species list</h2><section><p><b>Gained relative to 667:</b> {escape(gains)}.</p><p><b>Lost relative to 667:</b> {escape(losses)}.</p></section>
<h2>Runtime, memory and validation</h2><section><p>Native search: <b>{native_minutes:.1f} minutes</b>; total monitored search/audit workflow: <b>{workflow_minutes:.1f} minutes</b>, including regional construction and source/transport audits. Peak combined process-tree RSS: <b>{workflow_peak:.2f} GiB</b>; minimum free disk: <b>{workflow_free:.1f} GiB</b>. The cost cache is capped at 16 MiB; endpoint and source evidence are retained for the verified route. Earlier pilots and interrupted diagnostics are excluded from these benchmark timings and remain logged.</p><p>{escape(repair_note)}</p>
<p>{result['source_records_checked']:,} selected publisher source records pass the existing proxy policy. Road endpoints, dated BTS flight evidence, exact marine coordinates, coastline clearance, full sailing/boarding/walking budgets and the 366-day calendar are independently checked. Historical charter operation, weather, tide/depth safety and legal foot access are not certified.</p>
<p class="muted">The observer curve combines complete eligible terrestrial support with partial marine construction. Car roads use the frozen current OSM snapshot; actual 2024 traffic and closures are not reconstructed. No global optimum is claimed.</p></section>
<h2>Uncollected modeled species</h2><section><p>Filter by name, then inspect support dates and access type. <a href="/{folder.name}/uncollected-species.csv">Download the full CSV</a> · <a href="/{folder.name}/gap-analysis.json">Diagnostic data</a></p><input id="query" aria-label="Filter uncollected species" placeholder="Filter species…"><div class="scroll"><table><thead><tr><th>Species</th><th>Land options</th><th>Charter options</th><th>Recorded dates</th><th>First / last date</th><th>Nearby dates*</th></tr></thead><tbody id="missing"></tbody></table></div><p class="muted">*Dates with a source option within 30 air miles of that day's route end position. Not a feasibility claim.</p></section>
<p class="muted">Route identity: {result['route_sha256']}. All earlier validated routes and the original Parquet observation archive remain preserved.</p>
<script>const rows={datajson};function render(){{const q=document.querySelector('#query').value.toLowerCase();const body=document.querySelector('#missing');body.replaceChildren();for(const row of rows.filter(r=>r.species.toLowerCase().includes(q))){{const tr=document.createElement('tr');for(const value of [row.species,row.dated_land_opportunities,row.constructed_charter_options,row.available_dates,row.first_date+' / '+row.last_date,row.days_within_30_air_miles_of_route_end_position]){{const td=document.createElement('td');td.textContent=value;tr.append(td)}}body.append(tr)}}}}document.querySelector('#query').addEventListener('input',render);render();</script></main></html>'''
    target=folder.parent/'2024-joint-mobility-report.html' if publish else folder/'report-preview.html'
    target.write_text(body)
    if publish:shutil.copyfile(target,folder.parent/'pilot-report.html')
    dump(folder/'report-binding.json',{'report':str(target),'route_sha256':result['route_sha256'],'gap_analysis_file':'gap-analysis.json'})
    print(str(target),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);parser.add_argument('--preview',action='store_true')
    args=parser.parse_args();report(args.folder,publish=not args.preview)
