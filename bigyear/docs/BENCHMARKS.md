# Measured feasibility benchmarks

The initial feasibility sections use **synthetic fixtures**: their species totals and road costs are software measurements, not historical birding results or evidence of ecological change. The final Tuscaloosa section reports the real March 2025 sample pilot. The requested full-year 2024/2023 acceptance milestone remains pending authorized EBD/SED and reviewed supporting inputs.

## Environment

Measured on an Apple M4 Pro, 14 logical CPUs, 24 GiB RAM, macOS 15.3.1 arm64, Python 3.13.7 and DuckDB 1.5.6. The custom engine uses Apple Clang 17, C++17, `-O3 -pthread`. The optional adapter was compiled and executed against the official OR-Tools C++ 9.15.6755 distribution through CMake. Exact commands, model hashes, engine hashes and metrics are retained in [benchmark-results.json](benchmark-results.json) and the ignored run directories.

## Full-calendar, 500-location stress test

The initial ten-minute run has 139,371 input rows, 500 exact locations, 10,501 location/date opportunities, 400 fictional species and 250,000 directed cached costs. Its binary and source snapshot remain in `runs/scale-500/run-2024/engine-measured` and `engine-source/`. The candidate location target is therefore tested at the lower end of the proposed 500–2,000 range.

| Metric | Measured value |
|---|---:|
| Requested search / workers | 600 seconds / 4 |
| Engine elapsed | 600.000 seconds |
| Search orchestration, export and validation wall time | 605.828 seconds |
| Dataset preparation | 1.041 seconds |
| Candidate preparation | 0.144 seconds |
| Peak C++ process RSS | 21.25 MiB |
| Python process high-water RSS, including preparation | 461.33 MiB |
| Numeric engine input | 18,707,366 bytes |
| Candidate evaluations | 789,938,761 |
| First nonempty feasible route | 0.000611 seconds |
| First fixture ceiling reached | 0.012233 seconds |
| Best feasible species / annual recorded ceiling / candidate ceiling | 400 / 400 / 400 |
| Final travel minutes / fictional road miles | 10165.78 / 9318.63 |
| Independent audit | 366 days, 995 source-evidence rows, valid |

The ordinary output status is “best route found.” No numerical optimization upper bound is exported. Attaining this deliberately easy recorded fixture ceiling says little about real-world route quality; subsequent changes in this run improve the secondary travel objective.

C++ elapsed and first-feasible counters start after loading the numeric problem. Orchestration wall time includes export, process loading, search, result reconstruction and validation; dataset/candidate preparation are separate measurements.

The following are checkpoints **within that same initial run**, rather than separate seeded experiments:

| Elapsed budget | Species | Fictional road miles at latest saved improvement |
|---|---:|---:|
| 1 seconds | 400 | 23931.30 |
| 10 seconds | 400 | 13951.20 |
| 60 seconds | 400 | 11475.10 |
| 600 seconds | 400 | 9318.63 |

A later correction makes full worker resets correspond to the reported restart count. A separate 30-second regression check on the same frozen 500-location problem used the current engine: 400 species, 44,002,343 candidate evaluations, 21.28 MiB C++ peak RSS, and independent full-calendar validation passed. That row uses a different engine hash and is not a controlled extension of the initial convergence timeline.

Road-cache creation and raw fixture generation were not separately timed. No realistic road service, large authorized EBD, airport-network scaling or 2,000-location matrix was benchmarked. Memory and runtime on this small export do not establish national-export performance. Repeat measurements on the actual dataset, including routing preparation, disk use and thread scaling.

## Small exact benchmark

A ten-opportunity problem was solved three ways. Its observations remain fictional; each route was independently audited.

| Method | Species | Travel minutes | Engine seconds | Proven scope |
|---|---:|---:|---:|---|
| C++ exhaustive | 22 | 1260.34 | 0.000687 | Canonical within-day order, retained candidates |
| OR-Tools C++ CP-SAT | 22 | 1260.34 | 0.007368 | Single stop/day, retained candidates |
| C++ heuristic | 22 | 1260.34 | 2.000212 | Best route found |

Both exact methods establish the primary optimum for their own restricted models. They happen to return the same travel cost here. This is not an optimality claim for a national itinerary. Additional tests cover known-optimum local-distance, cumulative activity, fixed-start, missing-road and transit-day counterexamples.

## 2024/2023 demonstration

The current engine ran both full-calendar 16-location examples with the same five-second budget, two workers, free-start rule and travel settings. Each found all 80 fictional species. The main route plus five saved alternative files per year passed independent validation. `runs/demo/comparison.json` reports coverage, destination/region prevalence, arrival ranges, nontransit days, corridors and species-associated travel context. These comparisons exercise the pipeline only; they cannot be interpreted as migration trends or causal detour estimates.

## Remaining trial experiments

| Experiment | Status |
|---|---|
| Small exact-vs-heuristic correctness instance | Measured and independently validated on fixtures |
| Full calendar, 500 retained locations, 10 minutes | Measured and independently validated on fixtures |
| Same actual EBD problem, 1 hour | Pending actual reviewed input data |
| Same actual EBD problem, 6 hours | Pending actual reviewed input data |
| Expanded real candidates | Pending real baseline and road costs |
| Historical 2024 trace and 2023 comparison | Pending authorized EBD/SED, taxonomy and land/road inputs |

Run the staged budgets with `bigyear benchmark`; do not infer the unmeasured one-/six-hour improvement from the fixture results. A fixed seed identifies RNG streams; wall-clock cutoffs and parallel workers do not guarantee identical routes across runs. Repeat seeds/replicates before interpreting route prevalence or convergence.

Recreate a synthetic stress run:

```bash
./launch.sh demo --output runs/new-scale-500 --year 2024 --locations 500 --seconds 600 --threads 4
```

The official installation, data contracts and real-data trial commands are in [README.md](../README.md) and [DATA.md](DATA.md).

## Tuscaloosa real-data pilot, October 16, 2026

The authorized EBD_relMar-2025 sample covers Tuscaloosa County, March 1–31, 2025. User-approved fixed ABA 8.19 decisions retain 4,845 source rows, 121 species, 106 exact reported locations and 244 date/location opportunities. All locations were retained. Road costs and full geometry for all 11,236 directed cells came from the archived January 2025 Alabama OSM extract with OSRM 26.10.0, its upstream car profile and MLD. Road paths are unrestricted by county; road-access anchors are recorded separately from the original pins. Walking and birding share two hours per stop.

The pilot allows 12 activity hours and 30 road miles per birding day, and eight driving hours on driving-only days. A 60-second smoke test and three concurrent 600-second/four-thread runs (seeds 20250301–20250303) each found all 121 species with 273.729 road miles and 614.138 driving minutes. The three full-budget runs returned the same selected itinerary: 28 stops at 22 locations, 15 birding days, three transit days and 13 rest days. Peak activity was 10.984 hours on March 26; full coverage was reached March 30. Each main route and all 20 saved alternatives passed independent calendar/travel/evidence validation.

Attaining the 121-species input ceiling proves that ceiling is achievable under the stated model. The travel minimum is not proven. Single-stop deletion review of the selected result found no feasible deletion preserving all 121 species; it does not test joint route replacements. The pilot report, search comparison, frozen inputs and detailed audit files are under the ignored `runs/tuscaloosa-2025-03/` and `data/tuscaloosa-2025-03/`. These results do not establish national throughput or full-year coverage.

### Pilot timing breakdown

| Stage | Observed time | Scope |
|---|---:|---|
| Import, filtering and taxonomy join | 0.203 seconds | Recorded preparation timer |
| Candidate preparation | 0.034 seconds | All 106 locations retained |
| Alabama road graph preprocessing | Roughly 45 seconds | Approximate session tool-log observation; excludes downloads, not a saved stopwatch benchmark |
| Local road matrix and geometry queries | About 8 seconds | First/last request timestamps in `router.log`, one-second resolution; excludes graph preparation and final cache writes |
| Smoke search, export and validation | 64.690 seconds | Includes 60-second search |
| Seed 1 search, export and validation | 606.341 seconds | Includes 600-second search |
| Seed 2 search, export and validation | 606.409 seconds | Includes 600-second search |
| Seed 3 search, export and validation | 606.253 seconds | Includes 600-second search |

The three long runs were concurrent, using 12 worker threads in total. That stage therefore occupied about 10 minutes 6 seconds of elapsed wall time, with 30 minutes of aggregate search budgets. Peak RSS per search was 2.72 MiB for C++ and 185.7–194.8 MiB for Python (process high-water measurements, not whole-machine memory). First coverage of all 121 species occurred at 1.290, 1.541 and 0.976 seconds respectively. No complete setup-to-report stopwatch was recorded; source downloads, installation, land preparation and final review/report work must not be inferred from the search timers.

For scaling, the directed matrix has N² cells: 250,000 at 500 locations, 1 million at 1,000 and 4 million at 2,000, compared with 11,236 here. These factors describe pair counts, not elapsed-time forecasts. Fetching every pair's geometry should be replaced by fetching selected itinerary shapes at continental scale. Candidate preparation and cached costs can be reused across seeds and effort budgets. The whole continental road graph needs a separate build/memory benchmark. Full-year opportunity counts and competing seasonal windows make search quality substantially harder to predict than this easy county ceiling; use a real regional baseline, expanded candidates and repeated 10-minute/one-hour/six-hour budgets before estimating national convergence.

The generated pilot HTML report includes this breakdown and a proposed synthetic pelagic test matrix. Boat mode is not implemented by those proposals: the inland sample can exercise software contracts using explicitly fabricated trip overlays, but cannot validate real offshore occurrence, access or trip durations.
