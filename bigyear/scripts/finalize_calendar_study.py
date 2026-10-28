"""Verify study preservation, freeze final workflow files and retire losing caches."""
import argparse
import os
from pathlib import Path
import shutil

from bigyear_planner.util import ROOT,dump,read,sha256
from report_calendar_study import RUNS


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--retire-losing-caches',action='store_true')
    args=parser.parse_args()
    root=ROOT/'runs/lower48-eod-2024';out=root/'calendar-strategy-study-2024-v1'
    data=ROOT/'data/calendar-strategy-study-2024-v1';plan=read(data/'study-plan.json')
    result=read(out/'result.json');archive=ROOT/'data/eod-us-since-2000-zstd3'
    manifest=read(archive/'manifest.json')
    if not result['valid'] or sha256(plan['baseline_route'])!=plan['baseline_route_sha256']:
        raise ValueError('study or original baseline is not verified')
    if sha256(archive/'manifest.json')!=plan['source_archive_manifest_sha256']:
        raise ValueError('source archive manifest changed')
    for name,digest in plan['starting_cache']['sha256'].items():
        if sha256(Path(plan['starting_cache']['path'])/name)!=digest:
            raise ValueError('original bounded cache changed: '+name)
    model=ROOT/'data/lower48-joint-mobility-2024-v1/model/model.json'
    if sha256(model)!=plan['source_model_sha256']:raise ValueError('original joint model changed')
    if sha256(data/'island-model/model.json')!=plan['island_model_sha256']:
        raise ValueError('study island model changed')
    bindings=[];native_hashes=set()
    for binding in result['route_bindings']:
        arm=binding['arm'];folder=root/RUNS[arm];audit=read(folder/'validation.json')
        for file,key in [('route.json','route_sha256'),('validation.json','validation_sha256'),
                         ('result.json','result_sha256')]:
            if sha256(folder/file)!=binding[key]:raise ValueError('report binding changed: '+str(folder/file))
        if not audit['valid'] or audit['days_checked']!=366:raise ValueError('incomplete calendar audit: '+arm)
        if not read(folder/'router-stopped.json')['stopped']:raise ValueError('routing worker did not stop: '+arm)
        native_hash=sha256(folder/'engine-snapshot');native_hashes.add(native_hash)
        if arm!='control' and read(folder/'starting-cache-binding.json')!=plan['starting_cache']:
            raise ValueError('arm did not bind the common starting cache: '+arm)
        if arm!='control':
            search=read(folder/'study-search.json');selected=read(folder/'engine.json')['selected']
            if selected!=search['ids']:raise ValueError('saving changed the timed selected visits: '+arm)
            if not set(search['required_final_visits'])<=set(selected):raise ValueError('dated anchor was lost: '+arm)
        bindings.append({'arm':arm,'route_sha256':binding['route_sha256'],'valid':True,
            'days_checked':audit['days_checked'],'publisher_records':audit['joint_transport']['publisher_sources']['records_checked'],
            'native_search_binary_sha256':native_hash,'routing_worker_stopped':True,
            'saving_preserved_timed_selected_order':True if arm!='control' else 'not applicable; legacy single native search'})
    if len(native_hashes)!=1:raise ValueError('study native search builds differ')
    mismatches=[]
    for part in manifest['parts']:
        path=archive/part['name']
        if path.stat().st_size!=part['bytes'] or sha256(path)!=part['sha256']:mismatches.append(part['name'])
    proof={'valid':not mismatches,'parts_checked':len(manifest['parts']),
        'bytes':sum(p['bytes'] for p in manifest['parts']),'rows':manifest['rows'],
        'manifest_sha256':sha256(archive/'manifest.json'),'mismatches':mismatches}
    dump(out/'final-archive-verification.json',proof)
    if mismatches:raise ValueError('original Parquet parts changed')
    # This inventory describes the final workflow, separately from each arm's
    # own frozen executed code and original native executable snapshots.
    files=[*sorted((ROOT/'src/bigyear_planner').glob('*.py')),*sorted((ROOT/'scripts').glob('*.py')),
           *sorted((ROOT/'cpp').glob('*')),*sorted((ROOT/'web').glob('*'))]
    inventory=[];snapshot=out/'final-workflow-snapshot'
    for path in files:
        if not path.is_file():continue
        relative=path.relative_to(ROOT);target=snapshot/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target);inventory.append({'path':str(relative),'sha256':sha256(target)})
    dump(out/'final-workflow-inventory.json',{'scope':'Final workflow files; per-arm executed snapshots remain separate.',
        'files':inventory})
    retired=[];winner=RUNS[result['winner']['arm']]
    space=os.statvfs(ROOT);free_before=space.f_bavail*space.f_frsize
    if args.retire_losing_caches:
        candidates=[(ROOT/'data/lower48-joint-mobility-2024-v1')/(RUNS['control']+'-cache')]
        candidates += [data/(name+'-cache') for arm,name in RUNS.items() if arm!='control']
        candidates += [data/'calendar-study-nearby-2024-v1-cache',data/'island-landing-diagnostic-2024-v1-cache']
        for cache in candidates:
            if cache.name==winner+'-cache' or not cache.exists():continue
            previous=cache/'study-cache-retired.json'
            if previous.exists():retired.append(read(previous));continue
            removed=[]
            for name in ['access.bin','costs.bin']:
                path=cache/name
                if path.exists():
                    stat=path.stat()
                    removed.append({'name':name,'bytes':stat.st_size,'allocated_bytes':stat.st_blocks*512,
                        'links_before_unlink':stat.st_nlink,'sha256':sha256(path)})
            note={'path':str(cache),'retired':removed,'logical_bytes_retired':sum(x['bytes'] for x in removed),
                'allocated_file_bytes_retired':sum(x['allocated_bytes'] for x in removed),
                'reason':'Verified losing/diagnostic study cache. Saved routes embed geometry; source proofs and selected road databases remain.',
                'original_682_cache_preserved':True,'winner_cache_preserved':True}
            dump(previous,note)
            for item in removed:(cache/item['name']).unlink()
            retired.append(note)
    space=os.statvfs(ROOT);free_after=space.f_bavail*space.f_frsize
    final={'valid':True,'baseline_unchanged':True,'starting_cache_unchanged':True,
        'all_primary_arms_use_same_native_search_binary':True,
        'source_model_unchanged':True,'source_archive':proof,'route_bindings':bindings,
        'final_workflow_inventory_sha256':sha256(out/'final-workflow-inventory.json'),
        'retired_caches':retired,'logical_bytes_retired':sum(r['logical_bytes_retired'] for r in retired),
        'allocated_file_bytes_retired':sum(r['allocated_file_bytes_retired'] for r in retired),
        'free_bytes_before_retirement':free_before,'free_bytes_after_retirement':free_after,
        'free_space_change_bytes':free_after-free_before,
        'storage_note':'Allocated file bytes retired are distinct from logical sizes. Free-space change can also reflect other laptop activity and filesystem sharing.',
        'winner_cache_retained':winner+'-cache','all_verified_routes_and_evidence_retained':True}
    dump(out/'final-integrity.json',final)
    print({k:final[k] for k in ['valid','baseline_unchanged','starting_cache_unchanged','allocated_file_bytes_retired','winner_cache_retained']})


if __name__=='__main__':main()
