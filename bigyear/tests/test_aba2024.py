"""Historical scoring units and population/date restrictions remain explicit."""

import importlib.util
import json
from pathlib import Path

import duckdb
from test_countability import record

from bigyear_planner.countability import region_sql, region_supported

SPEC=importlib.util.spec_from_file_location('align_aba2024',Path(__file__).parents[1]/'scripts/align_aba2024.py')
# Script imports are intentionally local to the project scripts directory.
import sys
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
align=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(align)


def test_later_herring_split_collapses_to_one_2024_species():
    names=['Larus argentatus','Larus smithsonianus','Larus vegae']
    assert {align.ALIASES.get(n,n) for n in names}=={'Larus argentatus'}
    assert align.ALIASES.get('Vireo gilvus','Vireo gilvus')=='Vireo gilvus'
    assert align.ALIASES.get('Setophaga petechia','Setophaga petechia')=='Setophaga petechia'
    assert align.ALIASES.get('Numenius phaeopus','Numenius phaeopus')=='Numenius phaeopus'


def test_official_2024_membership_is_independent_of_rarity(monkeypatch):
    # Use the pinned public checklist rather than an ignored local analysis directory.
    monkeypatch.setattr(align, 'DATA', Path(__file__).parent/'fixtures/aba2024')
    aba=align.checklist()
    assert aba['Philomachus pugnax' if 'Philomachus pugnax' in aba else 'Calidris pugnax']['code']=='3'
    assert 'Machetornis rixosa' not in aba  # Provisional, not primary-approved.
    assert aba['Melopsittacus undulatus']['code']=='6'
    assert align.population_regions('Gymnogyps californianus')
    assert align.population_regions('Grus americana') is None


def test_population_rules_do_not_promote_outside_records_or_wrong_dates():
    parrot={'eligible_regions':json.dumps(align.population_regions('Amazona finschi'))}
    assert region_supported(parrot,'US-CA',32.74,-117.15,'2024-08-04')
    assert not region_supported(parrot,'US-TX',26.1,-98.2,'2024-01-25')
    swan={'eligible_regions':json.dumps(align.population_regions('Cygnus cygnus'))}
    assert region_supported(swan,'US-WA',48.5,-122.4,'2024-02-18')
    assert not region_supported(swan,'US-WA',48.5,-122.4,'2024-03-18')
    assert not region_supported({'eligible_regions':'[]'},'US-CA',35.,-119.,'2024-01-01')


def test_independent_sql_and_python_region_checks_agree_without_row_multiplication():
    z=align.population_regions('Cygnus cygnus')[0]
    cases=[('US-WA',48.5,-122.4,'2024-02-18'),('US-WA',48.5,-122.4,'2024-03-18'),
           ('US-OR',48.5,-122.4,'2024-02-18'),('US-WA',47.,-122.4,'2024-02-18')]
    tax={'eligible_regions':json.dumps([z,z])}  # overlapping envelopes
    with duckdb.connect() as con:
        con.execute('CREATE TABLE t(eligible_regions VARCHAR)')
        con.execute('INSERT INTO t VALUES (?)',[tax['eligible_regions']])
        con.execute('CREATE TABLE r(state VARCHAR,lat DOUBLE,lon DOUBLE,date DATE)')
        con.executemany('INSERT INTO r VALUES (?,?,?,?)',cases)
        result=con.execute('SELECT '+region_sql()+' FROM r CROSS JOIN t ORDER BY r.rowid').fetchall()
    assert [x[0] for x in result]==[region_supported(tax,*x) for x in cases]
    assert len(result)==len(cases)


def test_publisher_proxy_does_not_confuse_zoo_locality_with_captive_individual():
    row,raw,tax=record('Cardinalis cardinalis',locality='City Zoo grounds')
    row['state']='US-TX'
    tax.update(aba_eligible='1',eod_presence_eligible='1',eligible_regions='',
        review_basis='publisher_review_proxy',reference=align.previous.REVIEW)
    assert align.record_decision(row,raw,tax)[0]=='supported'
    raw[align.previous.DWC+'locality']='City Zoo captive aviary'
    assert align.record_decision(row,raw,tax)[0]=='unresolved'
    row,raw,tax0=record('Cardinalis cardinalis')
    row['state']='US-TX'
    raw={k.removeprefix(align.previous.DWC):v for k,v in raw.items()}
    assert align.record_decision(row,raw,tax)[0]=='supported'
    raw['stateProvince']='Oregon'
    assert align.record_decision(row,raw,tax)[0]=='unresolved'
