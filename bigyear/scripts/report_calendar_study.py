"""Publish a source-bound five-arm study and keep its standalone snapshot."""
import argparse
from html import escape
import json
import shutil

from bigyear_planner.util import ROOT,dump,read,sha256,write_csv


LABELS={'control':'Continued search control','nearby':'Nearby cleanup','tentpole':'Tentpole calendars',
        'island':'Island access','combined':'All three together'}
RUNS={arm:f'calendar-study-{arm}-2024-v1' for arm in LABELS}
RUNS['nearby']='calendar-study-nearby-2024-v2'


def progress_report(root,out,plan):
    rows=[]
    for arm,label in LABELS.items():
        folder=root/RUNS[arm]
        if (folder/'validation.json').exists() and read(folder/'validation.json').get('valid'):
            route=read(folder/'route.json');audit=read(folder/'validation.json')
            if audit.get('route_sha256')!=sha256(folder/'route.json'):raise ValueError('progress route audit differs')
            text=f"Verified: {route['species_total']} ({route['scoring_tiers']['primary']} primary + {route['scoring_tiers']['provisional']} provisional)"
            link=f'<a href="/?run={folder.name}/route.json">Open route</a>'
        elif (folder/'baseline-binding.json').exists():text='Running · final audit pending';link=''
        else:text='Queued';link=''
        rows.append(f'<tr><td>{escape(label)}</td><td>{text}</td><td>{link}</td></tr>')
    body='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60"><title>2024 calendar study in progress</title><style>body{font:16px/1.6 system-ui,sans-serif;background:#f4f7f8;color:#18323d;margin:0}main{max-width:1000px;margin:35px auto;padding:28px}h1{font-size:36px;line-height:1.2}section{background:white;border:1px solid #dce5e8;border-radius:12px;padding:22px;overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:15px 12px;text-align:left;border-bottom:1px solid #dce5e8}a{color:#006d83}.note{color:#536975;font-size:14px}</style><main><p>LOWER 48 + DC · ALL OF 2024</p><h1>Calendar strategy comparison is running</h1><p>Each arm starts independently from the verified 682-species route and the same routing cache, with a 15-minute search allocation. All observations and exact coordinates are retained, with 2.2 km/h walking. Sensitive-species policy is unchanged.</p><section><table><thead><tr><th>Arm</th><th>Status / audited count</th><th>Saved calendar</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table></section><p class="note">Search is followed by source retrieval and independent calendar/transport validation. Counts appear only after that validation passes. Arms run sequentially to bound laptop resource use. The completed report will replace this progress page automatically.</p><p>Island options assume a two-hour crossing each way from Ventura to Santa Cruz, plus boarding, birding and separately charged round-trip island walking. Historical sailings and footpaths are assumed.</p></main></html>'''
    (out/'progress-report.html').write_text(body);shutil.copyfile(out/'progress-report.html',root/'pilot-report.html')
    print({'progress_report':str(out/'progress-report.html')})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--publish',action='store_true');parser.add_argument('--partial',action='store_true');args=parser.parse_args()
    root=ROOT/'runs/lower48-eod-2024';out=root/'calendar-strategy-study-2024-v1';out.mkdir(exist_ok=True)
    plan_path=ROOT/'data/calendar-strategy-study-2024-v1/study-plan.json';plan=read(plan_path)
    if args.partial:return progress_report(root,out,plan)
    baseline=read(plan['baseline_route']);names={s['id']:s['name'] for s in baseline['species']}
    before={s for d in baseline['days'] for stop in d['stops'] for s in stop['species']}
    rows=[];series=[];changes=[];bindings=[];routes={};detail=[];construction_stats={}
    for arm,label in LABELS.items():
        folder=root/RUNS[arm];route=read(folder/'route.json');audit=read(folder/'validation.json')
        result=read(folder/'result.json');resource=read(root/(folder.name+'-resources.json'))
        digest=sha256(folder/'route.json')
        if not audit['valid'] or audit.get('route_sha256')!=digest or result['route_sha256']!=digest:
            raise ValueError('study report requires positive route-bound audit: '+arm)
        selected={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
        search=read(folder/'study-search.json') if (folder/'study-search.json').exists() else None
        budget=search['allocated_seconds'] if search else read(folder/'benchmark.json')['requested_seconds']
        seconds=search['elapsed_seconds'] if search else route['search']['elapsed_seconds']
        if abs(budget-plan['seconds_per_arm'])>.001:raise ValueError('study search allocations differ')
        row={'arm':arm,'experiment':label,'species':route['species_total'],**route['scoring_tiers'],
            'gain_vs_682':route['species_total']-682,'allocated_search_seconds':budget,'search_seconds':seconds,
            'wall_minutes':resource['wall_seconds']/60,'peak_gib':resource['combined_peak_rss_bytes']/2**30,
            'minimum_free_gib':resource['minimum_free_bytes']/2**30,'added':len(selected-before),'lost':len(before-selected),
            'flight_days':sum(d['kind']=='flight' for d in route['days']),
            'charter_days':sum(d['kind']=='pelagic' for d in route['days']),
            'island_days':sum(d['kind']=='island' for d in route['days']),
            'road_miles':sum(d['road_miles'] for d in route['days']),
            'rest_days':sum(d['kind']=='rest' for d in route['days']),
            'walking_hours':audit['walking']['minutes']/60,
            'route':folder.name+'/route.json','valid':True}
        rows.append(row);routes[arm]=route;series.append({'name':label,'arm':arm,'values':[d['cumulative_species'] for d in route['days']]})
        bindings.append({'arm':arm,'route_sha256':digest,'validation_sha256':sha256(folder/'validation.json'),
            'result_sha256':sha256(folder/'result.json'),'resources_sha256':sha256(root/(folder.name+'-resources.json'))})
        for state,ids in [('Added',selected-before),('Lost',before-selected)]:
            for s in sorted(ids):
                visits=[(d,stop) for d in route['days'] for stop in d['stops'] if s in stop['species']]
                first=visits[0][0]['date'] if visits else ''
                changes.append({'arm':arm,'change':state,'species_id':s,'species':names[s],'first_date':first})
        if search:
            scenarios=folder/'tentpole-construction/tentpole-footholds.json'
            constructed=read(scenarios) if scenarios.exists() else None
            detail.append({'arm':arm,'stages':search['stages'],'footholds':constructed,
                'required_final_visits':search['required_final_visits']})
            attempts=folder/'tentpole-construction/tentpole-construction.jsonl'
            if attempts.exists():
                stats={}
                for line in attempts.read_text().splitlines():
                    entry=json.loads(line);item=stats.setdefault(entry['name'],{'attempts':0,'pending':0,'feasible':0,'infeasible':0})
                    item['attempts']+=1
                    item['pending']+=int(entry['pending']);item['feasible']+=int(entry['feasible'])
                    item['infeasible']+=int(not entry['pending'] and not entry['feasible'])
                construction_stats[arm]=stats
        species_rows=[]
        for d in route['days']:
            for stop in d['stops']:
                for s in stop['new_species']:species_rows.append({'species_id':s,'species':names[s],'first_date':d['date'],
                    'mode':d['kind'],'latitude':stop['lat'],'longitude':stop['lon'],
                    'source_records':'|'.join(e['record'] for e in stop['evidence'][s])})
        write_csv(folder/'species-audit.csv',species_rows,list(species_rows[0]))
    control=rows[0]['species']
    for row in rows:row['gain_vs_control']=row['species']-control
    winner=max(rows,key=lambda r:(r['species'],-sum(d['travel_minutes'] for d in routes[r['arm']]['days']),-r['road_miles']))
    collected={arm:{s for d in route['days'] for stop in d['stops'] for s in stop['species']}
        for arm,route in routes.items()}
    union=set().union(*collected.values());winner_species=collected[winner['arm']]
    alternative_birds=[{'species_id':s,'species':names[s],
        'arms':[arm for arm,seen in collected.items() if s in seen]}
        for s in sorted(union-winner_species)]
    alternative_names=', '.join(sorted(b['species'] for b in alternative_birds)) or 'None'
    write_csv(out/'comparison.csv',rows,list(rows[0]));write_csv(out/'species-changes.csv',changes,list(changes[0]) if changes else ['arm','change','species_id','species','first_date'])
    result={'valid':True,'study_plan_sha256':sha256(plan_path),'baseline_unchanged':sha256(plan['baseline_route'])==plan['baseline_route_sha256'],
        'winner':winner,'rows':rows,'route_bindings':bindings,'detail':detail,'construction_stats':construction_stats,
        'union_of_verified_calendars_species':len(union),'seen_in_alternatives_but_not_winner':alternative_birds,
        'phase_handoff':plan.get('phase_handoff'),'limitations':plan['limitations']}
    if not result['baseline_unchanged']:raise ValueError('frozen682baseline changed')
    dump(out/'study-result.json',result)
    dump(out/'result.json',result)
    colors=['#8998a1','#356bb0','#9758a6','#b77414','#087e70']
    points=lambda v:' '.join(f'{45+i*880/365:.1f},{330-n*300/800:.1f}' for i,n in enumerate(v))
    curves=''.join(f'<polyline class="curve" data-arm="{s["arm"]}" fill="none" stroke="{c}" stroke-width="2.4" points="{points(s["values"])}"/>' for s,c in zip(series,colors))
    curves+=f'<polyline fill="none" stroke="#788b4b" stroke-dasharray="4 5" points="{points([v["cumulative_species"] for v in baseline["marine_observer_coverage"]["days"]])}"/>'
    curves+=f'<line x1="45" x2="925" y1="{330-759*300/800:.2f}" y2="{330-759*300/800:.2f}" stroke="#b65353" stroke-dasharray="6 5"/>'
    table=''.join(f'''<tr><td><a href="/?run={r['route']}">{escape(r['experiment'])}</a></td><td><b>{r['species']}</b></td><td>{r['primary']} + {r['provisional']}</td><td>{r['gain_vs_682']:+d}</td><td>{r['gain_vs_control']:+d}</td><td>{r['added']} / {r['lost']}</td><td>{r['flight_days']} / {r['charter_days']} / {r['island_days']}</td><td>{r['road_miles']:,.0f}</td><td>{r['peak_gib']:.2f}</td><td>{r['wall_minutes']:.1f}</td></tr>''' for r in rows)
    target_names=['Golden-cheeked Warbler','Black-capped Vireo','Colima Warbler','Rufous-capped Warbler',
        'White-eared Hummingbird','American Flamingo','Brown Noddy','White-winged Tern','Island Scrub-Jay']
    availability=read(out/'target-availability.json')
    if sha256(availability['source_path'])!=availability['source_sha256']:
        raise ValueError('target availability source changed')
    available={r['species']:r for r in availability['rows']}
    target_table=''
    for name in target_names:
        s=next(s for s,n in names.items() if n==name);cells=[]
        for arm in LABELS:
            dates=[d['date'] for d in routes[arm]['days'] if any(s in stop['species'] for stop in d['stops'])]
            cells.append('<td>'+escape(dates[0] if dates else '—')+'</td>')
        source=available[name]
        source_text=f"{source['available_dates']} dates · {source['first_date'][5:]}–{source['last_date'][5:]}"
        target_table+='<tr><td>'+escape(name)+'</td><td>'+escape(source_text)+'</td>'+''.join(cells)+'</tr>'
    data=json.dumps(changes).replace('</',r'<\/')
    routing_notes=[]
    for arm,stats in construction_stats.items():
        item=stats.get('Golden-cheeked Warbler')
        if item and not item['feasible'] and item['pending']:
            routing_notes.append(f"{LABELS[arm]}: Golden-cheeked Warbler had {item['pending']} pending routing attempts out of {item['attempts']}; no measured feasible foothold was established in the construction budget.")
    routing_note=' '.join(routing_notes)
    nearby_row=next(r for r in rows if r['arm']=='nearby')
    combined_detail=next(d for d in detail if d['arm']=='combined')
    combined_polish=next((s for s in combined_detail['stages'] if s['stage']=='nearby-polish'),None)
    polish_note=''
    if combined_polish:
        at=combined_detail['stages'].index(combined_polish)
        previous=max([682]+[s['species'] for s in combined_detail['stages'][:at] if 'species' in s])
        polish_note=f"In the combined arm, nearby cleanup raised the candidate count from {previous} to {combined_polish['species']} through {combined_polish['accepted']} accepted visits. The final saved calendar was then independently audited."
    pilot=root/'calendar-study-nearby-2024-v1';pilot_note=''
    if (pilot/'validation.json').exists():
        pilot_audit=read(pilot/'validation.json')
        if pilot_audit.get('valid') and pilot_audit.get('route_sha256')==sha256(pilot/'route.json'):
            pilot_note=f'''<p class="muted">A separate <a href="/?run={pilot.name}/route.json">preliminary nearby pilot</a> finished at {read(pilot/'route.json')['species_total']} and is excluded from the primary comparison. A queued-query handoff problem was identified during that pilot; its saving step was later recovered without repeating the search. Primary nearby v2 applies the handoff correction and adds road-corridor proposals beyond the pilot's stop-focused review. The pilot remains available for inspection.</p>'''
    body=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>2024 calendar strategy study</title>
<style>body{{margin:0;background:#f4f7f8;color:#18323d;font:16px/1.55 system-ui,sans-serif}}main{{max-width:1200px;margin:auto;padding:40px 26px}}h1{{font-size:38px;line-height:1.2;margin:10px 0}}h2{{font-size:23px;margin:0 0 14px}}section,.card{{background:white;border:1px solid #dce5e8;border-radius:12px;padding:23px;margin:22px 0}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.card{{margin:0}}.card strong{{display:block;font-size:34px;line-height:1.3}}.muted,.card span{{font-size:13px;color:#536975}}.eyebrow{{font-size:12px;font-weight:750;letter-spacing:.12em;color:#087e70}}a{{color:#006d83}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:11px 9px;border-bottom:1px solid #e1e8eb;text-align:left}}th{{background:#eef4f5}}.scroll{{overflow-x:auto}}.legend{{display:flex;gap:17px;flex-wrap:wrap;font-size:13px}}.legend label{{cursor:pointer}}input[type=search]{{font:inherit;padding:10px;border:1px solid #bacbd3;border-radius:6px;width:min(95%,560px)}}li{{margin:10px 0}}.note{{border-left:4px solid #087e70;background:#eaf3f1;padding:15px 18px}}@media(max-width:700px){{main{{padding:24px 14px}}h1{{font-size:29px}}.cards{{grid-template-columns:repeat(2,1fr)}}}}</style>
<main><div class="eyebrow">LOWER 48 + DC · 2024 · ALL OBSERVATIONS · EXACT COORDINATES</div><h1>Can better calendar construction collect more birds?</h1>
<p>Five arms start from the verified682-species calendar and identical bounded routing caches. Each receives15minutes of search, with driving, observed2024nonstop flights, modeled pelagic charters and2.2km/h walking. Island access is added only in the island and combined arms.</p>
<div class="cards"><div class="card"><strong>{winner['species']}</strong><span>best modeled count</span></div><div class="card"><strong>{winner['gain_vs_control']:+d}</strong><span>vs continued-search control</span></div><div class="card"><strong>{winner['primary']} + {winner['provisional']}</strong><span>primary + provisional</span></div><div class="card"><strong>{759-winner['species']}</strong><span>below759comparison target</span></div></div>
<p><a href="/?run={winner['route']}">Open the best verified route →</a> · <a href="/{out.name}/comparison.csv">Comparison CSV</a> · <a href="/{out.name}/result.json">Bound study results</a></p><p class="muted">Across the five verified calendars, {len(union)} different species are collected. {len(alternative_birds)} occur in an alternative calendar but not the winner: {escape(alternative_names)}. That union is a set of individually demonstrated opportunities, not proof they can all fit together.</p>
<section><h2>Matched search allocations</h2><div class="scroll"><table><thead><tr><th>Arm / route</th><th>Species</th><th>Primary + provisional</th><th>Δ682</th><th>Δcontrol</th><th>Added / lost</th><th>Flight / charter / island days</th><th>Road miles</th><th>Peak GiB</th><th>Wall min</th></tr></thead><tbody>{table}</tbody></table></div><p class="muted">Every arm has a900second search allocation. Wall time also includes setup, routing-worker drain, source retrieval and independent validation. Peak memory covers the planner and its child processes. One replicate per arm; this is a diagnostic comparison, not a statistical demonstration of superiority.</p>
<svg viewBox="0 0 960 365" role="img" aria-label="Cumulative species in the five equal-budget study arms"><path d="M45 15V330H925" fill="none" stroke="#bacbd3"/>{curves}<text x="5" y="35" font-size="12">800</text><text x="25" y="333" font-size="12">0</text><text x="45" y="355" font-size="12">January</text><text x="865" y="355" font-size="12">December</text></svg>
<div class="legend">{''.join(f'<label style="color:{c}"><input type="checkbox" checked data-arm="{s["arm"]}"> {escape(s["name"])}</label>' for s,c in zip(series,colors))}<span>Green dashed: source pool</span><span>Red dashed:759</span></div></section>
<section><h2>What changed</h2><p>{escape(polish_note)}</p><p>The standalone nearby arm finishes at {nearby_row['species']}, {nearby_row['gain_vs_control']:+d} relative to continued search. The best calendar remains {759-winner['species']} below the working 759 target. The objective throughout is feasible annual distinct-species coverage, then less travel time and fewer road miles; target construction changes the proposals and preserves specific dated visits, without adding rarity points.</p><p>Next comparisons should resolve the pending seasonal-target transfers before reconstruction, test whether species demonstrated in alternative calendars can fit together, and repeat the matched arms with multiple seeds. These runs establish feasible modeled calendars; they do not establish an optimum or show that the remaining gap requires more source data.</p>{pilot_note}</section>
<section><h2>Seasonal targets and island access</h2><div class="scroll"><table><thead><tr><th>Species</th><th>Source dates before access checks</th>{''.join('<th>'+escape(v)+'</th>' for v in LABELS.values())}</tr></thead><tbody>{target_table}</tbody></table></div><p class="muted">Source ranges describe recorded dates in the source pool, before checking travel or access. Route dates show actual selected observations; a dash means uncollected. Tentpole scenarios fix specific supported visits during reconstruction; no presence is interpolated across a seasonal window.</p><p class="note">{escape(routing_note)} Pending routing means unknown feasibility, not a proven calendar conflict.</p></section>
<section><h2>Species added and displaced</h2><input id="filter" type="search" placeholder="Filter by species, arm or added/lost" aria-label="Filter species changes"><p class="muted" id="count"></p><div class="scroll"><table><thead><tr><th>Arm</th><th>Change vs682</th><th>Species</th><th>First collection date</th></tr></thead><tbody id="changes"></tbody></table></div></section>
<section><h2>What this study tests</h2><ul><li><b>Nearby cleanup:</b> repeatedly test positive marginal additions around current exact visits, bracketing endpoints and measured baseline road corridors throughout their transfer gaps, including spare days. Corridors guide proposals; actual roads, dates, walking and the full current calendar decide feasibility.</li><li><b>Tentpoles:</b> construct feasible alternatives around seasonal specialties and brief rarities, then reserve search time for lower-scoring calendars while enforcing their exact dated anchors. The final objective remains one point per distinct species.</li><li><b>Island access:</b>262additional single-pin options near Prisoners Harbor and Scorpion Anchorage. Each returns to Ventura with two-hour crossings each way, one hour total boarding, two hours birding and full round-trip island walking at2.2km/h. Water paths and on-land walking geography are checked; near-landing foot access, weather and historical sailing availability are assumed. <a href="https://www.nps.gov/chis/planyourvisit/birdwatching.htm">NPS birdwatching reference</a>.</li><li><b>Evidence:</b> all original terrestrial options and charters remain present; exact coordinate strings and the original Parquet archive are retained. Every saved calendar passes independent transport, source and countability checks. Sensitive-species policy is unchanged.</li></ul><p class="note">The primary/provisional split remains visible. This public-source audit is a countability proxy, not certification of a real individual big year. The759target is not proof that759is feasible under these inputs and access rules.</p></section>
<script>const changes={data},labels={json.dumps(LABELS)};const filter=document.getElementById('filter'),tbody=document.getElementById('changes');function render(){{const query=filter.value.toLowerCase();const rows=changes.filter(r=>[r.arm,labels[r.arm],r.change,r.species,r.first_date].join(' ').toLowerCase().includes(query));tbody.replaceChildren();for(const r of rows){{const tr=document.createElement('tr');for(const value of [labels[r.arm],r.change,r.species,r.first_date]){{const td=document.createElement('td');td.textContent=value;tr.append(td)}}tbody.append(tr)}}document.getElementById('count').textContent=rows.length+' of '+changes.length+' species-change rows'}}filter.addEventListener('input',render);render();document.querySelectorAll('input[data-arm]').forEach(input=>input.addEventListener('change',()=>document.querySelector('polyline[data-arm="'+input.dataset.arm+'"]').style.display=input.checked?'':'none'));</script></main></html>'''
    # Readable prose spacing is deliberate even in compact generated HTML.
    body=body.replace('<svg viewBox=', '<svg style="display:block;width:100%;height:auto;margin-top:20px" viewBox=')
    for old,new in [('verified682','verified 682'),('receives15','receives 15'),('observed2024nonstop','observed 2024 nonstop'),
        ('and2.2km/h','and 2.2 km/h'),('a900second','a 900 second'),('below759comparison','below 759 comparison'),
        ('dashed:759','dashed: 759'),('759target','759 target'),('that759is','that 759 is'),('vs682','vs 682'),
        ('b>262additional','b> 262 additional'),('at2.2km/h','at 2.2 km/h'),
        ('15minutes','15 minutes'),('900second','900 second'),('The759','The 759'),('262additional','262 additional')]:body=body.replace(old,new)
    (out/'published-report.html').write_text(body)
    if args.publish:
        shutil.copyfile(out/'published-report.html',root/'2024-calendar-strategy-study.html')
        shutil.copyfile(out/'published-report.html',root/'pilot-report.html')
        dump(root/'run-status.json',{'stage':'completed','run':out.name,
            'label':'2024 calendar strategy study complete','best_species':winner['species'],
            'best_run':RUNS[winner['arm']],'verified_best':True,
            'message':'All five independent 15-minute arms and their calendar/source audits are complete.'})
    print({'report':str(out/'published-report.html'),'winner':winner['arm'],'species':winner['species']})


if __name__=='__main__':main()
