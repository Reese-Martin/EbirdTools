"""Read-only missed-species inventory bound to the current verified calendar.

Distances are proposal diagnostics, not driving costs or a feasibility claim.
Full source rows, scores, coordinates, holds and saved routes are unchanged.
"""

from collections import Counter
import csv
from datetime import date, timedelta
import math
from pathlib import Path
import time

import duckdb
import numpy as np

from bigyear_planner.calendar_study import NearbyPolisher
from bigyear_planner.compact_model import load
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv

BASE = ROOT/'runs/lower48-eod-2024/calendar-study-combined-2024-v1'
OUTPUT = ROOT/'runs/lower48-eod-2024/remaining-opportunities-2024-v1'


def distances(points, center):
    a, b = np.radians(points), np.radians(center)
    h = np.sin((a[:, 0]-b[0])/2)**2 + np.cos(a[:, 0])*np.cos(b[0])*np.sin((a[:, 1]-b[1])/2)**2
    return 2*3958.7613*np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def main():
    started = time.monotonic()
    OUTPUT.mkdir(exist_ok=True)
    route = read(BASE/'route.json')
    audit = read(BASE/'validation.json')
    if not audit['valid'] or audit['route_sha256'] != sha256(BASE/'route.json'):
        raise ValueError('current route must have a positive bound independent audit')
    model = load(read(BASE/'problem.json')['compact_source']['path'])
    try:
        if sha256(model.path) != route['provenance']['compact_model_sha256']:
            raise ValueError('route and complete model differ')
        seen = {s for d in route['days'] for stop in d['stops'] for s in stop['species']}
        if len(seen) != route['species_total']:
            raise ValueError('credited species count differs')
        missing = np.array([s['id'] not in seen for s in model.meta['species']])
        nspp = len(missing)
        counts = np.zeros((nspp, 366), dtype='u8')
        support_ids = {int(s): [] for s in np.flatnonzero(missing)}
        # Scan every eligible location/date/species entry in bounded blocks.
        for start in range(0, len(model.species), 4_000_000):
            block = model.species[start:start+4_000_000]
            keep = np.flatnonzero(missing[block])
            if not len(keep):
                continue
            spp = block[keep]
            absolute = keep.astype('u8')+start
            ids = np.searchsorted(model.records['offset'], absolute, side='right')-1
            np.add.at(counts, (spp, model.records['day'][ids]), 1)
            for s in np.unique(spp):
                support_ids[int(s)].extend(map(int, ids[spp == s]))
        source = date(2024, 1, 1)
        anchors, rest, here = {}, {}, route['initial_location']
        visited = []
        for day, entry in enumerate(route['days']):
            if entry['date'] != str(source+timedelta(days=day)):
                raise ValueError('calendar day indexing differs')
            if entry['kind'] == 'birding':
                anchors[day] = [[s['lat'], s['lon']] for s in entry['stops']]
                visited.extend(anchors[day])
            if entry['kind'] == 'rest' and here is not None:
                point = route['locations'][here]
                rest[day] = [point['lat'], point['lon']]
            if entry['legs']:
                last = entry['legs'][-1]
                here = last['to'] if last['end_fraction'] == 1 else None
            if entry['kind'] == 'birding' and entry['stops']:
                here = entry['stops'][-1]['location']
        current = list(map(int, route['search']['selected_model_opportunities']))
        polisher = NearbyPolisher(model, None, OUTPUT, reference_route=BASE/'route.json')
        proposals = polisher.proposals(current)
        ranked = {}
        for gain, distance, head in proposals:
            record = model.records[head]
            offset, n = int(record['offset']), int(record['count'])
            for sp in model.species[offset:offset+n]:
                if missing[sp]:
                    ranked.setdefault(int(sp), []).append((gain, distance, head))
        study = read(ROOT/'runs/lower48-eod-2024/calendar-strategy-study-2024-v1/study-result.json')
        alternatives = {x['species_id']: x['arms'] for x in study['seen_in_alternatives_but_not_winner']}
        taxonomy = {r['species_id']: r for r in csv.DictReader((Path(model.meta['dataset'])/'taxonomy.csv').open())}
        rows, heads, region_counts = [], [], Counter()
        for s in np.flatnonzero(missing):
            s = int(s)
            ids = np.asarray(support_ids[s], dtype=np.int64)
            if len(ids) != int(counts[s].sum()):
                raise ValueError('support inventory differs from independent count')
            records = model.records[ids]
            points = model.coords[records['location']]
            days = np.flatnonzero(counts[s])
            same, idle, anydate = (math.inf, None), (math.inf, None), math.inf
            for d in np.unique(records['day']):
                at = np.flatnonzero(records['day'] == d)
                for center in anchors.get(int(d), []):
                    ds = distances(points[at], center)
                    j = int(np.argmin(ds))
                    if ds[j] < same[0]: same = (float(ds[j]), int(ids[at[j]]))
                if int(d) in rest:
                    ds = distances(points[at], rest[int(d)])
                    j = int(np.argmin(ds))
                    if ds[j] < idle[0]: idle = (float(ds[j]), int(ids[at[j]]))
            for center in visited:
                anydate = min(anydate, float(distances(points, center).min()))
            # Location metadata uses bounded SQLite pages; only missing supports
            # are looked up, rather than materializing the national catalog.
            states = Counter()
            for loc, n in zip(*np.unique(records['location'], return_counts=True)):
                states[model.locations[int(loc)]['state']] += int(n)
            region_counts.update(states)
            tier = taxonomy.get(model.meta['species'][s]['id'], {}).get('score_tier', 'primary')
            row = {'species_id': model.meta['species'][s]['id'], 'species': model.meta['species'][s]['name'],
                   'score_tier': tier, 'support_pairs': len(ids), 'reported_days': len(days),
                   'first_date': str(source+timedelta(days=int(days[0]))),
                   'last_date': str(source+timedelta(days=int(days[-1]))),
                   'states': '|'.join(k for k, _ in states.most_common()),
                   'state_supports': dict(states),
                   'nearest_birding_same_date_air_miles': round(same[0], 3) if math.isfinite(same[0]) else None,
                   'nearest_rest_overnight_same_date_air_miles': round(idle[0], 3) if math.isfinite(idle[0]) else None,
                   'nearest_visited_point_any_date_air_miles': round(anydate, 3),
                   'nearby_proposals': len(ranked.get(s, [])),
                   'best_nearby_proposal_air_miles': round(min(x[1] for x in ranked[s]), 3) if s in ranked else None,
                   'seen_in_other_verified_arms': '|'.join(alternatives.get(model.meta['species'][s]['id'], [])),
                   'land_supports': int((records['reserved'] == 0).sum()),
                   'boat_supports': int((records['reserved'] != 0).sum())}
            rows.append(row)
            selected = []
            for kind, chosen in [('same_birding_date', same), ('same_rest_date', idle)]:
                if chosen[1] is not None: selected.append((kind, chosen[1], chosen[0]))
            selected.extend(('polisher_proposal', x[2], x[1]) for x in ranked.get(s, [])[:6])
            used = set()
            for kind, head, miles in selected:
                if head in used: continue
                used.add(head)
                r = model.records[head]
                loc = model.locations[int(r['location'])]
                offset, n = int(r['offset']), int(r['count'])
                new = [model.meta['species'][int(v)]['name'] for v in model.species[offset:offset+n] if missing[v]]
                heads.append({'species_id': row['species_id'], 'species': row['species'], 'kind': kind,
                              'opportunity': head, 'date': str(source+timedelta(days=int(r['day']))),
                              'state': loc['state'], 'latitude': loc['lat'], 'longitude': loc['lon'],
                              'air_miles_to_calendar_anchor_or_corridor': round(miles, 3),
                              'new_species_at_stop': '|'.join(new), 'potential_gain': len(new),
                              'existing_day_kind': route['days'][int(r['day'])]['kind'],
                              'existing_day_activity_minutes': route['days'][int(r['day'])]['activity_minutes'],
                              'boat_variant': int(r['reserved'])})
        rows.sort(key=lambda r: (r['best_nearby_proposal_air_miles'] if r['best_nearby_proposal_air_miles'] is not None else math.inf,
                                 -r['reported_days']))
        write_csv(OUTPUT/'missing-species.csv', rows, list(rows[0]))
        write_csv(OUTPUT/'candidate-visits.csv', heads, list(heads[0]))
        result = {'route_species': len(seen), 'primary': route['scoring_tiers']['primary'],
                  'provisional': route['scoring_tiers']['provisional'], 'modeled_species_pool': nspp,
                  'missing': len(rows), 'missing_support_pairs': int(counts.sum()),
                  'nearby_candidate_species': len(ranked),
                  'missing_in_alternative_verified_calendars': len(alternatives),
                  'region_supports': dict(region_counts.most_common()), 'species': rows,
                  'baseline_path': str(BASE/'route.json'), 'baseline_sha256': sha256(BASE/'route.json'),
                  'model_path': str(model.path), 'model_sha256': sha256(model.path),
                  'seconds': time.monotonic()-started,
                  'interpretation': 'Current-policy modeled opportunities. Geometric proximity and other verified calendars identify leads, not additive species or full-calendar feasibility. No countability or access policy was changed.'}
        marine = ROOT/'data/marine-2024-charter-v1'
        marine_manifest = read(marine/'manifest.json')
        if (sha256(marine/'offshore.parquet') != marine_manifest['offshore_sha256']
                or marine_manifest['parent_manifest_sha256'] != model.meta['manifest_sha256']):
            raise ValueError('offshore inventory and current countability dataset differ')
        pool = {s['id'] for s in model.meta['species']}
        with duckdb.connect() as con:
            offshore = con.execute('''SELECT species_id,min(common_name),count(*),count(DISTINCT date),
                min(date),max(date) FROM read_parquet(?)
                WHERE marine_disposition='policy_eligible_pending_marine_access' GROUP BY 1''',
                [str(marine/'offshore.parquet')]).fetchall()
        outside = [dict(zip(['species_id','species','source_groups','reported_days','first_date','last_date'],
                            [s,n,c,d,str(first),str(last)])) for s,n,c,d,first,last in offshore if s not in pool]
        result['additional_offshore_taxa_pending_access_and_jurisdiction'] = outside
        if outside:
            write_csv(OUTPUT/'unmodeled-marine-species.csv', outside, list(outside[0]))
        dump(OUTPUT/'analysis.json', result)
        print({k: v for k, v in result.items() if k != 'species'})
        for row in rows[:25]: print(row)
    finally:
        model.close()


if __name__ == '__main__':
    main()
