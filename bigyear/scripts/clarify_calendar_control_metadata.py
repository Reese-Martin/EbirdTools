"""Correct a legacy description; independently revalidate unchanged selections."""
from copy import deepcopy
import os

from bigyear_planner.util import ROOT,dump,read,sha256
from bigyear_planner.validate import validate


def main():
    folder=ROOT/'runs/lower48-eod-2024/calendar-study-control-2024-v1'
    route_path=folder/'route.json';before=read(route_path);audit=read(folder/'validation.json')
    if (folder/'metadata-clarification.json').exists():
        if not audit['valid'] or audit['route_sha256']!=sha256(route_path):raise ValueError('clarified control audit changed')
        print({'already_clarified':True});return
    if not audit['valid'] or audit['route_sha256']!=sha256(route_path):raise ValueError('control lacks route-bound positive audit')
    code=(folder/'code-snapshot/routing_resolution.py').read_text()
    if 'def nearest_airports(self,location,count=24)' not in code or 'self.airport_pair_limit=6' not in code:
        raise ValueError('frozen control does not support the asserted airport policy')
    route=deepcopy(before);changed=[]
    for i,value in enumerate(route['assumptions']):
        if 'Flight proposals consider eight nearby airports' in value:
            route['assumptions'][i]=value.replace('eight nearby airports','24 nearby airports').replace('up to two alternatives','up to 6 alternatives')
            changed.append(i)
    if len(changed)!=1:raise ValueError('expected exactly one legacy flight-description correction')
    old={}
    for name in ['route.json','validation.json','result.json','benchmark.json','countability-source-validation.json']:
        old[name]=sha256(folder/name);backup=folder/(name.removesuffix('.json')+'-before-metadata-clarification.json')
        if not backup.exists():os.link(folder/name,backup)
    # dump is atomic: the original inode remains available at the backup link.
    dump(route_path,route)
    try:
        checked=validate(route_path,folder/'problem.json',folder/'roads.sqlite')
        if not checked['valid']:raise ValueError('control metadata revalidation failed: '+str(checked['errors'][:5]))
    except BaseException:
        dump(route_path,before)
        raise
    dump(folder/'validation.json',checked)
    result=read(folder/'result.json');result['route_sha256']=sha256(route_path)
    result['flight_description_corrected_to_executed_policy']=True;dump(folder/'result.json',result)
    countability=read(folder/'countability-source-validation.json');countability['route_sha256']=sha256(route_path)
    dump(folder/'countability-source-validation.json',countability)
    benchmark=read(folder/'benchmark.json');benchmark['validation']=checked;dump(folder/'benchmark.json',benchmark)
    dump(folder/'metadata-clarification.json',{'valid':True,'fields':['assumptions['+str(i)+']' for i in changed],
        'reason':'Legacy prose said8airports/2pairs; frozen executed ResolvedJointRoads actually used24/6.',
        'old_sha256':old,'new_route_sha256':sha256(route_path),'new_validation_sha256':sha256(folder/'validation.json'),
        'native_search_repeated':False,'calendar_species_coordinates_geometry_changed':False,
        'species':route['species_total']})
    print({'valid':True,'species':route['species_total'],'metadata_only':True})


if __name__=='__main__':main()
