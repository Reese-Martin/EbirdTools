# 2024 record comparison

The working target is the user's 759-species Lower-48 record benchmark. A modeled
route is not a demonstrated real-world big year or ABA certification. The dated
Dobson itinerary and verified observer identifier have not been supplied. No
other birder's itinerary is used as a substitute.

The routing-resolution follow-up reached **682 modeled species (673 primary +
9 provisional)**, four above 678 and **77 below the working 759 target**. All four
previously unresolved targets now have measured feasible complete-calendar
alternatives. The selected route collects Eurasian Tree Sparrow in Missouri on
January 1, Fieldfare in Wisconsin on March 8, and European Goldfinch in Illinois
on March 11. White-winged Tern remains uncollected; its best measured alternative
scores 678, so the remaining issue is a calendar/search trade-off in the tested
routes, rather than a complete absence of resolved routing.

Compared with 678, the route also gains Smith's Longspur and Yellow Grosbeak and
loses Long-eared Owl. It has 29 flight days, 11 modeled charter days, 20,585.3 road
miles and birding visits in 20 states. All 3,839 publisher source records, 126
directed road paths and 366 days pass independent validation. The final monitored
workflow took 39.3 minutes and peaked at 2.71 GiB RSS; earlier interrupted
diagnostics are excluded from those timings and remain logged separately.

Alternate local car probes resolve directional matching failures; airport
approach probes address disconnected centroid matches. The wider 24-airport
shortlist includes Minneapolis–St. Paul, ninth in Fieldfare's proximity ranking,
which the old eight-airport limit missed. Up to six pairs receive measured
transfers with actual dated nonstop service. Cache migration carries only finite
costs with identical endpoints and requires fresh selected endpoint proofs.
An existing source-visit hold also now precedes cached stop-duration evaluation;
the held observation remains archived and physical road connectivity is unchanged.
No observation coordinates, scoring rules, walking speeds or access holds were
relaxed. All original Parquet parts still match their conversion checksums.

See the current [`joint report`](../runs/lower48-eod-2024/2024-joint-mobility-report.html)
and `runs/lower48-eod-2024/resolved-routing-2024-v3/` for the route, four routing
witnesses, target outcomes, full audit and executed source snapshots.

The earlier targeted regional follow-up reached **678 modeled species (669 primary +
9 provisional)**, five above the preceding 673 route and **81 below the working
759 target**. It collects three of the nine requested targets: Green-breasted
Mango in Louisiana on January 21, Himalayan Snowcock in Nevada on September 25,
and Taiga Bean-Goose in New York on December 31. The final list gains eleven
species and loses six relative to 673. Black-tailed Gull and Northern Wheatear
have measured feasible alternative calendars with lower totals. Tested campaigns
for Eurasian Tree Sparrow, European Goldfinch, Fieldfare and White-winged Tern
were routing-unresolved in that run; they had not been proved globally infeasible.

The new calendar has 26 flight days, 11 modeled charter days, 19,348.1 road miles,
637.6 boat nautical miles and birding visits in 17 states. All 3,898 selected
publisher records, 120 directed road paths and 366 calendar days pass independent
validation. The monitored workflow took 40.5 minutes and peaked at 2.81 GiB RSS:
15 minutes of targeted construction, two minutes each of boat/general regional
construction, 15 minutes of native search, plus input/transport/source audits.
Every original Parquet part also matches its verified conversion checksum.
Full observation support, exact source coordinates, all access holds and
2.2 km/h round-trip walking above 100 m remain unchanged.

See [`2024-joint-mobility-report.html`](../runs/lower48-eod-2024/2024-joint-mobility-report.html)
and `runs/lower48-eod-2024/targeted-regions-2024-v3/`. The report separates the
nine target outcomes, source dates, unresolved queries, complete-calendar
alternatives and all gains/losses. Search budgets differ across runs; the
comparison does not isolate a causal effect of the new proposal strategy.

The preceding verified joint driving/flight/charter follow-up reached **673 modeled species
(664 primary + 9 provisional)**, six above the fixed-calendar boat result and
seventeen above the 656-species land/air result. Eleven charter days use five
ports, with 26 flight days and 19,480.5 road miles. All 3,859 selected publisher
records, 120 directed road paths and the full 366-day calendar passed independent
checks. The source observations and verbatim coordinates remain in the original
Parquet archive, with 2.2 km/h round-trip walking above 100 m.

The search/audit workflow peaked at 2.87 GiB RSS and took 35.5 monitored minutes,
including the 900-second native search, a 60-second corrective pass, regional
construction and both audits. A first 673 candidate was rejected for a 571 m
open-water walk to a Curlew Sandpiper pin. That exact point remains held; the
final route uses a different actual observation on the same date at a previously
audited land-access location. No source pin was moved or erased. The two phases
and their source/engine hashes remain separate in `search.lineage`.

That calendar's working-target gap was **86 species**. Of its 119 uncollected source-pool taxa,
44 have at most seven modeled dates, 24 have a same-date option within 30 air
miles of the traveler's end position, nine have terrestrial support only in
unvisited states, and six occur only in constructed marine options. Proximity
and source availability do not establish travel/access feasibility. The calendar
still has 110 rest days; those dates cannot automatically replace tightly dated
rarity opportunities.

A source-bound, conservative calendar-conflict certificate has a 789-species
upper bound for this frozen candidate model, even allowing 200 km/h cars and
full local/walking slack. Recorded Common Swift and Great Knot options are one
incompatible pair under the current full-day flight calendar. The bound does not
rule out 759 or establish a feasible route at that score. It is not a real-world
ABA bound, and broader marine inputs or a different calendar need a new analysis.

See its preserved [`published-report.html`](../runs/lower48-eod-2024/joint-mobility-2024-access-v3/published-report.html)
and `runs/lower48-eod-2024/joint-mobility-2024-access-v3/`. Further work should
prioritize species-directed regional trips, resolved port/island access, and
matched calendar comparisons. The recordholder replay remains pending the
actual dated input; missing data cannot be separated species-by-species yet.

The earlier boat-only access benchmark reached **667 modeled species (658 primary
and 9 provisional)**, eleven above the verified 656-species incumbent. Eight
modeled charter days use four ports, retaining the original terrestrial birding
and 26 flight days. All 163 credited marine source records and the full calendar
passed independent checks. Island Scrub-Jay remains an unresolved marine access
case; its terrestrial eligibility is unchanged. See
[`2024-marine-charter-report.html`](../runs/lower48-eod-2024/2024-marine-charter-report.html).
That comparison's working-target gap was 92 species. It fixes the land/flight
calendar; the later joint comparison can rearrange the calendar.

## Search changes

* Road discovery and airport-transition construction have separate, bounded
  workers. One slow class cannot occupy both worker slots. Both use measured car
  routes; deferred queries never become invented disconnected roads.
* Python evaluation waits on discovery completion. Native proposals back off
  when their bounded retry queue is full, while retaining a feasible incumbent.
* A compact index contains every actual recorded date at each exact source
  location. Date moves choose a different recorded opportunity. The current
  follow-up mostly tries dates within fourteen days, with global date moves
  retained. Partial-year catalogs use their actual first date, including leap day.
* Regional heads take turns across eight regions and retain seasonal/geographic
  diversity. Raw and rarity-weighted heads are ranked against their annual
  scales. Trips are constructed lazily, full blocks are tested before partial
  repairs, and each addition uses the complete measured calendar evaluator.
* Nearby same-day stops can be proposed. Longer transfers reserve travel days.
  Unknown access remains retryable. Feasible regional footholds are retained in
  a bounded macro-move library even when they do not immediately improve the
  complete incumbent.

None of these changes thins the observation cohort or rounds coordinates.
Proposal shortlists guide search; they do not prove optimality.

## Scoring and water access

The dated July 2024 ABA committee decision and Pyle's 2024 Tables 2a/b allow
provisional species subject to the other recording rules. The primary-only v2
policy remains frozen for controls. A separate v3 policy labels primary,
provisional-A and provisional-B credit. It retains public publisher review as an
explicit occurrence proxy and checks selected source metadata for captive/escape
flags. It does not certify individual wildness.

The thirteen additions with supported Lower-48 jurisdictions/events are the
California/Utah Mandarin Duck; California Yellow-headed Parrot, Burrowing
Parakeet, Swinhoe's White-eye, Northern Red Bishop and Pin-tailed Whydah; Texas
White-fronted Parrot; Florida Orange-winged Parrot, Blue-and-yellow Macaw,
Chestnut-fronted Macaw, Blue-crowned Parakeet and White-eyed Parakeet; and the
Corpus Christi Cattle Tyrant event through May 4. Geographic envelopes are
conservative modeling assumptions. Yellow-headed Parrot's California provisional
status does not revive the historical Florida primary population. Cattle Tyrant
remains provenance-uncertain provisional credit, with TBRC non-acceptance
explicitly disclosed. Hawaii-only entries and unaccepted identification cases do
not gain mainland credit from provisional membership.

The resulting terrestrial pool is 783 modeled species, compared with 770 in v2.
The v3 model retains 87,706,132 eligible source groups representing 129,222,477
observations at 1,953,771 exact locations. Original Parquet and prior runs remain
unchanged. The source archive also retains held/excluded observations.

ABA geographic rules can include islands and adjacent ocean waters, to 200 miles
or the relevant jurisdictional midpoint. The existing Census-state polygon and
land mask are narrower model filters, not ABA rules. Some marine observations
are discarded before routing; changing only the land mask cannot restore a
complete maritime cohort. Source country/state labels alone do not establish
nearest-land jurisdiction.

A coastline audit checks selected straight-line walks. The original 648 route's
Napatree Point pin in Rhode Island has about 705 metres of water in its assumed
walk. A real land path may repair it, but none has been validated. The v3 follow-up
holds that exact pin on every date, without deleting any source observation or
caching a road as unreachable. Three species had no other support in the original
648 itinerary. A foot-routing probe returned a zero-length path and was rejected.

`surface_access.validate_boat_trip` validates supplied dated access blocks with
explicit sailing times, port access, 2.2 km/h foot travel, individual exact dated
observation/access references, jurisdiction and private-land permission. Full
sailing time counts, so a fourteen-hour pelagic trip exceeds the present
12-hour activity cap. This strict dated-service input validator is separate
from the modeled charter catalog. Scheduled passenger ferries and island
landings remain outside the native charter scenario. The earlier Python charter
extension assumed vessel availability within the same 12-hour budget, with
geography-checked water tracks and boat-only access holds. The later native
optimizer jointly selects those modeled charters, land visits and dated flights.
It does not claim actual historical operation. A dated
NPS concessionaire listing documents Island Packers in 2024; it does not prove
operation or availability of a particular departure. Car-profile ferries may already
be included in measured road geometry under the frozen static-network caveat;
that is not evidence of a dated 2024 passenger sailing.

## Controlled comparisons

Results and the full design are in
`runs/lower48-eod-2024/search-calendar-controls-v2/`. Each native arm starts from
the same independently verified 648-species route, uses the same base warm-cache
snapshot, random seed 20240101, two native threads and 180 active seconds. The
wider-calendar arms first resolve additional direct roads needed to evaluate the
same seed; this preparation is outside the search timer. A failed first start
without that preparation was excluded, then rerun.

| Native search bundle | 30-mile daily birding cap | 150-mile cap |
| --- | ---: | ---: |
| Previous binary + shared routing workers | 651 | 650 |
| Indexed recorded dates + reserved workers + bounded backpressure | 648 | 648 |

The indexed controls used uniformly sampled actual dates. The later follow-up
adds nearby-date preference and improved annual head ranking. Binary snapshots
and hashes distinguish these phases. Follow-up changes are not retroactively
credited to the four controlled arms.

The 30-mile indexed arm reduced deferred evaluations from 6,469,258 to 758,916,
but also completed fewer evaluations and found fewer species in this short
trial. Queue throughput improvement is not evidence of a better route. One
seed and a short budget are insufficient to rank stochastic search algorithms.
The wider mileage cap alone did not improve these runs. These tests retain
full-day flights, separate ground-access days, two-hour visits, twelve-hour
activity days, eight-hour transit driving and 2.2 km/h round-trip walking above
100 m. They do not test realistic intraday flight schedules.

A 600-second regional follow-up and 300-second native search use the original
calendar. A separate 300+300-second run tests the reconciled provisional/access
policy. The primary follow-up finished at 651 species. The reconciled run finished at
656 (647 primary + 9 provisional), with all 3,588 selected publisher records
supported under the policy proxy. Its 25 selected walks had no detected
open-water crossing over 100 m, and peak combined RSS was
3.07 GiB. That experiment's working-target gap was 103 species.
These different budgets/policies are not a controlled algorithm effect.
See `runs/lower48-eod-2024/2024-search-calendar-report.html` and
`search-calendar-controls-v2/evaluation-result.json` for results and provenance.

## Recordholder replay

`runs/lower48-eod-2024/search-calendar-controls-v2/recordholder-replay/` contains an
empty CSV template, identity/hash-bound manifest template and explicit
`pending_input` status. `scripts/replay_itinerary.py` accepts normalized CSV or
EBD columns, matches shared observer IDs as complete tokens, retains verbatim
coordinates, maps 2024 taxonomy and reports exact model support or specific gaps.
It never substitutes nearby sightings for a missing dated record. The CLI
currently performs the coverage/input audit. Its Python API can also evaluate
matched opportunities using the native calendar evaluator; that canonical-order
evaluation is not a reconstruction of the person's timed within-day route.

Once the actual data arrive, supply the verified observer ID, identity reference,
input path and SHA-256. Departure/arrival times, time zones, vessel tracks,
permissions and checklist chronology are still necessary for strict timed replay.
EOD absence is a coverage/policy gap, not evidence that Dobson failed to observe
a species. Until that input exists, the 759 gap cannot be assigned precisely to
missing data versus scoring, access, calendar assumptions and search quality.

## Primary references

* [ABA July 2024 committee decision](https://www.aba.org/aba-checklist-committee-report-july-2024/)
* [Pyle 2024, Tables 2a/b/c](https://www.birdpop.org/docs/pubs/Pyle_2024-ABA-Checklist-in-an-evolving-world_NAB-75-1-10.pdf)
* [ABA geographic and recording rules](https://www.aba.org/aba-recording-rules-and-interpretations/)
* [TBRC Cattle Tyrant statement](https://www.texasbirdrecordscommittee.org/review-information/list-of-statements/cattle-tyrant)
* [NPS visitor services, dated October 4, 2024](https://home.nps.gov/chis/learn/management/visitor-services-list.htm)
