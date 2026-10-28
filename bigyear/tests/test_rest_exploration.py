from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np

from bigyear_planner.rest_exploration import RestExplorer


DTYPE = [('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')]


def calendar():
    first = date(2024, 1, 1)
    return {'year': 2024, 'days': [
        {'date': str(first+timedelta(days=d)),
         'kind': 'birding' if d == 10 else 'road_transit' if d == 13 else 'rest'}
        for d in range(366)]}


def test_distant_rest_date_is_indexed_and_full_evaluator_decides(tmp_path):
    records = np.asarray([(10,0,0,1,0), (11,1,1,1,0), (11,2,2,1,0),
                          (12,3,3,1,1), (13,4,4,1,0)], dtype=DTYPE)
    species = np.asarray([0,1,0,2,3], dtype='u4')
    coordinates = np.asarray([[30.,-100.], [47.,-70.], [30.,-100.], [31.,-100.], [30.,-100.]])
    model = SimpleNamespace(records=records, species=species, coords=coordinates,
        meta={'species': [{'id': str(i), 'name': str(i)} for i in range(4)]}, path=tmp_path/'model')
    calls = []

    def evaluate(ids, timeout):
        calls.append(list(ids))
        return {'pending': False, 'feasible': 1 not in ids,
                'species': len({int(species[i]) for i in ids}), 'minutes': 0, 'miles': 0}

    explorer = RestExplorer(model, SimpleNamespace(evaluate=evaluate), tmp_path, calendar())
    groups, novelty, report = explorer.index([0])
    assert set(groups) == {1, 2} and groups[1] == [1] and groups[2] == [3]
    assert report['opportunity_dates'] == 2 and report['geographic_cutoff_miles'] is None
    assert any(d['boat_opportunities'] == 1 for d in report['days'])
    assert novelty[2] == 0  # Species already in the annual total is not a gain.
    before_records, before_coordinates = records.copy(), coordinates.copy()
    result = explorer.explore([0], seconds=1)
    assert any(1 in call for call in calls)  # A distant actual rest-date record is considered.
    assert result['ids'] == [0, 3] and result['score']['species'] == 2
    assert result['original_visits_retained']
    assert np.array_equal(records, before_records) and np.array_equal(coordinates, before_coordinates)


def test_frequent_species_cannot_starve_other_species(tmp_path):
    records = [(10,0,0,1,0)]+[(11,i,i,1,0) for i in range(1,101)]+[(12,101,101,1,0)]
    model = SimpleNamespace(records=np.asarray(records, dtype=DTYPE),
        species=np.asarray([0]+[1]*100+[2], dtype='u4'),
        coords=np.asarray([[30.,-100.]]*101+[[45.,-70.]]),
        meta={'species': [{'id': str(i), 'name': str(i)} for i in range(3)]}, path=tmp_path/'model')
    explorer = RestExplorer(model, None, tmp_path, calendar())
    plans, report = explorer.plans([0], per_species=2)
    assert {species for species, head in plans[:2]} == {1, 2}
    assert report['dated_opportunities'] == 101 and report['queued_proposals'] == 3
    targeted, _ = explorer.plans([0], targets=['2'], per_species=2)
    assert targeted == [(2, 101)]
    assert len(model.records) == 102  # Proposal quotas do not remove source opportunities.


def test_gain_updates_against_total_year_species_after_each_acceptance(tmp_path):
    records = np.asarray([(10,0,0,1,0), (11,1,1,2,0), (12,2,3,1,0)], dtype=DTYPE)
    species = np.asarray([0,1,2,2], dtype='u4')
    model = SimpleNamespace(records=records, species=species,
        coords=np.asarray([[30.,-100.], [30.001,-100.], [30.002,-100.]]),
        meta={'species': [{'id': str(i), 'name': str(i)} for i in range(3)]}, path=tmp_path/'model')
    calls = []

    def evaluate(ids, timeout):
        calls.append(list(ids))
        seen = set()
        for i in ids:
            r = records[i]
            seen.update(species[int(r['offset']):int(r['offset'])+int(r['count'])])
        return {'pending': False, 'feasible': True, 'species': len(seen), 'minutes': 0, 'miles': 0}

    result = RestExplorer(model, SimpleNamespace(evaluate=evaluate), tmp_path, calendar()).explore([0], seconds=1)
    assert result['ids'] == [0,1] and result['score']['species'] == 3
    assert [0,1,2] not in calls  # Species2 has already been collected elsewhere.


def test_regional_trial_shortlist_keeps_national_inventory_and_model_intact(tmp_path):
    model = SimpleNamespace(records=np.asarray([(10,0,0,1,0),(11,1,1,1,0),(12,2,2,1,0)], dtype=DTYPE),
        species=np.asarray([0,1,1], dtype='u4'),
        coords=np.asarray([[30.,-100.],[31.,-99.],[40.,-90.]]),
        locations=[{'state':'US-TX'},{'state':'US-TX'},{'state':'US-IL'}],
        meta={'species':[{'id':'0','name':'0'},{'id':'1','name':'1'}]}, path=tmp_path/'model')
    explorer = RestExplorer(model, None, tmp_path, calendar())
    plans, inventory = explorer.plans([0], proposal_states=['US-IL'])
    assert plans == [(1,2)]
    assert inventory['dated_opportunities'] == 2 and inventory['opportunity_dates'] == 2
    assert inventory['proposal_states'] == ['US-IL']
    national, _ = explorer.plans([0])
    assert {head for species,head in national} == {1,2}
    assert len(model.records) == 3
