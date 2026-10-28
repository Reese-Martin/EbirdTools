"""Disk-backed EBD processing. Bulk rows stay in DuckDB, never a dataframe."""

import json
import shutil
from pathlib import Path
import time

import duckdb

from .config import STATES
from .util import csv_rows, dump, hardware, header, ident, q, sha256


def scan(path, delimiter="\t"):
    quote = '"' if delimiter == "," else ""
    return (
        f"read_csv({q(Path(path).resolve())}, delim={q(delimiter)}, header=true, "
        f"all_varchar=true, quote={q(quote)}, escape={q(quote)}, null_padding=true, strict_mode=true)"
    )


def prepare(
    ebd, taxonomy, land, output, config, release, taxonomy_policy, sed=None, synthetic=False
):
    started = time.monotonic()
    out = Path(output)
    if out.exists() and any(out.iterdir()):
        raise ValueError("import output must be empty; use a new directory to preserve provenance")
    out.mkdir(parents=True, exist_ok=True)
    required = {
        "SCIENTIFIC NAME",
        "CATEGORY",
        "OBSERVATION COUNT",
        "COUNTRY CODE",
        "STATE CODE",
        "LOCALITY ID",
        "LOCALITY",
        "LATITUDE",
        "LONGITUDE",
        "OBSERVATION DATE",
        "SAMPLING EVENT IDENTIFIER",
        "APPROVED",
    }
    names = set(header(ebd))
    if required - names:
        raise ValueError(f"missing EBD headers: {sorted(required - names)}")
    mappings = list(csv_rows(taxonomy))
    tax_fields = {
        "source_name",
        "species_id",
        "common_name",
        "aba_eligible",
        "eligible_states",
        "reference",
    }
    if not mappings or tax_fields - set(mappings[0]):
        raise ValueError(f"taxonomy CSV requires {sorted(tax_fields)}")
    seen = set()
    for m in mappings:
        key = (m["source_name"], m.get("source_taxon_id", "") or "")
        if key in seen or m["aba_eligible"] not in {"0", "1", "review"} or not m["reference"]:
            raise ValueError(
                "taxonomy requires unique source-name/taxon-ID keys, eligibility 0/1/review, and a reference"
            )
        if m["aba_eligible"] == "1" and (
            not m["species_id"] or not m["common_name"] or not m["eligible_states"]
        ):
            raise ValueError(
                "eligible mappings require species ID, common name and eligible_states (* or US-XX|...)"
            )
        seen.add(key)
    labels = {}
    for m in mappings:
        if m["aba_eligible"] == "1":
            if m["species_id"] in labels and labels[m["species_id"]] != m["common_name"]:
                raise ValueError("each target species must have one common name")
            labels[m["species_id"]] = m["common_name"]
    if not {"locality_id", "latitude", "longitude", "on_land", "source"} <= set(header(land, ",")):
        raise ValueError(
            "land CSV requires locality_id, latitude, longitude, on_land (0/1), source"
        )
    con = duckdb.connect(str(out / "staging.duckdb"))
    con.execute(f"SET memory_limit={q(config.memory_limit)}")
    con.execute(f"SET threads={config.threads}")
    con.execute(f"CREATE TABLE taxonomy AS SELECT * FROM {scan(taxonomy, ',')}")
    if "source_taxon_id" not in mappings[0]:
        con.execute("ALTER TABLE taxonomy ADD COLUMN source_taxon_id VARCHAR")
    con.execute(f"CREATE TABLE land AS SELECT * FROM {scan(land, ',')}")
    bad_land = con.execute("""SELECT count(*) FROM land WHERE try_cast(latitude AS DOUBLE) IS NULL
        OR try_cast(longitude AS DOUBLE) IS NULL OR coalesce(on_land,'') NOT IN ('0','1') OR coalesce(source,'')=''
        """).fetchone()[0]
    duplicate_land = con.execute("""SELECT count(*) FROM (SELECT locality_id, latitude, longitude
        FROM land GROUP BY ALL HAVING count(*)>1)""").fetchone()[0]
    if bad_land or duplicate_land:
        raise ValueError("invalid or duplicate land classifications")
    optional = lambda col, fallback="NULL": ident(col) if col in names else fallback
    group_col = optional("GROUP IDENTIFIER")
    # SED is optional: join by checklist ID, never by coordinates or rounded geography.
    sed_join = ""
    if sed:
        sed_names = set(header(sed))
        if "SAMPLING EVENT IDENTIFIER" not in sed_names:
            raise ValueError("SED is missing SAMPLING EVENT IDENTIFIER")
        group_expr = ident("GROUP IDENTIFIER") if "GROUP IDENTIFIER" in sed_names else "NULL"
        con.execute(f"""CREATE TABLE sed AS SELECT {ident("SAMPLING EVENT IDENTIFIER")} AS checklist_id,
            min({group_expr}) AS group_id FROM {scan(sed)} GROUP BY 1""")
        sed_join = ' LEFT JOIN sed s ON e."SAMPLING EVENT IDENTIFIER"=s.checklist_id '
        group_col = f"coalesce({group_col}, s.group_id)"
    con.execute(f"""CREATE TABLE raw AS SELECT
        {ident("SCIENTIFIC NAME")} AS source_name, lower({ident("CATEGORY")}) AS category,
        {ident("OBSERVATION COUNT")} AS bird_count, {ident("COUNTRY CODE")} AS country,
        {ident("STATE CODE")} AS state, {ident("LOCALITY ID")} AS locality_id,
        {ident("LOCALITY")} AS locality, try_cast({ident("LATITUDE")} AS DOUBLE) AS lat,
        try_cast({ident("LONGITUDE")} AS DOUBLE) AS lon,
        try_cast({ident("OBSERVATION DATE")} AS DATE) AS date,
        {ident("SAMPLING EVENT IDENTIFIER")} AS checklist_id,
        coalesce(nullif({group_col},''), {ident("SAMPLING EVENT IDENTIFIER")}) AS event_id,
        {ident("APPROVED")} AS approved, {optional("EXOTIC CODE")} AS exotic_code,
        {optional("GLOBAL UNIQUE IDENTIFIER")} AS record_id,
        {optional("TAXON CONCEPT ID")} AS taxon_concept_id,
        {optional("SUBSPECIES SCIENTIFIC NAME")} AS subspecies_scientific_name,
        {optional("COUNTY CODE")} AS county_code,
        {optional("LOCALITY TYPE")} AS locality_type,
        {optional("OBSERVATION TYPE", optional("PROTOCOL TYPE"))} AS observation_type,
        {optional("PROTOCOL NAME")} AS protocol_name,
        {optional("PROTOCOL CODE")} AS protocol_code,
        {optional("TIME OBSERVATIONS STARTED")} AS start_time,
        {optional("DURATION MINUTES")} AS duration_minutes,
        {optional("EFFORT DISTANCE KM")} AS effort_distance_km,
        {optional("REVIEWED")} AS reviewed
        FROM {scan(ebd)} e {sed_join}""")
    stats = {"input_rows": con.execute("SELECT count(*) FROM raw").fetchone()[0]}
    states = ",".join(q(s) for s in sorted(STATES))
    con.execute(f"""CREATE TABLE scoped AS SELECT *,
        substr(sha256(concat(locality_id,'|',lat,'|',lon,'|',state)),1,24) AS location_id
        FROM raw WHERE country='US' AND state IN ({states}) AND year(date)={config.year}""")
    stats["year_geography_rows"] = con.execute("SELECT count(*) FROM scoped").fetchone()[0]
    stats["outside_year_geography_or_invalid_date"] = (
        stats["input_rows"] - stats["year_geography_rows"]
    )
    con.execute("""CREATE TABLE classified AS SELECT r.*, t.species_id, t.common_name, t.reference,
        CASE
          WHEN coalesce(approved,'') NOT IN ('1','true','TRUE') THEN 'not_validated'
          WHEN coalesce(checklist_id,'')='' OR coalesce(r.locality_id,'')='' THEN 'missing_identifier'
          WHEN lat IS NULL OR lon IS NULL OR NOT isfinite(lat) OR NOT isfinite(lon)
               OR lat NOT BETWEEN 24 AND 50 OR lon NOT BETWEEN -125 AND -66 THEN 'invalid_coordinates'
          WHEN upper(coalesce(bird_count,'')) != 'X' AND coalesce(try_cast(bird_count AS DOUBLE),0)<=0 THEN 'not_presence'
          WHEN category NOT IN ('species','issf','subspecies','form','domestic') OR category IS NULL THEN 'unresolved_or_hybrid'
          WHEN category='domestic' AND coalesce(t.source_taxon_id,'')='' THEN 'domestic_taxon_review'
          WHEN t.source_name IS NULL THEN 'unmapped_taxon_review'
          WHEN t.aba_eligible='review' THEN 'aba_review'
          WHEN t.aba_eligible='0' THEN 'aba_ineligible'
          WHEN t.eligible_states!='*' AND NOT list_contains(string_split(t.eligible_states,'|'),r.state) THEN 'regional_eligibility_review'
          WHEN coalesce(exotic_code,'') NOT IN ('','N') THEN 'exotic_record_review'
          WHEN l.locality_id IS NULL THEN 'land_unknown_review'
          WHEN l.on_land='0' THEN 'offshore_excluded'
          ELSE 'eligible' END AS disposition
        FROM scoped r LEFT JOIN LATERAL (
          SELECT t.source_name,t.source_taxon_id,t.species_id,t.common_name,
                 t.aba_eligible,t.eligible_states,t.reference
          FROM taxonomy t WHERE t.source_name=r.source_name
          AND (coalesce(t.source_taxon_id,'')='' OR t.source_taxon_id=r.taxon_concept_id)
          ORDER BY (coalesce(t.source_taxon_id,'')!='') DESC LIMIT 1
        ) t ON TRUE
        LEFT JOIN land l ON r.locality_id=l.locality_id
          AND r.lat=try_cast(l.latitude AS DOUBLE) AND r.lon=try_cast(l.longitude AS DOUBLE)""")
    stats["dispositions"] = dict(
        con.execute("SELECT disposition,count(*) FROM classified GROUP BY 1 ORDER BY 1").fetchall()
    )
    con.execute(
        f"COPY (SELECT * FROM classified WHERE disposition!='eligible') TO {q(out / 'excluded.parquet')} (FORMAT PARQUET)"
    )
    con.execute(f"""COPY (SELECT disposition,source_name,category,count(*) AS rows FROM classified
        WHERE disposition!='eligible' GROUP BY ALL ORDER BY 1,2) TO {q(out / "review.csv")} (HEADER, DELIMITER ',')""")
    # All original supporting identifiers survive in evidence; opportunities count a group event once per species.
    con.execute(
        "CREATE TABLE evidence AS SELECT DISTINCT * EXCLUDE(disposition) FROM classified WHERE disposition='eligible'"
    )
    con.execute(
        f"COPY evidence TO {q(out / 'evidence.parquet')} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    con.execute("""CREATE TABLE observations AS SELECT date,location_id,locality_id,locality,lat,lon,state,
        species_id,common_name,event_id, list(DISTINCT checklist_id ORDER BY checklist_id) AS checklists,
        count(*) AS source_rows FROM evidence GROUP BY ALL""")
    con.execute(
        f"COPY observations TO {q(out / 'observations.parquet')} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    stats["independent_species_events"] = con.execute(
        "SELECT count(*) FROM observations"
    ).fetchone()[0]
    stats["shared_or_duplicate_species_rows"] = (
        stats["dispositions"].get("eligible", 0) - stats["independent_species_events"]
    )
    stats["annual_recorded_species_ceiling"] = con.execute(
        "SELECT count(DISTINCT species_id) FROM observations"
    ).fetchone()[0]
    stats["locations"] = con.execute(
        "SELECT count(DISTINCT location_id) FROM observations"
    ).fetchone()[0]
    stats["coverage_by_month"] = [
        dict(zip(("month", "events", "species", "locations"), row))
        for row in con.execute(
            "SELECT month(date),count(DISTINCT event_id),count(DISTINCT species_id),count(DISTINCT location_id) FROM observations GROUP BY 1 ORDER BY 1"
        ).fetchall()
    ]
    stats["coverage_by_state"] = [
        dict(zip(("state", "events", "species"), row))
        for row in con.execute(
            "SELECT state,count(DISTINCT event_id),count(DISTINCT species_id) FROM observations GROUP BY 1 ORDER BY 1"
        ).fetchall()
    ]
    con.close()
    (out / "staging.duckdb").unlink()
    shutil.copyfile(taxonomy, out / "taxonomy.csv")
    shutil.copyfile(land, out / "land.csv")
    inputs = {"ebd": ebd, "taxonomy": taxonomy, "land": land}
    if sed:
        inputs["sed"] = sed
    manifest = {
        "schema": 1,
        "year": config.year,
        "release": release,
        "taxonomy_policy": taxonomy_policy,
        "synthetic": synthetic,
        "inputs": {
            k: {"path": str(Path(p).resolve()), "sha256": sha256(p)} for k, p in inputs.items()
        },
        "outputs": {p.name: sha256(p) for p in out.iterdir() if p.suffix in {".parquet", ".csv"}},
        "statistics": stats,
        "preparation_seconds": time.monotonic() - started,
        "hardware": hardware(),
        "duckdb": duckdb.__version__,
    }
    dump(out / "manifest.json", manifest)
    return manifest


def classify_land(ebd, geojson, output):
    """Classify unique exact points against a user-supplied terrestrial polygon mask."""
    from shapely import intersects_xy
    from shapely.geometry import shape
    from shapely.ops import unary_union

    raw = json.loads(Path(geojson).read_text())
    geometries = (
        [shape(f["geometry"]) for f in raw["features"]]
        if raw.get("type") == "FeatureCollection"
        else [shape(raw)]
    )
    mask = unary_union(geometries)
    if mask.geom_type not in {"Polygon", "MultiPolygon"} or mask.is_empty or not mask.is_valid:
        raise ValueError("land mask must contain valid EPSG:4326 terrestrial polygons")
    con = duckdb.connect()
    mask_hash = sha256(geojson)
    result = con.execute(f'SELECT DISTINCT "LOCALITY ID", "LATITUDE", "LONGITUDE" FROM {scan(ebd)}')
    import csv

    with open(output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["locality_id", "latitude", "longitude", "on_land", "source"])
        while batch := result.fetchmany(10000):
            for loc, lat, lon in batch:
                try:
                    inside = bool(intersects_xy(mask, float(lon), float(lat)))
                except (TypeError, ValueError):
                    inside = False
                w.writerow([loc, lat, lon, int(inside), "polygon-sha256:" + mask_hash])
    con.close()
