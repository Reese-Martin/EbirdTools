"""Fail-closed scoring decisions and exact event/date evidence boundaries."""

import importlib.util
from pathlib import Path
import csv
import json
import shutil

from bigyear_planner.regional_exploration import RegionalExplorer
import numpy as np
import duckdb
import pytest
from test_eod_archive import eod_input  # noqa: F401

SPEC=importlib.util.spec_from_file_location('audit_countability',Path(__file__).parents[1]/'scripts/audit_countability.py')
audit=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(audit)


def record(name='Pachyramphus major',day='2024-01-07',locality='Resaca de la Palma SP'):
    row={'record':'GBIF:123','source_name':name,'date':day,'latitude':25.99,'longitude':-97.56}
    raw={'key':123,'datasetKey':'4fa7b334-ce0d-4e88-aaae-2e0c138d049e'}
    raw.update({audit.DWC+k:v for k,v in {'scientificName':name,'taxonConceptID':'concept',
         'year':day[:4],'month':day[5:7],'day':day[8:],'country':'United States',
         'stateProvince':'Texas','county':'Cameron','locality':locality,
         'decimalLatitude':'25.99','decimalLongitude':'-97.56','basisOfRecord':'HumanObservation',
         'occurrenceStatus':'PRESENT'}.items()})
    return row,raw,{'source_taxon_id':'concept'}


def test_only_corroborated_rarity_event_gets_credit():
    default=audit.general_decision('Pachyramphus major','5',True)
    assert default[0]=='unresolved'
    assert audit.record_decision(*record(),default)[0]=='supported'
    assert audit.record_decision(*record(day='2024-04-01'),default)[0]=='unresolved'
    assert audit.record_decision(*record(locality='Other refuge'),default)[0]=='unresolved'


def test_missing_and_conflicting_metadata_never_passes():
    row,raw,taxon=record('Cardinalis cardinalis')
    default=audit.general_decision('Cardinalis cardinalis','1',True)
    assert audit.record_decision(row,None,taxon,default)[0]=='unresolved'
    raw[audit.DWC+'day']='08'
    assert audit.record_decision(row,raw,taxon,default)[0]=='unresolved'
    row,raw,taxon=record('Cardinalis cardinalis')
    raw['http://unknown.org/status']='not_reviewed'
    assert audit.record_decision(row,raw,taxon,default)[0]=='supported'
    raw[audit.DWC+'locality']='County Zoo aviary'
    assert audit.record_decision(row,raw,taxon,default)[0]=='unresolved'


def test_historical_exclusion_and_population_unresolved_are_separate():
    assert audit.general_decision('Melopsittacus undulatus','6',True)[0]=='excluded'
    assert audit.general_decision('Amazona finschi','2',True)[0]=='unresolved'
    assert audit.general_decision('Gymnogyps californianus','1',True)[0]=='unresolved'
    assert audit.general_decision('Passer domesticus','1',True)[0]=='supported'


def test_zero_credit_opportunities_remain_exact_and_do_not_inherit_neighbors():
    explorer=object.__new__(RegionalExplorer)
    explorer.records=np.array([(0,0,0,0),(0,1,0,2),(1,2,2,0),(1,3,2,1),(2,4,3,0)],
              dtype=[('day','u4'),('location','u4'),('offset','u8'),('count','u4')])
    explorer.species=np.array([0,1,2],dtype='u4')
    assert explorer.novelty(np.array([False,True,False])).tolist()==[0,1,0,1,0]
    assert explorer.novelty(np.zeros(3,dtype=bool),0,1).tolist()==[0]
    assert explorer.novelty(np.zeros(3,dtype=bool),2,3).tolist()==[0]


def test_source_override_is_exact_record_date_and_coordinate_bound(eod_input,tmp_path):  # noqa: F811
    from bigyear_planner.config import Config
    from bigyear_planner.eod import prepare_eod,attach_dataset
    from bigyear_planner.util import dump,sha256,write_csv

    _,archive,taxonomy,land,rows=eod_input
    dataset=tmp_path/'dataset'
    manifest=prepare_eod(archive,taxonomy,land,dataset,Config(),'fixture','fixture',synthetic=True)
    mappings=list(csv.DictReader((dataset/'taxonomy.csv').open()))
    mappings[0].update(aba_eligible='review',eod_presence_eligible='review')
    write_csv(dataset/'taxonomy.csv',mappings,list(mappings[0]))
    decision={'representative_gbifid':rows[0]['representative_gbifid'],
         'source_name':'Fixture bird','source_taxon_id':'exact-concept','decision':'supported',
         'date':'2024-01-01','latitude':rows[0]['decimallatitude'],'longitude':rows[0]['decimallongitude']}
    manifest['countability']={'policy':'fixture'}

    def query():
        write_csv(dataset/'countability-evidence.csv',[decision],list(decision))
        for name in ['countability-evidence.csv','taxonomy.csv']:
            manifest['outputs'][name]=sha256(dataset/name)
        dump(dataset/'manifest.json',manifest)
        with duckdb.connect() as con:
            evidence,_=attach_dataset(con,dataset)
            return con.execute(f'SELECT record_id FROM {evidence}').fetchall()

    assert query()==[('GBIF:'+rows[0]['representative_gbifid'],)]
    decision['latitude']=rows[1]['decimallatitude']
    assert query()==[]
    decision['latitude']=rows[0]['decimallatitude'];decision['date']='2024-01-02'
    assert query()==[]
    decision['date']='2024-01-01';decision['decision']='unresolved'
    assert query()==[]
    # Exercise the independent route validator as well as SQL classification:
    # an explicit supported occurrence may resolve a species-level review hold.
    decision['decision']='supported';query()
    dump(tmp_path/'source-retrieval.json',{'records':{}})
    policy={'id':'fixture','sources':{},'source_retrieval_sha256':sha256(tmp_path/'source-retrieval.json')}
    dump(dataset/'countability-policy.json',policy)
    manifest['taxonomy_policy']='fixture'
    manifest['countability']['policy_sha256']=sha256(dataset/'countability-policy.json')
    manifest['outputs']['countability-policy.json']=sha256(dataset/'countability-policy.json')
    dump(dataset/'manifest.json',manifest)
    from bigyear_planner.problem import build_problem
    from bigyear_planner.demo import synthetic_roads
    from bigyear_planner.runner import run
    from bigyear_planner.validate import validate

    cfg=Config(start_date='2024-01-01',end_date='2024-01-03')
    problem_path=tmp_path/'problem.json'
    build_problem(dataset,problem_path,cfg)
    roads=tmp_path/'roads.sqlite';synthetic_roads(problem_path,tmp_path/'roads.csv',roads)
    output=tmp_path/'search';run(problem_path,roads,output,seconds=.1,threads=1)
    checked=validate(output/'route.json',output/'problem.json',output/'roads.sqlite')
    assert checked['valid'],checked['errors']
    assert checked['species_total']==1 and checked['countability']['enabled']


def test_scoring_derivative_reuses_routing_identity_only_with_same_exact_catalog(eod_input,tmp_path):  # noqa: F811
    from bigyear_planner.compact_model import build,load
    from bigyear_planner.config import Config
    from bigyear_planner.eod import prepare_eod
    from bigyear_planner.util import dump,sha256

    _,archive,taxonomy,land,_=eod_input
    dataset=tmp_path/'dataset'
    config=Config(target_locations=10000000,walking_speed_kmh=2.2)
    prepare_eod(archive,taxonomy,land,dataset,config,'fixture','fixture',synthetic=True)
    original=build(dataset,tmp_path/'original',config)
    derived=tmp_path/'derived';derived.mkdir()
    meta=json.loads(json.dumps(original.meta))
    for name in meta['components']:
        shutil.copyfile(original.root/name,derived/name)
    records=original.records.copy();records['count'][0]=0;records['offset'][1]=0
    records.tofile(derived/'opportunities.bin')
    np.asarray(original.species[1:],dtype='<u4').tofile(derived/'species.bin')
    meta['species_entries']=1
    meta['components']={name:sha256(derived/name) for name in meta['components']}
    meta['countability_source']={'path':str(original.path),'sha256':sha256(original.path)}
    dump(derived/'model.json',meta)
    result=load(derived/'model.json')
    assert result.routing_model_sha256==original.routing_model_sha256
    assert result.opportunity(0)['species']==[]
    assert result.meta['locations']==2 and len(result.records)==2
    result.close()
    coords=np.asarray(original.coords).copy();coords[0,0]+=0.1
    coords.tofile(derived/'coordinates.bin')
    meta['components']['coordinates.bin']=sha256(derived/'coordinates.bin');dump(derived/'model.json',meta)
    with pytest.raises(ValueError,match='routing coordinates'):
        load(derived/'model.json')
    original.close()
