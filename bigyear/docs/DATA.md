# Input contract and scientific policy

## EBD and SED

Use your authorized eBird Basic Dataset download, extracted as tab-separated `.txt`/`.tsv` or gzip text. Raw exports are never loaded into a Python dataframe. DuckDB scans the file, writes disk-backed staging tables under the output directory, filters and joins, then compresses yearly Parquet. Set `memory_limit` (default `2GB`) and `threads` in the JSON config. Allow staging disk space comparable to the projected selected columns of the full export, plus eligible evidence and Parquet; compressed raw size is not a disk requirement estimate. Input hashing streams in bounded blocks. Do not put raw exports or processed bulk data in Git.

Required exact EBD headers:

```
SCIENTIFIC NAME  CATEGORY  OBSERVATION COUNT  COUNTRY CODE  STATE CODE
LOCALITY ID  LOCALITY  LATITUDE  LONGITUDE  OBSERVATION DATE
SAMPLING EVENT IDENTIFIER  APPROVED
```

These are tab-separated headers with spaces, not the whitespace layout above. `GROUP IDENTIFIER`, `GLOBAL UNIQUE IDENTIFIER`, and `EXOTIC CODE` are optional. Unknown metadata columns are ignored. Missing required headers or malformed rows fail; rows are never silently skipped by the CSV parser. Presence means a positive number or `X`. Approval requires `1`/`true`/`TRUE`; `REVIEWED` alone does not establish eligibility. Complete checklists are not required for this presence-only objective.

Optional SED must contain `SAMPLING EVENT IDENTIFIER`. Its `GROUP IDENTIFIER` fills a missing EBD group ID by an exact checklist-ID join. EBD already carries required event/location fields. No effort values are inferred from missing SED: each stop uses the explicit configurable birding-duration assumption. This first version does not recreate species-specific search time or the within-checklist travel path.

Independent species events are grouped by group identifier (or checklist ID if unshared), date, **exact locality ID/latitude/longitude/state**, and mapped species. Shared checklists' original IDs remain attached. A shared group at two different coordinates is kept separate. Raw evidence retains record identifiers, source taxon, counts, category, approval, and event/checklist IDs. The independent event counts prevent shared lists from inflating rarity scores.

## Grouped EOD Parquet

County or other polygon pilots can bind the original archive with
`import-eod --scope-geojson boundary.geojson --scope-label "County name"`.
The polygon must use EPSG:4326. Preparation freezes the boundary and a catalog of
exact coordinate strings; boundary points are included. No observations are rounded
or regrouped. County bindings cache only their filtered rows in bounded DuckDB
temporary storage during each connection, while reading their evidence from the
original compressed archive. `classify-land-eod --scope-geojson boundary.geojson`
uses the same observation scope. Independent route validation checks the polygon
again; road paths may leave it. Scope catalogs and boundaries are checksummed.

`convert-eod` accepts the documented 14-column grouped GBIF EOD TSV and writes
Zstandard-3 Parquet parts under a new directory. It uses bounded chunks and a
1 GB DuckDB memory budget. Each chunk undergoes a complete row/value round-trip
comparison, including coordinate text, missing values, taxon strings and source
IDs. A source SHA-256 and checksums/counts for every output part are recorded.
Only a complete, verified archive is accepted by `import-eod` and `inspect-eod`.
`--resume` rechecks completed parts against the unchanged source. The original
input is retained even after successful verification.

`import-eod` binds that archive directly, copying only the small taxonomy and
land catalogs and writing `review.csv` and a manifest. No expanded CSV, duplicate
yearly evidence table or national Python dataframe is required. Experiments
still apply their configured year, lower-48/DC geographic scope and date window.
The source archive retains the entire US cohort from 2000 onward.

EOD taxonomy uses the standard crosswalk fields below, plus **exact**
`source_taxon_id` and `eod_presence_eligible`. The source name is `v_scientificname`
and the concept is `v_taxonconceptid`, copied literally. Do not silently repair
concept prefixes or apply EBD mappings by approximate name. Name-only mappings
do not qualify EOD records.

`eod_presence_eligible` accepts `1`, `0` or `review`. Its default is `review` when
missing. A `1`, together with `aba_eligible=1`, is an explicit reviewed decision
that the exact source taxon can receive presence-only credit under the stated
reference and geography despite unavailable EBD approval/category/exotic metadata.
It is not inferred from taxonomic name matching or a GBIF occurrence ID. Regional,
introduced, domestic and unresolved taxa need substantive review before a `1`.
These decisions are distinct from an EBD crosswalk and are frozen in the dataset
binding. Original eBird approval is never fabricated.

EOD route evidence retains the original source name/concept, coordinate strings,
representative GBIF ID, source-row count and quality summaries. Checklist ID is
empty; approval, category, bird count and exotic code remain null. Internal
`gbif-group:` event labels identify aggregate rows, not original checklists.
Counts are not independent checklist counts. A group currently needs a positive
source count and zero flagged/unknown geospatial records for planner credit.
Groups failing these checks remain in the archive and appear in review. Missing
coordinate uncertainty does not become zero or prove precise access.

Because EOD locality IDs are unavailable, the adapter assigns a clearly derived
`EOD:` ID: SHA-256 of the compact JSON array `[US-state-code,latitude-text,longitude-text]`.
Both original coordinate strings participate in identity without rounding. The
land catalog requires this ID and the exact coordinate text. Use
`classify-land-eod` with a supplied detailed terrestrial GeoJSON mask to generate
the catalog, then review coastlines and access as for EBD. Numeric coordinates
are used for map/road calculations while the source text remains preserved.

## Frozen taxonomy and ABA eligibility

Supply one reviewed CSV crosswalk and use the identical file across compared years:

```csv
source_name,species_id,common_name,aba_eligible,eligible_states,reference
Exact EBD scientific name,chosen-stable-species-id,Species common name,1,*,Taxonomy release and ABA checklist decision reference
Exact subspecies scientific name,chosen-stable-species-id,Species common name,1,*,Explicit parent-species mapping reference
Uncertain example,,,review,,Manual eligibility decision pending
```

`source_name` must match `SCIENTIFIC NAME` exactly; no fuzzy name matching or automatic two-word truncation occurs. An optional `source_taxon_id` column can qualify a mapping by the exact EBD `TAXON CONCEPT ID`. Keys must be unique by `(source_name, source_taxon_id)`; an exact ID match takes precedence over a name-only mapping. Use stable target IDs from the chosen taxonomy. All accepted aliases/subspecies targeting one species must use one common name. `aba_eligible` is `1`, `0`, or `review`. Eligible rows require target ID, common name, `eligible_states` and a reference. Use `*` for a reviewed nationwide eligibility decision, or explicit codes such as `US-FL|US-TX` for regional eligibility, including established introduced populations. The reference should identify both taxonomy and ABA decision; the application does not decide countability from a species name.

EBD categories `species`, `issf`, `subspecies`, and `form` can receive credit through an explicit eligible mapping. Category `domestic` requires an exact `source_taxon_id` mapping as well as eligibility, exotic-code and geographic checks; a generic parent-species mapping is insufficient. This permits reviewed countable feral forms without admitting other domestic forms. For example, the supplied March 2025 sample identifies Rock Pigeon (Feral Pigeon) as `domestic` with concept `avibase-533DD36C` and exotic code `N`. Domestic records without an exact qualified decision receive `domestic_taxon_review`. Hybrids, slashes, spuhs and other unresolved categories are excluded even if a mapping was accidentally supplied. EBD's parent scientific name can already identify a subspecies record's parent species; map the actual contents of `SCIENTIFIC NAME`, not an assumed field format. Concept IDs identify the most specific reported taxon, so a subspecies ID is not necessarily the parent species ID.

The original taxon concept ID, subspecies scientific name, county and locality type, observation/protocol fields, start time, duration, effort distance and reviewed flag are preserved in evidence when present. Modern `OBSERVATION TYPE` and older `PROTOCOL TYPE` headers are supported. These effort fields provide provenance; they do not replace the explicit constant birding-duration model.

Missing mappings, `review`, mismatched regional eligibility, or exotic codes other than blank/`N` go to review rather than species credit. Naturalized `N` records still require explicit crosswalk eligibility for the state. A crosswalk-wide `*` is the operator's substantive decision; use regional restrictions when eligibility depends on an established population. This schema does not model county-level populations, individual provenance, or changing establishment dates. Such records must remain `review` until an appropriate study-specific prefilter/crosswalk decision is documented; do not mark an entire species eligible merely because it appears on the ABA list.

Give every import a release label and taxonomy-policy identifier. The file hash enforces the exact same crosswalk at comparison time. Outputs include `review.csv` grouped by reason/taxon and full `excluded.parquet`; inspect them before search. Reimport into a new directory after decisions change.

## Offshore exclusion

A US state code and a lower-48 bounding box do **not** prove that an observation was on land. Provide an exact-point classification catalog:

```csv
locality_id,latitude,longitude,on_land,source
L123456,32.123456,-110.123456,1,Detailed terrestrial polygon release or documented manual review
L654321,28.123456,-80.123456,0,Offshore review reference
```

Keys are exact locality ID, latitude and longitude. Missing classifications are `land_unknown_review`; explicit `0` is `offshore_excluded`. Both receive zero credit. A catalog entry cannot authorize a different coordinate for the same locality ID. State-code filtering and coordinate sanity bounds also apply. Source labels and catalog hashes are retained.

To produce the catalog from a **supplied detailed terrestrial polygon** mask in EPSG:4326:

```bash
./launch.sh classify-land --ebd /downloads/ebd_US.txt \
  --geojson data/terrestrial-polygons.geojson --output data/land.csv
```

The helper streams unique locations from DuckDB, includes boundary points, and records the polygon file hash. It accepts Polygon/MultiPolygon GeoJSON geometries or a FeatureCollection of them. Check coastal points and traveling-checklist precision manually. Do not use political state polygons that include territorial water as terrestrial masks. The bundled Natural Earth map is display-only, too coarse for offshore exclusion, and is deliberately never used by the importer.

## Directed road costs

An optional exact-point access catalog can separate reported observation pins from the drivable road anchors used for road costs. Pass it with `prepare --access-catalog FILE`; see README for fields. Original coordinates remain the evidence keys. The road cache records and checks both coordinate pairs, including legitimate zero driving between different pins sharing one anchor. Walking/search effort remains inside the configured per-stop allowance; neither a boat trip nor a precise walking route is inferred from an inland water-centered pin. The frozen problem contains the catalog hash and access metadata.

Use IDs from `problem.locations.csv`. The schema is:

```csv
origin,destination,miles,minutes,source,network_release,geometry
location-id-a,location-id-b,18.4,29.1,Routing engine or verified matrix,Network snapshot and routing profile,
```

`geometry` is optional, CSV-quoted GeoJSON `LineString`. Costs must be finite, nonnegative (positive for distinct coordinates), and in road miles/minutes. Directed pairs are not assumed symmetric. Real-road imports reject costs shorter than geodesic distance beyond a small rounding tolerance; this is only a sanity check, not road validation. Missing pairs are unreachable. Only identity travel is automatically zero. No straight-line distance validates a daily limit.

`roads-osrm` requests table duration and distance from fastest routes using the driving profile, with bounded batches. `--geometries` also requests route GeoJSON and uses that route's costs. Supply your own appropriate road network and label its snapshot/profile; its snapping and network connectivity remain modeling assumptions. A current routing network is not historical reconstruction. For 1,000 locations there can be one million directed pairs; geometries for every pair are optional and expensive. Pair costs stream through SQLite into the C++ input; Python uses a bounded hot-pair cache.

## Optional airport network

Set `flights=true`, provide these CSVs to `prepare --airports ... --flights ...`, and generate roads for the exported birding **and airport** locations:

```csv
airport_id,name,latitude,longitude,state,on_land
AAA,Example airport,32.2,-110.9,US-AZ,1
BBB,Other airport,25.8,-80.3,US-FL,1
```

```csv
from_airport,to_airport,minutes
AAA,BBB,360
```

Minutes include the simplified flight and airport overhead. Connections are directed; a return connection must be supplied separately. Access and egress use cached road legs on separate transit days, and every connection consumes an entire flight day without credit. No multihop airline schedule is inferred. This deliberately conservative access convention is identical across compared years.

## Sources

- [Cornell's eBird data guide](https://ebird.github.io/ebird-best-practices/ebird.html): EBD/SED, shared events, taxonomic rollup and spatial limitations.
- [eBird download guidance](https://support.ebird.org/en/support/solutions/articles/48000838205-download-ebird-data): obtain authorized downloads and metadata for your release.
- [ABA Checklist](https://www.aba.org/aba-checklist/): source for the operator's explicit eligibility review.
- [OSRM API](https://project-osrm.org/docs/v5.24.0/api/): directed road matrix and GeoJSON route contracts.
- [OR-Tools C++ installation](https://developers.google.com/optimization/install/cpp) and [CP-SAT status semantics](https://developers.google.com/optimization/cp/cp_solver).
- [Natural Earth terms](https://www.naturalearthdata.com/about/terms-of-use/): public-domain display basemap. Exact upstream hash is in `web/basemap-source.json`.

## Compact driving matrices

`roads-matrix` writes `*.matrix.json` plus a sibling `*.matrix.bin`. The manifest binds the full ordered observation-location catalog, distinct exact road-access anchors, the location-to-anchor index, source endpoint, road network release, byte count and full binary SHA-256. Binary entries are row-major little-endian float64 `(miles, minutes)` pairs. Unreachable pairs use two infinities. Coordinates are never rounded to create shared anchors, and sharing an identical anchor does not merge reported locations, source observations or the per-stop activity allowance.

The driving-only native BY2 input references this read-only file and the complete location-to-anchor mapping. Road transit requires `ceil(minutes / drive_minutes)` days, as in the original BY1 transition table. Completed run manifests and immutable hard-linked binary snapshots keep provenance verifiable without duplicating several gigabytes per seed. Preserve each manifest and its named binary together when exporting a run. SQLite/BY1 remains available for flight networks.
