"""Bounded, read-only 2024 national sizing audit; never launches a route search."""
import json
import shutil
import time

from bigyear_planner.config import STATES
from bigyear_planner.eod import STATE_NAMES
from bigyear_planner.eod_archive import archive_scan
from bigyear_planner.util import ManagedDuckDB, ROOT, csv_rows, dump, hardware, q, read, sha256, write_csv

OUT = ROOT/'runs/nationwide-readiness-2024'
ARCHIVE = ROOT/'data/eod-us-since-2000-zstd3'
WEST = ROOT/'data/west-coast-eod-2024'


def main():
    started = time.monotonic()
    OUT.mkdir(parents=True, exist_ok=True)
    source, archive = archive_scan(ARCHIVE)
    lower48_names = {STATE_NAMES[code.removeprefix('US-')] for code in STATES}
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'")
        con.execute('SET threads=2')
        con.execute(f'CREATE VIEW source2024 AS SELECT * FROM {source} WHERE year=2024')
        rows = con.execute('''SELECT stateprovince,count(*),sum(source_record_count),
             count(DISTINCT (decimallatitude,decimallongitude)),
             count(DISTINCT (v_scientificname,v_taxonconceptid)),
             count(DISTINCT (year,month,day)),
             count(*) FILTER (WHERE coalesce(geospatial_flagged_records,1)!=0
                                  OR coalesce(geospatial_unknown_records,1)!=0)
             FROM source2024 GROUP BY stateprovince ORDER BY stateprovince''').fetchall()
        fields = ['state','presence_groups','represented_observations','exact_coordinate_pairs',
                  'taxon_concepts','observed_dates','spatial_quality_review_groups']
        states = [dict(zip(fields, row)) for row in rows]
        write_csv(OUT/'2024-source-state-counts.csv', states, fields)
        names = ','.join(q(name) for name in sorted(lower48_names))
        taxa = con.execute(f'''SELECT v_scientificname,v_taxonconceptid,count(*),
             sum(source_record_count) FROM source2024 WHERE stateprovince IN ({names})
             GROUP BY 1,2 ORDER BY 1,2''').fetchall()
        taxon_fields = ['source_name','source_taxon_id','presence_groups','represented_observations']
        taxon_rows = [dict(zip(taxon_fields,row)) for row in taxa]
        write_csv(OUT/'2024-lower48-source-taxa.csv',taxon_rows,taxon_fields)
    selected = [row for row in states if row['state'] in lower48_names]
    totals = {key:sum(row[key] for row in selected) for key in
              ['presence_groups','represented_observations','exact_coordinate_pairs','spatial_quality_review_groups']}
    totals['taxon_concepts'] = len(taxa)
    west = read(WEST/'coverage-audit.json')
    graph = read(WEST/'road-network.json')
    measured = read(ROOT/'runs/west-coast-walking-2p2-2024/walking-full-search-resources.json')
    measured_hardware = read(ROOT/'runs/west-coast-walking-2p2-2024/mmap-600s/benchmark.json')['hardware']
    known = {(r['source_name'],r['source_taxon_id']) for r in csv_rows(WEST/'taxonomy.csv')}
    unseen = [r for r in taxon_rows if (r['source_name'],r['source_taxon_id']) not in known]
    write_csv(OUT/'taxa-not-in-western-review.csv',unseen,taxon_fields)
    result = {
        'ready_for_full_national_search':False,'national_search_started':False,'year':2024,
        'audit_scope':'Raw 2024 source-state cohorts, before national polygon, land, taxonomy and access decisions. These are sizing counts, not eligible planner counts.',
        'lower48_dc_raw_totals':totals,
        'alaska_hawaii_raw_counts':[r for r in states if r['state'] in {'Alaska','Hawaii'}],
        'planner_geographic_support':'Lower 48 plus DC; current geography checks exclude Alaska/Hawaii.',
        'raw_location_scale_vs_west_coast':totals['exact_coordinate_pairs']/west['exact_locations'],
        'new_taxon_concepts_not_in_western_review':len(unseen),
        'national_eligibility_review':'Existing Western taxonomy decisions are scoped to CA/OR/WA and cannot simply be applied nationally.',
        'prepared_graph':'Western US plus a separate Alabama graph; no full-national prepared OSRM graph.',
        'west_graph_extraction_peak_rss_bytes':max(s['peak_rss_bytes'] for s in graph['stages']),
        'latest_west_combined_peak_rss_bytes':measured['combined_peak_rss_bytes'],
        'laptop_physical_ram_bytes':measured_hardware['physical_ram_bytes'],
        'missing_requirements':[
            'A nationwide routing backend that connects regions and supports cross-region roads with strict endpoint evidence.',
            'A national taxonomy/land/access binding under the same presence-only rules.',
            'A measured full-cohort model preparation and loading method that fits the laptop; the current Python opportunity model has only been tested regionally.',
        ],
        'unchanged_rules':{'walking_speed_kmh':2.2,'walking_threshold_m':100,'round_trip':True,
                           'birding_minutes':120,'birding_day_activity_minutes':720,
                           'road_transit_minutes':480,'flights':False,
                           'retain_all_eligible_observations_and_exact_coordinates':True},
        'source_archive_manifest_sha256':sha256(ARCHIVE/'manifest.json'),
        'source_archive_rows':archive['rows'],'source_archive_bytes':archive['bytes'],
        'available_disk_bytes':shutil.disk_usage(ROOT).free,
        'audit_wall_seconds':time.monotonic()-started,'hardware':hardware(),
    }
    dump(OUT/'readiness.json',result)
    print(json.dumps(result),flush=True)


if __name__ == '__main__':
    main()
