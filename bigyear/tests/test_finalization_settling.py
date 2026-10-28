from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from bigyear_planner import national_runner, regional_exploration


def setup_case(tmp_path, monkeypatch, *, replay_species=2, sailing=0):
    path = tmp_path/'model.json'
    path.write_text('{}')
    problem = {'opportunities':[{'marine_trip':'trip'}] if sailing else [],
        'window_transitions':[],
        'joint_marine':{'selected_trips':{'trip':{'sailing_minutes':sailing}}}}
    monkeypatch.setattr(national_runner, 'selected_problem', lambda *args:problem)
    monkeypatch.setattr(national_runner, 'freeze_selected', lambda *args:None)
    monkeypatch.setattr(national_runner, 'materialize', lambda *args, **kwargs:
        {'days':[{'road_miles':200,'travel_minutes':280}]})
    calls = []

    class Evaluator:
        def __init__(self, *args):
            pass

        def open(self):
            return nullcontext(self)

        def evaluate(self, ids, timeout):
            calls.append(list(ids))
            return {'pending':False,'feasible':True,'species':replay_species,
                'miles':200,'minutes':280+sailing}

    monkeypatch.setattr(regional_exploration, 'NativeEvaluator', Evaluator)
    result = {'selected':[17,91],'species':2,'road_miles':100,'travel_minutes':300}
    return SimpleNamespace(path=path), result, calls


def test_newly_measured_road_is_replayed_without_changing_visits(tmp_path, monkeypatch):
    model, result, calls = setup_case(tmp_path, monkeypatch, sailing=20)
    saved, _, _ = national_runner.settle_selected_calendar(model, None, tmp_path, result)
    assert saved['road_miles'] == 200
    assert saved['travel_minutes'] == 300  # Road plus unchanged modeled sailing.
    assert saved['selected'] == [17,91]
    assert calls == [[17,91]]
    assert (tmp_path/'native-search-result-before-rescore.json').exists()
    assert (tmp_path/'fixed-calendar-rescores.json').exists()


def test_stable_calendar_does_not_start_another_native_evaluator(tmp_path, monkeypatch):
    model, result, calls = setup_case(tmp_path, monkeypatch)
    result.update(road_miles=200,travel_minutes=280)
    saved, _, _ = national_runner.settle_selected_calendar(model, None, tmp_path, result)
    assert saved == result
    assert not calls


def test_fixed_visit_replay_cannot_silently_change_species_credit(tmp_path, monkeypatch):
    model, result, _ = setup_case(tmp_path, monkeypatch, replay_species=1)
    with pytest.raises(ValueError,match='preserve the feasible species count'):
        national_runner.settle_selected_calendar(model, None, tmp_path, result)


def test_unstable_costs_remain_an_error_after_bounded_replays(tmp_path, monkeypatch):
    model, result, calls = setup_case(tmp_path, monkeypatch)
    monkeypatch.setattr(national_runner, 'materialize', lambda *args, **kwargs:
        {'days':[{'road_miles':400,'travel_minutes':200}]})
    with pytest.raises(ValueError,match='did not stabilize'):
        national_runner.settle_selected_calendar(model, None, tmp_path, result, max_rescores=1)
    assert calls == [[17,91]]
