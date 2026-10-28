"""Verify policy bindings, retained source cohort and actual served viewer."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import urllib.request

from bigyear_planner.util import ROOT, dump, read, sha256
from audit_countability import DATA, OUT
from benchmark_countability import TAG


def main():
    result=read(OUT/'countability-benchmark-result.json');folder=OUT/TAG
    route=read(folder/'route.json');audit=read(folder/'validation.json')
    assert result['valid'] and audit['valid'] and audit['species_total']==route['species_total']
    retention=read(DATA/'model-retention-audit.json');model=read(DATA/'model/model.json')
    assert retention['valid'] and retention['model_sha256']==sha256(DATA/'model/model.json')
    assert retention['exact_locations_retained']==1936106 and retention['dated_opportunities_retained']==6858225
    assert retention['source_observations_retained']==127097446
    assert retention['source_scoring_counts_independently_match_compact_entries']
    for name,key in [('problem.json','problem_sha256'),('roads.sqlite','roads_sha256'),('engine-snapshot','engine_sha256')]:
        assert sha256(folder/name)==route['provenance'][key]
    assert route['config']['walking_speed_kmh']==2.2 and route['config']['walking_threshold_m']==100
    assert route['taxonomy_policy']==result['policy']
    if result.get('active_time_clock_corrected'):
        engine=read(folder/'engine.json')
        benchmark=read(folder/'benchmark.json')
        assert 600<=engine['elapsed_seconds']<660
        assert engine['elapsed_seconds']<=benchmark['wall_seconds']
        assert read(folder/'idle-sleep-assertion-released.json')['released']
    digest=sha256(folder/'route.json')
    with tempfile.TemporaryDirectory(prefix='countability-viewer-') as temporary:
        target=Path(temporary)/'served.json'
        with urllib.request.urlopen(f'http://127.0.0.1:8773/api/route/{TAG}/route.json',timeout=120) as source,target.open('wb') as sink:
            shutil.copyfileobj(source,sink)
        shown=read(target)
        assert all(shown[k]==v for k,v in route.items())
        coverage=shown['observer_coverage']
        assert coverage['annual_species']==len(model['first_seen']) and len(coverage['days'])==366
        assert coverage['taxonomy_policy']==route['taxonomy_policy']
        for observed,day in zip(coverage['days'],route['days']):
            assert observed['date']==day['date'] and observed['cumulative_species']>=day['cumulative_species']
        tests=subprocess.run(['node','--test','tests/playback.test.mjs','tests/activity.test.mjs'],cwd=ROOT,
             env={**os.environ,'BIGYEAR_PILOT_ROUTE':str(target)},capture_output=True,text=True,check=True)
        (folder/'viewer-tests.txt').write_text(tests.stdout)
    for suffix in ['', '-audit']:
        assert read(OUT/(TAG+suffix)/'router-stopped.json')['stopped']
    assert not (folder/'problem.by').exists() and not (folder/'regional-evaluation.by').exists()
    with socket.socket() as sock:
        sock.settimeout(1);assert sock.connect_ex(('127.0.0.1',17779))!=0
    # The older presence-only comparison must remain available alongside the
    # audited one; changing the latest policy must not rewrite historical runs.
    with urllib.request.urlopen('http://127.0.0.1:8773/api/route/regional-flights-900s-600s/route.json',timeout=120) as source:
        previous=json.load(source)
    assert previous['observer_coverage']['annual_species']==764 and previous['species_total']==626
    assert sha256(folder/'route.json')==digest
    verified={'valid':True,'run':TAG,'route_sha256':digest,'supported_species':route['species_total'],
         'all_observer_supported_pool':coverage['annual_species'],'days_checked':366,
         'all_original_locations_dates_and_observations_retained':True,
         'old_presence_only_route_and_coverage_preserved':True,
         'routers_stopped':True,'expired_native_credentials_removed':True,'viewer_tests_passed':15}
    dump(OUT/'countability-viewer-verification.json',verified)
    print(json.dumps(verified),flush=True)


if __name__=='__main__':
    main()
