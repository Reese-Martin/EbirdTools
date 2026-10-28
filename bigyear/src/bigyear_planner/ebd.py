"""Direct Parquet EBD adapter with a frozen binding to the current 2024 policy.

The raw archive remains the authority for every row/column. The views below are
scoring projections, not a second bulk copy or a replacement for the archive.
"""

import csv
from pathlib import Path
import shutil
import time

from .countability import region_sql
from .ebd_archive import archive_scan
from .ingest import scan
from .util import ManagedDuckDB, ROOT, dump, ident, q, read, sha256

SOURCE_FORMAT = "EBD_PARQUET"
POLICY = "lower48-2024-primary-plus-documented-provisionals-v3"
DEFAULT_POLICY = ROOT / "data/lower48-aba2024-provisional-v3"


def validate_config(config):
    if config.year != 2024 or config.walking_speed_kmh != 2.2 or config.walking_threshold_m != 100:
        raise ValueError("EBD national import requires 2024, 2.2 km/h walking and 100 m threshold")
    if config.start_date or config.end_date or config.target_locations < 10_000_000:
        raise ValueError("require full-year configuration and complete national candidate capacity")
    if ((config.activity_minutes, config.birding_minutes, config.local_miles) != (720, 120, 30)
        or config.drive_minutes not in ((480,720) if config.mixed_road_birding else (480,))):
        raise ValueError("retain 12 h activity, 2 h birding and the legacy/boat 30-mile setting; mixed_road_birding allows the shared 12 h driving budget, or the earlier 8 h control")


def verify_policy(root):
    """Check the same frozen evidence and rule sources as the current EOD audit."""
    root = Path(root)
    policy = read(root / "policy.json")
    if policy["id"] != POLICY:
        raise ValueError("the current frozen 2024 countability policy is required")
    for name, digest in policy["decision_files"].items():
        if Path(name).name != name or sha256(root / name) != digest:
            raise ValueError("frozen countability decision changed: " + name)
    for name, digest in policy["sources"].items():
        if Path(name).name != name or sha256(root / "sources" / name) != digest:
            raise ValueError("frozen countability source changed: " + name)
    if sha256(root / "source-retrieval.json") != policy["source_retrieval_sha256"]:
        raise ValueError("frozen countability retrieval changed")
    for key, item in read(root / "source-retrieval.json")["records"].items():
        if Path(key).name != key:
            raise ValueError("invalid publisher evidence key")
        if item.get("retrieved") and sha256(root / "sources/selected-verbatim" / (key + ".json")) != item["sha256"]:
            raise ValueError("frozen publisher evidence changed: " + key)
    return policy


def register_views(con, dataset, manifest, verify_archive=True):
    dataset = Path(dataset)
    for name, digest in manifest["outputs"].items():
        if sha256(dataset / name) != digest:
            raise ValueError("EBD dataset component changed: " + name)
    policy_binding = manifest["inputs"]["policy"]
    if sha256(policy_binding["path"]) != policy_binding["sha256"]:
        raise ValueError("frozen policy binding changed")
    verify_policy(Path(policy_binding["path"]).parent)
    binding = manifest["inputs"]["ebd_archive"]
    archive = Path(binding["path"])
    if sha256(archive / "manifest.json") != binding["sha256"]:
        raise ValueError("EBD archive manifest changed")
    source, archive_meta = archive_scan(archive, verify_archive)
    if archive_meta["kind"] != "ebd":
        raise ValueError("EBD observation archive required")
    con.execute(f"CREATE VIEW ebd_raw AS SELECT * FROM {source}")
    names = set(archive_meta["columns"])

    def col(name, default="''"):
        return "r." + ident(name) if name in names else default

    group = col("GROUP IDENTIFIER")
    sed_join = ""
    if "sed_archive" in manifest["inputs"]:
        sed = manifest["inputs"]["sed_archive"]
        if sha256(Path(sed["path"]) / "manifest.json") != sed["sha256"]:
            raise ValueError("SED archive manifest changed")
        sed_source, sed_meta = archive_scan(sed["path"], verify_archive)
        if sed_meta["kind"] != "sed":
            raise ValueError("SED sampling archive required")
        con.execute(f"CREATE VIEW sed_raw AS SELECT * FROM {sed_source}")
        sed_group = 'nullif("GROUP IDENTIFIER",\'\')' if "GROUP IDENTIFIER" in sed_meta["columns"] else "NULL::VARCHAR"
        con.execute(f'''CREATE TEMP TABLE sed_groups AS SELECT "SAMPLING EVENT IDENTIFIER" AS checklist_id,
            min({sed_group}) AS group_id,count(DISTINCT {sed_group}) AS groups
            FROM sed_raw GROUP BY 1''')
        if con.execute("SELECT count(*) FROM sed_groups WHERE groups>1 OR checklist_id=''").fetchone()[0]:
            raise ValueError("conflicting SED group identifiers or blank checklist ID")
        sed_join = 'LEFT JOIN sed_groups s ON r."SAMPLING EVENT IDENTIFIER"=s.checklist_id'
        if con.execute(f'''SELECT count(*) FROM ebd_raw r {sed_join} WHERE
            coalesce({group},'')!='' AND s.group_id IS NOT NULL AND {group}!=s.group_id''').fetchone()[0]:
            raise ValueError("EBD/SED group identifiers conflict; refusing a fanout or guessed event")
        group = f"coalesce(nullif({group},''),s.group_id,'')"
    con.execute(f"CREATE VIEW ebd_tax AS SELECT * FROM {scan(dataset / 'taxonomy.csv', ',')}")
    con.execute(f"CREATE VIEW ebd_land AS SELECT * FROM {scan(dataset / 'land.csv', ',')}")
    con.execute(f'''CREATE VIEW ebd_scoped AS SELECT
        r._source_row, r."SCIENTIFIC NAME" AS source_name,r."TAXON CONCEPT ID" AS taxon_concept_id,
        r."CATEGORY" AS category,r."OBSERVATION COUNT" AS bird_count,
        r."COUNTRY CODE" AS country,r."STATE CODE" AS state,r."LOCALITY ID" AS locality_id,
        {col("LOCALITY")} AS locality,r."LATITUDE" AS latitude_text,r."LONGITUDE" AS longitude_text,
        try_cast(r."LATITUDE" AS DOUBLE) AS lat,try_cast(r."LONGITUDE" AS DOUBLE) AS lon,
        CAST(r."OBSERVATION DATE" AS DATE) AS date,
        r."SAMPLING EVENT IDENTIFIER" AS checklist_id,{group} AS group_id,
        r."GLOBAL UNIQUE IDENTIFIER" AS record_id,r."APPROVED" AS approved,
        {col("EXOTIC CODE")} AS exotic_code,{col("REVIEWED")} AS reviewed,
        {col("PROTOCOL CODE")} AS protocol_code,{col("PROTOCOL TYPE")} AS protocol_type,
        {col("DURATION MINUTES")} AS duration_minutes,{col("EFFORT DISTANCE KM")} AS effort_distance_km,
        {col("ALL SPECIES REPORTED")} AS all_species_reported,
        {col("SPECIES COMMENTS")} AS species_comments,{col("TRIP COMMENTS")} AS trip_comments,
        sha256(to_json([r."LOCALITY ID",r."LATITUDE",r."LONGITUDE",r."STATE CODE"])) AS location_id
        FROM ebd_raw r {sed_join}''')
    # Source-specific GBIF record approvals do not transfer to different EBD records.
    # Only exact frozen name + Avibase concept matches are eligible automatically.
    con.execute(f'''CREATE VIEW ebd_classified AS SELECT r.*,t.species_id,t.common_name,t.reference,
        coalesce(t.score_tier,'held') AS score_tier,
        CASE
          WHEN r.approved NOT IN ('1','true','TRUE') THEN 'not_validated'
          WHEN r.checklist_id='' OR r.locality_id='' OR r.record_id='' THEN 'missing_identifier'
          WHEN r.lat IS NULL OR r.lon IS NULL OR NOT isfinite(r.lat) OR NOT isfinite(r.lon)
               OR r.lat NOT BETWEEN -90 AND 90 OR r.lon NOT BETWEEN -180 AND 180 THEN 'invalid_coordinates'
          WHEN upper(r.bird_count)!='X' AND
               (try_cast(r.bird_count AS DOUBLE) IS NULL OR NOT isfinite(try_cast(r.bird_count AS DOUBLE))
                 OR try_cast(r.bird_count AS DOUBLE)<=0) THEN 'not_presence'
          WHEN r.category NOT IN ('species','issf','subspecies','form','domestic') THEN 'unresolved_or_hybrid'
          WHEN r.exotic_code='X' OR regexp_matches(lower(r.species_comments),
              'captive (bird|individual)|escaped (pet|from)|in an? aviary|rehabilitat') THEN 'wildness_review'
          WHEN t.source_name IS NULL THEN 'unmapped_taxon_review'
          WHEN t.aba_eligible='0' THEN 'aba_ineligible'
          WHEN coalesce(t.aba_eligible,'review')!='1' OR coalesce(t.eod_presence_eligible,'review')!='1'
               OR t.score_tier='held' THEN 'countability_review'
          WHEN t.eligible_states!='*' AND NOT list_contains(string_split(t.eligible_states,'|'),r.state)
               THEN 'regional_eligibility_review'
          WHEN NOT {region_sql()} THEN 'population_or_provenance_review'
          WHEN r.exotic_code NOT IN ('','N','P') OR
               (r.exotic_code='P' AND NOT starts_with(t.score_tier,'provisional')) THEN 'exotic_record_review'
          ELSE 'countable' END AS scoring_disposition,
        CASE WHEN l.locality_id IS NULL THEN 'unknown' WHEN l.on_land='1' THEN 'land'
             ELSE 'marine_or_island_access_review' END AS access_disposition
        FROM ebd_scoped r LEFT JOIN ebd_tax t ON r.source_name=t.source_name
          AND r.taxon_concept_id=t.source_taxon_id AND t.source_taxon_id!=''
        LEFT JOIN ebd_land l ON r.locality_id=l.locality_id
          AND r.latitude_text=l.latitude AND r.longitude_text=l.longitude''')
    con.execute('''CREATE VIEW ebd_countable_evidence AS SELECT *,
        CASE WHEN group_id!='' THEN 'group:'||group_id ELSE 'checklist:'||checklist_id END AS event_id,
        1::UBIGINT AS source_record_count FROM ebd_classified WHERE scoring_disposition='countable' ''')
    con.execute('''CREATE VIEW dataset_evidence AS SELECT * FROM ebd_countable_evidence
        WHERE access_disposition='land' ''')
    # One derived row per original observation, including duplicates and shared lists.
    # The compact model alone coalesces annual species credit at a dated stop.
    con.execute('''CREATE VIEW dataset_observations AS SELECT date,location_id,locality_id,
        locality,lat,lon,state,species_id,common_name,event_id,[checklist_id] AS checklists,
        1::UBIGINT AS source_rows,latitude_text,longitude_text FROM dataset_evidence''')


def prepare(archive, land, output, release, config, policy_dir=DEFAULT_POLICY, sed=None, *, synthetic=False):
    started = time.monotonic()
    archive, land, output = Path(archive).resolve(), Path(land).resolve(), Path(output).resolve()
    policy_dir = Path(policy_dir).resolve()
    validate_config(config)
    policy = verify_policy(policy_dir)
    _, source_meta = archive_scan(archive)
    if source_meta["kind"] != "ebd" or release != source_meta["source"]["release"]:
        raise ValueError("EBD release label differs from the verified archive")
    if output.exists():
        raise ValueError("dataset output exists; use a new directory")
    output.mkdir(parents=True)
    # Freeze all decision ledgers for provenance, but never apply GBIF occurrence
    # IDs as if they were EBD observations.
    for name in ["policy.json", *policy["decision_files"]]:
        shutil.copyfile(policy_dir / name, output / name)
    with (output / "taxonomy.csv").open(newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        taxa = list(reader)
    for taxon in taxa:
        taxon["source_taxon_id"] = taxon["source_taxon_id"].replace("avibase-avibase-", "avibase-", 1)
    keys = [(t["source_name"], t["source_taxon_id"].lower()) for t in taxa]
    if len(keys) != len(set(keys)):
        raise ValueError("ambiguous normalized EBD taxonomy crosswalk")
    with (output / "taxonomy.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(taxa)
    shutil.copyfile(land, output / "land.csv")
    manifest = {"schema": 3, "source_format": SOURCE_FORMAT, "year": 2024,
        "release": release, "taxonomy_policy": POLICY, "synthetic": synthetic,
        "inputs": {"ebd_archive": {"path": str(archive), "sha256": sha256(archive / "manifest.json")},
                   "taxonomy": {"path": str(output / "taxonomy.csv"), "sha256": sha256(output / "taxonomy.csv")},
                   "policy": {"path": str(policy_dir / "policy.json"), "sha256": sha256(policy_dir / "policy.json")}},
        "config": config.to_dict(),
        "countability": {"adapter": "ebd-parquet-v1", "policy_id": POLICY, "policy_sha256": sha256(output / "policy.json"),
            "mapping": "exact frozen scientific name and Avibase concept; unmatched taxa held",
            "record_decisions": "GBIF per-occurrence approvals are retained as provenance, not transferred"},
        "limitations": ["Reported coordinate strings preserved; checklist points do not certify individual bird positions.",
            "All rows/columns remain in the archive, including duplicates, held taxa and marine observations.",
            "Approved is a publisher-review proxy; REVIEWED=1 and complete checklists are not required.",
            "Taxonomy-release changes require explicit reconciliation; no fuzzy names or automatic lump/split.",
            "Land is an access lane, not a countability rule. Marine/island rows require vessel/landing validation.",
            "Sensitive/generalized locations are not reconstructed or expanded."]}
    if sed:
        sed = Path(sed).resolve()
        _, sed_meta = archive_scan(sed)
        if sed_meta["kind"] != "sed" or sed_meta["source"]["release"] != release:
            raise ValueError("SED must use the same release and cohort as EBD")
        manifest["inputs"]["sed_archive"] = {"path": str(sed), "sha256": sha256(sed / "manifest.json")}
    manifest["outputs"] = {p.name: sha256(p) for p in output.iterdir() if p.is_file()}
    with ManagedDuckDB() as con:
        con.execute(f"SET memory_limit={q(config.memory_limit)}")
        con.execute(f"SET threads={config.threads}")
        # Validate land before joins so a duplicate cannot multiply observations.
        con.execute(f"CREATE VIEW land_input AS SELECT * FROM {scan(output / 'land.csv', ',')}")
        bad = con.execute('''SELECT count(*) FROM land_input WHERE coalesce(on_land,'') NOT IN ('0','1')
            OR coalesce(source,'')='' OR try_cast(latitude AS DOUBLE) IS NULL
            OR try_cast(longitude AS DOUBLE) IS NULL''').fetchone()[0]
        duplicate = con.execute('''SELECT count(*) FROM (SELECT locality_id,latitude,longitude
            FROM land_input GROUP BY ALL HAVING count(*)>1)''').fetchone()[0]
        if bad or duplicate:
            raise ValueError("invalid or duplicate exact-point land classification")
        register_views(con, output, manifest, verify_archive=False)
        dispositions = dict(con.execute("SELECT scoring_disposition,count(*) FROM ebd_classified GROUP BY 1").fetchall())
        access = dict(con.execute("SELECT access_disposition,count(*) FROM ebd_classified GROUP BY 1").fetchall())
        rows, locations, species = con.execute('''SELECT count(*),count(DISTINCT location_id),
            count(DISTINCT species_id) FROM dataset_evidence''').fetchone()
        actual = con.execute("SELECT count(*) FROM ebd_classified").fetchone()[0]
        if actual != source_meta["rows"] or sum(dispositions.values()) != actual:
            raise ValueError("EBD classification did not preserve all source observations")
        manifest["statistics"] = {"input_rows": source_meta["input_rows"], "year_geography_rows": actual,
            "scoring_dispositions": dispositions, "access_dispositions": access,
            "dispositions": {"eligible": rows, "held_or_access_pending": actual-rows},
            "locations": locations, "annual_recorded_species_ceiling": species,
            "countable_species_before_access": con.execute("SELECT count(DISTINCT species_id) FROM ebd_countable_evidence").fetchone()[0],
            "shared_group_rows": con.execute("SELECT count(*) FROM ebd_scoped WHERE group_id!=''").fetchone()[0],
            "missing_sed_checklist_rows": (con.execute("SELECT count(*) FROM ebd_scoped r LEFT JOIN sed_groups s USING(checklist_id) WHERE s.checklist_id IS NULL").fetchone()[0] if sed else None),
            "duplicate_record_id_rows": con.execute("SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM ebd_raw GROUP BY \"GLOBAL UNIQUE IDENTIFIER\" HAVING count(*)>1)").fetchone()[0]}
        manifest['statistics']['state_month_coverage'] = [
            {'state': state, 'month': month, 'observations': n}
            for state, month, n in con.execute('''SELECT state,month(date),count(*)
                FROM ebd_scoped GROUP BY 1,2 ORDER BY 1,2''').fetchall()]
        manifest['statistics']['scoring_tiers_before_access'] = dict(con.execute('''
            SELECT score_tier,count(DISTINCT species_id) FROM ebd_countable_evidence GROUP BY 1''').fetchall())
        con.execute(f'''COPY (SELECT source_name,taxon_concept_id,scoring_disposition,
            count(*) AS observations,min(date) AS first_date,max(date) AS last_date
            FROM ebd_classified WHERE scoring_disposition!='countable' GROUP BY 1,2,3)
            TO {q(output / 'review.csv')} (HEADER,DELIMITER ',')''')
        con.execute(f'''COPY (SELECT source_name,taxon_concept_id,species_id,common_name,score_tier,
            scoring_disposition,access_disposition,count(*) AS observations,
            min(date) AS first_date,max(date) AS last_date
            FROM ebd_classified GROUP BY 1,2,3,4,5,6,7 ORDER BY 1,2,6,7)
            TO {q(output / 'taxon-inventory.csv')} (HEADER,DELIMITER ',')''')
    manifest["outputs"]["review.csv"] = sha256(output / "review.csv")
    manifest['outputs']['taxon-inventory.csv'] = sha256(output/'taxon-inventory.csv')
    manifest['preparation_seconds'] = time.monotonic() - started
    dump(output / "manifest.json", manifest)
    return manifest


def classify_land(archive, geojson, output):
    """Stream unique verbatim points; outside the mask is retained for access review."""
    from shapely.geometry import Point, box, shape
    from .eod import polygon_mask

    output = Path(output)
    if output.exists():
        raise ValueError("land output exists; choose a new path")
    source, _ = archive_scan(archive)
    geojson = Path(geojson)
    tiled = geojson.suffix.lower() == '.shp'
    mask = None if tiled else polygon_mask(geojson)
    provenance = "land-mask-sha256:" + sha256(geojson)
    if tiled:
        import shapefile
        from shapely.ops import unary_union
        # Binding includes both geometry and its index; no full-world union.
        provenance += ";shx-sha256:" + sha256(geojson.with_suffix('.shx'))
    pending = output.with_name(output.name + ".pending")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ManagedDuckDB() as con, pending.open("w", newline="") as f:
            con.execute("SET memory_limit='1GB'")
            con.execute("SET threads=1")
            con.execute(f'''CREATE TEMP TABLE land_points AS SELECT DISTINCT
                "LOCALITY ID" AS id,"LATITUDE" AS lat_text,"LONGITUDE" AS lon_text,
                try_cast("LATITUDE" AS DOUBLE) AS lat,try_cast("LONGITUDE" AS DOUBLE) AS lon
                FROM {source} WHERE try_cast("LATITUDE" AS DOUBLE) BETWEEN -90 AND 90
                AND try_cast("LONGITUDE" AS DOUBLE) BETWEEN -180 AND 180''')
            cells = con.execute('SELECT DISTINCT floor(lon/5)*5,floor(lat/5)*5 FROM land_points ORDER BY 1,2').fetchall() if tiled else [(None, None)]
            writer = csv.writer(f)
            writer.writerow(["locality_id", "latitude", "longitude", "on_land", "source"])
            for x, y in cells:
                if tiled:
                    bounds = (x-1e-7, y-1e-7, x+5+1e-7, y+5+1e-7)
                    with shapefile.Reader(shp=str(geojson), shx=str(geojson.with_suffix('.shx'))) as reader:
                        clip = box(*bounds)
                        mask = unary_union([shape(s.__geo_interface__).intersection(clip)
                                            for s in reader.iterShapes(bbox=bounds)])
                    result = con.execute('SELECT * FROM land_points WHERE lon>=? AND lon<? AND lat>=? AND lat<?', [x, x+5, y, y+5])
                else:
                    result = con.execute('SELECT * FROM land_points')
                for batch in iter(lambda: result.fetchmany(10000), []):
                    for locality, lat_text, lon_text, lat, lon in batch:
                        writer.writerow([locality, lat_text, lon_text, int(mask.covers(Point(lon, lat))), provenance])
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
