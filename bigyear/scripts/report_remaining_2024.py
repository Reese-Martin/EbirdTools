"""Summarize source-bound diagnostic opportunities, without promoting a route."""

from analyze_remaining_2024 import OUTPUT
from datetime import date, timedelta

import numpy as np
from bigyear_planner.compact_model import load
from bigyear_planner.util import dump, read, sha256


def main():
    analysis = read(OUTPUT/'analysis.json')
    probe = read(OUTPUT/'insertion-probes/result.json')
    candidate = read(OUTPUT/'insertion-probes/candidate-calendar.json')
    route = read(analysis['baseline_path'])
    if not (analysis['baseline_sha256'] == probe['baseline_sha256'] == candidate['source_route_sha256']
            == sha256(analysis['baseline_path'])):
        raise ValueError('remaining-species diagnostic baseline changed')
    model = load(analysis['model_path'])
    try:
        if analysis['model_sha256'] != probe['model_sha256'] or sha256(model.path) != analysis['model_sha256']:
            raise ValueError('probe and full source model differ')
        original = set(map(int, route['search']['selected_model_opportunities']))
        proposed = set(candidate['ids'])
        if not original <= proposed:
            raise ValueError('insertion probe removed an original visit')

        def seen(ids):
            result = set()
            for i in ids:
                record = model.records[i]
                first, count = int(record['offset']), int(record['count'])
                result.update(map(int, model.species[first:first+count]))
            return result

        before, after = seen(original), seen(proposed)
        if len(before) != 691 or len(after) != probe['candidate_score']['species'] or not before <= after:
            raise ValueError('candidate marginal species reconstruction differs')
        added = sorted(model.meta['species'][i]['name'] for i in after-before)
        if len(added) != len(probe['accepted']) or len(proposed-original) != len(probe['accepted']):
            raise ValueError('candidate additions differ from measured insertion log')
        names = {s['id']: s['name'] for s in model.meta['species']}
        wanted = [i for i,s in enumerate(model.meta['species'])
                  if s['name'] in {'Golden-cheeked Warbler','Black-capped Vireo'}]
        regional = []
        for day in range(98, 102):
            lo,hi = map(int, np.searchsorted(model.records['day'], [day, day+1]))
            records = model.records[lo:hi]
            first = int(records[0]['offset'])
            last = int(records[-1]['offset'])+int(records[-1]['count'])
            matches = np.isin(model.species[first:last], wanted)
            gains = np.add.reduceat(matches, (records['offset']-first).astype(np.intp))
            for index in np.flatnonzero((gains == 2) & (records['reserved'] == 0))[:3]:
                regional.append({'opportunity': lo+int(index),
                                 'date': str(date(2024,1,1)+timedelta(days=day)),
                                 'species': ['Golden-cheeked Warbler','Black-capped Vireo'],
                                 'status': 'dated model support only; no full-calendar feasibility claim'})
        dump(OUTPUT/'regional-candidates.json', {'model_sha256': analysis['model_sha256'], 'central_texas': regional})
        lines = [
            '# Remaining 2024 route opportunities', '',
            'The verified baseline remains **691 species (680 primary + 11 provisional)**. '
            'This is the full lower-48/DC 2024 EOD model, with driving, dated flights, modeled boats, '
            'exact coordinates and 2.2 km/h walking. Sensitive-species, countability and access holds are unchanged.', '',
            f"The current model has **{analysis['modeled_species_pool']} species**, so the baseline misses "
            f"**{analysis['missing']}**. Every missing modeled species was scanned across all eligible "
            f"dated location supports ({analysis['missing_support_pairs']:,} pairs). "
            'These are reported opportunities, not abundance or encounter-probability estimates.', '',
            '## Confirmed by the full-calendar evaluator', '',
            f"A {probe['seconds']:.0f}-second bounded insertion experiment found **five additions that fit together**, "
            'retaining every baseline visit. The candidate score is **696**. '
            'It has not received the independent publisher-record, source-evidence and transport audit required '
            'to promote a new verified route. The frozen 691 route was not replaced.', '',
            '| Species | Source date | State | Candidate cumulative count |',
            '|---|---|---|---:|']
        for row in probe['accepted']:
            lines.append(f"| {names[row['species_id']]} | {row['date']} | {row['state'].removeprefix('US-')} | {row['species']} |")
        lines += ['', 'Blue-crowned Parakeet uses the existing provisional-A tier; the other four use primary '
                  'scoring. The five stops add ten hours of standard birding time. All driving, flight, boat, '
                  'walking, day-length and local-mile constraints were evaluated on the complete calendar.', '',
                  f"There were {probe['attempts']} evaluator attempts, of which {probe['pending_attempts']} returned "
                  'pending routing discovery. Pending is not a proof of infeasibility. Some attempts retry the '
                  'same visit after queries finish, so this is not a unique-candidate failure rate.', '',
                  '## Next opportunities', '',
                  '1. **Finish nearby insertions with a fair per-species budget.** The source scan identifies '
                  f"{analysis['nearby_candidate_species']} species with candidates within the existing "
                  'nearby/corridor proposal rule. Kirtland’s Warbler, Black Swift, Black-backed Woodpecker '
                  'and Little Gull deserve targeted checks. Proximity is geometric; it is not a measured road detour.', '',
                  '2. **A central-Texas spring side trip.** Golden-cheeked Warbler has 146 source-supported days '
                  'and Black-capped Vireo has 178. The current April route visits eastern/coastal Texas; '
                  'both species are represented together at exact stops on April 8–11 elsewhere in Texas. '
                  'The calendar has April 9–11 resting, making an out-and-back trip a useful controlled test. '
                  'No feasibility or additive gain is claimed for that side trip yet.', '',
                  '3. **A Midwest habitat loop.** Eurasian Tree Sparrow is represented on 366 days '
                  '(11,636 supports) and Henslow’s Sparrow on 287 days. Eurasian Tree Sparrow was already '
                  'collected by two other verified study arms. A regional loop should be tested rather than '
                  'requiring a distant field site to fit the 30-mile local-day limit.', '',
                  '4. **Recover alternate-calendar targets.** Rufous-capped Warbler, Eurasian Tree Sparrow '
                  'and Willow Ptarmigan remain absent from the candidate 696 but were each collected by another '
                  'verified study calendar. Their visits are not automatically compatible with this route. '
                  'Rufous-capped Warbler is the strongest regional reconstruction lead; the two-day Willow '
                  'Ptarmigan record is a calendar-sensitive rarity, not a routine easy addition.', '',
                  '5. **Target remaining habitat gaps.** Cassia Crossbill has 104 source-supported days; '
                  'Baird’s Sparrow 158; Black-backed Woodpecker 340. These favor short habitat-focused '
                  'regional trips before chasing one-day rarities. The current finite candidate model '
                  'already contains their observations.', '',
                  '## What this says about the search', '',
                  'The missing set mixes ordinary habitat misses and extremely restricted rarities. '
                  'The five pure insertions show that the current route is still locally improvable under '
                  'unchanged data and rules. More uniform geographic exploration and finishing deferred '
                  'road queries are useful now; EBD is not required to test these improvements.', '',
                  'The objective remains one point per unique countable species, with travel time and road '
                  'miles breaking ties. Proposal priority can favor a target species without changing its '
                  'final point value. A species-balanced insertion sweep and regional campaigns can expose '
                  'routes that random location proposals miss.', '',
                  '## Scope limits', '',
                  'The 792-species model is a source/model pool, not a claim that all 792 are jointly '
                  'collectable, and not an inventory of every bird seen by everyone in the country. '
                  'The scope stays lower 48 + DC. EBD is still pending.', '',
                  'Two additional scored taxa occur in the frozen offshore holding pool but are not in '
                  'the current constructed trip catalog: White-faced Storm-Petrel (6 reported days) and '
                  'Murphy’s Petrel (7). Jurisdiction and vessel access remain to be established. They are '
                  'not counted as ready additions, and are a lower priority than the demonstrated insertions.', '',
                  'The 759 target remains 68 above the verified route, or 63 above this unaudited candidate. '
                  'This diagnostic does not establish that 759 is achievable in one route under these rules.', '',
                  '## Evidence', '',
                  '- `analysis.json`: complete model/route bindings and all 101 missing species.',
                  '- `missing-species.csv`: every missing species, reported dates, states and geometric proximity.',
                  '- `candidate-visits.csv`: exact dated candidate heads for follow-up evaluation.',
                  '- `insertion-probes/result.json`: evaluator outcomes and the five accepted insertions.',
                  '- `insertion-probes/candidate-calendar.json`: full selected model IDs; explicitly unaudited.',
                  '- `insertion-probes/attempts.jsonl`: pending and completed insertion attempts.',
                  '- `router-stopped.json`: confirms the temporary routing worker stopped.', '']
        (OUTPUT/'report.md').write_text('\n'.join(lines))
        proof = {'valid': True, 'baseline_unchanged': True, 'original_visits_retained': len(original),
                 'candidate_visits': len(proposed), 'candidate_species': len(after), 'added_species': added,
                 'independently_validated_new_route': False,
                 'analysis_sha256': sha256(OUTPUT/'analysis.json'),
                 'probe_sha256': sha256(OUTPUT/'insertion-probes/result.json'),
                 'candidate_sha256': sha256(OUTPUT/'insertion-probes/candidate-calendar.json'),
                 'report_sha256': sha256(OUTPUT/'report.md')}
        dump(OUTPUT/'report-verification.json', proof)
        print(proof)
    finally:
        model.close()


if __name__ == '__main__':
    main()
