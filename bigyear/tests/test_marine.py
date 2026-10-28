from collections import OrderedDict
from copy import deepcopy
import json

from shapely.geometry import box, mapping, Point
from shapely.prepared import prep

from bigyear_planner.marine import Coastline, Jurisdiction, charter_track
from bigyear_planner.marine_search import free_windows,audit_port_walk
from bigyear_planner.marine_validate import publisher_check


def coast(land):
    value=Coastline.__new__(Coastline)
    value.bounds=(-125,20,-65,50);value.land=land
    value.prepared=prep(land);value.memo=OrderedDict()
    return value


def test_charter_returns_to_port_visits_exact_pin_and_charges_observation_time():
    water=coast(box(-81,24,-80,25))
    a=[-79.9,24.5];pin=[-79.7,24.513249]
    result=charter_track(water,a,[pin],{'birding_minutes':120,'activity_minutes':720})
    assert result['geometry']['coordinates'][0]==result['geometry']['coordinates'][-1]==a
    assert result['legs'][0]['geometry']['coordinates'][-1]==pin
    assert result['birding_minutes']==120
    assert result['activity_minutes']==result['sailing_minutes']+180
    assert charter_track(water,a,[[-76,24.5]],{'birding_minutes':120,'activity_minutes':720}) is None


def test_water_router_does_not_walk_through_a_headland_or_move_an_on_land_pin():
    water=coast(box(-80.01,24.48,-79.99,24.52))
    a,b=[-80.03,24.5],[-79.97,24.5]
    path=water.route(a,b)
    assert path and len(path)>2
    assert path[0]==a and path[-1]==b
    assert water.clear(path)
    assert water.route(a,[-80,24.5]) is None


def test_offshore_jurisdiction_uses_nearest_country_and_holds_uncertain_borders(tmp_path):
    source=tmp_path/'countries.json'
    source.write_text(json.dumps({'features':[
        {'properties':{'ADMIN':'United States of America'},'geometry':mapping(box(-81,25,-80,26))},
        {'properties':{'ADMIN':'The Bahamas'},'geometry':mapping(box(-79.5,25,-78.5,26))}]}))
    boundary=Jurisdiction(source)
    assert boundary.check(25.5,-79.9)['status']=='verified_lower48_waters'
    assert boundary.check(25.5,-79.7)['status']=='jurisdiction_boundary_hold'
    assert boundary.check(25.5,-84.5)['status']=='jurisdiction_boundary_hold'


def test_free_windows_preserve_flights_and_track_the_actual_previous_position():
    base={'initial_location':0,'days':[
        {'kind':'rest','stops':[],'legs':[]},
        {'kind':'flight','stops':[],'legs':[{'from':0,'to':4}]},
        {'kind':'birding','stops':[{'location':6}],'legs':[{'from':4,'to':6}]},
        {'kind':'rest','stops':[],'legs':[]},
        {'kind':'rest','stops':[],'legs':[]},
        {'kind':'rest','stops':[],'legs':[]} ]}
    assert free_windows(base)==[(0,1,0),(3,6,6)]
    assert free_windows(base,{4})==[(0,1,0),(3,4,6),(5,6,6)]


def test_dock_walk_is_round_trip_at_22_kmh_and_open_water_is_held():
    config={'walking_threshold_m':100,'walking_speed_kmh':2.2}
    location={'lon':-80,'lat':25,'road_access':{'lon':-80.002,'lat':25}}
    land=box(-81,24,-80.00005,26)
    result=audit_port_walk({'id':'test'},location,land,config)
    assert result['walking_meters']>400
    assert abs(result['walking_minutes']-result['walking_meters']/2200*60)<1e-8
    import pytest
    with pytest.raises(ValueError,match='open water'):
        audit_port_walk({'id':'test'},location,Point(-81,24).buffer(.01),config)


def test_publisher_record_cannot_credit_a_different_date_point_or_captive_bird():
    row={'representative_gbifid':'123','v_scientificname':'A a','v_taxonconceptid':'concept',
        'date':'2024-05-04','stateprovince':'Florida','lat':25.123456,'lon':-80.123456,'species_id':'aba2024:A_a'}
    tax={'source_taxon_id':'concept','species_id':'aba2024:A_a','aba_eligible':'1',
        'eod_presence_eligible':'1','eligible_states':'*','eligible_regions':''}
    raw={'key':123,'datasetKey':'4fa7b334-ce0d-4e88-aaae-2e0c138d049e',
        'scientificName':'A a','taxonConceptID':'concept','basisOfRecord':'HumanObservation',
        'occurrenceStatus':'PRESENT','year':'2024','month':'5','day':'4','country':'United States',
        'stateProvince':'Florida','decimalLatitude':'25.123456','decimalLongitude':'-80.123456'}
    assert publisher_check(row,raw,tax)=='supported'
    shifted=deepcopy(raw);shifted['decimalLatitude']='25.1234'
    assert publisher_check(row,shifted,tax)=='publisher_metadata_mismatch'
    shifted=deepcopy(raw);shifted['day']='5'
    assert publisher_check(row,shifted,tax)=='publisher_metadata_mismatch'
    shifted=deepcopy(raw);shifted['isCaptive']=True
    assert publisher_check(row,shifted,tax)=='explicit_captive_hold'


def test_expedition_fits_between_fixed_days_and_keeps_full_driving_days():
    from bigyear_planner.marine_search import choose_expeditions,extend_calendar
    from bigyear_planner.road_matching import evidence,BROUTER_POLICY
    config={'walking_threshold_m':100,'walking_speed_kmh':2.2,'birding_minutes':120,
            'activity_minutes':720,'drive_minutes':480}
    home={'id':'home','name':'home','lon':-80.03,'lat':25,'state':'US-FL',
          'road_access':{'lon':-80.03,'lat':25,'offset_meters':0}}
    dock={'id':'dock','name':'dock','lon':-80,'lat':25,'state':'US-FL',
          'road_access':{'lon':-80.0001,'lat':25,'offset_meters':10}}
    def day(i):return {'date':f'2024-01-{i+1:02d}','kind':'rest','stops':[],'legs':[],
                       'road_miles':0,'travel_minutes':0,'activity_minutes':0}
    days=[day(i) for i in range(7)]
    for i in [0,6]:days[i].update(kind='birding',stops=[{'location':0,'species':['land']}])
    base={'year':2024,'config':config,'locations':[home],'days':days,'species_total':1,
          'initial_location':0,'assumptions':[]}
    water=coast(box(-82,24,-80.00005,26))
    trips=[]
    for i,s in [(2,'sea-a'),(4,'sea-b')]:
        pin=[-79.99,25.001+i*.0001];track=charter_track(water,[-80,25],[pin],config)
        trips.append({'id':str(i),'date':f'2024-01-{i+1:02d}','port':'dock',**track,
          'observations':[{'date':f'2024-01-{i+1:02d}','species':[s],'lat':pin[1],'lon':pin[0],
                           'latitude':str(pin[1]),'longitude':str(pin[0])}]})
    catalog={'ports':[{'id':'dock','name':'dock','boarding_point':[-80,25]}],'trips':trips}
    class Roads:
        release={'parameters':{'vmax':130}}
        minutes=60
        def location(self,i):return [home,dock][i]
        def __getitem__(self,pair):return {'miles':60,'minutes':self.minutes}
        def endpoint_evidence(self,a,b,cost):
            anchors=[[self.location(i)['road_access']['lon'],25] for i in [a,b]]
            return evidence({'waypoints':[{'location':x} for x in anchors],
                'routes':[{'distance':60*1609.344,'duration':self.minutes*60,
                    'geometry':{'type':'LineString','coordinates':anchors}}]},anchors,'frozen',BROUTER_POLICY)
    roads=Roads();found=choose_expeditions(base,catalog,roads,{'dock':water.land})
    assert found['new_species']=={'sea-a','sea-b'}
    result,_=extend_calendar(base,found,[home,dock],catalog)
    assert result['days'][0]['kind']==result['days'][6]['kind']=='birding'
    assert [d['kind'] for d in result['days'][1:6]]==['road_transit','pelagic','rest','pelagic','road_transit']
    assert result['days'][1]['legs'][0]['from']==result['days'][5]['legs'][0]['to']==0
    repeated=deepcopy(catalog)
    first=repeated['trips'][0]['observations'][0]
    repeated['trips'][1]['observations'][0].update(lat=first['lat'],lon=first['lon'],
        latitude=first['latitude'],longitude=first['longitude'])
    same_point=[first['lon'],first['lat']]
    repeated['trips'][1].update(charter_track(water,[-80,25],[same_point],config))
    repeat_search=choose_expeditions(base,repeated,roads,{'dock':water.land})
    repeat_route,_=extend_calendar(base,repeat_search,[home,dock],repeated)
    pins=[s['location'] for d in repeat_route['days'] for s in d['stops'] if d['kind']=='pelagic']
    assert len(pins)==2 and pins[0]==pins[1]
    assert len({v['id'] for v in repeat_route['locations']})==len(repeat_route['locations'])
    held_search=choose_expeditions(base,catalog,roads,{'dock':water.land},species_holds={'sea-b'})
    held_route,_=extend_calendar(base,held_search,[home,dock],catalog,species_holds={'sea-b'})
    assert held_search['new_species']=={'sea-a'} and held_route['species_total']==2
    roads.minutes=800
    assert not choose_expeditions(base,catalog,roads,{'dock':water.land})['expeditions']
    # A static unknown car pair must not be re-queried for every free window
    # or every portfolio round, and must never be credited as a usable route.
    from bigyear_planner.regional_roads import DeferredRoad
    base['days'].extend(day(i) for i in range(7,13))
    base['days'][12].update(kind='birding',stops=[{'location':0,'species':['land']}])
    bad_port={'id':'unknown','name':'unknown','boarding_point':[-80,25]}
    bad_trips=[{**trips[0],'id':'unknown-'+str(i),'port':'unknown','date':f'2024-01-{i+1:02d}',
                'observations':[{**trips[0]['observations'][0],'species':['unknown-seabird']}]} for i in [2,8]]
    class UnknownRoads(Roads):
        unknown_calls=0
        def location(self,i):return dock if i==2 else super().location(i)
        def __getitem__(self,pair):
            if 2 in pair:
                self.unknown_calls+=1;raise DeferredRoad('not resolved')
            return super().__getitem__(pair)
    unknown=UnknownRoads()
    evaluated=choose_expeditions(base,{'ports':[*catalog['ports'],bad_port],'trips':[*trips,*bad_trips]},
        unknown,{'dock':water.land,'unknown':water.land})
    assert unknown.unknown_calls==1
    assert 'unknown-seabird' not in evaluated['new_species']


def test_port_match_recovers_from_failed_waterward_probe_and_never_caches_unknown_as_infinite():
    from collections import Counter
    from types import SimpleNamespace
    import threading
    import numpy as np
    import pytest
    from bigyear_planner.marine_roads import MarineRoads
    from bigyear_planner.regional_roads import DeferredRoad
    value=MarineRoads.__new__(MarineRoads)
    value.model=SimpleNamespace(locations=[{'kind':'marine_port'}],coords=np.array([[25.,-80.]]),
        meta={'config':{'activity_minutes':720,'birding_minutes':120,'walking_speed_kmh':2.2,'walking_threshold_m':100}})
    value.locks=[threading.Lock()];value.anchors=np.array([[25.,-80.,float('inf')]])
    value.stats=Counter();calls=[]
    def request(coords,**kwargs):
        calls.append(coords)
        if coords[1][1]<=25:raise DeferredRoad('destination waterward')
        return {'features':[{'geometry':{'type':'LineString','coordinates':[[-80.0001,25],[-80.0001,25.02]]}}]}
    value.request=request
    result=value.access(0)
    assert result==(25.,-80.0001,120.)
    assert len(calls)==4
    assert value.access(0)==result and len(calls)==4
    value.anchors[:]=float('nan')
    def failure(*args,**kwargs):raise DeferredRoad('unknown')
    value.request=failure
    with pytest.raises(DeferredRoad,match='not cached unreachable'):value.access(0)
    assert np.isnan(value.anchors[0,2])
