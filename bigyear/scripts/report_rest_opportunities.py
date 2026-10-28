"""Report only verified rest-opportunity calendars and reindex final rest dates."""

from bigyear_planner.compact_model import load
from bigyear_planner.rest_exploration import RestExplorer
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv


ROOT_RUNS = ROOT/'runs/lower48-eod-2024'
BASE = ROOT_RUNS/'rest-study-baseline-696-2024-v1'
FINAL = ROOT_RUNS/'rest-opportunities-2024-v1'


def verified(folder):
    result, route, audit = (read(folder/name) for name in ['result.json','route.json','validation.json'])
    if not (result['valid'] and audit['valid'] and result['route_sha256'] == audit['route_sha256'] == sha256(folder/'route.json')):
        raise ValueError('report requires a route-bound independent positive audit: '+str(folder))
    if audit['days_checked'] != 366 or route['config']['walking_speed_kmh'] != 2.2:
        raise ValueError('full-year walking/calendar rule differs')
    return result, route, audit


def main():
    baseline, before, first_audit = verified(BASE)
    final, route, audit = verified(FINAL)
    model = load(read(FINAL/'problem.json')['compact_source']['path'])
    try:
        if not (before['provenance']['compact_model_sha256'] == route['provenance']['compact_model_sha256']
                == sha256(model.path)) or before['config'] != route['config']:
            raise ValueError('complete source model or scientific configuration changed')
        if before['taxonomy_policy'] != route['taxonomy_policy']:
            raise ValueError('countability policy changed')
        ids = route['search']['selected_model_opportunities']
        names = {s['id']: s['name'] for s in model.meta['species']}
        old_species = {s for day in before['days'] for stop in day['stops'] for s in stop['species']}
        new_species = {s for day in route['days'] for stop in day['stops'] for s in stop['species']}
        rows = []
        for change, species in [('added',new_species-old_species),('lost',old_species-new_species)]:
            for s in sorted(species):
                visits = [(day,stop) for day in route['days'] for stop in day['stops'] if s in stop['species']]
                rows.append({'change':change,'species_id':s,'species':names[s],
                    'first_date':visits[0][0]['date'] if visits else '',
                    'state':route['locations'][visits[0][1]['location']]['state'] if visits else ''})
        write_csv(FINAL/'species-changes.csv', rows, ['change','species_id','species','first_date','state'])
        helper = RestExplorer(model, None, FINAL/'final-rest-opportunities', route)
        _, _, inventory = helper.index(ids)
        dump(FINAL/'final-rest-opportunities/inventory.json', inventory)
        rest_rows = [{'date':entry['date'],'opportunities':entry['opportunities'],
                      'boat_opportunities':entry['boat_opportunities'],
                      'missing_species_count':len(entry['missing_species']),
                      'missing_species':'|'.join(names[s] for s in entry['missing_species'])}
                     for entry in inventory['days']]
        write_csv(FINAL/'remaining-rest-opportunities.csv', rest_rows,
                  ['date','opportunities','boat_opportunities','missing_species_count','missing_species'])
        phase_rows = []
        for phase in final['phases']:
            phase_rows.append({'phase':phase['label'],'species':phase['score'].get('species'),
                               'pending':phase['score']['pending'],'feasible':phase['score']['feasible'],
                               'seconds':phase.get('seconds')})
        write_csv(FINAL/'phase-comparison.csv',phase_rows,list(phase_rows[0]))
        text = ['# Verified 2024 rest-opportunity study', '',
            f"The five-insertion candidate independently validated at **{baseline['species']} species "
            f"({baseline['primary']} primary + {baseline['provisional']} provisional)**. "
            f"All 366 dates and {baseline['publisher_records_checked']:,} publisher records passed.", '',
            f"The final route independently validated at **{final['species']} species "
            f"({final['primary']} primary + {final['provisional']} provisional)**, "
            f"a gain of {final['species']-baseline['species']} over the verified 696 baseline. "
            f"There are {len(new_species-old_species)} additions and {len(old_species-new_species)} losses.", '',
            '| Phase | Best calendar count | Gain over 696 |', '|---|---:|---:|']
        for phase in phase_rows:
            count = phase['species'] if phase['feasible'] and not phase['pending'] else None
            text.append(f"| {phase['phase']} | {count if count is not None else 'unresolved'} | "
                        f"{count-696 if count is not None else 'unresolved'} |")
        text += ['', 'Texas and Midwest each started from 696. Their routing cache warmed during the trials, '
                 'so timing is not a controlled cold-cache comparison. Combined scores were re-evaluated on one '
                 'calendar; regional gains were not simply added. A phase returning 696 means that no improving '
                 'trip was found; it does not certify that the proposed regional trip was feasible.', '',
                 'The previously verified Tree Sparrow calendars started in Missouri on January 1. '
                 'The 696 calendar uses that date in Florida for its only selected American Flamingo stop. '
                 'Copying that old start would exchange one species for another unless Flamingo is '
                 'collected elsewhere. Other Midwest source dates remain possible and are not ruled out by '
                 'a finite trial with pending road queries.', '',
                 '## Species changes', '', '| Change | Species | Date | State |','|---|---|---|---|']
        for row in rows:
            text.append(f"| {row['change']} | {row['species']} | {row['first_date']} | {row['state']} |")
        national = read(FINAL/'national-rest/rest-result.json')
        text += ['', '## Rest-day policy and coverage', '',
            'Every actual baseline rest date with a source record of a species missing from the annual '
            'list was indexed, without a geographic cutoff. Exact source dates, land observations and existing '
            'boat options remained available. Geometry ranked proposals; measured full-calendar evaluation '
            'decided feasibility. Rest was not penalized and repeat sightings did not gain points.', '',
            f"The national sweep indexed {national['inventory']['opportunity_dates']} baseline rest dates, "
            f"{national['inventory']['dated_opportunities']:,} dated opportunities and "
            f"{national['inventory']['missing_species']} missing species. It attempted "
            f"{national['attempts']} evaluations across {national['rest_dates_attempted']} dates and "
            f"{national['target_species_attempted']} species; {national['pending_attempts']} attempts returned pending. "
            'Pending responses are not infeasibility proofs, and attempts can include retries.', '',
            f"The final calendar has {final['rest_days']} rest days. Reindexing that new actual calendar finds "
            f"{inventory['opportunity_dates']} dates with {inventory['dated_opportunities']:,} remaining source "
            f"opportunities for {inventory['missing_species']} missing species. They are saved in "
            '`remaining-rest-opportunities.csv`. A finite search does not prove that every remaining '
            'rest day is necessary or that every proposed trip is feasible.', '',
            '## Validation and retention', '',
            f"All 366 calendar dates and {final['publisher_records_checked']:,} publisher records passed the "
            'independent audit, including source identity, countability, road endpoints, walking, flight '
            'service and boat/landing constraints. The full source archive and complete candidate model '
            'remain unchanged; all observations and exact coordinate strings are retained. Walking remains '
            '2.2 km/h. Countability, provisional tiers and sensitive/access holds are unchanged.', '',
            f"The remaining gap to 759 is {759-final['species']}. This is the lower-48/DC 2024 EOD model, "
            'not yet the pending EBD. The result is an improved feasible route, not a proof of optimality.', '',
            f"Final route SHA-256: `{sha256(FINAL/'route.json')}`.",
            f"Model SHA-256: `{sha256(model.path)}`.", '']
        (FINAL/'report.md').write_text('\n'.join(text))
        dump(FINAL/'report-verification.json', {'valid':True,'baseline_sha256':sha256(BASE/'route.json'),
            'final_sha256':sha256(FINAL/'route.json'),'model_sha256':sha256(model.path),
            'report_sha256':sha256(FINAL/'report.md'),'rest_inventory_sha256':sha256(FINAL/'final-rest-opportunities/inventory.json'),
            'all_source_observations_retained':True,'exact_coordinates':True,'walking_kmh':2.2})
        print({'verified':True,'species':final['species'],'rest_dates':final['rest_days'],
               'remaining_opportunity_dates':inventory['opportunity_dates']})
    finally:
        model.close()


if __name__ == '__main__':
    main()
