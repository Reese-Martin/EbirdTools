"""Prepare a real earlier EOD year under the fixed, comparable scoring standard.

Never retime observations. Population envelopes are scenario assumptions shared
with 2024; independently dated exceptional events keep their actual chronology.
"""
import argparse
import csv
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import zipfile

from bigyear_planner.compact_model import build, load
from bigyear_planner.config import Config
from bigyear_planner.util import ROOT, dump, read, sha256, write_csv
import prepare_lower48 as national


YEAR = 2023
DATA = ROOT / f'data/lower48-eod-{YEAR}'
STANDARD = ROOT / 'data/lower48-aba2024-provisional-v3/dataset'
POLICY = 'fixed-ABA2024-concepts-and-provisionals-on-2023-observations-v1'


def configure():
    DATA.mkdir(exist_ok=True)
    national.DATA = DATA
    # Holding the same geographic polygon avoids changing scope with year.
    national.BOUNDARY = ROOT / 'data/lower48-eod-2024/sources/lower48-dc-acs2024.geojson'
    national.POLICY = POLICY
    cfg = read(ROOT / 'data/shared-12h-2024-v1/model/model.json')['config']
    cfg.update(year=YEAR, seed=20240101)
    national.CONFIG = Config(**cfg)
    dump(DATA / 'config.json', national.CONFIG.to_dict())


def taxonomy():
    rows = list(csv.DictReader((STANDARD / 'taxonomy.csv').open()))
    original_fields = list(rows[0])
    keys = {(r['source_name'], r['source_taxon_id']) for r in rows}
    aba_file = ROOT/'data/lower48-aba2024-aligned/sources/ABA_Checklist-8.16.csv.zip'
    with zipfile.ZipFile(aba_file) as archive:
        aba = {r[3]:r for r in csv.reader(io.StringIO(archive.read('ABA_Checklist-8.16.csv').decode('utf-8-sig')))
               if len(r)>5 and r[5] in list('123456')}
    additions = [item for item in csv.DictReader((DATA/'taxa.csv').open())
                 if (item['source_name'],item['source_taxon_id']) not in keys]
    dest = DATA/'sources/new-concept-verbatim'
    dest.mkdir(parents=True, exist_ok=True)
    def verify(item):
        target = dest/(item['example_gbif_id']+'.json')
        if not target.exists():
            national.download('https://api.gbif.org/v1/occurrence/'+item['example_gbif_id']+'/verbatim',target)
        raw = read(target)
        def get(k):
            return raw.get(k,raw.get('http://rs.tdwg.org/dwc/terms/'+k))
        if (get('scientificName'),get('taxonConceptID'),get('basisOfRecord'),get('occurrenceStatus'),raw['datasetKey']) != (
            item['source_name'],item['source_taxon_id'],'HumanObservation','PRESENT','4fa7b334-ce0d-4e88-aaae-2e0c138d049e'):
            raise ValueError('earlier-year source concept differs from publisher')
        return sha256(target)
    with ThreadPoolExecutor(max_workers=4) as pool:
        proofs = dict(zip([v['example_gbif_id'] for v in additions],pool.map(verify,additions)))
    parent_policy = read(STANDARD/'countability-policy.json')
    for name,digest in parent_policy['sources'].items():
        original = STANDARD.parent/'sources'/name
        if sha256(original)!=digest:
            raise ValueError('fixed countability rule source changed')
        target = DATA/'sources'/name
        if not target.exists():
            os.link(original,target)
    selected_sources = DATA/'sources/selected-verbatim'
    selected_sources.mkdir(exist_ok=True)
    for key,digest in proofs.items():
        target = selected_sources/(key+'.json')
        if not target.exists():
            os.link(dest/(key+'.json'),target)
    dump(DATA/'source-retrieval.json',{'records':{k:{'retrieved':True,'sha256':v}
        for k,v in proofs.items()},'purpose':'Verify earlier-year source concept identities; selected-route encounter checks are separate.'})
    unresolved = []
    resolved = []
    for item in additions:
        if (item['source_name'], item['source_taxon_id']) not in keys:
            row = dict.fromkeys(original_fields, '')
            row.update(source_name=item['source_name'], source_taxon_id=item['source_taxon_id'],
                common_name=item['source_name'], aba_eligible='review',
                eod_presence_eligible='review', eligible_states='*', score_tier='held',
                reference='https://api.gbif.org/v1/occurrence/'+item['example_gbif_id']+'/verbatim',
                review_basis='New earlier-year source concept requires explicit crosswalk review.')
            primary = aba.get(item['source_name'])
            if primary:
                row.update(species_id='aba2024:'+item['source_name'].replace(' ','_'),
                    common_name=primary[1],aba_eligible='1',eod_presence_eligible='1',
                    score_tier='primary',aba_2024_scientific_name=primary[3],aba_2024_code=primary[5],
                    taxonomic_mapping='exact_2024_scientific_name',
                    reference=row['reference']+'; https://www.aba.org/wp-content/uploads/2020/03/ABA_Checklist-8.16.csv.zip',
                    review_basis='Exact publisher name/concept verified; fixed 2024 primary membership; publisher public-review proxy, no extra rarity-code screen.')
                resolved.append(item)
            else:
                unresolved.append(item)
            rows.append(row)
    for row in rows:
        row['policy_id'] = POLICY
        if not row['eligible_regions']:
            continue
        regions = json.loads(row['eligible_regions'])
        for region in regions:
            # Annual population bounds are a fixed interannual scenario, not
            # evidence that an individual or population was established then.
            if (region.get('first'), region.get('last')) == ('2024-01-01', '2024-12-31'):
                region.update(first='2023-01-01', last='2023-12-31')
            elif row['source_name'] == 'Machetornis rixosa':
                region.update(first='2023-11-12', last='2024-05-04')
            # Whooper Swan's 2024 event is deliberately NOT moved to 2023.
        row['eligible_regions'] = json.dumps(regions, separators=(',', ':'))
    write_csv(DATA / 'taxonomy.csv', rows, original_fields)
    write_csv(DATA / 'unresolved-source-concepts.csv', unresolved,
        ['source_name','source_taxon_id','presence_rows','source_records','example_gbif_id'])
    policy = {'id': POLICY, 'year': YEAR, 'parent_policy_path': str(STANDARD/'countability-policy.json'),
        'parent_policy_sha256': sha256(STANDARD/'countability-policy.json'),
        'taxonomy_sha256': sha256(DATA/'taxonomy.csv'),
        'standard': 'Fixed ABA 8.16 concepts and user-approved 2024 provisional tiers, applied retrospectively for comparison; not a claim of contemporaneous 2023 ABA acceptance.',
        'population_rule': 'Same geographic population envelopes in both years; annual envelopes cover the tested year as a stated scenario assumption. Actual source support on each 2023 date remains mandatory.',
        'event_rule': 'Whooper Swan 2024 event is not retimed. Corpus Christi Cattle Tyrant is provisional-B only from documented first discovery 2023-11-12 through the existing 2024 cutoff.',
        'event_reference': 'https://www.texasbirdrecordscommittee.org/review-information/list-of-statements/cattle-tyrant',
        'record_rule': 'Source-specific 2024 record decisions are not transferred to different 2023 records. Every selected 2023 representative is independently checked against publisher metadata, captive/escape flags, exact date, concept and coordinates.',
        'unresolved_new_concepts': len(unresolved), 'all_source_observations_retained': True,
        'additional_primary_concepts': resolved, 'new_concept_publisher_sha256': proofs,
        'aba_source_sha256':sha256(aba_file),
        'sources':parent_policy['sources'],
        'source_retrieval_sha256':sha256(DATA/'source-retrieval.json'),
        'coordinates_rounded': False}
    dump(DATA/'countability-policy.json', policy)
    print(json.dumps({'stage':'taxonomy-ready', 'additional_primary_names':[r['source_name'] for r in resolved],
        'new_concepts_to_review':[r['source_name'] for r in unresolved]}), flush=True)


def dataset():
    national.dataset()
    folder = DATA/'dataset'
    manifest = read(folder/'manifest.json')
    if manifest.get('countability'):
        return
    for name in ['countability-policy.json']:
        (folder/name).write_bytes((DATA/name).read_bytes())
    # Explicit header-only table: earlier-year records have not inherited any
    # later-year record decisions. Selected-source checking remains mandatory.
    write_csv(folder/'countability-evidence.csv', [],
        ['representative_gbifid','source_name','source_taxon_id','date','decision','reason','reference','latitude','longitude'])
    manifest = deepcopy(manifest)
    manifest['countability'] = {'policy':POLICY,
        'policy_sha256':sha256(folder/'countability-policy.json'),
        'individual_wild_status':'Publisher public-review proxy; explicit captive/escape flags held in selected-source validation.'}
    for name in ['countability-policy.json','countability-evidence.csv']:
        manifest['outputs'][name] = sha256(folder/name)
    manifest['limitations'].extend([read(DATA/'countability-policy.json')[k]
        for k in ['standard','population_rule','event_rule','record_rule']])
    dump(folder/'manifest.json', manifest)


def compact():
    model = build(DATA/'dataset', DATA/'model', national.CONFIG)
    try:
        print(json.dumps({'stage':'compact-ready', **{k:model.meta[k] for k in
            ['locations','opportunities','eligible_presence_groups','represented_observations']}}), flush=True)
    finally:
        model.close()
    check = load(DATA/'model/model.json')
    check.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['audit','taxonomy','land','dataset','compact','all'])
    args = parser.parse_args()
    configure()
    for stage in ['audit','taxonomy','land','dataset','compact'] if args.stage=='all' else [args.stage]:
        {'audit':national.audit,'taxonomy':taxonomy,'land':national.land,
         'dataset':dataset,'compact':compact}[stage]()
