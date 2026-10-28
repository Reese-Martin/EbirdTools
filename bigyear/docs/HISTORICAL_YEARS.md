# Historical EOD comparisons

The 2023 benchmark uses actual recorded 2023 dates and exact source coordinates
from the shared, verified US EOD Parquet archive. It applies the fixed ABA 8.16
taxonomic concepts and the approved 2024 provisional tiers retrospectively for
comparison. It does not certify contemporaneous 2023 ABA acceptance or individual
wildness. Annual population envelopes are shared scenario assumptions; exceptional
events retain their real chronology. The Corpus Christi Cattle Tyrant cannot score
before its documented discovery on November 12, 2023.

Prepare the source survey, land mask, complete compact candidates, dated flight
network, charters and Santa Cruz landing choices from the repository root:

```sh
bigyear/.venv/bin/python bigyear/scripts/prepare_historical_year.py all
bigyear/.venv/bin/python bigyear/scripts/prepare_flights.py --year 2023 --output bigyear/data/lower48-eod-2023/flights-2023
bigyear/.venv/bin/python bigyear/scripts/prepare_historical_mobility.py all
bigyear/.venv/bin/python bigyear/scripts/audit_historical_populations.py
```

The population audit is required. Common Waxbill and Red Avadavat have Hawaii
introductions documented in the frozen ABA reference, but the two mainland
supports do not establish an approved population or natural dispersal. They receive
zero credit. Every source observation, exact location, dated candidate ID, and
other species entry is retained. The audited scoring pool has 785 species; the
binary species-label catalog also retains the two held labels with zero support.
The original review inventory and per-state source inventory remain retained-source
provenance; the overlay audit records the two scoring holds and audited totals.

The default historical benchmark reads the population-audited model. Use a fresh
tag for each run. The default search allocates 900 seconds to calendar changes,
600 to regional reconstruction, 600 to rest opportunities and 900 to final polish,
after initialization. It uses the shared twelve-hour activity budget, 2.2 km/h
walking, actual dated nonstop flights and the same modeled boat assumptions as
2024. No observation thinning or coordinate rounding is applied. Marine and island
option construction remains partial.

Keep the monthly BTS ZIPs. Independent validation recomputes selected flight
services and durations from the original records. The compact flight database
alone is insufficient for that audit. Re-running flight preparation restores
missing source archives only if their frozen hashes match. Temporary observation
projections and DuckDB spill files are removed after successful preparation;
audited inputs, bounded road caches, saved routes and evidence remain.

The completed comparison is built with:

```sh
bigyear/.venv/bin/python bigyear/scripts/report_cross_year.py
bigyear/.venv/bin/python -m bigyear_planner.cli serve --runs bigyear/runs/year-comparison-2023-2024 --port 8774
```

The saved 2023 route is `shared-12h-full-2023-v5`; the 2024 reference is
`shared-12h-full-2024-v2`. Source-stage and refinement handoffs are hash-bound in
their plans. Both have approximately fifty minutes of final study allocation,
but the 2024 incumbent benefited from substantial earlier optimization. These
are audited best-found routes, not controlled estimates of year quality or proof
of optimality. Shared annual species support also does not establish that the
specific dated visits fit one feasible calendar.

## Warm target and rest-day refinement

The 2023/2024 comparison identifies 107 species collected on the 2024 reference
route with scored 2023 support but absent from the verified 619-species 2023
calendar. A separate refinement preserves that starting route and its complete
source model:

```sh
bigyear/.venv/bin/python -u bigyear/scripts/benchmark_historical_targets.py
bigyear/.venv/bin/python -u bigyear/scripts/finalize_historical_targets.py
bigyear/.venv/bin/python bigyear/scripts/report_historical_targets.py
bigyear/.venv/bin/python -m bigyear_planner.cli serve --runs bigyear/runs/target-rest-2023-comparison-v1 --port 8775
```

The default study allocates five minutes to targeted rest-date insertions,
ten to targeted regional replacements, ten to unrestricted calendar search,
five to additions on the refreshed actual resting calendar, and ten to joint
calendar polish. Indexing and final independent validation add wall time.
Every target gets a short first routing probe before longer regional retries.
Unknown costs stay pending. Target lists and proposal shortlists guide finite
search only; they do not filter the full observation model or award extra points.
Rest receives no artificial penalty, and prior visits are not mandatory.

Checkpoints, routing-cache metrics, actual target attempts, source support,
remaining rest-date opportunities and independent validation are retained in the
new run. The report uses separate links to the baseline, refined 2023 route and
2024 reference. Re-running requires a fresh tag and a fresh comparison output;
the original verified calendars are preserved.

The first target-study search completed before finalization detected a direct
road finishing after the native replay. That road replaced a flight transfer
with a faster measured option. `finalize_historical_targets.py` resumes the same
saved visits in a separate output and performs no additional timed search.
Finalization now compares a cheap calendar replay with the native totals and,
when necessary, re-evaluates the identical visits with the native evaluator.
It retains the original score, bounds retries, rejects changed species credit,
and preserves the strict final cost check and independent source/travel audit.
The final study report reads the recovered `target-rest-2023-v2` output.
