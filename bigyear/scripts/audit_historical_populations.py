"""Hold Hawaii-only introductions without changing any 2023 candidate identity."""
import csv
from copy import deepcopy
import os

import numpy as np

from bigyear_planner.compact_model import load
from bigyear_planner.eod import attach_dataset
from bigyear_planner.joint_marine import build_joint
from bigyear_planner.island_validate import audit_catalog
from bigyear_planner.util import ROOT, ManagedDuckDB, dump, q, read, sha256, write_csv

DATA = ROOT/'data/lower48-eod-2023'
OUT = DATA/'population-audited-v2'
HELD = {'aba2024:Estrilda_astrild','aba2024:Amandava_amandava'}


def link(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    if not target.exists():
        os.link(source,target)


def main():
    if (OUT/'joint-model/model.json').exists():
        print('Audited historical model already exists')
        return
    OUT.mkdir(exist_ok=True)
    original = load(DATA/'model/model.json')
    try:
        with ManagedDuckDB() as con:
            con.execute("SET memory_limit='1GB'")
            con.execute('SET threads=1')
            _,obs = attach_dataset(con,original.meta['dataset'])
            names = ','.join(q(s) for s in sorted(HELD))
            counts = con.execute(f'''SELECT species_id,count(*),sum(source_rows),count(DISTINCT location_id)
                FROM {obs} WHERE species_id IN ({names}) GROUP BY 1 ORDER BY 1''').fetchall()
            if {v[0] for v in counts}!=HELD:
                raise ValueError('population review source identities differ')
        dataset = OUT/'dataset'
        dataset.mkdir(exist_ok=True)
        manifest = deepcopy(original.meta['manifest'])
        for name in manifest['outputs']:
            if name not in ['taxonomy.csv','countability-policy.json']:
                link(DATA/'dataset'/name,dataset/name)
        rows = list(csv.DictReader((DATA/'dataset/taxonomy.csv').open()))
        policy = deepcopy(read(DATA/'countability-policy.json'))
        policy['id'] += '-population-v2'
        for row in rows:
            row['policy_id'] = policy['id']
            if row['species_id'] in HELD:
                row.update(eod_presence_eligible='review',score_tier='held',
                    review_basis='Introduced primary membership is based on Hawaii. No supported established lower-48 population or natural dispersal established for the reviewed mainland record; unresolved, zero credit.',
                    reference=row['reference']+'; https://www.aba.org/aba-area-introduced-species/')
        write_csv(dataset/'taxonomy.csv',rows,list(rows[0]))
        policy['taxonomy_sha256'] = sha256(dataset/'taxonomy.csv')
        policy['additional_primary_concepts'] = [r for r in policy['additional_primary_concepts']
            if r['source_name'] not in {'Estrilda astrild','Amandava amandava'}]
        policy['unresolved_new_concepts'] += 2
        policy['introduced_population_review'] = {'held_species':sorted(HELD),
            'reason':'Fixed ABA primary membership alone does not authorize a mainland introduced individual. Established-population or natural-dispersal support is missing; Hawaii populations do not establish lower-48 eligibility.',
            'source':'https://www.aba.org/aba-area-introduced-species/',
            'source_sha256':policy['sources']['aba-introduced.html'],
            'source_groups_and_observations':counts,'archive_rows_deleted':0,
            'all_exact_locations_and_dated_opportunity_ids_retained':True}
        dump(dataset/'countability-policy.json',policy)
        for name,digest in policy['sources'].items():
            link(DATA/'sources'/name,OUT/'sources'/name)
        link(DATA/'source-retrieval.json',OUT/'source-retrieval.json')
        for path in (DATA/'sources/selected-verbatim').glob('*.json'):
            link(path,OUT/'sources/selected-verbatim'/path.name)
        removed_groups = sum(v[1] for v in counts)
        removed_observations = sum(v[2] for v in counts)
        manifest['taxonomy_policy'] = policy['id']
        manifest['inputs']['taxonomy'] = {'path':str(dataset/'taxonomy.csv'),
            'sha256':sha256(dataset/'taxonomy.csv')}
        manifest['countability'].update(policy=policy['id'],
            policy_sha256=sha256(dataset/'countability-policy.json'))
        for name in ['taxonomy.csv','countability-policy.json']:
            manifest['outputs'][name] = sha256(dataset/name)
        stats = manifest['statistics']
        stats['dispositions']['eligible'] -= removed_groups
        stats['dispositions']['eod_metadata_policy_review'] += removed_groups
        stats['annual_recorded_species_ceiling'] -= len(HELD)
        stats['eod_daily_presence_groups'] -= removed_groups
        stats['location_count_note'] = 'Complete original exact-location and dated-opportunity catalog retained; held population supports carry zero species credit.'
        manifest['limitations'].append(policy['introduced_population_review']['reason'])
        dump(dataset/'manifest.json',manifest)
        target = OUT/'model'
        target.mkdir(exist_ok=True)
        for name in ['coordinates.bin','locations.sqlite']:
            link(original.root/name,target/name)
        held_indices = {i for i,s in enumerate(original.meta['species']) if s['id'] in HELD}
        offset = 0
        removed_entries = 0
        with (target/'opportunities.bin').open('wb') as rf,(target/'species.bin').open('wb') as sf:
            for begin in range(0,len(original.records),250000):
                old = original.records[begin:begin+250000]
                start = int(old[0]['offset'])
                end = int(old[-1]['offset'])+int(old[-1]['count'])
                species = original.species[start:end]
                keep = ~np.isin(species,list(held_indices))
                removed_entries += int((~keep).sum())
                counts2 = np.add.reduceat(keep,(old['offset']-start).astype(np.intp),dtype=np.uint32)
                updated = old.copy()
                updated['count'] = counts2
                updated['offset'] = offset+np.cumsum(counts2,dtype=np.uint64)-counts2
                updated.tofile(rf)
                species[keep].tofile(sf)
                offset += int(counts2.sum())
        if removed_entries!=removed_groups:
            raise ValueError('population source and compact score delta differ')
        meta = deepcopy(original.meta)
        meta.update(dataset=str(dataset),manifest=manifest,manifest_sha256=sha256(dataset/'manifest.json'),
            eligible_presence_groups=meta['eligible_presence_groups']-removed_groups,
            represented_observations=meta['represented_observations']-removed_observations,
            species_entries=offset,first_seen=[r for r in meta['first_seen'] if r['species'] not in HELD],
            countability_source={'path':str(original.path),'sha256':sha256(original.path)},
            original_retained_presence_groups=original.meta['eligible_presence_groups'],
            original_retained_observations=original.meta['represented_observations'],
            components={n:sha256(target/n) for n in meta['components']})
        meta['reduction']['candidate_species_ceiling'] -= 2
        dump(target/'model.json',meta)
    finally:
        original.close()
    parent = load(target/'model.json')
    try:
        marine = OUT/'marine'
        marine.mkdir(exist_ok=True)
        old = DATA/'marine'
        for name in ['offshore.parquet','ports.json','boat-access-score-holds.json','sources/countries-ne-5.1.2.geojson']:
            link(old/name,marine/name)
        with ManagedDuckDB() as con:
            held = ','.join(q(s) for s in sorted(HELD))
            if con.execute(f'SELECT count(*) FROM read_parquet({q(marine/"offshore.parquet")}) WHERE species_id IN ({held})').fetchone()[0]:
                raise ValueError('population hold unexpectedly affects offshore catalog; explicit reclassification needed')
        marine_meta = deepcopy(read(old/'manifest.json'))
        marine_meta.update(parent_dataset=str(dataset),parent_manifest_sha256=sha256(dataset/'manifest.json'))
        dump(marine/'manifest.json',marine_meta)
        catalog = deepcopy(read(old/'charter-candidates.json'))
        catalog.update(marine_data=str(marine),marine_manifest_sha256=sha256(marine/'manifest.json'))
        dump(marine/'charter-candidates.json',catalog)
        island = deepcopy(read(old/'islands/landing-candidates.json'))
        island.update(marine_data=str(marine),marine_manifest_sha256=sha256(marine/'manifest.json'))
        island['island_access'].update(parent_path=str(parent.path),parent_sha256=sha256(parent.path),
            original_catalog_path=str(marine/'charter-candidates.json'),
            original_catalog_sha256=sha256(marine/'charter-candidates.json'))
        dump(marine/'landing-candidates.json',island)
        errors = []
        audit_catalog(island,lambda ok,msg:None if ok else errors.append(msg))
        if errors:
            raise ValueError(str(errors[:4]))
        joint = build_joint(parent.path,marine/'landing-candidates.json',marine/'boat-access-score-holds.json',OUT/'joint-model')
        try:
            before = load(DATA/'joint-model/model.json')
            try:
                if any(joint.meta['components'][n]!=before.meta['components'][n] for n in ['locations.sqlite','coordinates.bin']):
                    raise ValueError('population review changed physical routing identities')
                if len(joint.records)!=len(before.records) or joint.meta['species']!=before.meta['species']:
                    raise ValueError('population review changed canonical candidate/species indices')
                for field in ['day','location','reserved']:
                    if not np.array_equal(joint.records[field],before.records[field]):
                        raise ValueError('population review moved canonical opportunities')
                metadata = read(joint.path)
                metadata['routing_source'] = {'path':str(before.path),'sha256':sha256(before.path),
                    'reason':'Two unsupported introduced-population score holds only; all physical identities, coordinates, dates and candidate indices unchanged.'}
                metadata['reduction']['candidate_species_ceiling'] = len(metadata['first_seen'])
                dump(joint.path,metadata)
            finally:
                before.close()
        finally:
            joint.close()
    finally:
        parent.close()
    check = load(OUT/'joint-model/model.json')
    try:
        dump(OUT/'population-retention-audit.json',{'valid':True,
            'original_joint_model_sha256':sha256(DATA/'joint-model/model.json'),
            'audited_joint_model_sha256':sha256(check.path),'held_species':sorted(HELD),
            'held_source_groups':removed_groups,'held_source_observations':removed_observations,
            'canonical_ids_unchanged':True,'coordinates_unchanged':True,
            'archive_rows_deleted':0,'source_independent_score_delta_matches':True,
            'supported_pool':len(check.meta['first_seen'])})
        print(read(OUT/'population-retention-audit.json'),flush=True)
    finally:
        check.close()


if __name__=='__main__':
    main()
