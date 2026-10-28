# Big Year route lab

A standalone, local historical route planner. Python prepares data and manages experiments; DuckDB filters local EBD exports into yearly Parquet; a compiled C++17 engine searches a frozen opportunity model. A separate browser map reads saved results. Nothing imports or changes the existing Streamlit viewer.

**Continuing on another computer:** use the [machine handoff guide](docs/MACHINE_HANDOFF.md)
for installation, the private EBD input bundle, and the next import command.

The [rest-opportunity workflow](docs/REST_OPPORTUNITIES.md) treats source-dated
records of missing species on any resting date as full-calendar proposals,
including distant regions. It tests central Texas and Midwest trips, then a
national species-balanced queue, while retaining the full observation model.

The [current scenario](docs/CURRENT_SCENARIO.md) allows ordinary driving and
birding within one twelve-hour activity budget, including up to twelve driving
hours on a driving-only date.
It documents the twelve provisional credits, calendar rearrangement and the
geographic difference between our lower-48/DC cohort and the full ABA Area.
Older saved routes retain their original daily rules.

**Historical results require an authorized EBD/SED download or a verified grouped EOD Parquet archive, a reviewed ABA crosswalk, a terrestrial location classification, and road network costs.** EOD needs explicit source-specific eligibility decisions because it omits EBD metadata. The runnable demonstration uses fictional species, observations, checklists, land decisions, and road costs. Its 2024/2023 outputs are software fixtures, not historical or ecological findings.

## Install and try it

Requirements: macOS or Linux, Python 3.11+, and a C++17 compiler (`xcode-select --install` on macOS; your distribution's C++ toolchain on Linux). Python and C++ dependencies are independent of the parent project. The standard engine needs no CMake or OR-Tools installation.

```bash
cd /path/to/EbirdTools/bigyear
chmod +x launch.sh
./launch.sh build
./launch.sh demo --output runs/demo --seconds 5 --threads 2
./launch.sh validate runs/demo/run-2024
./launch.sh serve --runs runs/demo --port 8765
```

Open [the local map](http://127.0.0.1:8765). It includes an offline lower-48/DC basemap, route, date slider, playback, linked cumulative curve, daily activity/travel, stop selection, checklist evidence, assumptions and scoped solution status. The palette uses eBird logo green (`#36824b`, from [Cornell's official logo SVG](https://clo-brand-static-prod.s3.amazonaws.com/logos/ebird/clo_ebird_solo_web.svg)), white surfaces and black text. Solid roads have supplied geometry; dashed roads are schematic. Transit roads use black; flights use arched schematic paths. Synthetic checklists never link to real eBird pages.

Playback traces each road leg with a moving marker and reveals stops on arrival, adding species after each illustrated visit. Pause/resume, replay from the beginning and quarter-speed through four-speed controls are available. Selecting a date pauses at its completed-day totals; Play replays that day. Travel animation is paced by saved leg duration, with compressed stop/rest pauses. No observation clock times, exact driving speeds or walking paths are reconstructed. Only the active day is updated on animation frames; completed routes remain static. `node --test tests/playback.test.mjs` checks multi-stop ordering, partial transit, duplicate geometry, shared road anchors and schematic flights. Set `BIGYEAR_PILOT_ROUTE` to a saved pilot `route.json` to check every daily total too.

Equivalent explicit installation:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/bigyear build
.venv/bin/python -m pytest tests -q
```

`launch.sh` installs the environment on first use. Later invocations reuse it; after pulling dependency changes, rerun `pip install -e '.[dev]'`. The bundled basemap requires no remote tile service. The server binds only to `127.0.0.1` and has no optimization or write API.

## Import authorized data

For the pending **2024 lower-48 and DC EBD download**, use the new
[national EBD pipeline](docs/EBD_2024.md). It streams tar/ZIP/gzip/TSV into verified
Zstandard-3 Parquet, retains every supplied observation and coordinate string,
and binds the current primary/provisional countability policy. The national
configuration preserves 2.2 km/h walking and the current daily budgets. This
path supports complete memory-mapped candidates rather than location sampling.

Read [the input formats and taxonomy policy](docs/DATA.md) before preparing real data. Download both years in one EBD release, preferably with SED, and use exactly the same crosswalk in both years. Keep bulk files in `bigyear/data/` or outside the repository. `data/`, `runs/`, compiled binaries, environments and Parquet are ignored by this subproject's own `.gitignore`.

```bash
mkdir -p data runs
cp examples/config.json data/config-2024.json
# Supply reviewed data/taxonomy.csv and data/land.csv; see docs/DATA.md.
./launch.sh import --ebd /downloads/ebd_US_relRELEASE.txt \
  --sed /downloads/ebd_US_relRELEASE_sampling.txt \
  --taxonomy data/taxonomy.csv --land data/land.csv \
  --release RELEASE --taxonomy-policy reviewed-crosswalk-v1 \
  --config data/config-2024.json --output data/year-2024
./launch.sh prepare --dataset data/year-2024 --config data/config-2024.json \
  --output data/problem-2024.json
```

The prepare command also writes `problem-2024.locations.csv`. Generate road costs using your authorized/self-hosted OSRM service, or import an externally prepared directed matrix:

```bash
./launch.sh roads-osrm --problem data/problem-2024.json \
  --endpoint http://localhost:5000 --network-release OSM-snapshot-and-profile-label \
  --cache data/roads.sqlite --batch 40
# --geometries also fetches road shapes for every pair; this can be expensive.
# Alternative: ./launch.sh roads-import --problem data/problem-2024.json \
#   --csv data/roads.csv --cache data/roads.sqlite
./launch.sh search --problem data/problem-2024.json --roads data/roads.sqlite \
  --output runs/2024-baseline --seconds 600 --threads 4
./launch.sh validate runs/2024-baseline
./launch.sh serve --runs runs
```

`roads-osrm` is resumable. It never estimates missing network costs. Large national matrices belong on a service you control, not an unrequested public demo endpoint. Coordinates are sent to the endpoint explicitly supplied by the operator. Routing uses that network/profile snapshot, including its snapping assumptions, not reconstructed 2024 traffic or closures. Geometry-free matrices remain road costs, but their displayed connections are labeled schematic.

Run search in its own terminal, or use a normal shell background job and redirect logs. The map reads saved JSON and never starts a search. Every output directory must be new; the tools refuse to overwrite an existing import or run.

## Request a smaller eBird Observation Dataset (EOD) download

[GBIF SQL downloads](https://techdocs.gbif.org/en/data-use/api-sql-downloads) can filter
records on the server before you download them. With Python 3.10+ and curl, run
these commands from `bigyear/`; no package installation is needed:

```bash
# Preview SQL, or validate it with GBIF without starting a download job.
python3 scripts/download_eod.py
python3 scripts/download_eod.py --validate

# Request the filtered ZIP; curl prompts for your GBIF password in the terminal.
python3 scripts/download_eod.py --submit --username YOUR_GBIF_USERNAME --email you@example.org

# Optional: exclude GBIF geospatial flags before grouping, retaining the full cohort.
python3 scripts/download_eod.py --mappable --submit --username YOUR_GBIF_USERNAME

# Source-record totals by year, before grouping (not the resulting group counts).
python3 scripts/download_eod.py --count --submit --username YOUR_GBIF_USERNAME

# Use the key printed after submission to check status and get the final ZIP link.
python3 scripts/download_eod.py --status YOUR_DOWNLOAD_KEY
```

The defaults select only [Cornell's EOD](https://www.gbif.org/dataset/4fa7b334-ce0d-4e88-aaae-2e0c138d049e),
`countrycode = 'US'`, birds (`class = 'Aves'`), `HUMAN_OBSERVATION`, and observation
year **2000 onward, inclusive**. `countrycode` is the occurrence location, not
the publisher's country. Alaska and Hawaii are included; territories with separate
country codes are outside this filter. Repeat `--state` for multiple exact GBIF
state names (for example `--state Alabama --state Florida`); use names, not `US-AL`.
There is no default upper year bound, so future EOD updates remain eligible.

The Python `--submit` command builds and submits the grouped request directly;
no manual JSON is needed. The [JSON request](examples/eod-us-since-2000.json) is
also available for an API request tool.
The plain [SQL file](examples/eod-us-since-2000.sql) can also be pasted into
[GBIF's SQL download editor](https://www.gbif.org/occurrence/download/sql?sql=SELECT%20gbifid%20FROM%20occurrence%20WHERE%20countrycode%20%3D%20%27US%27).
`--json` prints the API request instead. The optional `GBIF_USER` and `GBIF_EMAIL`
environment variables supply username/email defaults. The script validates before
submission, prints a job key and download page, and leaves the large ZIP on GBIF
until you choose to fetch it. An email is sent only when `--email` is supplied.
After a submission timeout, check your GBIF download history before trying again.

The default query groups by **calendar day, exact latitude/longitude, state, and
reported taxon name/concept ID**, returning 14 columns instead of the previous 29.
Subspecies and forms remain separate source taxa for the eligibility crosswalk.
State remains part of the key so conflicting state reports are not silently
combined. There is no coordinate rounding or species-rank restriction. Numeric
bird counts, redundant interpreted taxonomy, locality strings, checklist fields,
and repeated dataset/license columns are omitted; retain the request and download
metadata/DOI as dataset provenance.

Each group retains `representative_gbifid` (the lexicographic minimum source ID)
and `source_record_count`. The ID links to one actual supporting occurrence at
`https://www.gbif.org/occurrence/GBIF_ID`; it is not necessarily the best-quality
record. Record counts are not bird abundance or independent checklist counts.
The full supporting-ID list is omitted to save space.

Groups require valid coordinates, a known month/day, and `PRESENT` status. Missing
numeric counts still qualify as presence. Spatially flagged records are retained
by default, with counts of flagged and unknown spatial status per group. The
maximum **reported** coordinate uncertainty and the number of records reporting
uncertainty are also retained; null uncertainty is unknown, never zero. These
summary fields describe the group, not necessarily its representative occurrence.

Optional `--mappable` adds `hasgeospatialissues = FALSE` **before** aggregation;
this excludes both flagged and unknown spatial status. A numeric uncertainty
threshold is not applied. Such a threshold would need an explicit policy for
missing uncertainty and should also be applied before aggregation; separate
group-level summaries cannot establish that one record meets joint criteria.
GBIF spatial flags are not a species-identification confidence score, eBird
approval, land classification, or access verification. `--raw` restores the
original 29-column ungrouped export and its original filters (`--mappable` still
works). Geographic/year options remain available, but no narrower scope is set
by default.

The ungrouped US cohort since 2000 exceeds a billion records. The reduction from
grouping depends on how often reports share exact coordinates, dates and taxa;
no size or reduction factor is guaranteed. At the October 16, 2026 check, the
EOD metadata listed coverage through December 31, 2024. The resulting
ZIP contains a SQL TSV export, **not an EBD file**. The existing importer still
requires EBD headers. For the supported EOD adapter, convert and bind the archive
as described below; its source-specific policy handles missing metadata explicitly.

## Read compressed EOD directly

The full grouped extract can be converted into a directory of Zstandard level 3
Parquet parts. All 14 columns and observations are retained. Coordinate strings
and identifiers remain text, without rounding or floating-point conversion.
Every part is read back and compared against every original source value before
it is finalized. `manifest.json` is published only after all records verify;
conversion never deletes or edits the original TSV.

```bash
# From bigyear/. This output directory must be new.
./launch.sh convert-eod --source /path/to/0031401-260928105237408.csv \
  --output data/eod-us-since-2000-zstd3 --expected-rows 743291019
# After an interrupted conversion, repeat that command with --resume.
./launch.sh inspect-eod --archive data/eod-us-since-2000-zstd3
# Optional: independently recheck the retained original's full checksum too.
./launch.sh inspect-eod --archive data/eod-us-since-2000-zstd3 \
  --source /path/to/0031401-260928105237408.csv

# Supply a detailed terrestrial polygon mask and reviewed EOD taxonomy policy.
./launch.sh classify-land-eod --archive data/eod-us-since-2000-zstd3 --year 2024 \
  --geojson data/terrestrial-polygons.geojson --output data/eod-land-2024.csv
./launch.sh import-eod --archive data/eod-us-since-2000-zstd3 \
  --taxonomy data/eod-taxonomy.csv --land data/eod-land-2024.csv \
  --config data/config-2024.json --release GBIF-0031401-260928105237408 \
  --taxonomy-policy reviewed-eod-presence-v1 --output data/eod-year-2024
./launch.sh prepare --dataset data/eod-year-2024 --config data/config-2024.json \
  --output data/eod-problem-2024.json
```

`import-eod` creates a small, hashed dataset binding with the reviewed catalogs
and a review summary. It does **not** expand the archive or duplicate annual
evidence/observation Parquet. Preparation, route materialization, and independent
validation query the compressed source directly. Archive parts and catalog hashes
are checked before use. The existing road-cost/search/serve commands then apply.
Saved EOD evidence links to representative GBIF occurrences; missing checklist,
approval, category and exotic fields stay missing. EOD and EBD runs cannot be
silently mixed in comparisons.

Read [the EOD contract](docs/DATA.md#grouped-eod-parquet) before creating the
taxonomy and land catalogs. The archive alone does not establish species
eligibility, terrestrial access, or an audited historical route.

### Tuscaloosa 2024 EOD evaluation

The county-year evaluation uses the January 2024 Census polygon for GEOID `01125`,
all exact points within it, a frozen ABA 8.19 presence-only policy, the expanded
OSM terrestrial mask and the existing January 2025 Alabama road graph as a snapshot
proxy. `import-eod` and `classify-land-eod` accept `--scope-geojson`; import also
accepts `--scope-label`. County queries cache their small filtered cohort in bounded
DuckDB temporary storage so planner aggregates do not repeatedly scan the national
archive.

The source-specific preparation and evaluation stages are recorded in
[`scripts/evaluate_eod_county.py`](scripts/evaluate_eod_county.py). Its paths are
specific to this retained pilot; existing dataset/run outputs are not overwritten.
Inputs, source checks, coverage tables, review decisions and measured pipeline
resources are under `data/tuscaloosa-eod-2024/`. Saved runs, independent validation,
the machine-readable evaluation and the report are under `runs/tuscaloosa-eod-2024/`.
To view retained results locally:

```bash
./launch.sh serve --runs runs/tuscaloosa-eod-2024 --port 8766
# Open http://127.0.0.1:8766/report for the evaluation, or / for the route viewer.
```

This is a technical test of deterministic presence-based routing. EOD does not
recover individual eBird approval or captive/exotic flags, and a county test does
not establish national routing performance or an ABA-certified historical result.

If road costs were prepared with OSRM's table service, fetch shapes for only the
saved driving legs with `viewer-roads --runs RUNS --endpoint LOCAL_OSRM
--network-release RELEASE`. The command checks the frozen road snapshots and
requires the route service's distances/times to agree with their cached costs to
0.11 metre/second. A separate `road-display-geometries.json` binds each shape to
the original route hash. The viewer uses these shapes without rewriting route,
cost, coordinate or validation files, and labels runs with no flight legs
"Driving only". The flight legend appears only for runs with actual flight legs.

## Rules and search scope

Experiments can set `start_date` and `end_date` (ISO dates within `year`); omitted bounds use January 1 and December 31. The engine, saved calendar and independent validator use the same inclusive window. For example, a March pilot can use `year=2025`, `start_date="2025-03-01"`, `end_date="2025-03-31"` and `activity_minutes=720`. The default activity limit remains eight hours; the pilot's 12-hour allowance is explicit in its frozen configuration.

`prepare --access-catalog FILE` can attach road-access anchors without changing observation coordinates or date/species evidence. The CSV requires `locality_id,latitude,longitude,road_latitude,road_longitude,offset_meters,source,walking_policy`. Every selected birding location needs an exact-point entry. Road caches bind both reported coordinates and road anchors; two distinct pins may share an access point with zero driving between them, while each visit still consumes its own birding allowance. A walking-proxy assumption is not an inferred footpath.

Defaults are calendar 2024; contiguous 48 states plus DC; land locations only; a free starting location on January 1; an unrestricted finish; at most 30 road miles and eight activity hours per birding day; two hours of birding per visited location/date; at most eight driving hours per road transit day; and zero species on transit days. Availability is deterministic throughout an explicitly reported day at the exact location. There is no interpolation between dates. Counts of `X` qualify as presence; individual counts do not affect the objective.

Travel is minimized after distinct species: first total travel minutes, then road miles. Birding time does not count as travel. Overnight repositioning is not allowed. The traveler retains their position on rest days. A long road route is split into equal-time daily portions, with road geometry position interpolated by path length for display. Flight networks are optional, directed and simplified: airport access road days, one entire flight day, then egress road days. They are not historical airline schedules.

The current heuristic orders same-day stops by stable opportunity ID. It supports multiple stops and enforces the aggregate budget, but does not search every within-day permutation. Its output is **best route found**. Candidate reduction also restricts the experiment. These limitations are visible on the map and in saved assumptions.

Candidate selection retains all reported dates at selected exact locations. It protects all locations supporting species with at most five opportunities or an observed annual window of at most seven days, adds geographic grid representatives, and ranks remaining locations by inverse opportunity frequency. The default 1,000-location target is soft: protection can exceed it. Grid locations are potential connectors, not a proof that every useful intermediate stop survives. No geographically separate checklists are merged.

## Experiment workflow

After a run validates, `./launch.sh review-detours RUN_DIRECTORY` writes a JSON/CSV review of removing each selected stop individually. It keeps every other stop and date, recomputes connecting travel, and independently validates the resulting calendar. It reports lost species, first credits observed again later, feasibility and travel savings. Counterfactual savings cannot be summed or all deletions applied together. This review does not replace full route optimization.

The Tuscaloosa March 2025 pilot uses `scripts/tuscaloosa_pilot.py` to prepare the clipped OSM land mask, approved import and separate road-access catalog. Optional GIS preparation uses `pip install -e '.[gis]'`. Sources and frozen inputs are in the ignored `data/tuscaloosa-2025-03/`; saved experiments, comparison and the HTML report are in `runs/tuscaloosa-2025-03/`. The county outline is U.S. Census display context only. `scripts/summarize_tuscaloosa.py` requires all three ten-minute runs to have completed independent validation before producing the final summary.

```bash
# Same frozen problem, controlled effort; defaults are 10 minutes, 1 hour, 6 hours.
./launch.sh benchmark --problem data/problem-2024.json --roads data/roads.sqlite \
  --output runs/budgets --budgets 600 3600 21600 --threads 4

# Resume the exact same frozen model into a new directory.
./launch.sh search --problem data/problem-2024.json --roads data/roads.sqlite \
  --resume runs/2024-baseline/checkpoint.json --output runs/2024-resumed --seconds 3600

# Expand around a promising route; regenerate roads for new locations before search.
./launch.sh prepare --dataset data/year-2024 --config data/config-2024.json \
  --expand-route runs/2024-baseline/route.json --expand-miles 50 \
  --output data/problem-2024-expanded.json
# --keep LOCATION_ID may be repeated to force selected locations into a new problem.
```

For a fixed-start sensitivity analysis, set `start_location` to an ID in the exported location CSV and prepare a new problem. It must be an observed land location in that yearly dataset. The fixed start is present on January 1; January 1 may be a transit day. Apply the same fixed place and travel rules in every compared year.

Once a real 2024 run validates, import 2023 from the same EBD release and crosswalk, changing only `year` and any explicitly controlled seed. Use comparable search duration/threads and retain the alternative routes:

```bash
./launch.sh compare runs/2024-baseline runs/2023-baseline --output runs/comparison.json
```

Comparison refuses mismatched rules, starts, EBD releases, taxonomy policy/hash or synthetic status. It reports alternative-route region/corridor prevalence, arrival ranges, stop visits, species/travel context, coverage and search convergence. Saved alternatives are a biased search sample, not a probability distribution. Stop travel associated with a species is descriptive; proving that a species forces a detour needs a separate ablation analysis. Increased reporting and convergence differences can resemble ecological changes.

## Exact checks and OR-Tools C++

The standard executable includes exhaustive enumeration for at most 24 opportunities. The limit counts location/date opportunities, not locations:

```bash
./launch.sh prepare --dataset data/year-2024 --config data/config-2024.json \
  --state US-AZ --from-date 2024-01-01 --to-date 2024-01-07 --output data/tiny.json
# Reduce the region/date scope further if this still exceeds 24 opportunities.
./launch.sh search --problem data/tiny.json --roads data/roads.sqlite \
  --output runs/tiny-exact --exact exhaustive --seconds 60
```

This proves the primary and secondary objectives only if enumeration finishes, and only within its retained candidates and canonical same-day ordering. A timed-out run remains best route found.

An optional C++ CP-SAT adapter implements a reduced **single stop per day** path model, including fixed/free starts, road/airport transitions and distinct-species scoring. It is limited to 250 opportunities. Install an official [OR-Tools C++ distribution](https://developers.google.com/optimization/install/cpp), plus CMake, then:

```bash
./launch.sh build --ortools-prefix /path/to/or-tools-distribution
./launch.sh search --problem data/reduced.json --roads data/roads.sqlite \
  --output runs/cpsat --exact ortools --engine build/ortools/bigyear-engine --seconds 60
```

CP-SAT's proven status concerns the primary species objective in that reduced model. The second solve minimizes travel rounded to seconds. A restricted-model optimum is not a bound on all possible real-world itineraries. The annual recorded ceiling and retained-candidate ceiling are separately labeled; no numerical optimization upper bound is currently exported. See [the measured benchmark report](docs/BENCHMARKS.md).

## Saved artifacts

Each search saves `problem.json`, an immutable `roads.sqlite` backup, the coarse `problem.by` engine input, the executed `engine-snapshot` binary, `engine.json`, atomic `checkpoint.json`, append-only `improvements.jsonl`, full-calendar `route.json`, alternative route JSON files, `itinerary.csv`, `species-audit.csv`, `validation.json`, and `benchmark.json`. Imports record input/output SHA-256 hashes, release and policy labels, exclusions, review counts, coverage, elapsed preparation time and hardware.

The validator never calls the C++ feasibility evaluator. It independently checks calendar continuity, road fractions and daily limits, transit exclusions, every location/date species credit, original checklist identifiers, approval, exact taxonomy mapping, geography and land classifications against frozen Parquet evidence and catalogs. It detects changed dataset files and road/problem hashes. It validates the stated model, not guaranteed access, real-life detectability, or the historical road network.

The national historical milestone still requires an EBD or EOD source paired with reviewed national taxonomy, land/access and road inputs. A verified archive or synthetic demonstration alone cannot establish the requested historical 2024 trace or 2023 ecological comparison.

See [acceptance status](docs/ACCEPTANCE.md) for the implemented and pending deliverables, and [architecture](docs/ARCHITECTURE.md) for the model and audit boundaries.

### Statewide matrices and the Alabama 2024 evaluation

For larger driving-only problems, `roads-matrix` stores exact OSRM miles and minutes as float64 pairs in a read-only binary file with a checksummed JSON manifest. Observation coordinates and evidence IDs are retained separately. Identical road-access anchors share a matrix index; this does not combine observation locations or visits. Unreachable pairs remain unavailable. The native engine maps the file directly and derives driving-transit days from the same eight-hour budget, avoiding a second quadratic table of transit plans. Completed run snapshots share the immutable binary through hard links; keep each manifest beside its named binary.

```bash
# The authorized local OSRM server must permit up to 2*batch input coordinates.
# For batch=3000, start osrm-routed with --max-table-size 6000 (or higher).
./launch.sh roads-matrix --problem data/problem-2024.json \
  --endpoint http://127.0.0.1:5002 --network-release OSM-snapshot-and-profile-label \
  --cache data/roads.matrix.json --batch 3000
./launch.sh search --problem data/problem-2024.json --roads data/roads.matrix.json \
  --output runs/state-2024 --seconds 600 --threads 2
./launch.sh validate runs/state-2024
```

The resumable builder holds two bounded HTTP blocks and writes the matrix directly to disk. NumPy accelerates numeric conversion; it does not round coordinates or reduce precision. Search requires `flights=false` with this compact native format; the existing SQLite/BY1 workflow still supports flights. Each run records its actual road manifest filename in provenance so validation, detour review, comparison and display geometry use the appropriate snapshot.

`scripts/evaluate_eod_alabama.py` reproduces the 2024 Alabama test in stages, reusing the verified national archive, fixed ABA 8.19 policy, 2024 Census boundary, coastline source and January 2025 Alabama road graph. `audit`, `land`, `source_check`, `taxonomy`, `inputs`, `access`, `roads`, `searches`, `monthly_probe`, `retention_audit`, `common_county`, `county_replay`, `count_bound` and `matched_county` retain their outputs under `data/alabama-eod-2024` and `runs/alabama-eod-2024`. The report compares three 600-second, two-thread statewide seeds against both the original Tuscaloosa baseline and county reruns using the same compact executable. Different geographic cohorts are compared explicitly in this evaluation, not passed off as a controlled year-to-year ecological comparison.

The independent `count_bound` stage verifies both singleton source groups for Sooty Tern and Pomarine Jaeger on September 13. Their anchors are over 287 miles apart even before following roads, so the 30-mile birding-day rule prevents crediting both. Its certificate binds the exact problem and road-manifest hashes; the viewer displays it only for matching frozen runs. It bounds the primary species count, not driving optimality.

The unchanged Alabama car profile includes the Fort Morgan–Dauphin Island vehicle ferry. Display geometry preparation now also checks OSRM step modes and labels these crossings explicitly. Ferry distance and modeled crossing time remain part of the frozen car-route budgets; historical ferry schedules, queues and boarding availability are not reconstructed. The statewide comparison therefore covers car-profile travel including vehicle ferries, with flights disabled.

### Complete national search and dated flights

The BY4 national candidate catalog retains every eligible observation's exact
location/date/species support. It maps fixed records and source coordinates into
the native search and keeps location metadata in a bounded SQLite page cache.
Observation coordinates remain separate from car-access anchors. Every visit
adds the full round-trip access walk above 100 m at 2.2 km/h.

`SearchRoads` adds a bounded discovery queue with two routing workers, temporary
cooldowns for unresolved queries, and a 32 MiB geometry cache. Pending costs are
never substituted with straight-line travel or cached as unreachable. Native
pending-query backoff avoids repeatedly querying the same unfinished request.
Deferred proposals have a bounded retry queue. Final selected geometry gets a
separate routing server with a 120-second allowance; both discovery and audit
servers shut down after the run. Verified previous endpoint proofs can be reused
only with matching road-network identity and independently checked costs/anchors.

The search can restore a complete verified checkpoint without rebuilding its
stops one at a time. It screens impossible local travel with a conservative
distance bound that permits walking to different anchors at both endpoints.
One worker improves the incumbent while other workers get timed independent
starts across eight latitude/longitude regions. All regions and all observations
remain available to global proposals; these are proposal indexes, not cohort
reductions. Native counters distinguish proposals, skipped unchanged proposals,
screened proposals, completed evaluations, pending retries and regional starts.
Completed evaluations can revisit the same candidate; they are not a count of
unique nationwide itineraries.

For example, from this folder:

```sh
.venv/bin/python scripts/evaluate_lower48.py search --seconds 600 \
  --folder my-driving-benchmark --discovery --road-seconds 8 \
  --resume runs/lower48-eod-2024/national-3600s/checkpoint.json
```

`scripts/prepare_flights.py` streams twelve public 2024 BTS Reporting Carrier
On-Time Performance ZIP files. It retains their hashes, the ZIPs, a frozen
end-of-2024 OurAirports snapshot, and a narrow SQLite network of operated nonstop
route dates. Monthly CSV files are never expanded onto disk. Cancelled and
diverted flights are excluded. This reporting-carrier network is incomplete;
scheduled-service airport metadata can omit historical airports or services.

`MobilityRoads` extends the compact search with dated airport transitions. Each
accepted connection uses measured directed access/egress roads, a full flight
day with no birding, and separate ground transit days. The flight day's travel
minutes use the ceiling of mean actual block time for that route/date plus 180
minutes of airport overhead. Eight nearby airports at each end are ranked by
observed service and estimated ground access; up to two nonstop alternatives
are measured. A verified network-wide flight-duration lower bound skips airport
searches when a measured local drive is already faster. The flight queue reserves
eight slots for ground discovery and 24 for airport transitions. Airport representative
coordinates and car anchors are distinct; terminal access and rental cars are
assumed. Seat availability, prices, exact departure times and midnight crossings
are not reconstructed. Connecting flights are not part of this first rollout.

The independent validator checks frozen airport metadata, operated-service date
and duration (including a streamed recheck of original BTS ZIP records for selected
months), calendar continuity, car geometry/endpoints, original observation
evidence and access walking. The original bird candidate catalog stays unchanged;
the flight overlay changes only travel options and appends selected airports to
the small display catalog. Flight arcs are schematic, and the existing mileage
curve continues to show road miles.

`scripts/benchmark_mobility.py` runs matched driving and flight trials from the
same verified national checkpoint and initial road-cache snapshot, using the
same native executable and two workers. The default search budget is 600 seconds;
`--seconds 300 --suffix=-v3` selects a shorter pair with separate result folders. It records
both search and final-audit runtime and preserves separate bounded caches.
`scripts/integrate_mobility.py` is a separate follow-up from the verified
ten-minute driving incumbent: it measures flight alternatives for the three
longest eligible travel gaps, restores that same bird-observation checkpoint,
then searches for 60 seconds. Its initial checkpoint comparison measures travel
savings without changing the bird observations. It is excluded from the matched
performance pair.
`scripts/report_mobility.py` publishes the independently verified comparison at
the existing viewer's `/report` URL. Keep the former capacity report separately.
`scripts/verify_mobility.py` checks audited output bindings and actually served
route/calendar/curve behavior, including the flight follow-up when present.
It reuses each run's completed independent source audit; use
`--repeat-source-audit` when a new source concern warrants another full scan.
Neither heuristic result is a proof of national optimality or a causal estimate
of migration effects.

`scripts/benchmark_regional.py` adds a constructive regional phase before native
polishing. It ranks every exact location/date opportunity against current species
coverage, proposes complete regional trips in overlapping four-week sections,
and replaces a section in one move. Trips reserve time at their boundaries for
measured ground access, observed nonstop service and airport egress. Complete
proposals use the same persistent native calendar evaluator as the main search;
unresolved costs defer the proposal. Repair changes only the proposed itinerary,
never the source catalog or coordinate precision.

The full native search then receives a bounded library of at most 512 regional
trip proposals. These enable coordinated changes across several stops while
retaining all original global, geographic and rare-species proposal modes. A
complete source-derived seed is evaluated once rather than rebuilt by greedy
insertion. Restoring a verified flight incumbent preserves its dated plans.

```sh
.venv/bin/python scripts/benchmark_regional.py --explore 900 --seconds 600 \
  --tag regional-flights-900s-600s
.venv/bin/python scripts/verify_regional.py
.venv/bin/python scripts/report_regional.py
```

Each run requires a new tag. The existing 440-species audited flight itinerary
supplies the starting checkpoint and reusable bounded cache. The same 18 GiB
process-tree RSS guard, 30 GiB free-space floor, two routing workers and bounded
logs apply throughout. Regional ranking works directly from memory-mapped
source records, with bounded temporary arrays. The original Parquet archive and
complete frozen model are retained. Expired evaluation bridges and query spill
are cleaned up; reusable caches and audited outputs are kept deliberately.

The regional report tracks the 500-species milestone and 705 intermediate target
separately from the reported 759-species 2024 Lower 48 human reference. The
land-only frozen-taxonomy model and human listing rules have different evidence
and eligibility, so this is a search benchmark rather than a birding-record claim.

The viewer can compare route accumulation against unique species reported by all observers through each selected date:

```bash
./launch.sh viewer-coverage --dataset data/alabama-eod-2024/dataset --runs runs/alabama-eod-2024
```

This scans the complete eligible source cohort before any search date or location reduction, credits each resolved species on its first report of the year, and saves a small `observer-species-coverage.json` supplement. The dashed reference curve, count, percentage and gap use the same taxonomy, geographic, land and quality rules as the route. Reports have date precision: the reference includes the whole selected day while the route count follows illustrated visits. Only routes with the exact matching dataset manifest, release, year and taxonomy policy receive the comparison; other routes show its absence explicitly. Frozen routes and source observations are unchanged. Alabama 2024 reaches 344 reported eligible species; the best tested routes reach 343 (99.7%).

Three additional cumulative charts track road-network miles (including modeled vehicle ferries), planned birding hours and completed rest days. Each uses its own units and follows the selected date and illustrative playback; clicking any chart selects a date for every chart and the map. Birding hours use the configured birding allowance per stop; access walking is charged separately in the daily activity budget and excluded from this curve. Rest days count only days explicitly marked `rest`, with one whole day credited after its playback finishes. These totals are calculated from saved routes without changing search artifacts. The selected Alabama `seed-2-600s` route ends at 4,888.6 miles, 166 planned birding hours and 262 rest days.

### Bounded on-demand road costs

`roads-bounded` configures a fixed-capacity measured-cost cache, replacing the quadratic all-pairs matrix for driving-only searches:

```bash
./launch.sh roads-bounded --problem data/problem.json --output data/roads.bounded.json \
  --endpoint http://127.0.0.1:5003 --network-release frozen-network-profile-label \
  --network-sha256 checksum-of-frozen-routing-inputs --capacity-mib 128 --hot-entries 65536
./launch.sh search --problem data/problem.json --roads data/roads.bounded.json \
  --output runs/bounded-test --seconds 600 --threads 2
```

The supplied endpoint must be an authorized OSRM instance serving the stated fixed car graph. The native `BY3` input retains every selected location and opportunity without allocating location-pair arrays. A local authenticated loopback bridge resolves native misses against OSRM's fastest car route. A bounded native LRU avoids repeated bridge calls; a fixed-size, four-way associative binary file reuses slots and evicts old costs in place. Keys remain directed and include the exact source/road-anchor model, network identity and matching policy. Slot checksums reject torn or corrupted records. Explicit `NoRoute` and `NoSegment` responses represent unavailable travel; timeouts, provider failures and misplaced successful endpoints abort the search. Geographic distances rank proposals only and never substitute for measured road costs in feasibility or validation. This ranking differs from the dense backend's measured-cost ranking, so time-limited search outcomes need not match.

The v2 bounded provider requests `radiuses=1;1` and `snapping=default` at the already supplied road-access anchors. Every successful cost response must return two waypoints within 1.2 m of those anchors (1 m search radius plus 0.2 m for coordinate rounding). Original observation pins and their separate access anchors remain unchanged. Before freezing a selected road cost, the provider requests full geometry, verifies both geometry endpoints and waypoints, and checks distance/time agreement. The immutable SQLite snapshot includes that full endpoint evidence, the matching policy and network hash. Real-data validation independently recomputes endpoint gaps offline and rejects missing or incompatible proof. V1 mutable caches are incompatible and must be discarded; old run snapshots remain historical diagnostics. Older dense/imported caches lack this proof and cannot pass the strengthened real-data validator without rebuilding selected road evidence.

For explicit access walking, set `walking_speed_kmh` and `walking_threshold_m` in the experiment configuration. The current West Coast walking evaluation uses the requested `2.2` km/h and `100` m; the earlier 1 mph (`1.609344` km/h) result remains saved separately. If the straight-line source-pin-to-road-anchor gap exceeds 100 m, each visit adds the **full round-trip distance**, divided by the walking speed, to the existing on-site birding duration. At or below 100 m, access remains within the fixed birding allowance. The added walking must fit the birding day's activity budget along with driving and birding; it cannot be moved into an unrecorded day or skipped at a freely chosen starting location. Every visit returns to the same road anchor. Original coordinates remain unchanged, and all opportunities remain in the input, including visits that cannot fit the time budget.

The default `walking_speed_kmh: null` preserves historical experiments' fixed stop allowance. Walking-enabled native formats (`BY1W`, `BY2W`, `BY3W`) carry a per-location stop duration, and the independent validator recomputes walking from the original coordinates. Saved stops and itinerary CSVs include walking distance and time. Viewer activity totals include walking; the birding-hours curve shows on-site birding, and stop details display the separate round-trip walking allowance. Walking is based on straight-line separation and assumes access is possible; footpaths, terrain and legal access are not established. A changed walking rule needs a new frozen problem and search; measured road costs can be reused because their endpoints, network and matching policy have not changed.

`scripts/evaluate_west_walking.py` preserves the full 2024 West Coast cohort and exact coordinates, audits the preceding route under the new walking rule, and runs a fresh 600-second search with two threads, seed 20240101, memory-mapped OSRM and the existing strict 8 MiB road-cost cache. The derived problem differs only in configuration; fingerprints verify that all locations, species and opportunities are identical. The 2.2 km/h results and report live under `runs/west-coast-walking-2p2-2024`; the original 1 mph results remain under `runs/west-coast-walking-2024`. Its `finalize` stage resumes postprocessing without repeating the search. Use `scripts/verify_west_endpoint_viewer.py --runs runs/west-coast-walking-2p2-2024 --port 8772` to check served coordinates, evidence, endpoints, walking/activity playback and the all-observer curve after starting the viewer on that port.

The cache has one owning provider process and can serve multiple native threads. Sequential searches reuse the same file. Independent simultaneous owners are rejected instead of racing on cache writes. Identical road anchors may share measured costs, while source pins, observation IDs, dates and visits remain separate.

Each completed search freezes only the road costs used by its main route and saved alternatives into `roads.sqlite`. Provenance binds that small immutable evidence snapshot; validation can run offline after the live provider stops and the evictable cache is removed. The run also records disk-cap usage, hits, misses, evictions, provider requests and the native hot-cache cap. Read-only completed problem files can be hard-linked across runs; mutable caches are never copied into run snapshots.

Planning queries use a temporary directory with a 4 GB spill limit and remove it on close. Large EOD cohorts query the compressed archive directly rather than staging every source column. Preparing all locations bypasses unnecessary ranking/reduction work. The West Coast capacity driver, `scripts/evaluate_eod_west.py`, uses California/Oregon/Washington's official 2024 polygons, unchanged source coordinates and all eligible observations, plus the January 2025 US West road graph. It deliberately tests an 8 MiB disk cache. Reproducible audit tables and superseded unanchored model files are removed after their checked replacements exist; source observations, reusable graph inputs, bounded cache and validated results are retained. New benchmark results consume additional storage deliberately; temporary work and cache misses do not create an unbounded file trail.

Large opportunity construction uses temporary integer date/location/species references and calendar-month aggregation windows. This avoids repeatedly grouping long coordinate IDs and names while retaining the same canonical opportunity IDs. The Python opportunity model is released while the native bounded search runs. Selected-leg evidence is saved separately; identical main/alternative routes share file storage. Successful `BY3` native input files contain expired bridge credentials and are deleted after validation, with their hashes and reproduction instructions retained. Checkpoint resumption checks the canonical frozen problem and provider rather than requiring that transient file. Marked query-spill directories left by an exited process are reclaimed on the next connection; live and unmarked directories are preserved.

The West Coast driver runs a 60-second cold-cache trial, a same-seed 60-second warm-cache trial, and a 600-second search, all with two threads. It records each engine's peak RSS, the shared Python process's cumulative RSS high-water mark, simultaneous combined RSS samples, cache evictions, provider requests, peak allocated storage and retained files. Road preparation has an 18 GiB process-RSS guard; the runtime has an 18 GiB combined-RSS guard and a 30 GiB minimum-free-disk guard. Road logs retain bounded diagnostic tails, while subsequent router starts suppress per-request logging. `cleanup` removes completed preprocessing files absent from the router's declared runtime inputs; the frozen runtime graph, PBF source/provenance, observations and validated results remain. `finalize` independently remeasures selected roads using the same graph in OSRM's read-only memory-mapping mode, builds the all-observer comparison and report, and stops that verification router.

The follow-up `scripts/benchmark_west_routers.py` keeps this exact frozen problem and runs a full 600-second search with `osrm-routed --mmap=true`, a fresh 8 MiB cache, two threads and seed 20240101. The `sample` stage creates a deterministic stratified routing-query sample, including selected and unselected local/interstate pairs, reverse directions, vehicle ferries and real island reachability cases. Sampling road queries does not reduce the search cohort. The `osrm-mmap`, `osrm-ram` and `brouter` stages replay the identical ordered queries twice against fresh server processes; responses are saved compressed and provider failures remain explicit. Run these stages sequentially, after `full-search`, to avoid competing benchmark workloads. `analyze`, `display` and `report` produce a comparison, independently checked road display and HTML report under `runs/west-coast-routing-2024`.

`scripts/prepare_brouter_benchmark.py` installs a portable checked Temurin JDK inside ignored `build/`, compiles the routing-only server from official BRouter v1.7.10 sources, and freezes the lookup dictionary and prebuilt western road tiles with SHA-256 provenance. It changes no system Java installation. The benchmark uses the bundled `car-vario` profile with explicit vmax=130 km/h, turn restrictions enabled, and no beeline/offroad mode; a separate 90 km/h sensitivity is recorded. The pinned release has a hard-coded 128 MiB routing memory class; its JVM heap cap is 2 GiB, and requests have a 20-second engine time limit. Current weekly BRouter tiles and its kinematic car-cost model differ from the January 2025 OSRM graph/profile, so cost/shape agreement cannot establish true driving accuracy or isolate engine differences alone. Router RSS includes resident file-backed pages. First-pass timings do not imply a cold OS disk cache; OSRM costs-only requests are timed separately from the matched full-geometry replay.

BRouter's default expanding waypoint-matching range can return road endpoints far from submitted anchors even with beeline and offroad disabled. The comparison records both endpoint distances and flags shifts over 100 m. After the main replays, `strict` repeats all shifted cases, selected no-route cases and nearby controls with `use_dynamic_range=0`, `waypointCatchingRange=100`, and `correctMisplacedViaPoints=0`; rerun `analyze` and `report` to include the result. The 100 m limit is a matching diagnostic, not a walking-access allowance or a change to source observations. BRouter profile overrides use numeric `0`/`1` flags in the pinned release. `cleanup` verifies that corrected OSRM replays preserve the earlier prototype's costs before deleting the superseded timing scratch directory; pinned inputs, compressed measured responses and completed results remain.

The follow-up also found endpoint rematching in OSRM. `strict-osrm` tests affected queries and controls with explicit `radiuses=100;100`; `endpoint-audit` compares every saved selected driving shape with its submitted anchors. The original mmap run passes the older calendar, cached-cost and evidence validator but fails the added endpoint check (20 of 145 distinct legs exceed 100 m, with a maximum near 100 km). Its species total is a diagnostic model score. The current bounded provider and validator enforce strict endpoints as described above.

`scripts/evaluate_west_endpoints.py` invalidates the two affected mutable West Coast v1 caches, rejects the previously displaced endpoints under the new 1 m policy, and reruns the unchanged full 2024 problem with memory-mapped OSRM, a fresh 8 MiB v2 cache, 600 seconds, two threads and seed 20240101. Results, a separate implementation of the frozen endpoint audit, cache invalidation records, source-retention hashes and a report are saved under `runs/west-coast-endpoints-2024`. The driver stops its router, deletes validated transient native input, and builds the viewer's geometry supplement directly from frozen selected-road evidence without contacting OSRM. It preserves the original observations, graph and historical failed-run snapshots. Correct matching does not establish walking or legal access between observation pins and road anchors; that remains an explicit model assumption.

### Lower 48 and DC: complete 2024 laptop run

`bigyear/scripts/prepare_lower48.py all` prepares detailed Census ACS 2024
boundaries, exact source-point land classifications, and an explicit national
publisher-concept/ABA 8.19 presence-only crosswalk. It uses the same experimental
eligibility rules as the regional evaluations. Approval, captive and exotic-status
metadata are unavailable; checklist membership alone does not establish ABA
countability for a particular observation or year. The original verified EOD archive
remains the observation authority, including held and excluded records.

`bigyear/scripts/prepare_regional_router.py` freezes BRouter 1.7.10, a minimal
match-only API extension, its car profile/lookup dictionary, and regional road
tiles. Tiles include a Canadian/Mexican halo because road travel may leave the
observation boundary. The Java heap is capped at 3 GiB; engine memory class is
512 MiB. This provider models current OSM car travel, not historical 2024
closures, traffic, or an OSRM fastest-route guarantee.

Run preparation and search with the repository virtual environment:

```sh
bigyear/.venv/bin/python bigyear/scripts/evaluate_lower48.py compact
bigyear/.venv/bin/python bigyear/scripts/evaluate_lower48.py search --seconds 600
```

The compact model stores every eligible location/date/species opportunity in
memory-mapped fixed-width records and a contiguous species array. Exact source
locality identities and display metadata stay in SQLite with an 8 MiB page
cache. The original source coordinate strings are preserved in Parquet; source
pins are never replaced with road anchors. No location/date/species sampling is
used to reduce the candidate model. Source multiplicities remain bound through
the archive and are reported separately from distinct search opportunities.

Car anchors are resolved when a proposal needs them, into one fixed 24-byte
slot per source location. The anchor is the actual start of a short locally routed
car path, with a fixed eastward probe and matching radii up to the feasible same-day
walking radius. A geometric nearest-way projection alone is insufficient because
it may not match the actual car-routing search. Local routing failures remain
unknown; only a separate explicit nearest-way miss over the complete walking
radius establishes unavailable same-day access. The access policy binds the
cache identity, so the earlier geometric cache cannot be reused.
For gaps greater than 100 m, the full round trip at 2.2 km/h is charged in
addition to the two-hour birding stop, including a freely chosen first stop.
The search uses a fixed 16 MiB road-cost cache and 65,536 native hot entries.
Unresolved queries defer the proposal; timeouts, endpoint mismatches and engine
errors are never cached as unreachable roads. Diagnostics report these deferrals.

Saved route JSON embeds only selected display locations and opportunities, with
an explicit binding back to the complete compact model and original opportunity
IDs. The independent validator checks that binding as well as source evidence,
all calendar days, road geometry/costs, endpoint matching and walking budgets.
The viewer reads frozen selected geometry; running Java and map tiles are not
needed to display a completed result. Full model/source catalogs are needed for
revalidation or another search.

The driver stops if its process tree exceeds 18 GiB RSS or free disk drops below
30 GiB. Query spill directories and temporary bridge credentials are cleaned on
normal completion/failure. Narrow preparation Parquet is removed after compact
model verification. Fixed road/access caches, frozen model files, checkpoints,
selected road evidence and final reports are retained for reuse and audit.
A new run adds a small result folder, not a duplicate national archive or an
all-pairs matrix. Existing regional results and original source files are kept.

A verified regional itinerary can supply starting opportunities without limiting
the national candidate pool:

```sh
bigyear/.venv/bin/python bigyear/scripts/seed_lower48.py
bigyear/.venv/bin/python bigyear/scripts/evaluate_lower48.py search --seconds 600 \
  --folder regional-seeded-600s \
  --seed-file bigyear/runs/lower48-eod-2024/west-derived-seed.json
```

The seed mapper checks exact locality IDs, coordinates and dates against the
complete national catalog, then orders opportunities by uncovered species.
Within the requested runtime, the native engine spends up to 75% rechecking
seed insertions using current walking and measured road costs, then searches
the full national pool with two workers. Old route costs are never imported.
`warm_start` diagnostics report attempted/accepted insertions and elapsed time.

The one-shot `scripts/continue_lower48.py` workflow continues from the verified
national seed for a one-hour search, then automatically freezes and independently
validates the result, updates the report, and checks all viewer curves. It writes
`runs/lower48-eod-2024/run-status.json` every ten seconds; the report displays this
live status. Extended road queries have a 120-second engine limit and 135-second
HTTP limit. Only the BY4 native bridge allows the longer 200-second socket limit;
legacy bounded OSRM clients keep their 55-second default. Longer limits add
previously unresolved queries; existing successful costs remain valid under the
same frozen network/profile and access policy.

Router logs rotate in place at 2 MiB. The current bounded road/access caches are
reused between consecutive runs with identical bindings. The obsolete geometric
access cache is removed after the corrected result passes validation; completed
baseline evidence remains in its run folder. Each router's process and shutdown
records stay with its own run. Long-run failures preserve the last verified
result and write a clear status; they never publish an unvalidated replacement.

The national heuristic also builds a complete species-to-opportunity proposal
index only when its fixed-width entries fit a 512 MiB cap. The 2024 cohort needs
about 329 MiB. This index lets an unseen rare species supply a proposal directly;
global and spatial proposals still reach the full retained model. Larger inputs
fall back to global proposals without removing observations. Warm starts rebuild
the verified incumbent first, keep only improving feasible insertions, and then
try the remaining verified regional opportunities and preserved national
candidates. Independent validation remains mandatory before a new route is
listed as a completed national result.

### Countability decisions and national rerun

`scripts/audit_countability.py fetch` freezes the 3,525 public GBIF verbatim
records supporting the original 626-species route. `decide` writes decisions for
each credited species and each supporting occurrence, plus defaults for the
complete 764-species source catalog. The policy and source hashes are retained
under `data/lower48-countability-2024/`.

The initial audit yields 601 supported modeled species, 23 unresolved species,
and two historical exclusions. “Supported” uses Cornell's documented public
review process as an explicit proxy for native wild occurrences, not individual
approval or wild-status certification. Code-3/4/5 records require a corroborated
place/date event under this conservative model policy; ABA rarity codes alone
do not make a species uncountable. Introduced populations, reintroduction
provenance, historical checklist restrictions and ambiguous captive-site records
receive separate decisions. Every unresolved or excluded record earns zero
credit. Accepted committee events and introduced-population matches apply to
the specific reviewed occurrences, not all records of those species.

`scripts/prepare_countability_model.py` creates a scoring derivative while
preserving every original exact location and canonical dated opportunity,
including zero-credit opportunities. Coordinates and location metadata are
hard-linked; new compact species spans carry only supported credit. An
independent query of the original archive must match every scoring entry count.
Verified identical coordinates, indices, calendar and configuration allow reuse
of the original bounded road evidence; changing scoring does not change road
costs. The original archive, model and 626-species route remain untouched.

```sh
bigyear/.venv/bin/python bigyear/scripts/audit_countability.py fetch
bigyear/.venv/bin/python bigyear/scripts/audit_countability.py decide
bigyear/.venv/bin/python bigyear/scripts/prepare_countability_model.py
bigyear/.venv/bin/python bigyear/scripts/benchmark_countability.py
bigyear/.venv/bin/python bigyear/scripts/audit_countability.py fetch \
  --route bigyear/runs/lower48-eod-2024/countability-flights-900s-600s/route.json \
  --label rerun
bigyear/.venv/bin/python bigyear/scripts/verify_countability_sources.py
bigyear/.venv/bin/python bigyear/scripts/verify_countability.py
bigyear/.venv/bin/python bigyear/scripts/report_countability.py
```

The benchmark allows up to 900 seconds of whole-regional-trip exploration followed by
600 seconds of native search, with the same dated 2024 flights, measured airport
driving and round-trip walking above 100 m at 2.2 km/h. Regional moves replace
complete calendar intervals, allowing coordinated travel and multiple visits to
cross barriers that single-stop changes cannot. All complete candidate records
remain available. Resource guards, bounded caches, router shutdown and expired
credential cleanup remain active.

The independent validator checks source/policy hashes and exact-record
overrides as well as original calendar, road, walking and flight evidence. A
post-search publisher-metadata check also applies the same decision rules to
newly selected records. Publication requires both checks to pass. The viewer
labels audited scores and retains separate all-observer curves for the original
presence-only and audited policies. The HTML report provides searchable species
decisions, unresolved cases and CSV exports. A hypothetical hindsight itinerary
does not establish actual encounters or a certified human Big Year total.

On macOS, the native search budget uses `mach_absolute_time`, matching Python's
awake monotonic clock instead of counting system suspension as search time.
`scripts/continue_countability.py` resumes a validated audited incumbent for
600 awake seconds and holds a process-scoped `caffeinate -i -w` assertion until
cleanup. It changes no persistent power settings. `guarded` records awake
elapsed, wall-clock elapsed and their suspension-time difference separately.

### Marine charter access experiment

Marine coastline preparation/auditing uses the existing optional `gis` extra
(`pyshp`); ordinary route viewing does not require loading the coastline files.

`scripts/prepare_marine_charter.py` preserves the 2024 offshore and
outside-state-polygon EOD groups in a separate Zstandard Parquet supplement.
It keeps the source counts and verbatim coordinate strings, and applies the
existing 2024 taxonomy, provisional, population and spatial-quality policy.
The original archive, CSV, terrestrial model and verified routes are preserved.

`scripts/build_marine_candidates.py` constructs exact-date, exact-pin water
trips from a versioned port inventory. A conservative nearest-country check
extends Lower-48 scoring into adjacent waters, with a 5 km uncertainty hold
near competing jurisdictions and the ABA 200 statute mile limit. Every boat
segment must clear the original OSM land polygons. The bounded routing grid
only adds navigation waypoints; it never moves or rounds an observation pin.
Unresolved harbors, on-land points and island landings remain held. Coastline
clearance does not establish bathymetry, tides or navigational permission.

`scripts/benchmark_marine_charter.py` adds same-port expeditions to the free
days of the verified 656-species land/air incumbent. Its terrestrial birding
and flight days stay fixed. Full measured driving days reach and leave each
port. Boat days include 12-knot sailing, 120 minutes of birding per exact pin,
60 minutes boarding and full round-trip dock walking above 100 m at 2.2 km/h,
all within the existing 720-minute activity limit. Charter availability and
weather are explicit scenario assumptions; these are modeled tracks, not
claimed historical operated sailings. This is a controlled insertion search,
not joint native optimization of roads, flights and boats.

The marine validator independently rechecks the original itinerary, driving
endpoint proofs, every water segment, source coordinate/date identity against
the original archive, publisher verbatim records, jurisdiction and all daily
budgets. The viewer distinguishes boat legs, counts boat miles in its mileage
curve, and includes boarding/walking in activity playback. Its all-observer
comparison labels the partial marine cohort explicitly.

Completed catalogs and runs refuse overwrite. To repeat a scientific scenario,
choose a new input version and run tag. Partial port construction is checkpointed;
its cache bindings must match the exact input and construction version. Road
costs remain bounded at 16 MiB, and the driver stops its owned router and
process-scoped wake assertion after validation.

Validate a saved marine experiment offline using the normal command:

```bash
bigyear/.venv/bin/bigyear validate bigyear/runs/lower48-eod-2024/marine-charter-2024-v2
```

The verified boat-only comparison reached **667 species (658 primary + 9
provisional)**, an increase of 11 over the fixed 656-species incumbent. Eight
charter days use four ports; all original birding and 26 flight days remain.
Island Scrub-Jay is explicitly held for marine access: the water-coded mixed
anchorage/beach checklist does not establish observation from a vessel, and
this scenario assumes no island landing. Its terrestrial eligibility remains.
The versioned default boat-access hold is retained in
`data/marine-2024-charter-v1/boat-access-score-holds.json` for later comparisons.

The original archive and exact source coordinates are unchanged. Temporary
port checkpoints and the superseded cache are removed after verification;
the rejected 668-species draft is retained in verified lossless gzip form.
The final cost cache is 16 MiB. See
`runs/lower48-eod-2024/2024-marine-charter-report.html`, the saved route's
`validation.json`, `negative-controls.json`, and `marine-source-holds.csv`.

### Joint driving, dated flights and modeled charters

`scripts/prepare_joint_mobility.py` builds a new source-bound compact catalog.
Every original terrestrial location, date, species entry and verbatim coordinate
is preserved; the existing 2,001 exact-date charter alternatives share nine
physical port nodes. Charter variants have separate, hashed activity and sailing
costs. This avoids inventing bird observations at the port or charging every
charter the same duration. The complete offshore Parquet supplement is retained.
Marine construction remains partial: its original shortlist targeted additions
to the 656-species incumbent, and four port references remain held.

The native evaluator and national search now optimize the three transport modes
in one calendar. One charter is allowed per date. Its full sailing, boarding,
on-site birding and 2.2 km/h dock walk share the 12-hour cap; up to 30 local road
miles can precede departure only when they fit that cap. The exact marine pin is
visited before credit, and the boat returns to its departure port. The previous
boat-only Island Scrub-Jay access hold still applies. Flights retain observed
2024 service dates, full flight days and separate ground-access days.

Regional proposals include whole multi-day port visits. Their native evaluator
now applies the same conservative calendar screen as the main search before
requesting expensive routing. This screen rejects impossible time/distance
combinations without labeling any physical road unreachable. A regression test
checks that a cross-country transfer with no transit day issues no road or
airport query.

```bash
bigyear/.venv/bin/python bigyear/scripts/prepare_joint_mobility.py
bigyear/.venv/bin/python bigyear/scripts/benchmark_joint_mobility.py --tag YOUR-NEW-RUN
bigyear/.venv/bin/python bigyear/scripts/diagnose_joint_mobility.py bigyear/runs/lower48-eod-2024/YOUR-NEW-RUN
bigyear/.venv/bin/python bigyear/scripts/verify_joint_tamper.py bigyear/runs/lower48-eod-2024/YOUR-NEW-RUN
bigyear/.venv/bin/python bigyear/scripts/report_joint_mobility.py bigyear/runs/lower48-eod-2024/YOUR-NEW-RUN
```

Default search budgets are 240 seconds of boat-led regional replacements,
240 seconds of general regional construction, and 900 seconds of native search.
Cold preparation, independent source/transport audits and verified cache reuse
are recorded separately. The resource guard is 18 GiB combined owned-process RSS
and 30 GiB minimum free disk. The 16 MiB road-cost cache, selected road geometry,
source snapshots and result evidence remain; temporary bridge credentials,
owned routers and the process-scoped wake assertion are cleaned up.

Independent validation compares the entire terrestrial catalog with its frozen
parent, rechecks the exact charter alternatives and activity sidecar, audits
selected land/dock walks against the original coastline, validates full road
endpoints and original dated BTS flight records, and checks every credited
publisher record. Marine source groups are also compared with the original
lossless archive. `negative-controls.json` binds deliberate pin, sailing-time,
boarding-time and unresolved-species mutations to the positive verified route.
A normal offline `bigyear validate RUN-DIRECTORY` runs the complete joint audit.

The gap report lists uncollected taxa, actual support dates, terrestrial and
constructed-charter option counts, and proximity to the route on matching dates.
Proximity guides search; it does not establish road or time feasibility. The
working 759-species recordholder replay remains pending the actual dated input.
The 792-species modeled source pool is not proof of a feasible 759-species route.

`scripts/analyze_calendar_conflicts.py RUN-DIRECTORY` also constructs a
source-bound conservative count certificate. It checks the complete support
of narrow-date taxa, allows an optimistic 200 km/h car speed, maximum local and
walking slack at both endpoints, and any flight when three transit days exist.
Disjoint mutually exclusive species cliques provide a directly checkable upper
bound for this frozen candidate model. This is neither a real-world ABA bound
nor a proof that an itinerary at that bound exists. For this model the bound is
789, so it does not rule out the working 759 target. Common Swift and Great Knot
are one incompatible pair under the current recorded dates and full-day travel
implementation. More complete marine inputs or a different timed calendar
require a fresh bound. The analysis retains all observations and exact pins.

The joint follow-up also audits newly selected terrestrial walks. A rejected
candidate used the exact San Diego source point `32.61321,-117.10431`, whose
assumed road-anchor walk crosses about 571 m of open water. The separate
`source-visit-holds.json` masks that visit on all dates without rewriting the
archive, moving its pin, or caching a false disconnected road. Future joint
benchmarks load the mask automatically. `scripts/repair_joint_access.py` tests
another actual observation on the same date at a previously audited land-access
location, rescoring the complete calendar before a short corrective native pass.
The final source/road/flight/charter/walking audit is repeated in full. Original
search and correction timings, source choices and SHA-bound lineage remain
separate; a rejected draft is never advertised as a verified result.

The preceding verified joint result is **673 modeled species (664 primary + 9 provisional)**:
11 charter days from five ports, 26 flight days and 19,480.5 road miles. All 3,859
credited publisher records and 120 directed road paths pass independent checks.
The full joint search plus access repair/audits took 35.5 monitored minutes and
peaked at 2.87 GiB RSS; the separate pilot is excluded. The gap to the working
759 target is 86. See `runs/lower48-eod-2024/joint-mobility-2024-access-v3/` and
`runs/lower48-eod-2024/2024-joint-mobility-report.html`. Source Parquet, exact
coordinates and earlier validated routes remain preserved. Superseded discovery
cache binaries are retired only after the final positive audit; provider
metadata and rejected-candidate diagnostics remain for provenance.

### Targeted trips in previously unvisited states

`TargetedExplorer` builds a small proposal index for the nine missing taxa from
the verified 673 route: Black-tailed Gull, Eurasian Tree Sparrow, European
Goldfinch, Fieldfare, Green-breasted Mango, Himalayan Snowcock, Northern Wheatear,
Taiga Bean-Goose and White-winged Tern. Every original opportunity remains in the
full evaluator and native search. Heads use actual observation dates and exact
coordinates; proposal ranking does not establish travel feasibility.

Construction first tries a single-date insertion, then multi-day regional
replacements with nearby source-dated visits. Targets take turns so a common
species with many observations cannot consume all expensive routing queries.
Measured feasible blocks can enter the native macro-move library even if they
temporarily sacrifice other species. Only the normal species total is awarded;
targets receive no artificial scoring bonus. Unknown routing remains deferred.

```bash
bigyear/.venv/bin/python bigyear/scripts/benchmark_joint_mobility.py \
  --tag YOUR-NEW-TARGETED-RUN \
  --incumbent bigyear/runs/lower48-eod-2024/joint-mobility-2024-access-v3 \
  --targeted 900 --boats 120 --regional 120 --native 900
```

The incumbent's positive audit, complete model, routing identities, airport
release and physical road proofs are checked before warm-start reuse. Existing
source-access holds remain active. The new route must pass the same full source,
calendar, walking, road, flight and charter audits before publication. Diagnostic
outputs include `targeted-species-results.csv`, `targeted-exploration.json` and
`incumbent-species-changes.csv`; feasible campaigns that were not selected are not
proof that every requested target can fit together.

The earlier targeted result was **678 modeled species (669 primary + 9
provisional)**, five above 673 and 81 below the working 759 target. Three of the
nine are collected: Green-breasted Mango (Louisiana, January 21), Himalayan
Snowcock (Nevada, September 25) and Taiga Bean-Goose (New York, December 31).
Two other targets have measured feasible alternatives with lower calendar
totals; four were routing-unresolved in that run. No global
infeasibility claim follows from those outcomes.

The 40.5-minute monitored workflow peaked at 2.81 GiB RSS. The final route has
26 flight days, 11 modeled charter days and 19,348.1 road miles; all 3,898 selected
publisher records and 120 directed road paths pass the full 366-day audit.
All 1,140 original Parquet parts match their verified conversion checksums.
See `runs/lower48-eod-2024/targeted-regions-2024-v3/` for the route, target CSV,
source audit, resource measurements and frozen executed code. Earlier routes
and the preceding published report are preserved.

### Resolving rural target routing

`--resolve-routing` adds versioned alternate local car probes after the original
eastward probe defers. It keeps finite validated anchors and explicit access
holds intact. Airport centroid failures can use separately recorded nearby
road approaches; observation coordinates are never offset. Rural flight
discovery considers 24 airports on each side and measures up to six candidate
pairs, using observed dated nonstop service and separate ground-access days.
The longer local routing budget is 120 seconds, with the same 3 GiB Java heap.

The old eight-airport cutoff excluded Minneapolis–St. Paul, ninth nearest to the
Fieldfare observation. Without that gateway, the remaining direct cross-country
car query returned a BRouter reconstruction error. The wider discovery resolves
that dated transfer without inventing a road cost. Connectivity witnesses for
each requested species use a complete year with a generous replacement window;
their lower scores are not optimized totals or a proof that all four fit together.

Cache migration checks the complete model, physical road release, airport
identities and access policy. It carries finite costs only where both anchors
are identical, drops unknown/infinite costs, and requires fresh endpoint proofs
for selected final paths. Existing source-visit holds also precede the cached
stop-duration fast path, so a finite historical anchor cannot revive a held
visit. This mask does not mark physical roads unreachable. The default migration uses the incumbent's cache;
`--routing-query-cache DIRECTORY` optionally reuses compatible measured query
results. `--routing-witnesses FILE` re-evaluates previously feasible calendars
only after checking their baseline/model/network bindings.

```sh
bigyear/.venv/bin/python bigyear/scripts/benchmark_joint_mobility.py \
  --tag YOUR-NEW-RESOLVED-RUN \
  --incumbent bigyear/runs/lower48-eod-2024/targeted-regions-2024-v3 \
  --resolve-routing --targeted 600 --boats 120 --regional 120 --native 900
```

The routing-resolution follow-up passed its full independent audit at **682
modeled species (673 primary + 9 provisional)**, four above 678 and 77 below the
working 759 target. All four requested species have measured feasible complete
calendar alternatives. The selected route collects Eurasian Tree Sparrow in
Missouri on January 1, Fieldfare in Wisconsin on March 8, and European Goldfinch
in Illinois on March 11. White-winged Tern remains uncollected; its best tested
alternative scores 678, so unresolved routing is no longer its diagnosis.

Relative to 678, the final route gains those three targets, Smith's Longspur and
Yellow Grosbeak, and loses Long-eared Owl. It has 29 flight days, 11 modeled
charter days, 20,585.3 road miles and birding visits in 20 states. All 3,839
selected publisher records, 126 directed road paths and 366 days pass validation.
The final monitored workflow took **39.3 minutes**, peaking at **2.71 GiB RSS**;
earlier interrupted diagnostics are logged separately. All observations, exact
coordinates, 2.2 km/h walking and original access holds are preserved. Python
regressions: 180 passed; viewer regressions: 19 passed, one unrelated skip; all
four charter negative controls are rejected for their expected reasons.

See `runs/lower48-eod-2024/resolved-routing-2024-v3/` and the current joint report
for source-bound routing witnesses, target outcomes, selected endpoint proofs,
resource measurements and frozen executed code. Earlier verified routes and
their published reports remain preserved. Superseded diagnostic cost/anchor
binaries are retired after verification; the final bounded cache is retained.

## Matched calendar construction study (2024)

`scripts/prepare_calendar_study.py` preserves the verified 682-species inputs and
adds a separately versioned Santa Cruz landing model. Its 262 additional choices
use actual eligible terrestrial source dates and exact coordinates near Scorpion
Anchorage or Prisoners Harbor. Each returns to Ventura: two hours aboard the
boat each way, 60 minutes total boarding, 120 minutes birding, and full out-and-back
island walking at 2.2 km/h, all within 12 hours. Crossing geometry clears the
frozen coastline. Near-landing foot access, weather and sailing availability are
modeled; historical ferries and public footpaths are not certified. The complete
terrestrial cohort and all 2,001 original pelagic charters remain unchanged.

The study compares continued native search, nearby cleanup, tentpole calendars,
island access and their combination. Each primary arm starts independently from
the same 682-species calendar and byte-identical bounded-cache copy, with a
900-second search allocation and two native workers. Input checks, routing-worker
drain, saving, publisher retrieval and independent validation are measured outside
that allocation. One replicate per arm is diagnostic, not statistical proof that
one strategy is generally superior. The declared plan and cache hashes are in
`data/calendar-strategy-study-2024-v1/study-plan.json`.

`calendar_study.py` implements repeated nearby additions around exact visits,
bracketing endpoints and measured baseline road corridors across their full
transfer gaps, including spare days. Corridor geometry guides proposals only;
every accepted insertion passes the full current native calendar evaluator. Tentpole
construction tests actual supported visits, retains feasible alternative calendars
even without an immediate gain, and launches native recovery searches with hard
required visits. `BIGYEAR_REQUIRED_OPPORTUNITIES` binds those exact opportunity
IDs; every proposal, restart and saved incumbent retains them. Species still earn
one point each; travel time and road miles only break equal-species ties.

At phase boundaries, unstarted obsolete routing requests are cancelled without
discarding measured costs or marking unknown connections unreachable. Before
saving, a fresh native evaluator checks the chosen calendar, and all selected
physical road legs, including airport access/egress, are warmed and temporarily
pinned. Pins accept only measured finite cached costs and are capped at 8,192
entries; the on-disk road-cost cache remains 16 MiB. This prevents a long-lived
native client's hot entries from hiding provider-cache eviction during saving.
The final 0.01-second native invocation materializes an already selected calendar;
it is not additional optimization time.

Primary outputs are `calendar-study-control-2024-v1`,
`calendar-study-nearby-2024-v2`, `calendar-study-tentpole-2024-v1`,
`calendar-study-island-2024-v1` and `calendar-study-combined-2024-v1` under
`runs/lower48-eod-2024/`. Nearby v1 is retained as a diagnostic: its queued-query
handoff problem was identified before its final result was known, and its saving
step was subsequently resumed without repeating the search. The primary nearby
version also includes the road-corridor review absent from that first pilot.
The pilot is excluded from the primary comparison. `report_calendar_study.py --partial` shows verified
progress; `--publish` creates the completed comparison, cumulative curves, target
dates, source-bound results and species changes. A pending routing attempt means
unknown feasibility, never a proven calendar conflict.

<!-- calendar-study-results:start -->
### Verified results

All five 900-second search arms passed full 366-day transport, source and countability audits. The combined arm reached **691 modeled species (680 primary + 11 provisional)**: nine above the preserved 682 baseline, six above continued search and 68 below the working 759 target.

| Arm | Species | Primary + provisional | Change vs 682 | Wall minutes | Peak process-family GiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| Continued search control | 685 | 675 + 10 | +3 | 24.0 | 2.97 |
| Nearby cleanup | 689 | 677 + 12 | +7 | 20.9 | 3.64 |
| Tentpole calendars | 685 | 676 + 9 | +3 | 21.5 | 3.87 |
| Island access | 683 | 674 + 9 | +1 | 25.7 | 4.05 |
| All three together | 691 | 680 + 11 | +9 | 24.2 | 3.49 |

Nearby cleanup alone added seven species without losing any baseline species. In the combined arm it added five species through four visits after anchored reconstruction had reached 686. The island arm demonstrated a feasible November 8 Santa Cruz visit, adding Island Scrub-Jay. The tentpole arm collected Rufous-capped Warbler and White-eared Hummingbird, but its Golden-cheeked Warbler construction attempts remained routing-pending; that is not an infeasibility finding. The source pool already contains 146 supported Golden-cheeked dates.

The five primary calendars collectively demonstrate 696 species. The winner misses five species seen in alternatives: Aplomado Falcon, Blue-crowned Parakeet, Eurasian Tree Sparrow, Rufous-capped Warbler, Willow Ptarmigan. This union is not proof they all fit together. Resolving dated seasonal-target transfers, testing combinations of these alternatives and repeating multiple seeds are the next controlled comparisons.

All original observations, exact coordinates, 2.2 km/h walking and sensitive-species policy are preserved. The full 1,140-part Parquet archive and original 682 route/cache are retained. Independent island negative controls reject moved pins, missing or shortened returns, undercharged walking/activity and credit at a mainland port. Python regressions: 188 passed; viewer regressions against the 691 route: 20 passed, one unrelated fixture skipped.

`runs/lower48-eod-2024/calendar-strategy-study-2024-v1/` contains the published interactive report, comparison CSV, species changes, per-arm result bindings, regression logs and final integrity/archive verification. Each arm retains its selected road database, embedded map geometry, source payloads and audit. Redundant study cache binaries are retired only after verification; the winner and original 682 caches remain. `final-integrity.json` records logical and allocated file sizes separately. The viewer uses saved geometry and does not need losing routing caches to render.

The control’s legacy airport-search description was corrected to its executed 24-airport / six-pair policy, then independently revalidated. Old metadata is preserved; no selected visit, coordinate, geometry or score changed. See its `metadata-clarification.json`.

One replicate per arm remains a diagnostic result, not a demonstrated optimum or statistical proof of a strategy’s general superiority. Island sailings, weather and near-landing foot access remain explicitly modeled.
<!-- calendar-study-results:end -->
