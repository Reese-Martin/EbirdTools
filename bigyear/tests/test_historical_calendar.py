"""A real non-leap catalog must not borrow dates from the adjacent year."""
import csv
from datetime import date

import pytest

from bigyear_planner.compact_model import build
from bigyear_planner.config import Config
from bigyear_planner.eod import prepare_eod
from bigyear_planner.eod_archive import SCHEMA, convert
from bigyear_planner.joint_marine import build_joint
from bigyear_planner.util import dump
from test_eod_archive import eod_input  # noqa: F401


@pytest.mark.parametrize('year,days',[(2023,365),(2024,366)])
def test_last_day_charter_uses_source_year_and_preserves_land(eod_input,tmp_path,year,days):  # noqa: F811
    _,_,taxonomy,land,rows = eod_input
    source = tmp_path/'year.tsv'
    with source.open('w',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=list(SCHEMA),delimiter='\t')
        writer.writeheader()
        for row in rows:
            writer.writerow({**row,'year':str(year)})
    archive = tmp_path/'year-archive'
    convert(source,archive,expected_rows=2,progress=lambda _:None)
    cfg = Config(year=year,target_locations=10000000,threads=1,memory_limit='256MB',walking_speed_kmh=2.2)
    prepare_eod(archive,taxonomy,land,tmp_path/'dataset',cfg,'fixture','fixture',synthetic=True)
    parent = build(tmp_path/'dataset',tmp_path/'parent',cfg)
    try:
        original = parent.records.copy()
        port = {'id':'fixture-port','name':'Fixture port','boarding_point':[-87,33],'state':'US-AL'}
        trip = {'id':'last-day','date':f'{year}-12-31','port':port['id'],
            'activity_minutes':180,'sailing_minutes':60,
            'observations':[{'species':['BIRD']}]}
        dump(tmp_path/'catalog.json',{'ports':[port],'trips':[trip]})
        dump(tmp_path/'holds.json',{'species':[]})
        joint = build_joint(parent.path,tmp_path/'catalog.json',tmp_path/'holds.json',tmp_path/'joint')
        try:
            assert len(joint.meta['joint_marine']['old_to_new_day_offsets'])==days
            assert int(joint.records[-1]['day'])==days-1
            assert int(joint.records[-1]['reserved'])==1
            for field in ['day','location','count','reserved']:
                assert (joint.records[:-1][field]==original[field]).all()
        finally:
            joint.close()
        trip['date'] = str(date(year+1,1,1))
        dump(tmp_path/'bad-catalog.json',{'ports':[port],'trips':[trip]})
        with pytest.raises(ValueError,match='outside the source model year'):
            build_joint(parent.path,tmp_path/'bad-catalog.json',tmp_path/'holds.json',tmp_path/'bad-joint')
    finally:
        parent.close()
