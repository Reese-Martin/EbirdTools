# Current 2024 optimization scenario

The October 21, 2026 decisions retain the lower 48 states and DC as the working
geography. The full ABA Area additionally includes Alaska, Hawaii, Canada,
St. Pierre and Miquelon, and adjacent waters. See the
[ABA area definitions](https://www.aba.org/listing-areas-and-regions/).
A 759-species full-area result is a different geographic comparison, rather
than an established 60-species optimization deficit for the current model.
Expanding geography will require additional observation, countability and
transport inputs; this policy change does not expand the data cohort.

## Daily schedule

New scenario configurations set `mixed_road_birding: true` and
`activity_minutes: 720`. Ordinary birding dates can combine measured driving,
birding and walking within twelve activity hours. There is no separate
eight-hour driving cap or road-mile cap on these dates. Each exact location/date still
requires two birding hours plus the existing access walking allowance. Thus
two two-hour visits and eight driving hours fit exactly if no additional
walking is needed. A walking allowance is additional activity time, so the
same example with access walking must use less driving time.
A single two-hour visit can share a date with ten driving hours; a driving-only
date can use all twelve hours. The clarified configuration uses
`drive_minutes: 720` together with `activity_minutes: 720`.

The legacy `local_miles: 30` field remains for reproducible older runs and
unchanged boat-day access. Charter and island days keep their existing
exclusive-visit, sailing, boarding, dock/island walking and daily budget rules.
Flights still occupy a full nonbirding day with separate measured ground
access and egress days. Longer road transitions continue to use whole transit
days; splitting a single road leg across the end of a birding day and the start
of another is not yet modeled. Within-date stop order remains canonical.

Detection probability, fatigue, mandatory recovery, monetary costs and
weather restrictions remain outside this perfect-hindsight experiment. The
birder may start anywhere and finish anywhere. All observations, exact
coordinates, 2.2 km/h round-trip walking, countability and sensitive/access
holds remain unchanged. Unknown routing costs remain pending.

## Calendar rearrangement

The objective remains unique countable species first, followed by travel time
and road miles. Rest has no independent penalty. A warm start supplies a
feasible incumbent, not a list of mandatory visits. Recorded-date moves,
replacement of selected stops and reconstruction of regional/calendar windows
remain enabled. A species already credited may be collected at another actual
source-supported location/date to free a conflicting date for a missing bird.
No source dates are invented. Explicit tentpole experiments can still require
specific visits; the mixed-policy regression has no required visits.

## Twelve provisional species in the verified 699-species incumbent

These are separate from the 687 primary species. All twelve currently use the
frozen policy's **provisional-A** treatment; this does not establish primary-list
acceptance or certify every individual's wild origin.

| Species | First credited date | State |
|---|---|---|
| Blue-and-yellow Macaw | 2024-04-26 | Florida |
| Blue-crowned Parakeet | 2024-04-29 | Florida |
| Burrowing Parakeet | 2024-11-28 | California |
| Chestnut-fronted Macaw | 2024-04-26 | Florida |
| Mandarin Duck | 2024-12-06 | California |
| Northern Red Bishop | 2024-11-30 | California |
| Orange-winged Amazon | 2024-04-26 | Florida |
| Pin-tailed Whydah | 2024-08-23 | California |
| Swinhoe's White-eye | 2024-07-22 | California |
| White-eyed Parakeet | 2024-04-28 | Florida |
| White-fronted Amazon | 2024-01-06 | Texas |
| Yellow-headed Parrot | 2024-11-30 | California |

Yellow-headed Parrot is credited through the California provisional population;
the historical years in its existing display label refer to an older Florida
primary-population treatment, not the date of these California observations.
Cattle Tyrant is a provisional-B candidate in the model, but is not collected
in the verified 699-species route.

The source route is `rest-opportunities-2024-v1/route.json`, SHA-256
`be0b7bc6ffcf68a1e5885ed0e0c3ca2e6220cb25667a625c08ab822c214e8194`.

## Prepare and verify the separate scenario

From `bigyear/`:

```sh
.venv/bin/python scripts/prepare_mixed_activity.py
.venv/bin/python -u scripts/benchmark_mixed_activity.py --seconds 60
```

The prepared metadata changes `mixed_road_birding` and `drive_minutes`. All complete-model
components are hard linked to the frozen inputs, avoiding a duplicate dataset.
The physical road network, directed costs and access anchors are unchanged;
their caches can be reused. Selected roads and the final complete calendar
still receive independent validation. New output directories preserve the
legacy 699-species route and its original rules. The short regression is a
finite search, not a claim of optimized national coverage under the new policy.

## Completed eight-hour control before the driving clarification

`runs/lower48-eod-2024/mixed-activity-2024-v1` is independently verified at
699 species (688 primary + 11 provisional), with all 366 dates and 4,318
publisher records checked. It used a 60-second native search while still
retaining an eight-hour driving cap. It gained Kirtland's Warbler and lost
provisional White-eyed Parakeet relative to the original 699-species route.
Travel decreased by 14.15 hours and unused dates increased from 95 to 156.
This remains a feasible baseline under the broader shared twelve-hour rule;
it is not a completed optimization of that broader rule.

The eight-hour control and its source catalog are preserved. The clarified
twelve-hour model is separately bound in `data/shared-12h-2024-v1/model`.
New general and EBD example configurations use the clarified twelve-hour total.

## Full-year shared-budget benchmark

The full benchmark retains the complete source catalog and the approved
provisional pool. It replays both verified 699-species calendars, tests their
combined visits, then allocates 900 seconds to native calendar rearrangement,
600 seconds to regional reconstructions, 600 seconds to missing species on
actual rest dates, and 900 seconds to joint calendar polishing with regional
blocks. Driving, dated flights and existing boat/landing alternatives compete
throughout. Every final calendar requires the independent source and transport
audit. Conditional credits are included in the objective and reported separately
from primary species.

The new rest-date reference is reconstructed under the shared twelve-hour
schedule without rereading observation evidence. This reference is an internal
proposal aid and carries no validation claim. The final route still receives
full evidence materialization and independent audit.

Measured physical road costs can be reused. Old eight-hour transit schedules
are discarded and recomputed. A pending overland alternative must not block an
already measured flight, and pending airport discovery must not block a
measured driving option. If every connection is unresolved, the candidate
remains pending. These fallbacks never supply estimated feasibility costs.

```sh
.venv/bin/python -u scripts/benchmark_shared_year.py --tag shared-12h-full-2024-v2
.venv/bin/python scripts/report_shared_year.py --tag shared-12h-full-2024-v2
```

The comparison preserves the original 699 route. Reports bind both route
hashes and identical observation/coordinate components, show added and lost
species, primary/provisional counts, travel, rest dates, search/runtime and
memory use. Search budgets and warm-cache preparation differ from the older
study; this is not a controlled timing comparison or a proof of optimality.

### Verified full-year result

`shared-12h-full-2024-v2` independently validated at **714 species: 701 primary
+ 13 provisional**, with **15 additions and no losses** relative to the
original 699-species route. All currently eligible provisional taxa were
collected, including provisional-B Cattle Tyrant on January 14 in Texas.
All 366 dates and 4,689 publisher records passed the independent audit.

The route uses 23,608 road miles, 29 flight days, 10 pelagic charter days and
one island landing day. It has 144 rest days versus 95 in the original route.
Sixty-two ordinary birding days exceed the former 30-mile cap, and three exceed
the former eight-hour driving cap while fitting the shared twelve-hour budget.
End-to-end runtime was 61.6 minutes, with 2.88 GiB peak sampled process-tree RSS.
Both managed routing workers stopped; the original source archive and baseline
remain unchanged. All 234 Python regression tests and 18 viewer tests passed.

The final actual rest-calendar inventory has 3,285 dated opportunities for
65 missing species. These still require measured connected-calendar tests.
The result is a feasible best-found route, not a proof of optimality.

The report and species-change/provisional tables are in
`runs/lower48-eod-2024/shared-12h-full-2024-v2/`. Final route SHA-256:
`f6f0b041a97701a6e9eb413bafaacd24da9211b3de2ce94aab1506af63e784f4`.

## Historical 2023 comparison

`runs/lower48-eod-2023/shared-12h-full-2023-v5/` is independently verified at
**619 species: 610 primary and 9 provisional**. All 365 calendar days and
3,291 selected publisher records passed. The run retains all original observations
and exact coordinates and uses the same shared twelve-hour activity budget,
2.2 km/h walking, full flight days and modeled boat rules.

The scored 2023 cohort contains 785 species. Common Waxbill and Red Avadavat
mainland records are held under the established-population rules; their original
records remain archived. The audited derivative preserves every canonical
candidate ID and every other dated species entry. Historical taxonomic and
population assumptions and preparation commands are in `HISTORICAL_YEARS.md`.

The 2023 route visits 10 states, drives 8,358 miles, spends 112 hours birding,
and has 233 rest days, 25 flight days and six pelagic days. It chooses no island
landing. The 2024 reference visits 19 states and has 144 rest days. The routes
share 599 species, with 20 unique to the 2023 route and 115 to the 2024 route.
Of those route differences, 107 of the 2024-only species have scored 2023 support,
and 17 of the 2023-only species have scored 2024 support. Feasible combined trips
still require calendar and travel tests.

Both final study allocations are approximately fifty minutes after initialization;
the 2024 incumbent has substantial additional earlier optimization history. These
results do not establish that 2023 is inherently poorer or that either route is
optimal. The paired viewer and exports are in `runs/year-comparison-2023-2024/`,
served at `http://127.0.0.1:8774/report`. All 239 Python regression tests passed.
The original 2024 route and shared observation archive remain unchanged. Required
monthly BTS archives are retained for independent validation; temporary routing
workers have stopped.

Final 2023 route SHA-256:
`6420571a5be4ae6baa6f95e505912573c56f403190cc551e034a9a05bfaa6733`.

## Verified 2023 target and rest-day refinement

`runs/lower48-eod-2023/target-rest-2023-v2/` independently validates at
**680 species: 671 primary and 9 provisional**, a gain of 61 over 619.
There are 65 additions and four losses; 54 of the 107 supported targets were
collected, leaving 53. All 365 days and 4,353 selected publisher records passed.
The source model, all observations, exact coordinates, countability policy,
shared 12-hour budget, full flight days, modeled boats and 2.2 km/h walking
remain unchanged.

Rest days fell from 233 to 173; birding time rose from 112 to 160 hours.
The route visits 17 states and uses 36 flight days, eight pelagic days and one
island landing. It drives 15,934 miles and spends 580.6 hours traveling.
Two ordinary birding days use more than eight hours of driving while respecting
the shared twelve-hour activity cap. The complete source model SHA-256 remains
`3dbc66114c095e31a57c7792566734c9de0be2a59c485fef02df4ab68af7d095`.

The search allocated 40 additional minutes across targeted rest insertions,
regional replacements, calendar search, refreshed rest opportunities and final
polish. Finalization was resumed without further search after a pending road
finished and changed one preferred transfer. Native fixed-calendar replays now
handle this race while preserving the strict final totals and independent audit.

The baseline, improved route and 2024 reference are linked from
`runs/target-rest-2023-comparison-v1/pilot-report.html`, served at
`http://127.0.0.1:8775/report`. Species changes, all 107 target outcomes and
remaining rest-date opportunities are exported in the verified run directory.
All 173 final rest dates have support for an uncollected species somewhere in
the model; 97 missing species have such support. These remain opportunities,
rather than feasibility or impossibility proofs. Fifty of the 53 uncollected
targets had pending routing attempts during the finite search.

All 244 Python regressions and 21 viewer tests passed (one legacy viewer fixture
was skipped). Final route SHA-256:
`e0769e37a7155a4f81d382cb0d4eed10245631f5a969e08b35118fd83925d208`.
