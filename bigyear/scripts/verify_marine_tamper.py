"""Negative controls on the real saved marine result; original files untouched."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile

from bigyear_planner.marine_validate import validate_marine
from bigyear_planner.util import ROOT,dump,read,sha256

OUT=ROOT/'runs/lower48-eod-2024/marine-charter-2024-v2'


def main():
    route=read(OUT/'route.json');audit=read(OUT/'validation.json')
    if not audit['valid'] or audit['route_sha256']!=sha256(OUT/'route.json'):
        raise ValueError('positive complete source/calendar audit is required first')
    boat=next(i for i,d in enumerate(route['days']) if d['kind']=='pelagic')
    controls=[]
    def changed(label,mutate,expected):
        trial=deepcopy(route);mutate(trial)
        controls.append((label,trial,expected))
    changed('changed_exact_coordinate',lambda r:r['days'][boat]['stops'][0].update(
        lat=r['days'][boat]['stops'][0]['lat']+.0001),'marine source pin changed')
    changed('understated_boat_time',lambda r:r['days'][boat]['legs'][0].update(
        minutes=r['days'][boat]['legs'][0]['minutes']/2),'boat distance/speed differs')
    changed('held_island_species',lambda r:r['days'][boat]['stops'][0]['species'].append(
        'aba2024:Aphelocoma_insularis'),'boat species differ from candidate/access holds')
    flight=next(i for i,d in enumerate(route['days']) if d['kind']=='flight')
    changed('overwritten_flight_day',lambda r:r['days'][flight].update(kind='rest'),
            'terrestrial activity changed')
    results=[]
    with tempfile.TemporaryDirectory(prefix='marine-negative-controls-') as temporary:
        temp=Path(temporary)
        for name in ['problem.json','roads.sqlite','marine-plan.json','marine-source-retrieval.json']:
            os.link(OUT/name,temp/name)
        (temp/'marine-verbatim').symlink_to(OUT/'marine-verbatim',target_is_directory=True)
        for label,trial,expected in controls:
            dump(temp/'route.json',trial)
            # The successful positive audit just rechecked the unchanged base
            # hashes and full source archive. These negative controls isolate
            # the independent marine/calendar checks, with no network/router.
            checked=validate_marine(temp/'route.json',temp/'problem.json',temp/'roads.sqlite',
                                    base_validation={'valid':True})
            passed=not checked['valid'] and any(expected in e for e in checked['errors'])
            results.append({'control':label,'rejected_as_expected':passed,'errors':checked['errors']})
            if not passed:raise ValueError('negative control accepted or wrong rejection: '+label)
    dump(OUT/'negative-controls.json',{'valid':True,'positive_route_sha256':sha256(OUT/'route.json'),
        'baseline_full_audit_reused':True,'controls':results})
    print({'negative_controls_rejected':len(results)},flush=True)


if __name__=='__main__':main()
