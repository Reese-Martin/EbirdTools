"""Real-route negative controls for the independent joint charter-day auditor.

The positive calendar/source audit is required first. Tests use copies, leave
verified artifacts unchanged, and reuse its already-bound shoreline windows.
"""
import argparse
from copy import deepcopy
from pathlib import Path

from shapely import from_wkb
from shapely.prepared import prep

from bigyear_planner.joint_validate import audit_day
from bigyear_planner.marine import Coastline,Jurisdiction
from bigyear_planner.travel import load_roads
from bigyear_planner.util import dump,read,sha256


def main(folder):
    folder=Path(folder).resolve();r=read(folder/'route.json');p=read(folder/'problem.json')
    audit=read(folder/'validation.json');digest=sha256(folder/'route.json')
    if not audit['valid'] or digest!=read(folder/'result.json')['route_sha256']:
        raise ValueError('negative controls require a route-bound valid positive result')
    joint=p['joint_marine'];catalog=read(joint['catalog_path']);ports={v['id']:v for v in catalog['ports']}
    trips={v['id']:v for v in catalog['trips']};coasts={}
    for key in {t['port'] for t in joint['selected_trips'].values()}:
        port=ports[key];coast=Coastline.__new__(Coastline)
        if sha256(port['coast_path'])!=port['coast_sha256']:raise ValueError('bound coast changed')
        coast.land=from_wkb(Path(port['coast_path']).read_bytes());coast.prepared=prep(coast.land)
        coast.bounds=tuple(read(Path(port['coast_path']).with_suffix('.json'))['bounds']);coasts[key]=coast
    ctx={'joint':joint,'catalog':catalog,'trips':trips,'ports':ports,'coasts':coasts,
        'held':{v['species_id'] for v in read(joint['holds_path'])['species']},
        'jurisdiction':Jurisdiction(Path(joint['marine_data'])/'sources/countries-ne-5.1.2.geojson'),
        'boat_days':0,'max_activity':0.}
    roads=load_roads(p,folder/'roads.sqlite')
    try:
        daynum=next(i for i,d in enumerate(r['days']) if d['kind']=='pelagic');original=r['days'][daynum]
        seen={s for d in r['days'][:daynum] for stop in d['stops'] for s in stop['species']}
        opp=p['opportunities'][original['stops'][0]['opportunity']]
        here=original['legs'][0]['from'] if original['legs'][0]['mode']=='road' else opp['location']
        def evaluate(day):
            errors=[]
            audit_day(ctx,day,here,p,r,roads,set(seen),lambda okay,message:errors.append(message) if not okay else None,set(),[])
            return errors
        if evaluate(deepcopy(original)):raise ValueError('positive charter control failed')
        variants=[]
        shift=deepcopy(original);shift['stops'][0]['lat']+=.0001
        variants.append(('source_pin_shift',shift,'source coordinates'))
        fast=deepcopy(original);leg=next(v for v in fast['legs'] if v['mode']=='boat');leg['minutes']-=1
        fast['travel_minutes']-=1;fast['activity_minutes']-=1
        variants.append(('understated_sailing',fast,'sailing speed'))
        board=deepcopy(original);board['boarding_minutes']=0;board['activity_minutes']-=60
        variants.append(('missing_boarding_time',board,'boarding total'))
        held=deepcopy(original);held['stops'][0]['species'].append(next(iter(ctx['held'])))
        variants.append(('unresolved_boat_species_credit',held,'boat species/holds'))
        results=[]
        for name,day,expected in variants:
            errors=evaluate(day);passed=any(expected in e for e in errors)
            results.append({'mutation':name,'rejected_for_expected_reason':passed,'errors':errors})
            if not passed:raise ValueError('negative control was not rejected for its specific cause: '+name)
        result={'valid':True,'positive_route_sha256':digest,'positive_day':original['date'],
            'scope':'Independent charter physics/access/source-coordinate day auditor; complete positive calendar/source audit required separately.',
            'controls':results,'verified_files_unchanged':sha256(folder/'route.json')==digest}
        dump(folder/'negative-controls.json',result);print(result,flush=True)
    finally:roads.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);args=parser.parse_args();main(args.folder)
