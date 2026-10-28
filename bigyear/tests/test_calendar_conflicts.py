import numpy as np

from bigyear_planner.calendar_conflicts import pair_conflict,clique_cover
from bigyear_planner.config import Config


def test_full_flight_days_conflict_even_with_generous_car_and_walking_slack():
    config=Config(walking_speed_kmh=2.2,activity_minutes=720).to_dict()
    swift=np.asarray([[1,137,39.,-75.,0]])
    knot=np.asarray([[2,135,47.,-124.,1],[3,140,47.,-124.,2]])
    proof=pair_conflict(swift,knot,config)
    assert proof and proof['source_pairs_checked']==2 and proof['minimum_car_distance_margin_km']>500
    later=np.vstack([knot,[4,141,47.,-124.,3]])
    assert pair_conflict(swift,later,config) is None


def test_shared_charter_can_credit_two_taxa_but_two_charters_cannot_share_date():
    config=Config(walking_speed_kmh=2.2,activity_minutes=720).to_dict()
    a=np.asarray([[1,137,36.,-122.,1]])
    b=np.asarray([[2,137,36.,-122.,2]])
    assert pair_conflict(a,b,config)['same_date_charter_exclusive_pairs']==1
    assert pair_conflict(a,a,config) is None
    land=np.asarray([[3,138,36.1,-122.,0]])
    assert pair_conflict(a,land,config) is None


def test_clique_certificate_is_disjoint_and_does_not_treat_a_path_as_a_clique():
    graph={0:{1,2},1:{0,2,3},2:{0,1},3:{1,4},4:{3}}
    cover=clique_cover(graph,attempts=50);used=set()
    for group in cover:
        assert not used&set(group)
        assert all(b in graph[a] for a in group for b in group if a!=b)
        used.update(group)
    assert sum(len(g)-1 for g in cover)>=2
