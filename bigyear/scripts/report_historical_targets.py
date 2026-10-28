"""Report an audited warm-target study without replacing its starting route."""
import argparse
from html import escape
import json
import os
from pathlib import Path

from bigyear_planner.compact_model import load
from bigyear_planner.rest_exploration import RestExplorer
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
from benchmark_shared_year import metrics, verified
from report_cross_year import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=ROOT/'runs/lower48-eod-2023/target-rest-2023-v2')
    parser.add_argument('--output', type=Path, default=ROOT/'runs/target-rest-2023-comparison-v1')
    args = parser.parse_args()
    final = args.run.resolve()
    plan, result = read(final/'plan.json'), read(final/'result.json')
    base = Path(plan['baseline_path'])
    before, old_problem = verified(base)
    route, problem = verified(final)
    if (not result['valid'] or result['route_sha256'] != sha256(final/'route.json')
        or plan['baseline_route_sha256'] != sha256(base/'route.json')
        or any(old_problem['compact_source'][k] != problem['compact_source'][k]
            for k in ['path','sha256','components'])
        or before['config'] != route['config'] or before['taxonomy_policy'] != route['taxonomy_policy']):
        raise ValueError('require the same complete model, rules and audited bound calendars')
    model = load(problem['compact_source']['path'])
    try:
        helper = RestExplorer(model,None,final/'final-rest-opportunities',route)
        _,_,inventory = helper.index(route['search']['selected_model_opportunities'])
        dump(final/'final-rest-opportunities/inventory.json',inventory)
        names = {s['id']:s['name'] for s in model.meta['species']}
        rows = [{'date':d['date'],'missing_species_count':len(d['missing_species']),
            'dated_opportunities':d['opportunities'],'boat_opportunities':d['boat_opportunities'],
            'missing_species':'|'.join(names[s] for s in d['missing_species'])} for d in inventory['days']]
        write_csv(final/'remaining-rest-opportunities.csv',rows,
            ['date','missing_species_count','dated_opportunities','boat_opportunities','missing_species'])
    finally:
        model.close()
    changes = []
    for change, ids, calendar in [('added',result['added_species'],route),
        ('lost',result['lost_species'],before)]:
        for sid in ids:
            day,stop = next((d,s) for d in calendar['days'] for s in d['stops'] if sid in s['species'])
            changes.append({'change':change,'species_id':sid,'species':names[sid],
                'first_date':day['date'],'state':calendar['locations'][stop['location']]['state']})
    write_csv(final/'species-changes.csv',changes,['change','species_id','species','first_date','state'])
    target_details = {v['species_id']:dict(v) for v in read(final/'targets.json')}
    for t in target_details.values():
        t.update(attempts=0,pending_attempts=0,measured_feasible_attempts=0,source_opportunities=None)
    inventory_path = final/'target-source-inventory.json'
    if inventory_path.exists():
        source_inventory = read(inventory_path)
        if source_inventory['model_sha256'] != plan['model_sha256']:
            raise ValueError('target source inventory describes a different model')
        for t in source_inventory['targets']:
            target_details[t['species_id']].update(support_dates=t['support_dates'],
                first_supported_date=t['first_date'],last_supported_date=t['last_date'])
    regional = read(final/'target-regional/targeted-exploration.json')
    for t in regional['targets']:
        target_details[t['species_id']]['source_opportunities'] = t['source_opportunities']
    for folder,filename in [('target-rest','rest-attempts.jsonl'),
        ('target-regional','targeted-attempts.jsonl'),('refreshed-rest','rest-attempts.jsonl')]:
        for line in (final/folder/filename).open():
            a = json.loads(line)
            if a['target'] not in target_details:
                continue
            t = target_details[a['target']]
            t['attempts'] += 1
            t['pending_attempts'] += a['pending']
            t['measured_feasible_attempts'] += not a['pending'] and a['feasible']
    for sid,t in target_details.items():
        visits = [(d,s) for d in route['days'] for s in d['stops'] if sid in s['species']]
        t['collected'] = bool(visits)
        t['first_date'] = visits[0][0]['date'] if visits else ''
        t['state'] = route['locations'][visits[0][1]['location']]['state'] if visits else ''
        t['status'] = ('collected' if visits else 'feasible candidate found; absent from final route'
            if t['measured_feasible_attempts'] else 'routing attempts pending when tested' if t['pending_attempts']
            else 'no improving calendar found' if t['attempts'] else 'not directly tested')
    targets = sorted(target_details.values(),key=lambda t:t['species'])
    write_csv(final/'targeted-species-results.csv',targets,list(targets[0]))
    reference = ROOT/'runs/lower48-eod-2024/shared-12h-full-2024-v2'
    summaries = [summarize(p) for p in [base,final,reference]]
    output = args.output.resolve()
    output.mkdir(exist_ok=True)
    labels = ['2023 baseline','2023 improved','2024 reference']
    summary = {'valid':True,'baseline':metrics(before),'final':metrics(route),
        'targets_collected':len(result['targets_collected']),'targets_remaining':len(result['targets_remaining']),
        'directly_attempted_targets':sum(t['attempts']>0 for t in targets),
        'remaining_with_pending_queries':sum(not t['collected'] and t['pending_attempts']>0 for t in targets),
        'remaining_rest_opportunity_dates':inventory['opportunity_dates'],
        'remaining_rest_supported_missing_species':inventory['missing_species'],
        'targets':targets,'baseline_route_sha256':sha256(base/'route.json'),
        'species_changes':changes,
        'final_route_sha256':sha256(final/'route.json'),'model_sha256':plan['model_sha256'],
        'same_complete_model_and_policy':True,'optimization_proven':False}
    dump(final/'target-study-summary.json',summary)
    for source,view in zip([base,final,reference],['baseline','improved','reference-2024']):
        folder = output/view
        folder.mkdir(exist_ok=True)
        for filename in ['route.json','problem.json','validation.json','roads.sqlite','result.json',
            'itinerary.csv','benchmark.json','source-decisions.csv']:
            src,dest = source/filename,folder/filename
            if dest.exists():
                if sha256(src) != sha256(dest):
                    raise ValueError('preserve an existing comparison; select a fresh output')
            else:
                os.link(src,dest)
    audit = read(final/'validation.json')
    text = ['<!doctype html><html lang="en"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>2023 target and rest study</title>',
        '<style>body{font:16px system-ui,sans-serif;color:#173b34;background:#f4f7f4;margin:0}main{max-width:1180px;margin:auto;padding:32px}h1{font-size:36px}p,li{line-height:1.6}section{background:white;padding:24px;border:1px solid #d6e2dc;border-radius:14px;margin:24px 0}table{border-collapse:collapse;width:100%}th,td{padding:10px;border-bottom:1px solid #dce6df;text-align:left}td:nth-child(n+2),th:nth-child(n+2){text-align:right}a{color:#14689d}svg{width:100%;height:auto}.tags{display:flex;gap:8px;flex-wrap:wrap}.tag{padding:5px 10px;border:1px solid #ceddd3;border-radius:20px;font-size:14px}summary{cursor:pointer;font-weight:600}</style><main>',
        '<p>FULL COHORT · EXACT COORDINATES · AUDITED CALENDARS</p><h1>2023: targeted trips and rest-day opportunities</h1>',
        f'<p><strong>{route["species_total"]} species, up {route["species_total"]-before["species_total"]} from 619.</strong> '
        f'Collected {len(result["targets_collected"])} of the 107 supported targets; {len(result["targets_remaining"])} remain uncollected. '
        f'The final independent audit passed all {audit["days_checked"]} dates and {result["publisher_records_checked"]:,} publisher records.</p>',
        '<p><a href="/?run=improved/route.json">Open improved 2023 route ↗</a> · '
        '<a href="/?run=baseline/route.json">Open original 619-species route ↗</a> · '
        '<a href="/?run=reference-2024/route.json">Open 714-species 2024 route ↗</a></p>',
        '<section><h2>Activity and travel</h2><table><tr><th>Metric</th>'+''.join(f'<th>{s}</th>' for s in labels)+'</tr>']
    for label,key in [('Species','species'),('Primary','primary'),('Provisional','provisional'),
        ('States with birding','states_with_birding'),
        ('Driving miles','road_miles'),('Travel hours','travel_hours'),('Birding hours','birding_hours'),
        ('Walking hours','walking_hours')]:
        text.append('<tr><td>'+label+'</td>'+''.join(f'<td>{y["metrics"][key]:,.1f}</td>'
            if key.endswith(('miles','hours')) else f'<td>{y["metrics"][key]:,}</td>' for y in summaries)+'</tr>')
    for kind in ['birding','road_transit','flight','pelagic','island','rest']:
        text.append('<tr><td>'+kind.replace('_',' ').title()+' days</td>'+''.join(
            f'<td>{y["metrics"]["day_counts"].get(kind,0)}</td>' for y in summaries)+'</tr>')
    text.extend(['</table></section><section><h2>Species accumulated through the year</h2>',
        '<p>Blue: 2023 baseline · Green: 2023 improved · Orange: 2024 reference</p>',
        '<svg viewBox="0 0 1100 330" role="img" aria-label="Monthly cumulative species comparison">'])
    for y in [0,200,400,600,800]:
        pos = 290-y/800*250
        text.append(f'<line x1="60" x2="1060" y1="{pos}" y2="{pos}" stroke="#dbe6df"/><text x="8" y="{pos+5}" fill="#5b7267">{y}</text>')
    for data,color in zip(summaries,['#14689d','#24806a','#b86324']):
        points = ' '.join(f'{60+(i+1)/12*1000},{290-m["cumulative_species"]/800*250}' for i,m in enumerate(data['months']))
        text.append(f'<polyline points="60,290 {points}" fill="none" stroke="{color}" stroke-width="3"/>')
    for i,month in enumerate(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']):
        text.append(f'<text x="{60+(i+1)/12*1000}" y="320" text-anchor="middle" fill="#5b7267">{month}</text>')
    text.extend(['</svg></section><section><h2>Target outcomes</h2>'])
    for collected,label in [(True,'Collected'),(False,'Still missing')]:
        entries = [t for t in targets if t['collected']==collected]
        text.append(f'<details open><summary>{label}: {len(entries)}</summary><p class="tags">')
        text.extend(f'<span class="tag" title="{escape(t["status"])}">{escape(t["species"])}'
            f'{" · "+t["first_date"] if collected else ""}</span>' for t in entries)
        text.append('</p></details>')
    text.extend(['</section><section><h2>Interpretation</h2><ul>',
        '<li>All observations, exact coordinates and countability rules remain unchanged. The shared activity budget is 12 hours, walking is 2.2 km/h, flights consume full days, and modeled boats remain enabled.</li>',
        f'<li>The final calendar still has {inventory["rest_dates"]} rest dates. Of these, {inventory["opportunity_dates"]} have source support for {inventory["missing_species"]} uncollected species somewhere in the modeled country. These are opportunities to investigate, not proof of a feasible addition.</li>',
        f'<li>Directly tested {summary["directly_attempted_targets"]} of 107 targets. {summary["remaining_with_pending_queries"]} remaining targets had pending routing attempts; no claim of impossibility is made.</li>',
        f'<li>This study adds {sum(plan["budgets_seconds"].values())/60:g} minutes of declared search allocation to the previously optimized 619-species calendar. Its elapsed time includes proposal construction, routing and validation. Best found does not mean optimal.</li>',
        '<li>The 2024 reference has a longer accumulated search history. Taxonomic concepts are applied retrospectively; marine and island access remains partial and modeled. This is a lower-48/DC comparison, rather than the full ABA Area.</li>',
        '</ul></section></main></html>'])
    (output/'pilot-report.html').write_text('\n'.join(text))
    dump(output/'comparison.json',summary)
    dump(output/'report-verification.json',{'valid':True,'model_sha256':plan['model_sha256'],
        'baseline_sha256':sha256(base/'route.json'),'final_sha256':sha256(final/'route.json'),
        'html_sha256':sha256(output/'pilot-report.html')})
    print(json.dumps({k:v for k,v in summary.items() if k not in ['baseline','final','targets','species_changes']}))


if __name__ == '__main__':
    main()
