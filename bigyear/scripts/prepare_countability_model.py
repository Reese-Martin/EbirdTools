"""Derive audited scoring while preserving every canonical location/date ID."""

import csv
from copy import deepcopy
import json
import os

import numpy as np

from bigyear_planner.compact_model import load
from bigyear_planner.eod import attach_dataset
from bigyear_planner.util import ROOT, ManagedDuckDB, dump, read, sha256, write_csv
from audit_countability import DATA, POLICY

ORIGINAL = ROOT/'data/lower48-eod-2024'


def prepare():
    policy=read(DATA/'policy.json')
    for name,digest in policy['decision_files'].items():
        if sha256(DATA/name)!=digest:
            raise ValueError('countability decisions changed: '+name)
    dataset=DATA/'dataset';dataset.mkdir(exist_ok=True)
    if (DATA/'model/model.json').exists():
        model=load(DATA/'model/model.json')
        try:
            if model.meta['manifest_sha256']!=sha256(dataset/'manifest.json') or sha256(dataset/'countability-policy.json')!=sha256(DATA/'policy.json'):
                raise ValueError('existing scoring model belongs to different audit decisions; use a fresh data directory')
        finally:
            model.close()
        return
    manifest=deepcopy(read(ORIGINAL/'dataset/manifest.json'))
    manifest['countability']={'policy':POLICY,'policy_sha256':sha256(DATA/'policy.json'),
         'original_manifest_sha256':sha256(ORIGINAL/'dataset/manifest.json'),
         'score':'Supported evidence only. Unresolved and excluded evidence receive zero credit.',
         'individual_wild_status':'Publisher review is an explicit native-occurrence proxy, not certification.'}
    manifest['taxonomy_policy']=POLICY
    for name in ['land.csv','scope.geojson','scope-points.csv']:
        target=dataset/name
        if not target.exists():
            os.link(ORIGINAL/'dataset'/name,target)
    for source,target in [(DATA/'taxonomy.csv',dataset/'taxonomy.csv'),
                          (DATA/'policy.json',dataset/'countability-policy.json')]:
        target.write_bytes(source.read_bytes())
    decisions=list(csv.DictReader((DATA/'evidence-decisions.csv').open()))
    projected=[{**{k:r[k] for k in ['representative_gbifid','source_name','source_taxon_id','date','decision','reason','reference']},
                'latitude':r['latitude_text'],'longitude':r['longitude_text']} for r in decisions]
    write_csv(dataset/'countability-evidence.csv',projected,list(projected[0]))
    manifest['inputs']['taxonomy']={'path':str(dataset/'taxonomy.csv'),'sha256':sha256(dataset/'taxonomy.csv')}
    manifest['limitations'] += [
        'Countability policy scores supported evidence only; unresolved/excluded observations are retained in the archive and complete canonical candidate catalog with zero species credit.',
        'Native code-1/2 occurrences use the publisher public-review process as an explicit quality/wild-occurrence proxy; individual wild status and observer encounter are not certified.',
        'Independent rarity-event corroboration is an additional conservative model screen, not an ABA requirement for every rarity.',
        'The supported and original presence-only all-observer curves use different scoring policies and must not be compared as identical species pools.'
    ]
    manifest['outputs']={name:sha256(dataset/name) for name in ['taxonomy.csv','land.csv','scope.geojson','scope-points.csv','countability-evidence.csv','countability-policy.json']}
    # EOD views need the frozen input identities but not precomputed statistics.
    dump(dataset/'manifest.json',manifest)
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='2GB'");con.execute('SET threads=2')
        evidence,obs=attach_dataset(con,dataset,manifest)
        dispositions=dict(con.execute('SELECT disposition,count(*) FROM eod_classified GROUP BY 1').fetchall())
        first=[{'species':s,'date':str(d)} for s,d in con.execute(f'SELECT species_id,min(date) FROM {obs} GROUP BY 1 ORDER BY 2,1').fetchall()]
        states=[dict(zip(['state','presence_groups','represented_observations','exact_locations'],v)) for v in con.execute(
            f'SELECT state,count(*),sum(source_rows),count(DISTINCT location_id) FROM {obs} GROUP BY 1 ORDER BY 1').fetchall()]
    manifest['statistics']={**manifest['statistics'],'dispositions':dispositions,
          'annual_recorded_species_ceiling':len(first),'eod_daily_presence_groups':dispositions['eligible'],
          'original_presence_groups':read(ORIGINAL/'dataset/manifest.json')['statistics']['dispositions']['eligible'],
          'location_count_note':'Complete original exact-location catalog retained; per-state exact_locations counts scoring-eligible locations.'}
    dump(dataset/'manifest.json',manifest)
    print(json.dumps({'stage':'source-scoring-query','eligible_groups':dispositions['eligible'],'supported_species_pool':len(first)}),flush=True)
    model=load(ORIGINAL/'model/model.json')
    try:
        target=DATA/'model';target.mkdir(exist_ok=True)
        taxonomy={r['species_id']:r for r in csv.DictReader((dataset/'taxonomy.csv').open())}
        supported=np.array([taxonomy[v['id']]['eod_presence_eligible']=='1' for v in model.meta['species']])
        positions={v['id']:i for i,v in enumerate(model.meta['species'])}
        overrides={}
        for r in decisions:
            item=int(r['opportunity']);sp=positions[r['species_id']]
            original=model.records[item];off=int(original['offset']);count=int(original['count'])
            sequence=model.species[off:off+count]
            at=int(np.searchsorted(sequence,sp))
            if at>=count or sequence[at]!=sp:
                raise ValueError('evidence decision is not in original canonical opportunity')
            absolute=off+at
            status=r['decision']=='supported'
            if absolute in overrides and overrides[absolute]!=status:
                raise ValueError('conflicting evidence decisions for same support')
            overrides[absolute]=status
        for name in ['coordinates.bin','locations.sqlite']:
            if not (target/name).exists():
                os.link(model.root/name,target/name)
        offset=0
        override_positions=np.array(sorted(overrides),dtype=np.int64)
        with (target/'opportunities.bin').open('wb') as records_file,(target/'species.bin').open('wb') as species_file:
            for begin in range(0,len(model.records),250000):
                old=model.records[begin:begin+250000]
                first_offset=int(old[0]['offset']);last_offset=int(old[-1]['offset'])+int(old[-1]['count'])
                species=model.species[first_offset:last_offset]
                keep=supported[species].copy()
                lo,hi=np.searchsorted(override_positions,[first_offset,last_offset])
                for absolute in override_positions[lo:hi]:
                    keep[int(absolute)-first_offset]=overrides[int(absolute)]
                counts=np.add.reduceat(keep,(old['offset']-first_offset).astype(np.intp),dtype=np.uint32)
                updated=old.copy();updated['count']=counts
                updated['offset']=offset+np.cumsum(counts,dtype=np.uint64)-counts
                updated.tofile(records_file);species[keep].tofile(species_file)
                offset+=int(counts.sum())
        if offset!=dispositions['eligible']:
            raise ValueError(f'compact scoring differs from independent original-archive query: {offset} vs {dispositions["eligible"]}')
        meta=deepcopy(model.meta)
        meta.update(dataset=str(dataset.resolve()),manifest=manifest,manifest_sha256=sha256(dataset/'manifest.json'),
              eligible_presence_groups=offset,species_entries=offset,
              represented_observations=sum(r['represented_observations'] for r in states),
              first_seen=first,state_counts=states,
              components={name:sha256(target/name) for name in model.meta['components']},
              countability_source={'path':str(model.path),'sha256':sha256(model.path)},
              scoring_policy=policy['id'],
              original_retained_presence_groups=model.meta['eligible_presence_groups'],
              original_retained_observations=model.meta['represented_observations'])
        meta['reduction']['candidate_species_ceiling']=len(first)
        meta['reduction']['policy']='Every original exact location and dated opportunity retained; countability changes species credit only, including zero-credit opportunities.'
        dump(target/'model.json',meta)
    finally:
        model.close()
    verified=load(target/'model.json');verified.close()
    dump(DATA/'model-retention-audit.json',{'valid':True,'model_sha256':sha256(target/'model.json'),
         'original_model_sha256':sha256(ORIGINAL/'model/model.json'),
         'exact_locations_retained':meta['locations'],'dated_opportunities_retained':meta['opportunities'],
         'source_groups_retained':meta['original_retained_presence_groups'],
         'source_observations_retained':meta['original_retained_observations'],
         'scoring_eligible_groups':offset,'supported_species_pool':len(first),
         'source_scoring_counts_independently_match_compact_entries':True})
    print(json.dumps({'stage':'audited-model-ready','opportunities':meta['opportunities'],'scoring_species':len(first)}),flush=True)


if __name__=='__main__':
    prepare()
