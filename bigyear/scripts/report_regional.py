"""Publish the audited regional benchmark with explicit search and record goals."""

from collections import Counter
from html import escape
import json
import re
import shutil

from bigyear_planner.util import ROOT, dump, read, sha256

OUT = ROOT/'runs/lower48-eod-2024'


def main():
    summary = read(OUT/'regional-benchmark-result.json')
    tag = summary['run'];folder = OUT/tag
    if not summary['valid'] or not read(folder/'validation.json')['valid']:
        raise ValueError('publish only independently audited regional results')
    verified = read(OUT/'regional-viewer-verification.json')
    if not verified['valid'] or verified['route_sha256']!=sha256(folder/'route.json'):
        raise ValueError('served viewer verification is missing or stale')
    route = read(folder/'route.json')
    baseline = read(OUT/'national-flights-60s/route.json')
    engine = read(folder/'engine.json')
    resources = read(OUT/(tag+'-resources.json'))
    audit = read(folder/'validation.json')
    construction = summary['regional_construction']
    original_sha = read(folder/'benchmark-design.json')['source_archive_manifest_sha256']
    if sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json')!=original_sha:
        raise ValueError('original observation archive changed')
    states = Counter(route['locations'][s['location']]['state'] for d in route['days'] for s in d['stops'])
    days = Counter(d['kind'] for d in route['days'])
    flights = [{'date':d['date'],'from':route['locations'][leg['from']]['code'],
         'to':route['locations'][leg['to']]['code'],'minutes':leg['minutes']}
         for d in route['days'] for leg in d['legs'] if leg['mode']=='flight']
    def species_ids(r):
        return {species for d in r['days'] for s in d['stops'] for species in s['species']}
    old,new = species_ids(baseline),species_ids(route)
    if len(old)!=baseline['species_total'] or len(new)!=route['species_total']:
        raise ValueError('saved route species identities differ from audited counts')
    names = {v['id']:v['name'] for v in route['species']}
    historical_labels = sorted(names[v] for v in new if re.search(r'\(\d{4}-\d{4}\)',names[v]))
    policy = route['taxonomy_policy']
    gained = sorted(new-old,key=lambda v:names[v])
    lost = sorted(old-new,key=lambda v:names[v])
    comparisons = {'run':tag,'baseline':'national-flights-60s','baseline_species':baseline['species_total'],
       'regional_species':route['species_total'],'gained_species':[names[v] for v in gained],
       'lost_species':[names[v] for v in lost],'species_ceiling':764,'working_target':705,
       'reported_2024_lower48_reference':759,'historical_reference_source':'https://en.wikipedia.org/wiki/Big_year',
       'historical_reference_note':'Reported human result; experimental presence-only count lacks approval/captive/exotic metadata. Taxonomy, pelagic eligibility and observation evidence also differ. Not a like-for-like record claim.',
       'taxonomy_policy':policy,'aba_countability_verified':False,
       'selected_historical_date_labels_for_review':historical_labels,
       'visited_states':dict(states),'flights':flights,'all_observations_retained':True,
       'source_archive_unchanged':True,'source_audit_valid':True,'served_viewer_valid':True}
    dump(OUT/'regional-comparison.json',comparisons)
    flight_rows = ''.join(f"<tr><td>{v['date']}</td><td>{v['from']} → {v['to']}</td><td>{v['minutes']:.0f}</td></tr>" for v in flights)
    state_rows = ''.join(f"<tr><td>{v.removeprefix('US-')}</td><td>{n}</td></tr>" for v,n in sorted(states.items()))
    improvement = route['species_total']-baseline['species_total']
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>2024 regional exploration benchmark</title>
<style>body{{font:16px/1.6 system-ui;max-width:1160px;margin:35px auto;padding:0 24px;color:#15251b;background:#f6f8f6}}section{{background:white;padding:24px;margin:20px 0;border-radius:10px}}h1,h2{{line-height:1.25}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;border-bottom:1px solid #dbe9df;padding:10px}}.table{{overflow:auto}}a{{color:#24683c}}.note{{border-left:4px solid #36824b}}.cards{{display:flex;gap:28px;flex-wrap:wrap}}.cards strong{{display:block;font-size:32px}}</style>
<h1>2024 regional exploration benchmark</h1><p><a href="/?run={tag}/route.json">Open the audited national route and cumulative curves ↗</a></p>
<section class="note"><h2>{route['species_total']} modeled species · {len(states)} birding states · {len(flights)} flights</h2>
<div class="cards"><div><strong>+{improvement}</strong>species over the 440-species flight incumbent</div><div><strong>{route['species_total']/764:.1%}</strong>of the eligible national species pool</div><div><strong>{summary['road_miles']:,.0f}</strong>road miles</div></div>
<p>The route visits {', '.join(v.removeprefix('US-') for v in sorted(states))}. Birding observations span longitudes {summary['westernmost_birding_longitude']:.2f}° to {summary['easternmost_birding_longitude']:.2f}°. Every selected stop has original exact-point and date evidence; every selected flight has an operated 2024 service date and measured airport driving.</p>
<p>The 500-species milestone is {'achieved' if route['species_total']>=500 else 'not yet achieved'}. The remaining gap to the working 705-species target is {max(0,705-route['species_total'])}. This is the best found itinerary, with no claim of national optimality.</p></section>
<section class="note"><h2>Species count: experimental presence-only policy</h2><p>The {route['species_total']}-species route and 764-species comparison pool use the unchanged <code>{escape(policy)}</code> policy. The extract lacks individual observation approval, captive and exotic-status metadata. Validation establishes source evidence and modeled travel feasibility; it does not certify ABA countability.</p><p>Selected checklist labels requiring particular review: {escape(', '.join(historical_labels)) or 'None flagged by historical date labels'}. This is a limited flag, not an exhaustive countability audit. The ABA applies population and time restrictions to introduced species; see its <a href="https://www.aba.org/aba-area-introduced-species/">introduced-species guidance</a>. A countability and observation-quality audit is needed before comparing this total with human Big Year records. The source observations and search counts have not been silently changed.</p></section>
<section><h2>What changed in the search</h2><p>Whole regional trips can replace a section of the annual route in one move. This permits coordinated entry, several birding stops and exit, so the optimizer can cross a barrier that single-stop changes cannot. Overlapping four-week proposals reserve room for airport access, a full flight day and airport egress. Proposed stops are ranked using all {construction['all_opportunities_ranked']:,} exact location/date opportunities; all remain available to the full native search.</p>
<p>The regional phase accepted {construction['accepted_blocks']} improving trip replacements. The subsequent native search used a bounded library of {engine.get('regional_block_library',0)} regional proposals, attempted {engine.get('regional_block_proposals',0):,} block moves and completed {engine['candidate_evaluations']:,} candidate evaluations. Counts can include repeated candidates. Unknown or time-limited routing remains deferred; no estimated distance is accepted as a road cost.</p>
<p>86,330,028 eligible presence groups still represent 127,097,446 observations at 1,936,106 exact locations across all lower 48 states and DC. The all-observer comparison remains all 764 eligible species. No coordinate rounding, spatial aggregation or candidate thinning was introduced.</p></section>
<section><h2>Runtime and laptop capacity</h2><div class="table"><table><tr><th>Stage</th><th>Elapsed</th></tr><tr><td>Regional construction</td><td>{construction['elapsed_seconds']/60:.2f} min</td></tr><tr><td>Native full-cohort search</td><td>{engine['elapsed_seconds']/60:.2f} min</td></tr><tr><td>Total including preparation and independent audit</td><td>{resources['wall_seconds']/60:.2f} min</td></tr><tr><td>Combined peak process RSS</td><td>{resources['combined_peak_rss_bytes']/2**30:.2f} GiB</td></tr><tr><td>Free disk after completion</td><td>{shutil.disk_usage(ROOT).free/2**30:.1f} GiB</td></tr></table></div>
<p>The earlier 440-species route used a different search strategy and budget, so this is a before/after capacity and result comparison, not a matched estimate of the effect of any one operator. The prior matched five-minute trials reached 437 driving and 433 with flights enabled at about 2.1–2.2 GiB peak RSS.</p>
<p>Road costs, access anchors, discovery queues and geometry caches remain bounded. Native bridge credentials and marked query spill are removed. Discovery and audit routers are stopped. The original Parquet archive is preserved; saved routes render without loading national road tiles or restarting routing services. Reusable inputs, one bounded run cache and audited outputs are retained deliberately.</p></section>
<section><h2>Travel and validation</h2><p>{days['birding']} birding days, {days['road_transit']} road transit days, {days['flight']} flight days, {days['rest']} rest days. Birding remains 120 minutes per stop, with full round-trip walking above 100 m at 2.2 km/h. Local road travel remains limited to 30 miles on birding days and driving-only transit remains limited to 480 minutes per day.</p>
<p>The independent audit checked all 366 days, {audit['evidence_rows_checked']:,} selected species evidence rows and {audit['endpoint_matching']['pairs_checked']} directed road endpoint pairs. Maximum endpoint gap is {audit['endpoint_matching']['max_gap_m']:.3f} m against a 1.2 m tolerance. It also streamed the original BTS ZIP records for the {len(audit['flight_service']['original_monthly_archives_checked'])} selected flight months. Served viewer playback, activity and all-observer curves pass verification.</p>
<table><tr><th>Flight date</th><th>Airports</th><th>Minutes including overhead</th></tr>{flight_rows}</table>
<p>Flights use observed noncancelled, nondiverted reporting-carrier service. Each nonstop occupies a full nonbirding day, with separate measured driving days to and from airport road anchors. Travel minutes include mean actual block time plus 180 minutes of airport overhead. Airport pins are representative coordinates; terminal access and rental cars are assumed. Only the bounded nearby-airport nonstop heuristic is searched; seats, fares, exact schedules and midnight crossings are not reconstructed.</p></section>
<section><h2>Geographic coverage</h2><table><tr><th>State</th><th>Birding stops</th></tr>{state_rows}</table></section>
<section><h2>Changes in the bird list</h2><p><b>{len(gained)} additions:</b> {escape(', '.join(names[v] for v in gained)) or 'None'}</p><p><b>{len(lost)} species in the prior route only:</b> {escape(', '.join(names[v] for v in lost)) or 'None'}</p><p>These are saved-route differences, not proof that a species is unreachable by another itinerary.</p></section>
<section><h2>Longer-term comparison</h2><p>705 remains an intermediate target. Published summaries report <a href="https://en.wikipedia.org/wiki/Big_year">759 species for Ezekiel Dobson’s 2024 Lower 48 Big Year</a>. This historical reference is distinct from the model’s 764-species presence-only evidence pool. Missing countability metadata, land-only eligibility and frozen taxonomy prevent a direct record comparison. Exceeding a numerical target alone would not establish a like-for-like birding record.</p></section></html>'''
    (OUT/'regional-report.html').write_text(html)
    (OUT/'pilot-report.html').write_text(html)
    print(json.dumps({'species':route['species_total'],'gained':len(gained),'lost':len(lost),'states':len(states),'flights':len(flights)}))


if __name__=='__main__':
    main()
