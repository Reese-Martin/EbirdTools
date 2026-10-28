"""Lossless offshore supplement; leave the terrestrial archive/model untouched."""
import json

from bigyear_planner.config import STATES
from bigyear_planner.countability import region_sql
from bigyear_planner.eod import STATE_NAMES
from bigyear_planner.eod_archive import archive_scan
from bigyear_planner.util import ROOT, ManagedDuckDB, dump, q, sha256

DATA = ROOT / 'data/marine-2024-charter-v1'
PARENT = ROOT / 'data/lower48-aba2024-provisional-v3/dataset'


def main():
    year = int(json.loads((PARENT/'manifest.json').read_text())['year'])
    DATA.mkdir(parents=True, exist_ok=True)
    target = DATA / 'offshore.parquet'
    if target.exists():
        raise ValueError('marine supplement already exists; preserve it and use another version')
    archive = ROOT / 'data/eod-us-since-2000-zstd3'
    scan, _ = archive_scan(archive, True)
    values = ','.join(f'({q(name)},{q("US-"+code)})' for code, name in STATE_NAMES.items()
                      if 'US-'+code in STATES)
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='2GB'")
        con.execute('SET threads=2')
        con.execute(f'''CREATE VIEW raw_marine AS SELECT a.*,s.state,
          try_cast(a.decimallatitude AS DOUBLE) lat,
          try_cast(a.decimallongitude AS DOUBLE) lon,
          try_cast(concat(a.year,'-',a.month,'-',a.day) AS DATE) date,
          l.on_land,CASE WHEN p.latitude IS NULL THEN 'outside_state_polygon'
                        ELSE 'inside_state_polygon_offshore' END geographic_class
          FROM {scan} a JOIN (VALUES {values}) s(name,state) ON a.stateprovince=s.name
          LEFT JOIN read_csv({q(PARENT/'scope-points.csv')},all_varchar=true) p
            ON p.state=s.state AND p.latitude=a.decimallatitude AND p.longitude=a.decimallongitude
          LEFT JOIN read_csv({q(PARENT/'land.csv')},all_varchar=true) l
            ON l.latitude=a.decimallatitude AND l.longitude=a.decimallongitude
            AND l.locality_id='EOD:'||sha256(to_json([s.state,a.decimallatitude,a.decimallongitude]))
          WHERE a.year={year} AND (p.latitude IS NULL OR l.on_land='0')''')
        con.execute(f'''CREATE VIEW scored AS SELECT r.*,t.species_id,t.common_name,t.score_tier,
          CASE WHEN r.date IS NULL OR NOT isfinite(lat) OR NOT isfinite(lon)
                 OR lat NOT BETWEEN -90 AND 90 OR lon NOT BETWEEN -180 AND 180 THEN 'invalid_coordinates'
            WHEN source_record_count<=0 OR NOT regexp_full_match(r.representative_gbifid,'[0-9]+')
                 THEN 'invalid_source_group'
            WHEN geospatial_flagged_records IS NULL OR geospatial_unknown_records IS NULL
                 OR geospatial_flagged_records!=0 OR geospatial_unknown_records!=0 THEN 'spatial_quality_hold'
            WHEN t.aba_eligible!='1' OR t.eod_presence_eligible!='1' OR t.species_id IS NULL THEN 'taxonomy_hold'
            WHEN t.eligible_states!='*' AND NOT list_contains(string_split(t.eligible_states,'|'),r.state)
                 THEN 'regional_hold'
            WHEN NOT {region_sql()} THEN 'population_hold'
            WHEN coalesce(cd.decision,'') IN ('excluded','unresolved') THEN 'record_hold'
            ELSE 'policy_eligible_pending_marine_access' END marine_disposition
          FROM raw_marine r LEFT JOIN read_csv({q(PARENT/'taxonomy.csv')},all_varchar=true) t
            ON r.v_scientificname=t.source_name AND r.v_taxonconceptid=t.source_taxon_id
          LEFT JOIN read_csv({q(PARENT/'countability-evidence.csv')},all_varchar=true) cd
            ON r.representative_gbifid=cd.representative_gbifid
            AND r.v_scientificname=cd.source_name AND r.v_taxonconceptid=cd.source_taxon_id
            AND r.date=try_cast(cd.date AS DATE)
            AND r.decimallatitude=cd.latitude AND r.decimallongitude=cd.longitude''')
        con.execute(f'COPY scored TO {q(target.with_suffix(".part"))} (FORMAT PARQUET, COMPRESSION ZSTD)')
        target.with_suffix('.part').replace(target)
        counts = [dict(zip(['disposition','groups','observations','taxa'],r)) for r in con.execute(
            'SELECT marine_disposition,count(*),sum(source_record_count),count(DISTINCT species_id) FROM scored GROUP BY 1').fetchall()]
        con.execute(f'''COPY (SELECT DISTINCT decimallatitude latitude,decimallongitude longitude,
            lat,lon FROM scored WHERE marine_disposition='policy_eligible_pending_marine_access'
            ORDER BY 1,2) TO {q(DATA/'exact-pins.csv')} (HEADER)''')
    result = {'schema':1,'year':year,'parent_dataset':str(PARENT),
        'parent_manifest_sha256':sha256(PARENT/'manifest.json'),
        'archive_manifest_sha256':sha256(archive/'manifest.json'),
        'offshore_sha256':sha256(target),'exact_pins_sha256':sha256(DATA/'exact-pins.csv'),
        'counts':counts,'source_rows_removed':0,'coordinates_rounded':False,
        'eligibility':'Same 2024 taxonomy, provisional, source-quality and population policy; marine jurisdiction and vessel access checked separately.',
        'access_scenario':'Modeled same-port charters, not proof of operated historical sailing, seat availability, weather or navigational safety.',
        'rules_source':'https://www.aba.org/aba-recording-rules-and-interpretations/'}
    dump(DATA/'manifest.json',result)
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
