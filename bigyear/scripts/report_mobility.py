"""Publish the verified paired benchmark, preserving the earlier capacity report."""

from html import escape
import json
import shutil

from bigyear_planner.util import ROOT, dump, read, sha256

OUT = ROOT/'runs/lower48-eod-2024'


def main():
    comparison = read(OUT/'mobility-comparison.json')
    drive, flight = comparison['driving'], comparison['flights']
    routes = {key:read(OUT/value['run']/'route.json') for key,value in [('driving',drive),('flights',flight)]}
    for item in [drive,flight]:
        if not item['valid'] or not read(OUT/item['run']/'validation.json')['valid']:
            raise ValueError('only independently verified benchmarks can be published')
    if sha256(OUT/drive['run']/'engine-snapshot') != sha256(OUT/flight['run']/'engine-snapshot'):
        raise ValueError('paired benchmarks used different native search executables')
    network = read(ROOT/'data/lower48-eod-2024/flights-2024/manifest.json')
    baseline = read(OUT/'national-3600s/engine.json')
    longer_drive = OUT/'paired-driving-600s'
    longer = (read(longer_drive/'route.json') if (longer_drive/'validation.json').exists()
              and read(longer_drive/'validation.json')['valid'] else None)
    longer_note = (f"A separate ten-minute driving trial reached <a href='/?run=paired-driving-600s/route.json'>{longer['species_total']} verified species</a>. It used a longer search budget and is retained separately from this matched comparison."
                   if longer else '')
    names = {v['id']:v['name'] for v in routes['flights']['species']}
    support = {v['id']:v for v in read(OUT/'species-support.json')['species']}
    missed_note = ''
    if longer:
        p = read(longer_drive/'problem.json')
        seen = {p['species'][i]['id'] for o in p['opportunities'] for i in o['species']}
        if len(seen)!=longer['species_total']:
            raise ValueError('incumbent species support mapping differs from audited count')
        missed = [v for k,v in support.items() if k not in seen]
        persistent = sum(v['reported_days']>=300 for v in missed)
        brief = sum(v['reported_days']<=7 for v in missed)
        examples = ', '.join(v['name'] for v in sorted(missed,key=lambda v:v['location_date_supports'],reverse=True)[:3])
        comparison['unseen_species_report_support'] = {'baseline':longer_drive.name,'unseen':len(missed),
             'reported_at_least_300_days':persistent,'reported_at_most_7_days':brief,
             'method':'Distinct national reporting days across every eligible exact location/date support; not abundance, biological rarity or itinerary feasibility.'}
        missed_note = f"<section><h2>The remaining gap includes persistent reports</h2><p>Of the {len(missed)} species absent from the {longer['species_total']}-species driving incumbent, {persistent} were reported nationally on at least 300 days and {brief} on at most seven days. Highly supported missing species include {escape(examples)}.</p><p>Travel constraints can matter, but short-lived sightings do not explain the whole gap. The results also point to limited regional exploration and difficulty replacing several itinerary stops together. National reporting persistence does not establish that a species was reachable from a particular route.</p></section>"
    comparison['flight_only_species_support'] = [support[v] for v in comparison['flight_only_species']]
    all_flights = []
    for day in routes['flights']['days']:
        for leg in day['legs']:
            if leg['mode']=='flight':
                a,b = [routes['flights']['locations'][leg[k]] for k in ['from','to']]
                all_flights.append({'date':day['date'],'from':a['code'],'to':b['code'],'minutes':leg['minutes']})
    comparison['flight_legs'] = all_flights
    integration_note = ''
    if (OUT/'flight-integration-result.json').exists():
        integrated = read(OUT/'flight-integration-result.json')
        if not integrated['valid'] or not read(OUT/integrated['run']/'validation.json')['valid']:
            raise ValueError('national flight integration result is not independently valid')
        integrated_route = read(OUT/integrated['run']/'route.json')
        if integrated_route['provenance']['compact_model_sha256'] != routes['driving']['provenance']['compact_model_sha256']:
            raise ValueError('national flight follow-up changed the observation cohort')
        legs = [{'date':day['date'], 'from':integrated_route['locations'][leg['from']]['code'],
                 'to':integrated_route['locations'][leg['to']]['code']}
                for day in integrated_route['days'] for leg in day['legs'] if leg['mode']=='flight']
        comparison['flight_integration_followup'] = {**integrated,'legs':legs}
        text = '; '.join(f"{v['date']}: {v['from']} → {v['to']}" for v in legs)
        fixed = integrated['fixed_checkpoint']
        integration_note = f"""<section class="note"><h2>National flight integration: {integrated['species']} verified species</h2>
<p><a href='/?run={integrated['run']}/route.json'>Open the national itinerary with {integrated['flight_days']} flight days and cumulative curves ↗</a></p>
<p>This separate follow-up starts from the longer 440-species driving result, measures flight alternatives for its three longest eligible travel gaps, then searches for 60 seconds. It is excluded from the equal-budget comparison. All eligible national observations remain available.</p>
<p>For the unchanged starting bird observations, modeled road mileage falls from {fixed['road_miles_before']:,.0f} to {fixed['road_miles_after']:,.0f}, and travel time from {fixed['travel_minutes_before']/60:,.1f} to {fixed['travel_minutes_after']/60:,.1f} hours. The final itinerary has {integrated['species']} species and {integrated['road_miles']:,.0f} road miles.</p><p><b>Audited flight dates:</b> {escape(text)}</p>
<p>This establishes modeled travel savings with real dated flight service. A species-count benefit from targeting fleeting reports remains to be demonstrated.</p></section>"""
    # Compare with the retained, verified national source digest rather than
    # deriving any identity from a selected-only display file.
    original = read(OUT/'completion.json')['source_archive_manifest_sha256']
    comparison['source_archive_unchanged'] = sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json') == original
    if not comparison['source_archive_unchanged']:
        raise ValueError('original observation archive manifest changed')
    dump(OUT/'mobility-comparison.json',comparison)
    delta = flight['species']-drive['species']
    interpretation = (f'The best flight-enabled itinerary found {delta} more species under the modeled travel rules. This is consistent with travel time limiting access to dated sightings.'
                      if delta>0 else 'This short search did not establish a species-count advantage for flights. Routing throughput and search variability still limit the comparison.')
    rows = []
    for label,value in [('Improved driving',drive),('Driving + flights',flight)]:
        engine = value['search']
        rows.append(f"<tr><th><a href='/?run={value['run']}/route.json'>{label}</a></th><td>{value['species']}</td><td>{engine['elapsed_seconds']:.1f}s</td><td>{value['resources']['wall_seconds']/60:.1f} min</td><td>{engine['warm_start']['seconds']:.3f}s</td><td>{engine['candidate_evaluations']:,}</td><td>{value['resources']['combined_peak_rss_bytes']/2**30:.2f} GiB</td><td>{value['road_miles']:,.0f}</td><td>{value['road_transit_days']}</td><td>{value['flight_days']}</td></tr>")
    geography = ''.join(f"<li><b>{label}:</b> {', '.join(v.removeprefix('US-') for v in value['visited_states'])}</li>" for label,value in [('Driving',drive),('Flights',flight)])
    gains = ', '.join(escape(names.get(v,v)) for v in comparison['flight_only_species']) or 'None'
    losses = ', '.join(escape(names.get(v,v)) for v in comparison['driving_only_species']) or 'None'
    flight_rows = ''.join(f"<tr><td>{v['date']}</td><td>{v['from']} → {v['to']}</td><td>{v['minutes']:.0f}</td></tr>" for v in all_flights)
    support_rows = ''.join(f"<tr><td>{escape(v['name'])}</td><td>{v['reported_days']}</td><td>{v['location_date_supports']:,}</td><td>{v['first_date']} – {v['last_date']}</td></tr>" for v in comparison['flight_only_species_support'])
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>2024 national driving and flight benchmark</title>
<style>body{{font:16px/1.6 system-ui;max-width:1200px;margin:35px auto;padding:0 24px;color:#15251b;background:#f6f8f6}}section{{background:white;padding:22px;margin:20px 0;border-radius:10px}}h1,h2{{line-height:1.25}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;border-bottom:1px solid #dbe9df;padding:10px}}.table{{overflow:auto}}a{{color:#24683c}}.note{{border-left:4px solid #36824b}}small{{font-size:13px}}</style>
<h1>2024 national driving and flight benchmark</h1>
<p><a href="/?run={drive['run']}/route.json">Driving route and cumulative curves ↗</a> · <a href="/?run={flight['run']}/route.json">Flight-enabled route and cumulative curves ↗</a></p>
{integration_note}
<section class="note"><h2>{drive['species']} species driving · {flight['species']} with flights enabled</h2><p>{interpretation} Both itineraries pass independent source, calendar, road-endpoint and walking checks. The flight audit additionally checks airport coordinates and operated-service dates against frozen inputs.</p><p>This is a comparison of heuristic search results. It does not establish a nationwide optimum or isolate a causal migration effect. Regional starts receive equal exploration time; the final itinerary visits regions when the search finds a feasible advantage.</p><p>{longer_note}</p></section>
<section><h2>Same observations, same search budget</h2><p>{escape(comparison['design'])}</p><p>86,330,028 eligible presence groups represent 127,097,446 observations at 1,936,106 exact locations. Every eligible candidate remains available; the all-observer curve includes all 764 eligible species.</p>
<div class="table"><table><tr><th>Mode</th><th>Species</th><th>Native search</th><th>Total with audit</th><th>Restore</th><th>Evaluations</th><th>Peak tree RSS</th><th>Road miles</th><th>Road transit days</th><th>Flight days</th></tr>{''.join(rows)}</table></div>
<p>The previous search spent {baseline['warm_start']['seconds']/60:.1f} minutes rebuilding its starting itinerary and completed {baseline['candidate_evaluations']} evaluations in {baseline['elapsed_seconds']/60:.1f} minutes. The improved search restores its checkpoint directly, screens provably impossible local travel, queues road discovery and gives separate starts time in eight geographic regions.</p><p>Evaluation counts include repeated candidates and inexpensive infeasible proposals; they are not counts of distinct nationwide itineraries. Provider queries and latency are recorded separately in each benchmark JSON.</p><ul>{geography}</ul></section>
{missed_note}
<section><h2>What changed in the bird list?</h2><p><b>Found by the flight result only:</b> {gains}</p><p><b>Found by the driving result only:</b> {losses}</p><p>These differences describe the saved routes. They do not imply that a species is impossible to reach by the other travel mode. The following reporting support is calculated from every eligible national location/date/species combination. Sparse reports measure source coverage, not biological rarity or migration speed.</p><table><tr><th>Flight-result addition</th><th>Reported days nationally</th><th>Exact location/date supports</th><th>First–last report</th></tr>{support_rows}</table></section>
<section><h2>Observed 2024 flight service, conservative travel days</h2><p>{network['airports']} airports and {network['route_dates']:,} dated nonstop connections, derived from {sum(s['accepted_operated_flights'] for s in network['sources']):,} operated flights. Source: <a href="{network['source_documentation']}">US Bureau of Transportation Statistics</a>. Airport coordinates are frozen to an end-of-2024 OurAirports snapshot.</p><p>{escape(network['policy'])} Reported departure dates guide availability; midnight crossings are not reconstructed. Airport pins are representative coordinates; measured road anchors and assumed terminal access are separate. Eight nearby airports at each end are ranked using observed service and estimated ground access; up to two nonstop alternatives are measured. This is a restricted flight heuristic, not an exhaustive airline journey planner.</p><p>Short discovery queries keep exploration moving. Final selected-road geometry receives a separate 120-second routing allowance and strict endpoint/cost validation. Unknown queries remain deferred.</p><p>The matched flight-enabled result uses {flight['flight_days']} actual flight days.</p><table><tr><th>Flight date</th><th>Airport pair</th><th>Minutes including airport overhead</th></tr>{flight_rows}</table></section>
<section><h2>Laptop storage and cleanup</h2><p>The memory-mapped observation model and original Parquet archive are preserved. Road-discovery queues, native hot caches and geometry caches have fixed caps. Matched benchmarks retain separate bounded road caches and their common starting snapshot. Monthly airline ZIPs and a narrow dated network remain as reproducible source inputs; they are read as streams and never expanded into full monthly CSV files.</p><p>Both discovery and audit routers stop after each run. Saved maps render from their route and coverage files without running the routing services or loading national road tiles. Source archive manifest unchanged: {comparison['source_archive_unchanged']}.</p></section></html>'''
    previous = OUT/'pilot-report.html'
    if not (OUT/'capacity-report.html').exists() and previous.exists():
        shutil.copyfile(previous,OUT/'capacity-report.html')
    previous.write_text(html)
    (OUT/'mobility-report.html').write_text(html)
    print(json.dumps({'driving_species':drive['species'],'flight_species':flight['species'],'flight_legs':len(all_flights),'source_archive_unchanged':comparison['source_archive_unchanged']}))


if __name__ == '__main__':
    main()
