# Rest dates as optimization opportunities

`bigyear_planner.rest_exploration.RestExplorer` indexes every actual rest date in
an independently saved reference calendar against every source-dated opportunity
that contains a species absent from the current annual list. There is no
geographic distance cutoff. Both land observations and existing boat variants
are candidates. All source observations, dates and exact coordinates remain in
the complete memory-mapped model.

Rest is not penalized in the objective. The objective remains unique countable
species first, with time and road miles breaking ties. A rest date becomes a
proposal when a missing species was reported somewhere on that date; the full
calendar evaluator must still find measured driving, dated flight service,
airport access, walking and any boat/landing access that fit around the other
visits. A remote record is an opportunity to test, not permission to teleport or
ignore a travel day.

The queue is balanced across species. A species with thousands of source pins
cannot consume all attempts before another species gets a turn. Within each
species, distinct dates get priority over alternate pins on the same date.
Geometric trip burden orders proposals but never substitutes for road costs.
Annual gain is recalculated after accepted visits; collecting the same species
again adds no score. Pending routing queries stay unresolved and are retried
within the search budget; a timeout is not classified as impossible travel.

The current benchmark tests:

1. Central Texas: Golden-cheeked Warbler and Black-capped Vireo, with Texas
   source locations guiding proposals.
2. Midwest: Eurasian Tree Sparrow, Henslow's Sparrow and Kirtland's Warbler,
   with source locations in MO, IL, IA, IN, WI, MI, MN and OH guiding proposals.
3. Their combination, evaluated as one full calendar rather than adding scores.
4. Missing species on all rest dates nationwide, without a state or distance
   filter on proposals.

The first two trials start independently from the same verified incumbent.
Their measured routing cache is shared and warms over time, so their runtimes
are not a controlled comparison of cold routing performance. Regional
reconstruction is allowed to change selected visits, while keeping every source
observation in the model. The best final calendar receives separate source,
publisher-record, road endpoint, walking, flight, boat and full-year validation.
The final count is reported only after that audit passes.

Example from `bigyear/`:

```sh
.venv/bin/python -u scripts/benchmark_rest_opportunities.py \
  --incumbent runs/lower48-eod-2024/rest-study-baseline-696-2024-v1 \
  --tag rest-opportunities-2024-v1 \
  --regional-seconds 300 --rest-seconds 600
```

Every run requires a new tag. The source countability policy, sensitive-species
and exact access holds, all observations, exact coordinates, 2.2 km/h walking,
12-hour activity budget and existing drive/flight/boat rules stay unchanged.
Search is bounded; the full inventory remains separate from the finite
per-species proposal shortlist. Reports distinguish indexed, attempted, pending
and untested opportunities. No claim of exhaustive optimization is made.

The reference rest calendar is frozen for a trial. As accepted trips consume
rest dates, the evaluator rechecks the entire calendar. Repeating the workflow
with the newly verified incumbent rebuilds the inventory from that route's
actual rest dates and newly missing annual species. This also picks up dates
freed by regional replacements. Inventory generation does not require new EBD
data or a new observation model.

Saved artifacts include per-phase `rest-opportunity-inventory.json`,
`rest-attempts.jsonl`, exact model-ID checkpoints, `phase-result.json`, the combined
`search-result.json`, and the final independent `validation.json`. Temporary
native bridge inputs and managed DuckDB spill are removed; source data and
bounded road caches are retained. Routing workers are stopped on completion.
