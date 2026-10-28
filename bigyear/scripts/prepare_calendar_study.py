"""Versioned inputs and a declared equal-budget calendar comparison."""
import os

from bigyear_planner.compact_model import load
from bigyear_planner.island_access import build_catalog
from bigyear_planner.island_validate import audit_catalog
from bigyear_planner.joint_marine import build_joint
from bigyear_planner.util import ROOT,dump,read,sha256


def main():
    data=ROOT/'data/calendar-strategy-study-2024-v1';data.mkdir(exist_ok=True)
    baseline=ROOT/'runs/lower48-eod-2024/resolved-routing-2024-v3'
    original_path=ROOT/'data/lower48-joint-mobility-2024-v1/model/model.json'
    original=read(original_path)
    parent=load(original['joint_marine']['parent_path'])
    try:
        catalog=build_catalog(parent,original['joint_marine']['catalog_path'],data)
        errors=[];audit_catalog(read(catalog),lambda ok,msg:None if ok else errors.append(msg))
        if errors:raise ValueError('island input audit failed: '+str(errors[:5]))
        model=build_joint(parent.path,catalog,original['joint_marine']['holds_path'],data/'island-model')
        try:
            for name in ['locations.sqlite','coordinates.bin']:
                if model.meta['components'][name]!=original['components'][name]:
                    raise ValueError('island model changed physical routing coordinates or identities')
                # These immutable files are byte-identical to the verified
                # parent. Share their disk blocks instead of retaining copies.
                target=model.root/name;source=original_path.parent/name
                if not os.path.samefile(target,source):
                    target.unlink();os.link(source,target)
            if model.meta['species']!=original['species']:raise ValueError('island model changed species scoring pool')
            meta=read(model.path);meta['routing_source']={'path':str(original_path),'sha256':sha256(original_path),
                'reason':'Only additional source-supported boat variants; all location identities and exact routing coordinates unchanged.'}
            if meta!=model.meta:dump(model.path,meta)
        finally:model.close()
    finally:parent.close()
    plan={'id':'calendar-strategy-study-2024-v1','baseline_route':str(baseline/'route.json'),
        'baseline_route_sha256':sha256(baseline/'route.json'),'baseline_validation_sha256':sha256(baseline/'validation.json'),
        'baseline_species':682,'baseline_primary':673,'baseline_provisional':9,
        'seconds_per_arm':900,'threads':2,'seed':20240101,
        'arms':{'control':'Ordinary full native search,900seconds.',
            'nearby':'Ordinary search, then deterministic source-dated additions near stops and measured baseline road corridors throughout transfer gaps; unused polish time returns to ordinary search.',
            'tentpole':'Actual supported dated anchors, including seasonal Texas opportunities; retain feasible lower-score alternative calendars for hard-anchor recovery, then refine the best calendar.',
            'island':'Add262source-supported Santa Cruz landing alternatives with two-hour crossings each way; guided new-boat insertion, then ordinary unconstrained search.',
            'combined':'Island access, hard-anchor calendar recovery, global refinement and nearby stop/corridor additions.'},
        'cache_rule':'Every arm begins with its own byte-for-byte copy of the same verified682 cache. No discoveries or improved calendars pass between arms.',
        'budget_rule':'Equal900second active monotonic search allocations; model/input verification, saved-route materialization, publisher retrieval, routing-worker drain and independent validation are measured separately.',
        'all_observations_retained':True,'exact_coordinates':True,'walking_kmh':2.2,
        'sensitive_species_policy_unchanged':True,'source_model_sha256':sha256(original_path),
        'island_model_sha256':sha256(data/'island-model/model.json'),'island_catalog_sha256':sha256(catalog),
        'source_archive_manifest_sha256':sha256(ROOT/'data/eod-us-since-2000-zstd3/manifest.json'),
        'starting_cache':{'path':str(ROOT/'data/lower48-joint-mobility-2024-v1/resolved-routing-2024-v3-cache'),
            'sha256':{name:sha256(ROOT/'data/lower48-joint-mobility-2024-v1/resolved-routing-2024-v3-cache'/name)
                for name in ['provider.json','access.bin','costs.bin']}},
        'limitations':['One stochastic replicate per arm; differences are diagnostic, not a statistical estimate of strategy superiority.',
            'Corridor proposals reference the verified682baseline; refined calendars may differ, so each current calendar is rechecked in full.',
            'Island crossings, near-landing foot access and weather availability are modeled; historical sailings and public footpaths are not certified.',
            'Tentpoles use exact supported records; dates between observations never receive interpolated credit.',
            'The incumbent and scoring tiers are preserved;759 remains a comparison target, not an established feasible score.']}
    target=data/'study-plan.json'
    if target.exists():
        old=read(target)
        if 'phase_handoff' in old:plan['phase_handoff']=old['phase_handoff']
        if 'starting_cache' not in old:old['starting_cache']=plan['starting_cache']
        if old!=plan:raise ValueError('declared study inputs changed')
    if not target.exists() or read(target)!=plan:dump(target,plan)
    print({'plan':str(target),'baseline':682,'seconds_per_arm':900,'island_options':262})


if __name__=='__main__':main()
