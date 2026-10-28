"""Verify saved regional output bindings and the actual served national viewer."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import urllib.request

from bigyear_planner.util import ROOT, dump, read, sha256

OUT = ROOT/'runs/lower48-eod-2024'


def main():
    result = read(OUT/'regional-benchmark-result.json')
    tag = result['run'];folder = OUT/tag
    audit = read(folder/'validation.json')
    if not result['valid'] or not audit['valid']:
        raise ValueError('regional result has not passed its independent source audit')
    route = read(folder/'route.json')
    digest = sha256(folder/'route.json')
    for name,key in [('problem.json','problem_sha256'),('roads.sqlite','roads_sha256'),('engine-snapshot','engine_sha256')]:
        assert sha256(folder/name)==route['provenance'][key]
    model = read(ROOT/'data/lower48-eod-2024/model/model.json')
    benchmark = read(folder/'benchmark.json')
    for key in ['locations','opportunities','species_entries','eligible_presence_groups','represented_observations']:
        assert benchmark['model'][key]==model[key]
    assert route['config']['walking_speed_kmh']==2.2 and route['config']['walking_threshold_m']==100
    assert audit['species_total']==route['species_total'] and audit['days_checked']==366
    assert (folder/'validation.json').stat().st_mtime_ns>=(folder/'route.json').stat().st_mtime_ns
    with tempfile.TemporaryDirectory(prefix='regional-viewer-') as temporary:
        target = Path(temporary)/'served.json'
        with urllib.request.urlopen(f'http://127.0.0.1:8773/api/route/{tag}/route.json',timeout=120) as source,target.open('wb') as sink:
            shutil.copyfileobj(source,sink)
        shown = read(target)
        assert all(shown[key]==value for key,value in route.items())
        coverage = shown['observer_coverage']
        assert coverage['annual_species']==764 and len(coverage['days'])==len(route['days'])==366
        for observed,day in zip(coverage['days'],route['days']):
            assert observed['date']==day['date'] and observed['cumulative_species']>=day['cumulative_species']
        tests = subprocess.run(['node','--test','tests/playback.test.mjs','tests/activity.test.mjs'],cwd=ROOT,
                env={**os.environ,'BIGYEAR_PILOT_ROUTE':str(target)},capture_output=True,text=True,check=True)
        (folder/'viewer-tests.txt').write_text(tests.stdout)
    assert sha256(folder/'route.json')==digest
    for suffix in ['', '-audit']:
        assert read(OUT/(tag+suffix)/'router-stopped.json')['stopped']
    assert not (folder/'problem.by').exists() and not (folder/'regional-evaluation.by').exists()
    assert read(folder/'regional-evaluator-provenance.json')['expired_bridge_credentials_removed']
    with socket.socket() as sock:
        sock.settimeout(1)
        assert sock.connect_ex(('127.0.0.1',17779))!=0
    verified = {'valid':True,'run':tag,'route_sha256':digest,'species':route['species_total'],
         'flight_service':audit['flight_service'],'days_checked':366,'all_observer_species':764,
         'complete_candidate_counts_unchanged':True,'served_frozen_fields_unchanged':True,
         'routers_stopped':True,'expired_bridge_credentials_removed':True,
         'source_audit':'Completed independent original observation, calendar, road, walking and BTS archive checks reused; viewer tests run on served output.'}
    dump(OUT/'regional-viewer-verification.json',verified)
    print(json.dumps(verified),flush=True)


if __name__=='__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    main()
