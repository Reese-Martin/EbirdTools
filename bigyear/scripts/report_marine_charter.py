"""Publish only a source- and calendar-verified marine comparison."""
import csv
from html import escape
from pathlib import Path

from bigyear_planner.util import ROOT,dump,read,sha256,write_csv

OUT=ROOT/'runs/lower48-eod-2024'
TAG='marine-charter-2024-v2'


def main():
    folder=OUT/TAG;route=read(folder/'route.json');result=read(folder/'result.json')
    audit=read(folder/'validation.json')
    if not audit['valid'] or audit['route_sha256']!=sha256(folder/'route.json'):
        raise ValueError('marine route lacks current successful validation')
    resources=read(OUT/(TAG+'-resources.json'))
    earlier=[p for p in [OUT/'marine-charter-2024-v1-resources.json',
        folder/'resources-discovery-attempt-1.json',folder/'resources-unvalidated-668.json'] if p.exists()]
    attempt_metrics=[read(p) for p in earlier]
    data=ROOT/'data/marine-2024-charter-v1';catalog=read(data/'charter-candidates.json')
    baseline=read(Path(read(folder/'problem.json')['base_route']))
    taxonomy={v['species_id']:v for v in csv.DictReader((ROOT/'data/lower48-aba2024-provisional-v3/dataset/taxonomy.csv').open())}
    seen={s for d in route['days'] for stop in d['stops'] for s in stop['species']}
    provisional={s for s in seen if taxonomy[s]['score_tier'].startswith('provisional')}
    added_provisional=len(provisional.intersection(result['new_species']))
    added_primary=len(result['new_species'])-added_provisional
    names={s['id']:s['name'] for s in route['species']}
    result.update(primary=len(seen)-len(provisional),provisional=len(provisional),
        combined_peak_rss_gib=max([resources['combined_peak_rss_bytes'],*[v['combined_peak_rss_bytes'] for v in attempt_metrics]])/2**30,
        successful_catalog_construction_seconds=catalog['elapsed_seconds'],
        cumulative_port_discovery_and_audit_seconds=resources['wall_seconds']+sum(v['wall_seconds'] for v in attempt_metrics),
        previous_discovery_attempts=[str(p) for p in earlier],final_run_cache_state='warm from prior discovery; selected endpoint proofs independently checked')
    dump(folder/'result.json',result)
    itinerary=[];species_audit=[];unique=set()
    for day in route['days']:
        itinerary.append({'date':day['date'],'kind':day['kind'],'new_species':len(day['new_species']),
            'cumulative_species':day['cumulative_species'],'road_miles':day['road_miles'],
            'boat_nautical_miles':day.get('boat_nautical_miles',0),'activity_hours':day['activity_minutes']/60})
        for stop in day['stops']:
            for species in stop['species']:
                if species in unique:continue
                unique.add(species)
                species_audit.append({'species_id':species,'common_name':names[species],'first_date':day['date'],
                    'latitude':stop['lat'],'longitude':stop['lon'],'score_tier':taxonomy[species]['score_tier'],
                    'access':'modeled charter' if day['kind']=='pelagic' else 'terrestrial incumbent',
                    'source_records':'|'.join(e['record'] for e in stop['evidence'][species])})
    write_csv(folder/'itinerary.csv',itinerary,list(itinerary[0]))
    write_csv(folder/'species-audit.csv',species_audit,list(species_audit[0]))
    comparison=[{'model':'verified land + air incumbent','species':baseline['species_total'],
        'primary':647,'provisional':9,'boat_days':0,'flights':sum(d['kind']=='flight' for d in baseline['days']),
        'road_miles':sum(d['road_miles'] for d in baseline['days']),
        'boat_nautical_miles':0,'rest_days':sum(d['kind']=='rest' for d in baseline['days'])},
        {'model':'same incumbent + modeled charter expeditions','species':route['species_total'],
        'primary':result['primary'],'provisional':result['provisional'],'boat_days':result['boat_days'],
        'flights':sum(d['kind']=='flight' for d in route['days']),
        'road_miles':sum(d['road_miles'] for d in route['days']),
        'boat_nautical_miles':sum(d.get('boat_nautical_miles',0) for d in route['days']),
        'rest_days':sum(d['kind']=='rest' for d in route['days'])}]
    write_csv(folder/'comparison.csv',comparison,list(comparison[0]))
    added=''.join(f'<li>{escape(names[s])}</li>' for s in result['new_species'])
    hold_path=folder/'marine-access-score-holds.json'
    holds=read(hold_path)['species'] if hold_path.exists() else []
    if holds:
        held_rows=[{'species_id':h['species_id'],'status':h['status'],'scope':h['scope'],
            'reason':h['reason'],'resolution':h['resolution'],
            'example_record':e['record'],'example_locality':e['locality'],
            'latitude':e['latitude'],'longitude':e['longitude']}
            for h in holds for e in h.get('examples',[])]
        write_csv(folder/'marine-source-holds.csv',held_rows,list(held_rows[0]))
    port_names={v['id']:v['name'] for v in catalog['ports']}
    def formatted(key,value):
        if key in {'road_miles','boat_nautical_miles'}:return f'{value:,.1f}'
        return f'{value:,}' if isinstance(value,int) else str(value)
    table=''.join('<tr>'+''.join(f'<td>{escape(formatted(k,v))}</td>' for k,v in row.items())+'</tr>' for row in comparison)
    headers=''.join(f'<th>{escape(key.replace("_"," "))}</th>' for key in comparison[0])
    data_size=sum(p.stat().st_size for p in data.rglob('*') if p.is_file())
    run_size=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
    result['retained_marine_inputs_bytes']=data_size;result['retained_run_bytes']=run_size
    dump(folder/'result.json',result)
    html=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>2024 marine access benchmark</title><style>
body{{font:17px/1.6 system-ui,sans-serif;color:#14232a;background:#f6fafb;margin:0}}main{{max-width:1100px;margin:auto;padding:48px 30px}}
h1{{font-size:44px;line-height:1.15;max-width:850px}}h2{{margin-top:36px}}a{{color:#08617c}}.eyebrow{{letter-spacing:.13em;text-transform:uppercase;font-size:12px;color:#526d78}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:30px 0}}.card{{background:white;border:1px solid #d8e4e8;border-radius:12px;padding:22px}}.card strong{{display:block;font-size:35px;line-height:1.3}}.card span{{font-size:14px;color:#526d78}}
.note{{border-left:4px solid #087e9b;padding:14px 22px;background:#e8f4f7}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;background:white;width:100%;font-size:14px}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #d8e4e8}}th{{white-space:nowrap}}ul.species{{columns:3;column-gap:32px}}li{{break-inside:avoid}}@media(max-width:700px){{.cards{{grid-template-columns:1fr 1fr}}h1{{font-size:34px}}ul.species{{columns:1}}}}
</style><main><p class="eyebrow">Lower 48 + DC · 366 calendar days · 2024</p>
<h1>Boat access adds {len(result['new_species'])} species to the verified route</h1>
<p>The terrestrial itinerary stays fixed. Exact-date offshore trips use its free days, with complete measured driving days to and from each port.</p>
<div class="cards"><div class="card"><strong>{route['species_total']}</strong><span>modeled species · was {baseline['species_total']}</span></div>
<div class="card"><strong>+{len(result['new_species'])}</strong><span>{added_primary} primary + {added_provisional} provisional additions</span></div><div class="card"><strong>{result['boat_days']}</strong><span>charter days · {len(result['ports'])} ports</span></div>
<div class="card"><strong>{result['combined_peak_rss_gib']:.2f} GiB</strong><span>peak workflow process memory</span></div></div>
<p><a href="/?run={TAG}/route.json">Open the marine route viewer →</a> · <a href="/?run=reconciled-2024-provisional-access-v3/route.json">656-species baseline</a> · <a href="/{TAG}/comparison.csv">Comparison CSV</a></p>
<div class="note"><b>Modeled charter scenario.</b> A 12-knot return charter is assumed available on the exact observation date. Each exact pin gets 2 hours of birding; sailing, 1 hour boarding and 2.2 km/h dock walking fit within 12 hours. These are geography-checked modeled tracks, not historical operated sailings or navigational instructions. Weather, depth, tides, restricted waters and vessel availability remain assumptions.</div>
<h2>Controlled result</h2><p>{result['primary']} primary-list species + {result['provisional']} separately labeled 2024 provisionals. All original birding and {comparison[0]['flights']} flight days remain. Added expeditions depart from and return to the incumbent's actual position, using only previously free dates.</p>
<div class="scroll"><table><thead><tr>{headers}</tr></thead><tbody>{table}</tbody></table></div>
<p>Ports: {escape(', '.join(port_names[p] for p in result['ports']))}. This is a marine extension of the incumbent, not a joint native optimization of roads, flights and boats, and does not establish global optimality.</p>
<h2>Additional species</h2><ul class="species">{added}</ul>
<p>Island Scrub-Jay remains an unresolved boat-only access case: its mixed anchorage/beach coordinate does not establish a vessel observation. Its terrestrial eligibility is unchanged. <a href="/{TAG}/marine-source-holds.csv">Held access case and resolution needed</a>.</p>
<h2>Evidence and resource use</h2><p>The supplement retains all 946,985 marine or outside-state-polygon source groups (2,088,547 represented observations), with verbatim coordinate strings. The original archive and CSV are preserved. Unmapped, flagged, population-held, on-land, ambiguous-jurisdiction and unresolved-access cases receive no marine credit.</p>
<p>The catalog contains {len(catalog['trips']):,} geographically checked single-pin charters and targets species absent from the fixed incumbent. The viewer's all-observer curve is explicitly labeled as the terrestrial cohort plus these constructed charters; complete ocean coverage is still unavailable.</p>
<p>Successful catalog construction: {catalog['elapsed_seconds']/60:.1f} minutes. Final search/source-check/audit pass: {result['elapsed_seconds']/60:.1f} minutes using a warm cost/proof cache. Cumulative monitored port discovery and audit work, including interrupted discovery and the rejected 668-species draft: {result['cumulative_port_discovery_and_audit_seconds']/60:.1f} minutes. Peak combined monitored process memory: {result['combined_peak_rss_gib']:.2f} GiB. Retained marine inputs and bounded cache: {data_size/2**20:.1f} MiB; saved run: {run_size/2**20:.1f} MiB. Development and tests are additional time.</p>
<p>Independent validation rechecks the full terrestrial calendar and flight evidence, {audit['new_directed_port_roads_checked']} directed port roads, exact marine source rows against the original archive, all {audit['marine_source_records_checked']} selected marine publisher records, nearest-country jurisdiction and every water-track segment against the original OSM coastline. Maximum boat-day activity: {audit['max_boat_day_activity_minutes']/60:.2f} hours.</p>
<p><a href="/{TAG}/validation.json">Validation</a> · <a href="/{TAG}/marine-added-species.csv">Added species</a> · <a href="/{TAG}/species-audit.csv">Complete species audit</a> · <a href="/{TAG}/marine-source-decisions.csv">Marine source decisions</a> · <a href="/{TAG}/marine-plan.json">Timed expedition plan</a></p>
<h2>Remaining access gaps</h2><p>Island landings and unresolved harbor/river connections remain held. A coastline-clear path does not certify bathymetry, legal navigation or safe operation. The marine catalog is limited by the original country=US extract and exact dated reports; missing offshore records are not invented.</p>
<p>The next search improvement is to optimize terrestrial and marine trips together, allowing calendar changes and flights to position for offshore rarities. The current gain isolates boat access without changing the incumbent.</p>
<p class="eyebrow">Recording geography: <a href="https://www.aba.org/aba-recording-rules-and-interpretations/">ABA recording rules</a> · Frozen 2024 taxonomy/provisional policy unchanged</p></main></html>'''
    target=OUT/'2024-marine-charter-report.html';target.write_text(html)
    (OUT/'pilot-report.html').write_text(html)
    dump(OUT/'run-status.json',{'stage':'completed','label':'Marine charter benchmark complete',
        'run':TAG,'best_run':TAG,'best_species':route['species_total'],
        'message':f"{route['species_total']} modeled species, including {len(result['new_species'])} new species from modeled charters; source and calendar audits passed. Terrestrial route fixed."})
    print({'report':str(target),'species':route['species_total'],'new':len(result['new_species'])},flush=True)


if __name__=='__main__':main()
