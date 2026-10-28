"""Check audited output bindings and test the actually served playback curves."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

from bigyear_planner.util import ROOT, dump, read, sha256
from bigyear_planner.validate import validate

OUT = ROOT/'runs/lower48-eod-2024'


def main(repeat_source_audit=False):
    comparison = read(OUT/'mobility-comparison.json')
    results = {}
    runs = {mode:comparison[mode]['run'] for mode in ['driving','flights']}
    if (OUT/'flight-integration-result.json').exists():
        runs['integration'] = read(OUT/'flight-integration-result.json')['run']
    for mode,tag in runs.items():
        folder = OUT/tag
        route_digest = sha256(folder/'route.json')
        # run() has just performed the independent source/geometry/service
        # audit. Reuse that result for viewer QA unless a new source concern
        # warrants explicitly repeating the full national evidence scan.
        audit = (validate(folder/'route.json',folder/'problem.json',folder/'roads.sqlite')
                 if repeat_source_audit else read(folder/'validation.json'))
        if repeat_source_audit:
            dump(folder/'validation-full.json',audit)
            dump(folder/'validation.json',audit)
        if not audit['valid']:
            raise ValueError(str(audit['errors'][:8]))
        frozen = read(folder/'route.json')
        assert audit['species_total']==frozen['species_total'] and audit['days_checked']==366
        assert (folder/'validation.json').stat().st_mtime_ns >= (folder/'route.json').stat().st_mtime_ns
        for name,key in [('problem.json','problem_sha256'),('roads.sqlite','roads_sha256'),('engine-snapshot','engine_sha256')]:
            assert sha256(folder/name)==frozen['provenance'][key]
        with tempfile.TemporaryDirectory(prefix='mobility-viewer-') as temporary:
            target = Path(temporary)/'served.json'
            with urllib.request.urlopen(f'http://127.0.0.1:8773/api/route/{tag}/route.json',timeout=120) as source, target.open('wb') as sink:
                shutil.copyfileobj(source,sink)
            shown = read(target)
            for key,value in frozen.items():
                if shown[key]!=value:
                    raise ValueError('viewer modified frozen route field: '+key)
            observer = shown['observer_coverage']
            assert observer['annual_species']==764 and len(observer['days'])==len(shown['days'])==366
            for a,b in zip(observer['days'],shown['days']):
                assert a['date']==b['date'] and a['cumulative_species']>=b['cumulative_species']
            tests = subprocess.run(['node','--test','tests/playback.test.mjs','tests/activity.test.mjs'],
                                   cwd=ROOT,env={**os.environ,'BIGYEAR_PILOT_ROUTE':str(target)},
                                   capture_output=True,text=True,check=True)
            (folder/'viewer-tests.txt').write_text(tests.stdout)
        assert sha256(folder/'route.json')==route_digest
        for suffix in ['', '-audit']:
            assert read(OUT/(tag+suffix)/'router-stopped.json')['stopped']
        if mode=='integration':
            assert read(OUT/(tag+'-preflight')/'router-stopped.json')['stopped']
        assert not (folder/'problem.by').exists()
        results[mode] = {'valid':True,'route_species':frozen['species_total'], 'route_sha256':route_digest,
                         'all_observer_species':764,'days_checked':366,
                         'flight_service':audit['flight_service'], 'served_frozen_fields_unchanged':True,
                         'source_audit_repeated':repeat_source_audit,
                         'router_processes_stopped':True,'temporary_bridge_credentials_removed':True}
    dump(OUT/'mobility-verification.json',results)
    print(json.dumps(results),flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeat-source-audit',action='store_true')
    args = parser.parse_args()
    main(args.repeat_source_audit)
