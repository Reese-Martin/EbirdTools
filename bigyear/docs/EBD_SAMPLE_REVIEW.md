# EBD sample compatibility review

## Conclusion

The supplied `ebd-datafile-SAMPLE.zip` contains the information required by the historical planner's reported-day, location-point availability model. Its observation and sampling files both parse with the production DuckDB reader, and the complete import with the sampling-file join succeeds. This verifies sample compatibility, not national-export throughput or an ABA-countable species total.

This is a real-data **format sample for Tuscaloosa County, Alabama, March 1–31, 2025**, release `EBD_relMar-2025`. It contains no 2024 or 2023 observations. The bundled metadata is version 1.16, revised February 19, 2025. The zip contains ten files, including observation data, sampling data, metadata, protocol definitions, ancillary area codes and citation/terms documents.

## Measured contents

| Measure | Observation file | Sampling Event Data |
|---|---:|---:|
| Rows | 4,863 | 317 |
| Distinct checklist identifiers | 313 | 317 |
| Exact locality/latitude/longitude combinations | 106 | 109 |
| Shared-checklist group identifiers | 41 | 41 |

All 313 observation checklists match sampling records. Date, locality ID and coordinates agree across every joined observation row. Four sampling records have no corresponding observation rows in this archive; their presence is not a join failure and does not imply an additional bird detection. No observation, checklist or locality identifier is missing. All observation identifiers are unique, and all coordinates parse numerically.

The observation file has 4,760 `species`, 57 `issf`, 38 `domestic`, and eight `spuh` rows. There are 120 distinct scientific names among the species/ISSF records, with five infraspecific concepts. All infraspecific parent names also occur as species records. Before ABA/land screening, shared-event and parent-species aggregation reduces 4,817 species/ISSF rows to 3,718 event/species/location/date combinations. These are diagnostic counts, not a completed eligibility assessment.

Every observation has `APPROVED=1`; 41 have `REVIEWED=1` and 4,822 have `REVIEWED=0`. The metadata explains that automated acceptance and human review are different statuses. Filtering only for `REVIEWED=1` would improperly remove most accepted observations. The existing approval filter is appropriate for the selected model.

There are 29 presence-only `X` counts, all supported by the importer. All other counts parse as positive numbers. Exotic codes are blank in 4,626 rows, `N` in 227, and `X` in ten. Exotic status does not, by itself, establish ABA countability.

## Information available

| Planner need | Fields present | Assessment |
|---|---|---|
| Species and taxonomic rollup | `CATEGORY`, scientific/common names, subspecies names, `TAXON CONCEPT ID` | Sufficient for an external reviewed crosswalk; concept IDs can identify a subspecies/form rather than its parent |
| Date-specific availability | `OBSERVATION DATE` | Exact reported dates; no interpolation needed |
| Location and geographic filtering | Locality ID/type/name, latitude, longitude, country/state/county codes | Sufficient point metadata; not proof of terrestrial placement, public access or species-level detection coordinates |
| Evidence and shared-event handling | Global observation identifier, sampling event identifier, group identifier | Complete in this sample and compatible with checklist audit links |
| Validation and exotic screening | `APPROVED`, `REVIEWED`, `EXOTIC CODE` | Present; ABA decisions remain an external policy layer |
| Effort and protocol context | Start time, duration, effort distance, observation type, protocol name/code, complete-list flag | Present, with some missing effort; useful provenance, not reconstructed search/detection times |

Of the 313 observation checklists, 165 are traveling, 78 stationary, and 70 incidental. Sampling durations range from one to 302 minutes where supplied, and the maximum reported effort distance is 28.425 km. Duration is absent from 73 sampling records, and effort distance from 151. These omissions do not block the current explicitly assumed per-stop birding duration.

The metadata states that GPS tracks are not publicly supplied. A traveling checklist's coordinate is therefore an approximate search-location anchor, not an exact coordinate and time for each observed bird. The model's optimistic whole-day availability and spatial-precision limitation remain necessary. The metadata also describes sensitive/nonpublic records omitted from standard exports; results must be scoped to the supplied dataset.

## Issues found and addressed

**Domestic-category feral forms need specific decisions.** The sample contains 28 Rock Pigeon (Feral Pigeon) records with `CATEGORY=domestic`, `TAXON CONCEPT ID=avibase-533DD36C`, and exotic code `N`. The initial blanket domestic exclusion would discard them. Cornell explicitly distinguishes this countable feral form from ordinary domestic types, and ABA lists established Rock Pigeon populations across the ABA Area. This does not make every domestic record eligible: the sample's nine domestic Muscovy Duck and one domestic Mallard records have exotic code `X`.

The importer now supports an optional `source_taxon_id` crosswalk qualifier. An exact qualified mapping takes precedence over the generic parent-name mapping; domestic-category records require that exact qualified decision plus all ordinary eligibility, geographic and exotic checks. Unqualified domestic records are reported as `domestic_taxon_review`. The independent validator implements the same policy independently. Regression tests confirm that parent-name approval cannot admit an unspecified domestic form, escapees remain excluded, an exact denial overrides generic approval, and an explicitly permitted feral form can be audited through a saved route.

**Additional provenance is retained.** Original taxon concept and subspecies names, county/locality type, observation/protocol fields, start time, duration, effort distance and human-review status are now preserved in the evidence Parquet. The modern `OBSERVATION TYPE` and older `PROTOCOL TYPE` headers are supported. The new fields are optional, and an existing saved 2024 demonstration still passes the updated validator.

**The 2025 header revision is compatible.** This sample uses `OBSERVATION TYPE`, `PROTOCOL NAME`, `PROJECT NAMES`, `PROJECT IDENTIFIERS`, and `CHECKLIST COMMENTS`. The parser identifies required columns by name and tolerates the final empty tab-delimited column. No older project/comment header is required by the search model.

## Import verification and boundaries

The production importer was run over both complete files using a crosswalk that marks all countability decisions as pending review and an empty land catalog. It preserved all 4,863 input rows in the staged/audit path, reporting 4,817 rows for ABA review, 38 domestic-form reviews and eight unresolved rows. It intentionally credited zero species; no land or ABA decisions were fabricated to obtain a positive test result.

Twenty tests pass after the compatibility changes, including end-to-end synthetic fixtures for exact feral-form qualification. The sample itself has not been used to claim an optimized real route.

The full-year trial still needs a reviewed taxonomy/ABA crosswalk, terrestrial classifications and real road routing costs. EBD provides observed checklist effort, not road distances/times between stops. Keep the matching sampling file, metadata and release citation when requesting the full 2023/2024 U.S. download.

## Audit and sources

Archive SHA-256: `992e9b3e2fffbc19b0853be5d747fc0c609a1f217512f89864b5049d18515b19`.

The source zip remains in the user's Downloads directory. Extracted data, metadata page renders, `review-summary.json`, the pending-review inputs and import outputs are under the Git-ignored `bigyear/data/ebd-sample/`. No bulk records or observer identifiers are included in this report or committed to Git.

- Bundled EBD metadata v1.16: taxonomy/concept definitions on pp. 5–7, sampling-data semantics on p. 4, location/date fields on pp. 9–10, approval/group/effort definitions on pp. 12–13.
- [Cornell's taxonomy documentation](https://support.ebird.org/en/support/solutions/articles/48000837816-the-ebird-taxonomy): domestic-category exceptions and feral Rock Pigeons.
- [ABA introduced-species guidance](https://www.aba.org/aba-area-introduced-species/): established population context, including Rock Pigeon.
- [Input contract](DATA.md): updated crosswalk qualification and retained provenance.
