# Architecture and audit boundaries

```
authorized local EBD + optional SED
   + reviewed taxonomy CSV + exact land catalog
      -> DuckDB disk staging / exclusions / event deduplication
      -> yearly observations.parquet + original evidence.parquet + manifests
      -> candidate reduction + all retained location/dates -> problem.json
      + directed road SQLite cache + optional airport connections
      -> coarse problem.by -> C++ search / optional reduced CP-SAT
      -> checkpoints + winning/alternative opportunity IDs
      -> Python full calendar / itinerary / species-credit evidence
      -> independent Python validator against frozen evidence and road cache
      -> saved route.json -> read-only localhost SVG map
```

## Coarse search boundary

The C++ process receives all data before searching. There are no Python callbacks per move. Opportunity records use integer day/location/species IDs and species bitsets; road distances/times use flat directed arrays. Parallel workers maintain current routes and coverage counts for incremental novelty scoring, propose rarity-weighted insertions, geographically clustered insertions, stop deletion/replacement, alternate reported dates at the same location, and date-window replacement. Full compact feasibility evaluation checks each proposed itinerary. A synchronized archive saves improvements and near/equal alternatives.

The initial heuristics are deliberately simple. They provide a feasibility/performance baseline, not state-of-the-art route quality. No claim is made that local search explores all combinations, all same-day stop orders, or all flight networks. The C++ exhaustive checker uses the exact same canonical-order model; the independent Python validator separately recalculates constraints and evidence.

The text `BY1` protocol contains a header with full-calendar length, location/species/opportunity counts, fixed/free start and budgets; opportunity rows; directed road rows; and cached transit-plan rows. IDs are zero-based. `problem.json` holds the human-readable coordinates, names, policy and provenance; the C++ input has only compact search values. Resume requires a byte-identical coarse problem hash. Elapsed improvement timestamps restart at zero in each resumed run; do not concatenate them as one timeline without adjusting offsets.

## Travel semantics

Each birding date is an ordered group of location/date opportunities. A move from the preceding position to the first stop can occur within that birding day's local budget, or on preceding whole transit days. Travel among the day's stops is always local, summed across the entire day. Returning to a hotel is not required. No hidden evening travel is available.

Long road edges occupy `ceil(total_minutes / drive_minutes)` nonbirding days. Each receives an equal fraction of aggregate network minutes/miles. Saved road fractions preserve continuity without inventing observed intermediate birding stops. Their displayed position is approximate because per-segment historical speeds are not reconstructed.

For flights, access and egress use separate full road transit days and the directed airport connection consumes one entire flight day. These conservative plans can require more days than driving, so the engine retains different feasible plans and chooses the least travel time that fits the available dates. Birding cannot occur on any of those transit days. No airline timetable, connection frequency or vehicle logistics is inferred.

Python streams road entries from SQLite with a bounded 8,192-pair hot cache; C++ stores the numeric matrix. Geometry is optional. Sparse missing pairs prohibit that connection; they do not imply impossibility of a real trip outside the supplied network model. With airports enabled, preparing pairwise flight plans can be expensive: its work depends on the number of airport links and retained location pairs.

## Independent validation

The validator does not call `Problem::evaluate`, the engine binary, or the itinerary materializer. It reconstructs the calendar position, complete local chains, partial long-road continuity, mode limits and cumulative species from the exported daily route. It joins the selected date/location keys to frozen original evidence, then checks approval, resolved taxonomy category, count/X presence, lower-48/DC geography, land classification, ABA mapping and every claimed original checklist/event/record/taxon tuple.

The imported evidence/catalog hashes are checked, as are the frozen problem and road-cache hashes. This detects accidental or intentional changes after export. It does not certify that an operator's taxonomy/land/road inputs were scientifically correct. Original EBD/SED input hashes remain in the manifest; repeated EBD validation does not reread and hash the entire raw bulk export. The preserved approved EBD evidence is that source's audit boundary.

Grouped EOD uses a separate path. `eod_archive.py` converts bounded TSV chunks to
Zstandard-3 Parquet and verifies every row/value before publishing a complete
archive manifest. `eod.py` binds reviewed source-specific taxonomy and exact-point
land catalogs, then exposes evidence/opportunity views over the compressed
archive. No annual bulk copy is made. Preparation, materialization and validation
all use these views; archive-part checksums and the frozen adapter version are
checked before use. Validation independently checks the EOD policy, exact land
keys, source identifiers and route rules. Missing EBD checklist/approval/category
metadata remains missing, with explicit limitations attached to saved routes.

## Experimental limits

The annual recorded ceiling concerns every eligible species in the supplied yearly dataset. The retained-candidate ceiling concerns only selected opportunities. Neither is a guarantee that those species can be combined in a feasible route. An exhaustive or CP-SAT optimum applies only to its explicitly restricted model. Normal production output remains “best route found.” Candidate expansion is a sensitivity analysis, not proof that reduction did not matter.

Comparison alternatives are the archive left by a particular search. Their destination prevalence can reveal flexibility but is not an unbiased estimate over all equally good routes. Coverage, taxonomy, EBD release, rule choices, hardware/thread effort and search convergence belong beside every cross-year interpretation.
