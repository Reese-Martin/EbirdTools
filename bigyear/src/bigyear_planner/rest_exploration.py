"""Species-balanced dated opportunities on an incumbent's resting calendar.

Every rest date with an unseen-species support is indexed without a geographic
radius. Distance orders proposals only. Full measured calendar evaluation must
authorize all driving, flight, boat and walking time before accepting a visit.
The complete candidate model and its observations are never filtered or changed.
"""

from collections import Counter
from datetime import date, timedelta
import json
import math
from pathlib import Path
import time

import numpy as np

from .calendar_study import superior
from .regional_exploration import RegionalExplorer
from .util import dump


class RestExplorer(RegionalExplorer):
    def __init__(self, model, evaluator, output, reference_route):
        super().__init__(model, evaluator, output)
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.reference_route = reference_route
        origin = date(reference_route['year'], 1, 1)
        if len(reference_route['days']) != self.days:
            raise ValueError('rest exploration requires the complete calendar')
        self.rest_days = []
        for day, entry in enumerate(reference_route['days']):
            if entry['date'] != str(origin+timedelta(days=day)):
                raise ValueError('rest calendar date indexing differs')
            if entry['kind'] == 'rest':
                self.rest_days.append(day)
        self.rest_days = np.asarray(self.rest_days, dtype=np.int32)

    def index(self, current):
        seen = self.seen(current)
        novelty = self.novelty(seen)
        mask = np.isin(self.records['day'], self.rest_days) & (novelty > 0)
        ids = np.flatnonzero(mask)
        groups, day_summary = {}, {}
        for head in ids:
            row = self.records[head]
            offset, count = int(row['offset']), int(row['count'])
            species = self.species[offset:offset+count]
            missing = species[~seen[species]]
            day = int(row['day'])
            entry = day_summary.setdefault(day, {'opportunities': 0, 'species': set(), 'boat_opportunities': 0})
            entry['opportunities'] += 1
            entry['boat_opportunities'] += int(row['reserved'] != 0)
            entry['species'].update(map(int, missing))
            for sp in missing:
                groups.setdefault(int(sp), []).append(int(head))
        report = {'rest_dates': len(self.rest_days), 'opportunity_dates': len(day_summary),
                  'missing_species': len(groups), 'dated_opportunities': len(ids),
                  'geographic_cutoff_miles': None,
                  'source_dates_only': True, 'days': []}
        origin = date(self.reference_route['year'], 1, 1)
        for day, entry in sorted(day_summary.items()):
            report['days'].append({'date': str(origin+timedelta(days=day)),
                'opportunities': entry['opportunities'], 'boat_opportunities': entry['boat_opportunities'],
                'missing_species': [self.model.meta['species'][s]['id'] for s in sorted(entry['species'])]})
        return groups, novelty, report

    def burden(self, current, head):
        """Geometric trip burden per available transfer date; no hard cutoff."""
        day = int(self.records[head]['day'])
        center = self.coords[int(self.records[head]['location'])]
        before = [i for i in current if int(self.records[i]['day']) < day]
        after = [i for i in current if int(self.records[i]['day']) > day]
        score = 0.
        for neighbor in [before[-1] if before else None, after[0] if after else None]:
            if neighbor is None:
                continue
            point = self.coords[int(self.records[neighbor]['location'])]
            miles = float(np.hypot((point[0]-center[0])*69.1,
                (point[1]-center[1])*69.1*np.cos(np.radians(center[0]))))
            gap = abs(int(self.records[neighbor]['day'])-day)
            score += miles/max(1, gap)
        return score

    def plans(self, current, targets=None, per_species=24, proposal_states=None):
        groups, novelty, report = self.index(current)
        target_ids = set(targets) if targets is not None else None
        state_filter = set(proposal_states) if proposal_states is not None else None
        states = {}
        lists = {}
        for species, ids in groups.items():
            sid = self.model.meta['species'][species]['id']
            if target_ids is not None and sid not in target_ids:
                continue
            if state_filter is not None:
                kept = []
                for head in ids:
                    location = int(self.records[head]['location'])
                    if location not in states:
                        states[location] = self.model.locations[location]['state']
                    if states[location] in state_filter:
                        kept.append(head)
                ids = kept
                if not ids:
                    continue
            ranked = sorted(ids, key=lambda i: (-int(novelty[i]), self.burden(current, i), i))
            # One best exact point per date gets a turn before many nearby pins
            # from the same day. Later rounds retain alternate exact pins.
            first, later, days = [], [], set()
            for head in ranked:
                day = int(self.records[head]['day'])
                (later if day in days else first).append(head)
                days.add(day)
            lists[species] = (first+later)[:per_species]
        ordered_species = sorted(lists, key=lambda s: (
            -int(novelty[lists[s][0]]), self.burden(current, lists[s][0]), s))
        # Round-robin species: tens of thousands of common-bird records cannot
        # monopolize the proposal queue. This is a shortlist, not model reduction.
        plans = [(s, lists[s][rank]) for rank in range(per_species)
                 for s in ordered_species if rank < len(lists[s])]
        report.update(proposed_species=len(lists), queued_proposals=len(plans),
                      proposal_states=sorted(state_filter) if state_filter is not None else None,
                      per_species_shortlist=per_species,
                      interpretation='All baseline rest-date supports are indexed. This finite search does not exhaust all routes; untested and unresolved proposals are retained as opportunities.')
        return plans, report

    def explore(self, initial, seconds=300, targets=None, per_species=24, proposal_states=None):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('rest exploration requires positive seconds')
        started = time.monotonic()
        deadline = started+seconds
        current = self.canonical(initial)
        best = self.evaluator.evaluate(current, timeout=min(60, seconds))
        if best['pending'] or not best['feasible']:
            raise ValueError('rest exploration requires a measured feasible incumbent')
        plans, inventory = self.plans(current, targets, per_species, proposal_states)
        dump(self.output/'rest-opportunity-inventory.json', inventory)
        attempts, accepted, retry = [], [], []
        tested = set()
        tried_species, tried_days = Counter(), Counter()
        with (self.output/'rest-attempts.jsonl').open('w') as log:
            def probe(species, head, timeout):
                nonlocal current, best
                if self.seen(current)[species]:
                    return
                candidate = self.canonical([*current, head])
                key = tuple(candidate)
                if key in tested:
                    return
                score = self.evaluator.evaluate(candidate, timeout=timeout)
                entry = {'target': self.model.meta['species'][species]['id'],
                         'target_name': self.model.meta['species'][species]['name'],
                         'head': head, 'source_day': int(self.records[head]['day']),
                         'boat_variant': int(self.records[head]['reserved']),
                         'geometric_burden_rank_only': self.burden(current, head),
                         'incumbent_species': int(best['species']), **score}
                attempts.append(entry)
                tried_species[species] += 1
                tried_days[entry['source_day']] += 1
                log.write(json.dumps(entry)+'\n'); log.flush()
                if score['pending']:
                    retry.append((species, head))
                else:
                    tested.add(key)
                if superior(score, best):
                    old = int(best['species'])
                    current, best = candidate, score
                    accepted.append(entry)
                    dump(self.output/'rest-checkpoint.json', {'ids': current, 'score': best})
                    print(json.dumps({'stage': 'rest-improvement', 'target': entry['target_name'],
                                      'day': entry['source_day'], 'before': old, **score}), flush=True)

            # First sweep favors a cheap measured answer. Retry unresolved heads
            # fairly after their road queries have had time to complete.
            first_deadline = started+seconds*.65
            for species, head in plans:
                if time.monotonic() >= first_deadline:
                    break
                probe(species, head, min(2, max(.01, deadline-time.monotonic())))
            retry = list(dict.fromkeys(retry))
            for species, head in retry:
                if time.monotonic() >= deadline:
                    break
                probe(species, head, min(10, max(.01, deadline-time.monotonic())))
        result = {'ids': current, 'score': best, 'accepted': accepted,
                  'attempts': len(attempts), 'pending_attempts': sum(a['pending'] for a in attempts),
                  'target_species_attempted': len(tried_species), 'rest_dates_attempted': len(tried_days),
                  'inventory': inventory, 'seconds': time.monotonic()-started,
                  'original_visits_retained': set(initial) <= set(current),
                  'scope': 'Any dated missing species on an incumbent rest date, including distant regions and boat variants. All travel uses full measured calendar feasibility; no rest penalty or artificial species bonus.'}
        dump(self.output/'rest-result.json', result)
        return result
