"""Check the served corrected West Coast route against its immutable snapshot."""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

from bigyear_planner.road_matching import POLICY, gap_m
from bigyear_planner.travel import road_point
from bigyear_planner.util import ROOT, dump, read, sha256


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs',type=Path,default=ROOT/'runs/west-coast-endpoints-2024')
    parser.add_argument('--port',type=int,default=8770)
    args=parser.parse_args()
    root = args.runs.resolve()
    path = root/'mmap-600s/route.json'
    digest = sha256(path)
    frozen = read(path)
    with tempfile.TemporaryDirectory(prefix='bigyear-endpoint-viewer-') as folder:
        fixture = Path(folder)/'served.json'
        with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/api/route/mmap-600s/route.json',timeout=120) as source:
            with fixture.open('wb') as target:
                shutil.copyfileobj(source,target)
        shown = read(fixture)
        for key,value in frozen.items():
            if key != 'days':
                assert shown[key] == value, key
        road_legs = 0
        for original, displayed in zip(frozen['days'],shown['days'], strict=True):
            for key,value in original.items():
                if key != 'legs':
                    assert displayed[key] == value, (original['date'],key)
            for a,b in zip(original['legs'],displayed['legs'],strict=True):
                for key,value in a.items():
                    if key not in {'geometry','schematic'}:
                        assert b[key] == value, (original['date'],key)
                if b['mode'] == 'road':
                    assert b['geometry']['type'] == 'LineString' and not b['schematic']
                    anchors = [[road_point(shown['locations'][b[k]])[v] for v in ('lon','lat')]
                               for k in ('from','to')]
                    vertices = b['geometry']['coordinates']
                    assert max(gap_m(x,y) for x,y in zip(anchors,(vertices[0],vertices[-1]))) <= POLICY['endpoint_tolerance_m']
                    road_legs += 1
        assert shown['observer_coverage']['annual_species'] == 570
        assert len(shown['observer_coverage']['days']) == len(shown['days']) == 366
        assert shown['display_geometry']['verified_road_legs'] == road_legs
        job = subprocess.run(['node','--test','tests/playback.test.mjs','tests/activity.test.mjs'],cwd=ROOT,
                             env={**os.environ,'BIGYEAR_PILOT_ROUTE':str(fixture)},
                             check=True,text=True,capture_output=True)
        (root/'viewer-playback-tests.txt').write_text(job.stdout)
        assert sha256(path) == digest
    result = {'valid':True,'days_verified':366,'road_legs_verified':road_legs,
              'source_coordinates_and_evidence_unchanged':True,
              'route_species':frozen['species_total'],'all_observer_species':570,
              'route_sha256':digest,'live_browser_logic_tests':int(re.search(r'# tests (\d+)',job.stdout)[1]),
              'temporary_fixture_removed':True,
              'routing_endpoint_audit_passed':True,
              'scope':'Served data integrity, full-path endpoints, daily playback and walking/activity totals, and all-observer curve. Footpaths and legal access are not certified.'}
    dump(root/'viewer-verification.json',result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
