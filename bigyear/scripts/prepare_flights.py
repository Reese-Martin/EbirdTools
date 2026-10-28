"""Build dated BTS flights; retain monthly ZIPs for independent source audit."""

from collections import defaultdict
import csv
from datetime import date
import io
import json
import math
import sqlite3
import subprocess
import zipfile

from bigyear_planner.config import STATES
from bigyear_planner.util import ROOT, dump, read, sha256

OUT = ROOT / 'data/lower48-eod-2024/flights-2024'


def download(url, path):
    if path.exists():
        return
    part = path.with_suffix(path.suffix + '.part')
    subprocess.run(['/usr/bin/curl', '-fLsS', '--retry', '2', '--max-time', '180', url, '-o', str(part)], check=True)
    part.replace(path)


def main(year=2024, output=None):
    global OUT
    if output is not None:
        from pathlib import Path
        OUT = Path(output)
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / 'manifest.json').exists():
        frozen = read(OUT/'manifest.json')
        if frozen['year']!=year:
            raise ValueError('existing flight network belongs to a different year')
        for source in frozen['sources']:
            path = OUT/source['file']
            download(source['url'],path)
            if sha256(path)!=source['sha256']:
                raise ValueError('required original flight-audit archive changed: '+path.name)
        print('Frozen flight inputs already exist')
        return
    commit_file = OUT / 'airport-source-commit.json'
    download(f'https://api.github.com/repos/davidmegginson/ourairports-data/commits?path=airports.csv&until={year}-12-31T23%3A59%3A59Z&per_page=1', commit_file)
    commit = read(commit_file)[0]['sha']
    airport_url = f'https://raw.githubusercontent.com/davidmegginson/ourairports-data/{commit}/airports.csv'
    download(airport_url, OUT / 'airports-source.csv')
    airports = {}
    with (OUT / 'airports-source.csv').open() as stream:
        for row in csv.DictReader(stream):
            if row['iso_region'] in STATES and row['iata_code'] and row['scheduled_service'] == 'yes':
                airports[row['iata_code']] = {
                    'id': 'airport:' + row['iata_code'], 'code': row['iata_code'],
                    'name': row['name'], 'lat': float(row['latitude_deg']), 'lon': float(row['longitude_deg']),
                    'state': row['iso_region'], 'kind': 'airport',
                    'coordinate_source': airport_url,
                }
    db = sqlite3.connect(OUT / 'flights.sqlite')
    db.execute('PRAGMA cache_size=-8192')
    db.execute('CREATE TABLE IF NOT EXISTS flights(origin TEXT,destination TEXT,day INTEGER,minutes REAL,operated INTEGER,PRIMARY KEY(origin,destination,day)) WITHOUT ROWID')
    sources = []
    used = set()
    for month in range(1, 13):
        name = f'On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip'
        url = 'https://transtats.bts.gov/PREZIP/' + name
        path = OUT / name
        download(url, path)
        totals = defaultdict(lambda: [0., 0])
        rows = accepted = 0
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith('.csv')]
            if len(members) != 1:
                raise ValueError('ambiguous monthly flight CSV')
            with archive.open(members[0]) as raw, io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as text:
                for row in csv.DictReader(text):
                    rows += 1
                    if row['Origin'] not in airports or row['Dest'] not in airports:
                        continue
                    if float(row['Cancelled'] or 0) or float(row['Diverted'] or 0) or not row['ActualElapsedTime']:
                        continue
                    day = date.fromisoformat(row['FlightDate'][:10])
                    if day.year != year or day.month != month:
                        raise ValueError('flight source date differs from archive label')
                    minutes = float(row['ActualElapsedTime'])
                    if not 0 < minutes <= 1260:
                        continue
                    key = (row['Origin'], row['Dest'], (day-date(year, 1, 1)).days)
                    totals[key][0] += minutes
                    totals[key][1] += 1
                    accepted += 1
        db.executemany('INSERT OR REPLACE INTO flights VALUES (?,?,?,?,?)',
                       [(*key, math.ceil(value[0]/value[1]) + 180, value[1]) for key, value in totals.items()])
        db.commit()
        for a, b, day in totals:
            used.update([a, b])
        sources.append({'url': url, 'file': name, 'sha256': sha256(path), 'bytes': path.stat().st_size,
                        'rows': rows, 'accepted_operated_flights': accepted, 'route_dates': len(totals)})
        dump(OUT / 'preparation-progress.json', {'completed_month': month, 'sources': sources})
        print(json.dumps(sources[-1]), flush=True)
    count = db.execute('SELECT count(*) FROM flights').fetchone()[0]
    db.close()
    dump(OUT / 'airports.json', [airports[k] for k in sorted(used)])
    dump(OUT / 'manifest.json', {
        'schema': 1, 'year': year, 'source': 'BTS Reporting Carrier On-Time Performance, operated non-cancelled non-diverted domestic flight dates',
        'source_documentation': 'https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FGJ&QO_fu146_anzr=b0-gvzr',
        'airport_source': airport_url, 'airports_sha256': sha256(OUT / 'airports.json'),
        'database_sha256': sha256(OUT / 'flights.sqlite'), 'sources': sources,
        'airports': len(used), 'route_dates': count,
        'policy': 'A nonstop is available only on a date with a reported operated flight. Travel minutes equal the ceiling of mean actual block time on that date plus 180 minutes airport overhead. A flight occupies a full nonbirding day, with separate measured access and egress driving days. Rental cars at both ends assumed; no seat, fare, departure-time or overnight-flight reconstruction. Only reporting carriers and scheduled-service airport metadata are represented; this is an incomplete commercial network.'})


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--year', type=int, default=2024)
    parser.add_argument('--output')
    args = parser.parse_args()
    main(args.year, args.output)
