"""Measure terrestrial mask exclusions without inventing marine jurisdiction."""
from bigyear_planner.eod_archive import archive_scan
from bigyear_planner.eod import STATE_NAMES
from bigyear_planner.config import STATES
from bigyear_planner.util import ROOT,ManagedDuckDB,q,dump,sha256


def main():
    data=ROOT/'data/lower48-eod-2024';out=ROOT/'runs/lower48-eod-2024/search-calendar-controls-v2/rules-access'
    archive=ROOT/'data/eod-us-since-2000-zstd3'
    scan,_=archive_scan(archive,True)
    values=','.join(f'({q(name)},{q("US-"+code)})' for code,name in STATE_NAMES.items() if 'US-'+code in STATES)
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
        con.execute(f'''CREATE VIEW marine_mask_audit AS SELECT s.state,r.v_scientificname,
           r.source_record_count,CASE WHEN p.latitude IS NULL THEN 'outside_terrestrial_polygon_unassessed'
           WHEN l.on_land='0' THEN 'inside_polygon_offshore'
           WHEN l.on_land='1' THEN 'inside_polygon_on_land' ELSE 'land_unresolved' END AS scope
           FROM {scan} r JOIN (VALUES {values}) s(name,state) ON r.stateprovince=s.name
           LEFT JOIN read_csv({q(data/'dataset/scope-points.csv')},all_varchar=true) p
             ON p.state=s.state AND p.latitude=r.decimallatitude AND p.longitude=r.decimallongitude
           LEFT JOIN read_csv({q(data/'dataset/land.csv')},all_varchar=true) l
             ON l.latitude=p.latitude AND l.longitude=p.longitude
             AND l.locality_id='EOD:'||sha256(to_json([s.state,p.latitude,p.longitude]))
           WHERE r.year=2024''')
        counts=[dict(zip(['scope','source_groups','represented_observations','source_taxa'],row)) for row in
          con.execute('SELECT scope,count(*),sum(source_record_count),count(DISTINCT v_scientificname) FROM marine_mask_audit GROUP BY 1').fetchall()]
        con.execute('COPY (SELECT scope,v_scientificname,count(*) source_groups,sum(source_record_count) represented_observations FROM marine_mask_audit GROUP BY 1,2 ORDER BY 1,2) TO ? (HEADER)',[str(out/'marine-mask-species.csv')])
    dump(out/'marine-cohort.json',{'raw_archive_sha256':sha256(archive/'manifest.json'),
      'scope_mask_sha256':sha256(data/'dataset/scope-points.csv'),'counts':counts,
      'aba_rule_source':'https://www.aba.org/aba-recording-rules-and-interpretations/',
      'limitations':['Outside terrestrial polygon includes marine, border and geocoding cases; not all are valid Lower-48 marine observations.',
        'State names and source country=US do not establish nearest-land jurisdiction in offshore waters.',
        'The original SQL extract can omit offshore observations without US country assignment.',
        'A complete marine cohort needs maritime jurisdiction and supported vessel/shore access, not merely removal of the land mask.']})
    print(counts,flush=True)


if __name__=='__main__':main()
