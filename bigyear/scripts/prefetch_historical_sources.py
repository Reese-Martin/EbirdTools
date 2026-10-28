"""Read-only publisher prefetch for a measured intermediate calendar.

This does not certify a candidate or assign species credit. Final materialization
checks every selected record independently, including all changed visits.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
from pathlib import Path
import ssl
import urllib.request

from bigyear_planner.compact_model import load
from bigyear_planner.eod import attach_dataset
from bigyear_planner.util import ManagedDuckDB, q, read, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--checkpoint',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    model = load(args.model)
    try:
        checkpoint = read(args.checkpoint)
        if checkpoint['model_sha256']!=sha256(model.path):
            raise ValueError('prefetch checkpoint/model differs')
        origin = date(model.meta['config']['year'],1,1)
        joint = model.meta['joint_marine']
        catalog = read(joint['catalog_path'])
        trips = {t['id']:t for t in catalog['trips']}
        land = set()
        marine = set()
        for i in checkpoint['ids']:
            row = model.records[i]
            variant = int(row['reserved'])
            if not variant:
                land.add((str(origin+timedelta(days=int(row['day']))),model.locations[int(row['location'])]['id']))
            else:
                trip = trips[joint['trip_ids'][variant-1]]
                for obs in trip['observations']:
                    if trip.get('kind')=='island_landing':
                        land.add((trip['date'],obs['source_location']['id']))
                    else:
                        marine.add((trip['date'],obs['latitude'],obs['longitude']))
        keys = set()
        with ManagedDuckDB() as con:
            con.execute("SET memory_limit='1GB'")
            con.execute('SET threads=1')
            evidence,_ = attach_dataset(con,model.meta['dataset'])
            con.execute('CREATE TABLE wanted(date DATE,location_id VARCHAR)')
            if land:
                con.executemany('INSERT INTO wanted VALUES (?,?)',sorted(land))
                keys.update(r[0].removeprefix('GBIF:') for r in con.execute(
                    f'SELECT DISTINCT record_id FROM {evidence} JOIN wanted USING(date,location_id)').fetchall())
            if marine:
                con.execute('CREATE TABLE marine(date DATE,latitude VARCHAR,longitude VARCHAR)')
                con.executemany('INSERT INTO marine VALUES (?,?,?)',sorted(marine))
                keys.update(r[0] for r in con.execute(f'''SELECT DISTINCT representative_gbifid
                    FROM read_parquet({q(Path(joint['marine_data'])/'offshore.parquet')}) r
                    JOIN marine k ON r.date=k.date AND r.decimallatitude=k.latitude
                    AND r.decimallongitude=k.longitude
                    WHERE r.marine_disposition='policy_eligible_pending_marine_access' ''').fetchall())
        target = args.output/'publisher-verbatim'
        target.mkdir(exist_ok=True)
        trust = ssl.create_default_context(cafile='/etc/ssl/cert.pem') if Path('/etc/ssl/cert.pem').exists() else ssl.create_default_context()
        def fetch(key):
            path = target/(key+'.json')
            if path.exists():
                return True
            partial = path.with_suffix('.prefetch.part')
            try:
                with urllib.request.urlopen('https://api.gbif.org/v1/occurrence/'+key+'/verbatim',timeout=35,context=trust) as response:
                    payload = response.read()
                if str(json.loads(payload).get('key'))!=key:
                    return False
                partial.write_bytes(payload)
                # A final source check may have fetched this record while the
                # prefetch was in flight. Never overwrite its frozen payload.
                try:
                    path.hardlink_to(partial)
                except FileExistsError:
                    pass
                partial.unlink()
                return True
            except Exception:
                partial.unlink(missing_ok=True)
                return False
        print(json.dumps({'stage':'publisher-prefetch','records':len(keys),'candidate_verified':False}),flush=True)
        completed = supported = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for success in pool.map(fetch,sorted(keys)):
                completed += 1
                supported += success
                if completed%100==0:
                    print(json.dumps({'fetched':completed,'available':supported,'total':len(keys)}),flush=True)
        print(json.dumps({'stage':'publisher-prefetch-complete','records':len(keys),'available':supported}),flush=True)
    finally:
        model.close()


if __name__=='__main__':
    main()
