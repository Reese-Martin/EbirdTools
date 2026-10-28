"""Direct EOD Parquet views with explicit source-specific eligibility decisions."""

import hashlib
import json
from pathlib import Path
import shutil
import time


from .config import STATES
from .countability import region_sql
from .eod_archive import archive_scan
from .ingest import scan
from .util import ManagedDuckDB, csv_rows, dump, header, q, read, sha256, write_csv


SOURCE_FORMAT = "EOD_DAILY_PRESENCE"
ADAPTER_VERSION = "eod-presence-v1"
STATE_NAMES = dict(
    zip(
        "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split(),
        "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming".split(
            "|"
        ),
    )
)


def locality_id(state, latitude_text, longitude_text):
    raw = json.dumps([state, latitude_text, longitude_text], separators=(",", ":"))
    return "EOD:" + hashlib.sha256(raw.encode()).hexdigest()


def polygon_mask(path):
    from shapely.geometry import shape
    from shapely.ops import unary_union

    raw = read(path)
    if raw.get("type") == "FeatureCollection":
        geometries = [f["geometry"] for f in raw["features"]]
    elif raw.get("type") == "Feature":
        geometries = [raw["geometry"]]
    else:
        geometries = [raw]
    mask = unary_union([shape(g) for g in geometries])
    if mask.geom_type not in {"Polygon", "MultiPolygon"} or mask.is_empty or not mask.is_valid:
        raise ValueError("scope/land mask must contain valid EPSG:4326 polygons")
    xmin, ymin, xmax, ymax = mask.bounds
    if not (-180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90):
        raise ValueError("scope/land mask is outside EPSG:4326 bounds")
    return mask


def scoped_points(con, source, year, geojson=None):
    """Small exact-coordinate catalog; observation rows stay in the original archive."""
    from shapely import intersects_xy

    mask = polygon_mask(geojson) if geojson else None
    bounds = ""
    if mask is not None:
        xmin, ymin, xmax, ymax = mask.bounds
        bounds = (f" AND try_cast(r.decimallatitude AS DOUBLE) BETWEEN {ymin} AND {ymax}"
                  f" AND try_cast(r.decimallongitude AS DOUBLE) BETWEEN {xmin} AND {xmax}")
    values = ",".join(f"({q(name)},{q('US-' + code)})" for code, name in STATE_NAMES.items())
    states = ",".join(q(s) for s in sorted(STATES))
    result = con.execute(f"""SELECT DISTINCT s.state,r.decimallatitude,r.decimallongitude
        FROM {source} r JOIN (VALUES {values}) s(name,state) ON r.stateprovince=s.name
        WHERE r."year"={int(year)} AND s.state IN ({states}){bounds}
        ORDER BY 1,2,3""")
    for batch in iter(lambda: result.fetchmany(10000), []):
        for state, lat, lon in batch:
            if mask is None or intersects_xy(mask, float(lon), float(lat)):
                yield {"state": state, "latitude": lat, "longitude": lon}


def register_views(con, dataset, manifest, verify_archive=True):
    """Create evidence/observation views, leaving the full cohort in its archive."""
    dataset = Path(dataset)
    if manifest.get("eod_adapter_version") != ADAPTER_VERSION:
        raise ValueError(
            "EOD adapter version differs from the frozen dataset; create a new binding"
        )
    source = manifest["inputs"]["eod_archive"]
    archive = Path(source["path"])
    if sha256(archive / "manifest.json") != source["sha256"]:
        raise ValueError("EOD archive manifest changed")
    raw_scan, _ = archive_scan(archive, verify_archive)
    con.execute(f"CREATE VIEW eod_taxonomy AS SELECT * FROM {scan(dataset / 'taxonomy.csv', ',')}")
    columns = {r[0] for r in con.execute("DESCRIBE eod_taxonomy").fetchall()}
    # Missing EOD-specific review is conservative, never implied by an EBD decision.
    additions = []
    for field in ("source_taxon_id", "eod_presence_eligible", "eligible_regions"):
        if field not in columns:
            additions.append(f"NULL::VARCHAR AS {field}")
    if additions:
        con.execute("CREATE VIEW eod_tax AS SELECT *," + ",".join(additions) + " FROM eod_taxonomy")
    else:
        con.execute("CREATE VIEW eod_tax AS SELECT * FROM eod_taxonomy")
    con.execute(f"CREATE VIEW eod_land AS SELECT * FROM {scan(dataset / 'land.csv', ',')}")
    countability_join = ''
    countability_status = "''"
    if manifest.get('countability'):
        decisions = dataset/'countability-evidence.csv'
        if sha256(decisions)!=manifest['outputs']['countability-evidence.csv']:
            raise ValueError('countability evidence decisions changed')
        con.execute(f"CREATE VIEW countability_decisions AS SELECT * FROM {scan(decisions, ',')}")
        duplicate = con.execute('''SELECT count(*) FROM (SELECT representative_gbifid
            FROM countability_decisions GROUP BY 1 HAVING count(*)!=1)''').fetchone()[0]
        if duplicate:
            raise ValueError('duplicated countability evidence decision')
        countability_join = '''LEFT JOIN countability_decisions cd
            ON r.representative_gbifid=cd.representative_gbifid
            AND r.v_scientificname=cd.source_name AND r.v_taxonconceptid=cd.source_taxon_id
            AND r.date=try_cast(cd.date AS DATE)
            AND r.latitude_text=cd.latitude AND r.longitude_text=cd.longitude'''
        countability_status = "coalesce(cd.decision,'')"
    values = ",".join(f"({q(name)},{q('US-' + code)})" for code, name in STATE_NAMES.items())
    year = int(manifest["year"])
    scope_join = ""
    if manifest.get("geographic_scope"):
        scope_join = (f" JOIN {scan(dataset / 'scope-points.csv', ',')} scope"
                      " ON s.state=scope.state AND r.decimallatitude=scope.latitude"
                      " AND r.decimallongitude=scope.longitude")
    # A county binding is small. Cache its filtered rows in DuckDB's bounded
    # temporary storage so each planner aggregate does not rescan the US archive.
    # Small cohorts benefit from staging; large cohorts use projection/predicate
    # pushdown directly against Parquet instead of spilling full observation rows.
    scoped_relation = ("TEMP TABLE" if scope_join and
                       manifest.get('geographic_scope', {}).get('points', 0) <= 50000 else "VIEW")
    con.execute(f"""CREATE {scoped_relation} eod_scoped AS
        SELECT r.*,s.state, r.decimallatitude AS latitude_text,r.decimallongitude AS longitude_text,
          try_cast(concat(r."year",'-',r."month",'-',r."day") AS DATE) AS date,
          try_cast(r.decimallatitude AS DOUBLE) AS lat,
          try_cast(r.decimallongitude AS DOUBLE) AS lon,
          'EOD:'||sha256(to_json([s.state,r.decimallatitude,r.decimallongitude])) AS locality_id
        FROM {raw_scan} r LEFT JOIN (VALUES {values}) s(name,state) ON r.stateprovince=s.name
        {scope_join}
        WHERE r."year"={year}""")
    states = ",".join(q(s) for s in sorted(STATES))
    con.execute(f"""CREATE VIEW eod_classified AS
        SELECT r.*,t.species_id,t.common_name,t.reference,
          CASE
            WHEN state IS NULL OR state NOT IN ({states}) THEN 'outside_geography'
            WHEN r.date IS NULL THEN 'invalid_date'
            WHEN lat IS NULL OR lon IS NULL OR NOT isfinite(lat) OR NOT isfinite(lon)
                 OR lat NOT BETWEEN 24 AND 50 OR lon NOT BETWEEN -125 AND -66 THEN 'invalid_coordinates'
            WHEN coalesce(source_record_count,0)<=0 OR coalesce(r.representative_gbifid,'')=''
                 OR NOT regexp_full_match(r.representative_gbifid,'[0-9]+') THEN 'invalid_source_group'
            WHEN geospatial_flagged_records IS NULL OR geospatial_unknown_records IS NULL
                 OR geospatial_flagged_records!=0 OR geospatial_unknown_records!=0 THEN 'eod_spatial_quality_review'
            WHEN t.source_name IS NULL THEN 'unmapped_taxon_review'
            WHEN {countability_status}='excluded' THEN 'countability_record_excluded'
            WHEN {countability_status}='unresolved' THEN 'countability_record_unresolved'
            WHEN t.aba_eligible='0' THEN 'aba_ineligible'
            WHEN t.aba_eligible!='1' AND {countability_status}!='supported' THEN 'aba_review'
            WHEN t.eod_presence_eligible='0' AND {countability_status}!='supported' THEN 'eod_metadata_policy_excluded'
            WHEN coalesce(t.eod_presence_eligible,'review')!='1' AND {countability_status}!='supported' THEN 'eod_metadata_policy_review'
            WHEN t.eligible_states!='*' AND NOT list_contains(string_split(t.eligible_states,'|'),state)
                 THEN 'regional_eligibility_review'
            WHEN NOT {region_sql()} THEN 'population_or_provenance_review'
            WHEN l.locality_id IS NULL THEN 'land_unknown_review'
            WHEN l.on_land='0' THEN 'offshore_excluded'
            ELSE 'eligible' END AS disposition
        FROM eod_scoped r LEFT JOIN eod_tax t ON r.v_scientificname=t.source_name
          AND r.v_taxonconceptid=t.source_taxon_id AND coalesce(t.source_taxon_id,'')!=''
        {countability_join}
        LEFT JOIN eod_land l ON r.locality_id=l.locality_id
          AND r.latitude_text=l.latitude AND r.longitude_text=l.longitude""")
    con.execute("""CREATE VIEW dataset_evidence AS SELECT
        v_scientificname AS source_name,NULL::VARCHAR AS category,NULL::VARCHAR AS bird_count,
        'US' AS country,state,locality_id,'EOD '||latitude_text||','||longitude_text AS locality,
        lat,lon,date,'' AS checklist_id,'gbif-group:'||representative_gbifid AS event_id,
        NULL::VARCHAR AS approved,NULL::VARCHAR AS exotic_code,
        'GBIF:'||representative_gbifid AS record_id,v_taxonconceptid AS taxon_concept_id,
        species_id,common_name,reference,latitude_text,longitude_text,source_record_count,
        geospatial_flagged_records,geospatial_unknown_records,max_reported_coordinate_uncertainty_m,
        records_with_reported_coordinate_uncertainty
        FROM eod_classified WHERE disposition='eligible'""")
    con.execute("""CREATE VIEW dataset_observations AS SELECT
        date,locality_id AS location_id,locality_id,locality,lat,lon,state,species_id,common_name,
        event_id,[]::VARCHAR[] AS checklists,source_record_count AS source_rows
        FROM dataset_evidence""")
    con.execute(
        "CREATE VIEW dataset_evidence_with_location AS SELECT *,locality_id AS location_id FROM dataset_evidence"
    )


def prepare_eod(archive, taxonomy, land, output, config, release, taxonomy_policy, synthetic=False,
                scope_geojson=None, scope_label=None):
    started = time.monotonic()
    output, archive = Path(output).resolve(), Path(archive).resolve()
    if output.exists():
        raise ValueError("EOD dataset output must be a new directory")
    mappings = list(csv_rows(taxonomy))
    required = {
        "source_name",
        "species_id",
        "common_name",
        "aba_eligible",
        "eligible_states",
        "reference",
    }
    if not mappings or not required <= set(mappings[0]):
        raise ValueError("taxonomy CSV is missing the documented crosswalk columns")
    seen, labels = set(), {}
    for m in mappings:
        key = (m["source_name"], m.get("source_taxon_id") or "")
        if key in seen or m["aba_eligible"] not in {"0", "1", "review"} or not m["reference"]:
            raise ValueError(
                "taxonomy requires unique source-name/concept keys, decisions and references"
            )
        seen.add(key)
        if m.get("eod_presence_eligible", "review") not in {"0", "1", "review"}:
            raise ValueError("eod_presence_eligible must be 0, 1, or review")
        if m["aba_eligible"] == "1":
            if not all(m[k] for k in ("species_id", "common_name", "eligible_states")):
                raise ValueError(
                    "eligible taxonomy rows require species ID, name and geographic policy"
                )
            if m["species_id"] in labels and labels[m["species_id"]] != m["common_name"]:
                raise ValueError("one species cannot have conflicting common names")
            labels[m["species_id"]] = m["common_name"]
    if not {"locality_id", "latitude", "longitude", "on_land", "source"} <= set(header(land, ",")):
        raise ValueError("land catalog is missing required exact-point columns")
    # Check archive integrity before creating a dataset binding.
    raw_scan, _ = archive_scan(archive)
    output.mkdir(parents=True)
    shutil.copyfile(taxonomy, output / "taxonomy.csv")
    shutil.copyfile(land, output / "land.csv")
    manifest = {
        "schema": 2,
        "source_format": SOURCE_FORMAT,
        "eod_adapter_version": ADAPTER_VERSION,
        "year": config.year,
        "release": release,
        "taxonomy_policy": taxonomy_policy,
        "synthetic": synthetic,
        "inputs": {
            "eod_archive": {"path": str(archive), "sha256": sha256(archive / "manifest.json")},
            "taxonomy": {"path": str(Path(taxonomy).resolve()), "sha256": sha256(taxonomy)},
            "land": {"path": str(Path(land).resolve()), "sha256": sha256(land)},
        },
        "limitations": [
            "EOD presence groups lack original checklist/group/locality IDs, eBird approval, category and exotic code.",
            "Eligibility requires exact source-name/concept mappings and explicit eod_presence_eligible=1 review.",
            "Representative GBIF IDs link one supporting record; source counts are not independent checklist counts.",
            "Coordinate strings remain exact in the archive; numerical routing coordinates use DOUBLE.",
        ],
    }
    with ManagedDuckDB() as con:
        con.execute(f"SET memory_limit={q(config.memory_limit)}")
        con.execute(f"SET threads={config.threads}")
        scope_outputs = ()
        if scope_geojson:
            shutil.copyfile(scope_geojson, output / "scope.geojson")
            point_count = 0
            def scope_rows():
                nonlocal point_count
                for point in scoped_points(con, raw_scan, config.year, output / "scope.geojson"):
                    point_count += 1
                    yield point
            write_csv(output / "scope-points.csv", scope_rows(), ["state", "latitude", "longitude"])
            manifest["geographic_scope"] = {
                "label": scope_label or str(Path(scope_geojson).name),
                "boundary_sha256": sha256(output / "scope.geojson"),
                "points": point_count,
                "boundary_rule": "polygon covers point; boundary points included; no coordinate rounding",
                "travel_rule": "observation stops constrained; road paths may leave the polygon",
            }
            manifest["inputs"]["scope"] = {"path": str(Path(scope_geojson).resolve()),
                                               "sha256": sha256(scope_geojson)}
            scope_outputs = ("scope.geojson", "scope-points.csv")
        register_views(con, output, manifest, verify_archive=False)
        invalid = con.execute("""SELECT count(*) FROM eod_land WHERE
            coalesce(on_land,'') NOT IN ('0','1') OR coalesce(source,'')='' OR
            try_cast(latitude AS DOUBLE) IS NULL OR try_cast(longitude AS DOUBLE) IS NULL""").fetchone()[
            0
        ]
        duplicates = con.execute("""SELECT count(*) FROM (SELECT locality_id,latitude,longitude
            FROM eod_land GROUP BY ALL HAVING count(*)>1)""").fetchone()[0]
        if invalid or duplicates:
            con.close()
            raise ValueError("invalid or duplicate exact-point land classifications")
        dispositions = dict(
            con.execute("SELECT disposition,count(*) FROM eod_classified GROUP BY 1").fetchall()
        )
        con.execute(f"""COPY (SELECT disposition,v_scientificname AS source_name,
            v_taxonconceptid AS source_taxon_id,count(*) AS rows FROM eod_classified
            WHERE disposition!='eligible' GROUP BY ALL ORDER BY 1,2,3)
            TO {q(output / "review.csv")} (HEADER,DELIMITER ',')""")
        counts = con.execute("""SELECT count(DISTINCT species_id),count(DISTINCT locality_id),count(*)
            FROM dataset_observations""").fetchone()
        con.close()
        manifest["statistics"] = {
            "year_rows": sum(dispositions.values()),
            "year_geography_rows": sum(dispositions.values())
            - dispositions.get("outside_geography", 0),
            "dispositions": dispositions,
            "annual_recorded_species_ceiling": counts[0],
            "locations": counts[1],
            "eod_daily_presence_groups": counts[2],
        }
        manifest["outputs"] = {
            name: sha256(output / name)
            for name in ("taxonomy.csv", "land.csv", "review.csv", *scope_outputs)
        }
        manifest["preparation_seconds"] = time.monotonic() - started
        dump(output / "manifest.json", manifest)
        return manifest


def attach_dataset(con, dataset, manifest=None):
    dataset = Path(dataset)
    manifest = manifest or read(dataset / "manifest.json")
    if manifest.get("source_format") == "EBD_PARQUET":
        from .ebd import register_views as register_ebd

        register_ebd(con, dataset, manifest)
        return "dataset_evidence", "dataset_observations"
    if manifest.get("source_format") == SOURCE_FORMAT:
        register_views(con, dataset, manifest)
        return "dataset_evidence_with_location", "dataset_observations"
    con.execute(
        f"CREATE VIEW dataset_evidence AS SELECT * FROM read_parquet({q(dataset / 'evidence.parquet')})"
    )
    con.execute(
        f"CREATE VIEW dataset_observations AS SELECT * FROM read_parquet({q(dataset / 'observations.parquet')})"
    )
    return "dataset_evidence", "dataset_observations"


def classify_land_eod(archive, year, geojson, output, scope_geojson=None):
    """Stream exact EOD points to the standard reviewed land-catalog format."""
    import csv

    from shapely import intersects_xy
    mask = polygon_mask(geojson)
    if Path(output).exists():
        raise ValueError("land output exists; choose a new path")
    source, _ = archive_scan(archive)
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'")
        con.execute("SET threads=2")
        source_label = "polygon-sha256:" + sha256(geojson)
        try:
            with open(output, "w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["locality_id", "latitude", "longitude", "on_land", "source"])
                for point in scoped_points(con, source, year, scope_geojson):
                    state, lat, lon = (point[k] for k in ("state", "latitude", "longitude"))
                    try:
                        inside = bool(intersects_xy(mask, float(lon), float(lat)))
                    except (TypeError, ValueError):
                        inside = False
                    writer.writerow([locality_id(state, lat, lon), lat, lon, int(inside), source_label])
        finally:
            con.close()
