import csv
from copy import deepcopy
from pathlib import Path
import sys

import pytest
from shapely.geometry import box

from bigyear_planner.config import Config
from bigyear_planner.itinerary_replay import rows,template
from bigyear_planner.surface_access import audit_walk,validate_boat_trip
from bigyear_planner.countability import region_supported


def test_shared_ebd_observer_ids_match_whole_tokens(tmp_path):
    path=tmp_path/'ebd.txt'
    header=['OBSERVATION DATE','LATITUDE','LONGITUDE','STATE CODE','SCIENTIFIC NAME','OBSERVER ID','APPROVED']
    with path.open('w') as f:
        writer=csv.writer(f,delimiter='\t');writer.writerow(header)
        writer.writerow(['2024-02-29','40.000000000001','-75','US-PA','Bird one','obsr1,obsr2','1'])
        writer.writerow(['2024-03-01','40','-75','US-PA','Bird two','obsr12','1'])
    result=list(rows(path,'obsr1'))
    assert len(result)==1 and result[0]['date']=='2024-02-29'
    assert result[0]['latitude']=='40.000000000001'
    assert result[0]['reviewed']=='1'
    template(tmp_path/'pending')
    assert not list(csv.DictReader((tmp_path/'pending/dated-itinerary.csv').open()))


def test_open_water_is_held_even_with_enough_slow_walking_time():
    location={'lat':0.,'lon':0.,'road_access':{'lat':0.,'lon':.01}}
    result=audit_walk(location,Config(walking_speed_kmh=2.2).to_dict(),box(-1,-1,.001,1))
    assert result['walking_minutes']<120
    assert result['open_water_meters']>900 and result['status']=='marine_access_required'


def boat():
    return {'id':'synthetic-test','date':'2024-04-12','departure_port':'A','arrival_port':'A',
       'departure':'2024-04-12T08:00:00-07:00','arrival':'2024-04-12T18:00:00-07:00',
       'service_reference':'synthetic fixture','operated_evidence':'synthetic fixture',
       'foot_roundtrip_meters':2200,'exact_observations':[{'date':'2024-04-12',
          'source_record':'synthetic','latitude':'34.000000000001','longitude':'-120',
          'access_reference':'synthetic track','jurisdiction_status':'verified_lower48_waters'}]}


def test_full_boat_time_walking_and_private_permission_are_required():
    config=Config(activity_minutes=720,walking_speed_kmh=2.2).to_dict()
    trip=boat();result=validate_boat_trip(trip,config)
    assert result['activity_minutes']==660 and result['walking_minutes']==60
    trip['arrival']='2024-04-12T22:00:00-07:00'
    with pytest.raises(ValueError,match='full boat trip'):validate_boat_trip(trip,config)
    trip=boat();trip['private_land_required']=True
    with pytest.raises(ValueError,match='permission'):validate_boat_trip(trip,config)
    trip=boat();trip['exact_observations'][0]['jurisdiction_status']='unresolved'
    with pytest.raises(ValueError,match='jurisdiction'):validate_boat_trip(trip,config)


def test_dated_provisional_rules_do_not_authorize_hawaii_or_old_florida_population():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    try:from reconcile_2024_rules import revise
    finally:sys.path.pop(0)
    tax={'source_name':'Amazona oratrix','aba_eligible':'0','eod_presence_eligible':'0','review_basis':'old','eligible_states':'*','reference':'old','eligible_regions':''}
    result=revise(deepcopy(tax))
    assert result['score_tier']=='provisional-A'
    assert region_supported(result,'US-CA',34,-118,'2024-01-01')
    assert not region_supported(result,'US-FL',26,-80,'2024-01-01')
    for name in ['Parus major','Lophura nycthemera','Coturnix japonica']:
        assert revise({**tax,'source_name':name})['score_tier']=='held'


def test_replay_reports_exact_source_gaps_without_snapping_or_claiming_identity(tmp_path):
    import json
    import sqlite3
    from types import SimpleNamespace
    import numpy as np
    from bigyear_planner.itinerary_replay import replay,FIELDS
    from bigyear_planner.util import dump,sha256,write_csv
    config=Config(walking_speed_kmh=2.2).to_dict()
    db=sqlite3.connect(':memory:');db.execute('CREATE TABLE locations(idx,metadata)')
    db.executemany('INSERT INTO locations VALUES (?,?)',[(i,json.dumps({'state':'US-AL','lat':lat,'lon':-87.})) for i,lat in enumerate([33.000000100000,33.000000100001])])
    dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4'),('reserved','u4')]
    records=np.array([(0,0,0,1,0),(1,1,1,1,0)],dtype=dtype)
    write_csv(tmp_path/'taxonomy.csv',[{'source_name':'Fixture bird','species_id':'bird'}],['source_name','species_id'])
    source=tmp_path/'model.json';dump(source,{'synthetic':True})
    model=SimpleNamespace(meta={'config':config,'dataset':str(tmp_path),'species':[{'id':'bird'}]},
        path=source,locations=SimpleNamespace(db=db),records=records,
        opportunity=lambda i:{'species':[0]})
    base=dict(zip(FIELDS,['2024-01-01','33.000000100000','-87','US-AL','Fixture bird','S1','obsr1','08:00','120','1']))
    observations=[base,{**base,'date':'2024-01-02'},{**base,'latitude':'33.000001'},
                  {**base,'scientific_name':'Other bird'},{**base,'reviewed':''}]
    path=tmp_path/'observations.csv';write_csv(path,observations,FIELDS)
    manifest={'subject':'Synthetic fixture, not the recordholder','observer_id':'obsr1',
       'identity_reference':'Synthetic fixture only','itinerary_path':str(path),'itinerary_sha256':sha256(path)}
    dump(tmp_path/'input.json',manifest)
    result=replay(model,tmp_path/'input.json',tmp_path/'replayed')
    assert result['matched_species']==1 and result['selected']==[0]
    assert result['status_counts']=={'exact_date_location_species_supported':1,'no_exact_dated_species_support':1,
       'no_exact_model_location':1,'taxon_not_scored':1,'unreviewed_input':1}
    assert not result['exact_recordholder_replay_complete']
    manifest['identity_reference']=None;dump(tmp_path/'input.json',manifest)
    with pytest.raises(ValueError,match='identity'):replay(model,tmp_path/'input.json',tmp_path/'invalid')
    db.close()


def test_provisional_occurrence_checks_hold_explicit_captive_metadata(monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    try:import reconcile_2024_rules as rules
    finally:sys.path.pop(0)
    monkeypatch.setattr(rules.primary,'record_decision',lambda *args:('supported','fixture','fixture'))
    for raw in [{'captive':True},{'establishmentMeans':'captive'},
                {'http://rs.tdwg.org/dwc/terms/occurrenceRemarks':'An escaped pet was reported here'}]:
        assert rules.record_decision({},raw,{})[0]=='unresolved'
    assert rules.record_decision({},{'establishmentMeans':'introduced'},{})[0]=='supported'


def test_viewer_tier_counts_are_bound_to_frozen_taxonomy(tmp_path):
    from bigyear_planner.display import attach_scoring_tiers
    from bigyear_planner.util import dump,sha256,write_csv
    dataset=tmp_path/'dataset';dataset.mkdir()
    tax=dataset/'taxonomy.csv'
    write_csv(tax,[{'species_id':'a','score_tier':'primary'},{'species_id':'b','score_tier':'provisional-A'}],['species_id','score_tier'])
    dump(dataset/'manifest.json',{'inputs':{'taxonomy':{'sha256':sha256(tax)}}})
    dump(tmp_path/'problem.json',{'dataset':str(dataset)})
    route={'species_total':2,'days':[{'stops':[{'species':['a','b','a']}]}],
      'provenance':{'problem_sha256':sha256(tmp_path/'problem.json'),'dataset_manifest_sha256':sha256(dataset/'manifest.json')}}
    shown=attach_scoring_tiers(tmp_path/'route.json',deepcopy(route))
    assert shown['scoring_tiers']['primary']==1 and shown['scoring_tiers']['provisional']==1
    assert shown['scoring_tiers']['provisional_species_ids']==['b']
    tax.write_text(tax.read_text().replace('provisional-A','primary'))
    assert 'scoring_tiers' not in attach_scoring_tiers(tmp_path/'route.json',deepcopy(route))
