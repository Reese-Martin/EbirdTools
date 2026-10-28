"""Check cost, geometry and failure interpretation in the provider comparison."""
import importlib.util
import io
import json
from pathlib import Path
import urllib.error
import urllib.parse

import pytest

SPEC = importlib.util.spec_from_file_location(
    'router_benchmark', Path(__file__).parents[1]/'scripts/benchmark_west_routers.py')
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


@pytest.fixture
def query():
    return {'id':'1:2','anchors':[{'lat':47.1234567890123,'lon':-122.123456789012},
                                 {'lat':47.2,'lon':-122.2}]}


def test_providers_receive_identical_coordinates_and_no_beeline_override(query):
    b = urllib.parse.parse_qs(urllib.parse.urlsplit(BENCH.query_url('brouter',query)).query)
    coordinates = '|'.join(f"{p['lon']},{p['lat']}" for p in query['anchors'])
    assert b['lonlats']==[coordinates]
    assert b['profile:add_beeline']==['0'] and b['profile:use_offroad']==['0']
    assert b['profile:considerTurnRestrictions']==['1']
    assert coordinates.replace('|',';') in BENCH.query_url('osrm-mmap',query)
    strict=urllib.parse.parse_qs(urllib.parse.urlsplit(BENCH.query_url('brouter',query,strict=True)).query)
    assert strict['profile:use_dynamic_range']==['0']
    assert strict['profile:waypointCatchingRange']==['100']


def test_brouter_parses_seconds_meters_and_ferries_without_relabeling_them(query,monkeypatch):
    coordinates=[[p['lon'],p['lat'],100] for p in query['anchors']]
    body={'features':[{'properties':{'track-length':'12000','total-time':'600',
          'messages':[['WayTags'],['highway=service route=ferry']], 'creator':'BRouter-test'},
          'geometry':{'type':'LineString','coordinates':coordinates}}]}
    monkeypatch.setattr(BENCH.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(json.dumps(body).encode()))
    row,_=BENCH.request_road('brouter',query)
    assert row['status']=='ok'
    assert row['distance_meters']==12000 and row['duration_seconds']==600
    assert row['ferry_message_rows']==1 and max(row['endpoint_offsets_meters'])==0


def test_osrm_explicit_no_route_is_distinct_from_timeout(query,monkeypatch):
    def missing(*a,**k):
        raise urllib.error.HTTPError('http://local',400,'No route',None,
                                     io.BytesIO(b'{"code":"NoRoute"}'))
    monkeypatch.setattr(BENCH.urllib.request,'urlopen',missing)
    row,_=BENCH.request_road('osrm-mmap',query)
    assert row['status']=='unreachable' and 'distance_meters' not in row
    def timeout(*a,**k):
        raise TimeoutError('timed out')
    monkeypatch.setattr(BENCH.urllib.request,'urlopen',timeout)
    row,_=BENCH.request_road('brouter',query)
    assert row['status']=='timeout' and 'distance_meters' not in row


def test_negative_cost_is_a_provider_error_and_not_a_route(query,monkeypatch):
    body={'code':'Ok','routes':[{'distance':-1,'duration':10,'legs':[]}]}
    monkeypatch.setattr(BENCH.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(json.dumps(body).encode()))
    row,_=BENCH.request_road('osrm-mmap',query,geometry=False)
    assert row['status']=='provider-error' and 'distance_meters' not in row


def test_brouter_matching_failure_does_not_prove_road_unreachable(query,monkeypatch):
    def failed(*a,**k):
        raise urllib.error.HTTPError('http://local',400,'Unmatched',None,
              io.BytesIO(b'from-position not mapped in existing datafile'))
    monkeypatch.setattr(BENCH.urllib.request,'urlopen',failed)
    row,_=BENCH.request_road('brouter',query,strict=True)
    assert row['status']=='endpoint-not-matched' and 'distance_meters' not in row


def test_shape_agreement_distinguishes_same_path_from_different_roads():
    first=[[-122,47],[-121.99,47.01]]
    assert BENCH.shape_agreement(first,first)==1
    assert BENCH.shape_agreement(first,[[-123,47],[-122.99,47.01]])==0
