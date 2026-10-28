"""Freeze a separate 2024 provisional policy; keep historical primary controls.

Pyle's dated 2024 Tables 2a/b authorize provisional treatment. Their listed
jurisdictions bound this conservative publisher-review proxy, not every feral
individual nationwide. Identification-unaccepted Table 2c remains held.
"""
from collections import Counter
from copy import deepcopy
import csv
import json
import os
import re
import shutil
import subprocess

from bigyear_planner.compact_model import build
from bigyear_planner.config import Config
from bigyear_planner.eod import attach_dataset
from bigyear_planner.util import ROOT,ManagedDuckDB,dump,read,sha256,write_csv
import align_aba2024 as primary

DATA=ROOT/'data/lower48-aba2024-provisional-v3'
POLICY='lower48-2024-primary-plus-documented-provisionals-v3'
PYLE='https://www.birdpop.org/docs/pubs/Pyle_2024-ABA-Checklist-in-an-evolving-world_NAB-75-1-10.pdf'
TBRC='https://www.texasbirdrecordscommittee.org/review-information/list-of-statements/cattle-tyrant'
# Table 2a jurisdictions, not generic approval of the source's other states.
PROVISIONAL_A={
 'Aix galericulata':['CA','UT'],'Amazona oratrix':['CA'],'Amazona albifrons':['TX'],
 'Amazona amazonica':['FL'],'Cyanoliseus patagonus':['CA'],'Ara ararauna':['FL'],
 'Ara severus':['FL'],'Thectocercus acuticaudatus':['FL'],'Psittacara leucophthalmus':['FL'],
 'Zosterops simplex':['CA'],'Euplectes franciscanus':['CA'],'Vidua macroura':['CA']}


def record_decision(row,raw,tax):
    status,reason,reference=primary.record_decision(row,raw,tax)
    if status!='supported' or raw is None:return status,reason,reference
    for key in ['establishmentMeans','degreeOfEstablishment']:
        value=raw.get(primary.previous.DWC+key,raw.get(key,''))
        if re.search(r'\bcaptive\b|\bdomestic(?:ated)?\b',str(value),re.I):
            return 'unresolved','explicit_individual_captive_or_domestic_metadata',primary.previous.RULES
    if any(str(raw.get(k,'')).lower()=='true' for k in ['isCaptive','captive']):
        return 'unresolved','explicit_individual_captive_flag',primary.previous.RULES
    remarks=raw.get(primary.previous.DWC+'occurrenceRemarks',raw.get('occurrenceRemarks',''))
    if re.search(r'\bcaptive (?:bird|individual)\b|\bescaped (?:pet|from)\b|\bin an? aviary\b',str(remarks),re.I):
        return 'unresolved','individual_remarks_require_wildness_review',primary.previous.RULES
    return status,reason,reference


def revise(tax):
    item={**tax,'policy_id':POLICY,'score_tier':'primary' if tax['aba_eligible']=='1' else 'held'}
    name=tax['source_name']
    if name in PROVISIONAL_A:
        regions=[primary.zone(s,label='Pyle 2024 Table 2a provisional jurisdiction; public occurrence proxy') for s in PROVISIONAL_A[name]]
        item.update(aba_eligible='1',eod_presence_eligible='1',eligible_states='*',
           eligible_regions=json.dumps(regions,separators=(',',':')),score_tier='provisional-A',
           review_basis='2024_Table_2a_provisional; publisher_review_proxy; not_primary_establishment',reference=PYLE+'; '+primary.previous.REVIEW)
    elif name=='Machetornis rixosa':
        # Known Corpus Christi bird, through May 4. TBRC questioned arrival;
        # ABA's separate provisional allowance does not make it primary.
        regions=[primary.zone('TX',(27.75,27.85),(-97.45,-97.35),last='2024-05-04',label='Corpus Christi 2023-105; provisional, provenance uncertain')]
        item.update(aba_eligible='1',eod_presence_eligible='1',eligible_states='*',
           eligible_regions=json.dumps(regions,separators=(',',':')),score_tier='provisional-B',
           review_basis='2024_Table_2b_provisional; known_Corpus_Christi_event; provenance_uncertain_not_primary',reference=PYLE+'; '+TBRC)
    return item


def freeze():
    if (DATA/'policy.json').exists():return
    DATA.mkdir(exist_ok=True)
    shutil.copytree(primary.DATA/'sources',DATA/'sources',dirs_exist_ok=True)
    retrieval=deepcopy(read(primary.DATA/'source-retrieval.json'))
    retrieval['parent_policy_sha256']=sha256(primary.DATA/'policy.json')
    retrieval['new_sources']={}
    for name,url in [('pyle-2024-provisionals.pdf',PYLE),('tbrc-cattle-tyrant.html',TBRC)]:
        subprocess.run(['/usr/bin/curl','--fail','--location','--silent','--show-error','--max-time','90',
                        '-o',str(DATA/'sources'/name),url],check=True)
        retrieval['new_sources'][name]={'url':url,'sha256':sha256(DATA/'sources'/name)}
    dump(DATA/'source-retrieval.json',retrieval)
    rows=[revise(t) for t in csv.DictReader((primary.DATA/'taxonomy.csv').open())]
    write_csv(DATA/'taxonomy.csv',rows,list(rows[0]))
    # Exact held evidence from v2 remains held unless independently re-reviewed.
    for name in ['evidence-decisions.csv','first-23-decisions.csv']:
        shutil.copyfile(primary.DATA/name,DATA/name)
    decisions=[]
    for tax in rows:
        status,reason,reference=primary.source_decision(tax)
        decisions.append({**tax,'default_decision':status,'reason':reason,'decision':status,
                          'reference':reference,'source_groups':tax['presence_rows']})
    write_csv(DATA/'species-decisions.csv',decisions,list(decisions[0]))
    ledger=[]
    prior=list(csv.DictReader((ROOT/'runs/lower48-eod-2024/aba2024-gap-diagnostics/provisional-rule-review.csv').open()))
    byname={r['source_name']:r for r in rows}
    for old in prior:
        t=byname[old['source_name']]
        reason=t['review_basis']
        if old['source_name'] in ['Parus major','Lophura nycthemera']:
            reason='Not on Pyle 2024 Tables 2a/b; previous candidate list overstated 2024 provisional eligibility.'
        elif t['score_tier']=='held':reason='No supported lower-48 jurisdiction/event in dated 2024 provisional source; occurrence remains held.'
        ledger.append({**old,'new_score_tier':t['score_tier'],'new_reason':reason,'reference':t['reference']})
    write_csv(DATA/'provisional-decisions.csv',ledger,list(ledger[0]))
    policy=deepcopy(read(primary.DATA/'policy.json'))
    policy.update(id=POLICY,parent_policy_sha256=sha256(primary.DATA/'policy.json'),
      taxonomic_standard='ABA 8.16 primary units plus Pyle 2024 Table 2a/b provisional allowance, labeled separately.',
      certification='Modeled publisher-review proxy; provisional credit is not primary-list acceptance or certification of individual wildness.',
      rules=[
        'Primary taxonomy and existing population/event decisions remain unchanged.',
        'Dated 2024 provisional Tables 2a/b are countable subject to remaining recording rules; Table 2c is not authorized.',
        'Table 2a provisional jurisdiction envelopes are explicit search assumptions, not proof every individual is wild.',
        'Yellow-headed Parrot California provisional treatment does not revive the extirpated 1975-1982 Florida primary population.',
        'Cattle Tyrant Corpus Christi through May 4 is provisional-B; TBRC non-acceptance of provenance remains disclosed.',
        'Explicit captive, escape, aviary and rehabilitation flags remain held in selected-source checks.',
        'Marine and island birds can be countable geographically; terrestrial polygon masks are an access/model limitation, not an ABA exclusion.',
        'A walking allowance cannot authorize open-water travel. Passenger boats require dated port-to-port service, full sailing time and foot access; pelagic credit requires supported vessel access to the exact dated observation.',
        'The current search retains the terrestrial road/air cohort. Marine observations and unsupported island transport remain unscored, not declared uncountable.',
        'All source observations and exact coordinates remain preserved in the original archive.'],
      score_tiers=dict(Counter(t['score_tier'] for t in rows)),
      source_dispositions=dict(Counter(t['decision'] for t in decisions)),
      population_envelopes={t['source_name']:json.loads(t['eligible_regions']) for t in rows if t['eligible_regions']},
      sources={p.name:sha256(p) for p in sorted((DATA/'sources').iterdir()) if p.is_file()},
      source_retrieval_sha256=sha256(DATA/'source-retrieval.json'))
    policy['decision_files']={n:sha256(DATA/n) for n in ['taxonomy.csv','evidence-decisions.csv','species-decisions.csv','first-23-decisions.csv','provisional-decisions.csv']}
    dump(DATA/'policy.json',policy)
    print(json.dumps({'stage':'policy-frozen','tiers':policy['score_tiers']}),flush=True)


def prepare():
    freeze()
    dataset=DATA/'dataset';dataset.mkdir(exist_ok=True)
    if not (dataset/'manifest.json').exists():
        manifest=deepcopy(read(primary.DATA/'dataset/manifest.json'))
        manifest.update(taxonomy_policy=POLICY,countability={'policy':POLICY,'policy_sha256':sha256(DATA/'policy.json'),
            'score':'Primary plus separately labeled documented 2024 provisionals; publisher-review proxy.'})
        for name in ['land.csv','scope.geojson','scope-points.csv','countability-evidence.csv']:
            os.link(primary.DATA/'dataset'/name,dataset/name)
        for source,target in [('taxonomy.csv','taxonomy.csv'),('policy.json','countability-policy.json')]:
            shutil.copyfile(DATA/source,dataset/target)
        manifest['inputs']['taxonomy']={'path':str(dataset/'taxonomy.csv'),'sha256':sha256(dataset/'taxonomy.csv')}
        manifest['limitations']=read(DATA/'policy.json')['rules']
        manifest['outputs']={n:sha256(dataset/n) for n in manifest['outputs']}
        # Parent's counts are not assertions about this new scoring policy.
        manifest['statistics']={'status':'computed by complete model build; see model.json'}
        dump(dataset/'manifest.json',manifest)
    manifest=read(dataset/'manifest.json')
    if 'dispositions' not in manifest['statistics']:
        with ManagedDuckDB() as con:
            con.execute("SET memory_limit='3GB'");con.execute('SET threads=2')
            _,observations=attach_dataset(con,dataset,manifest)
            dispositions=dict(con.execute('SELECT disposition,count(*) FROM eod_classified GROUP BY 1').fetchall())
            species,locations,groups,count=con.execute(f'SELECT count(DISTINCT species_id),count(DISTINCT location_id),count(*),sum(source_rows) FROM {observations}').fetchone()
        manifest['statistics']={'dispositions':dispositions,'annual_recorded_species_ceiling':species,
          'locations':locations,'eod_daily_presence_groups':groups,'represented_observations':count}
        dump(dataset/'manifest.json',manifest)
    model=build(dataset,DATA/'model',Config(**read(primary.DATA/'model/model.json')['config']))
    print(json.dumps({'stage':'model-ready','species':len(model.meta['species']),
        'groups':model.meta['eligible_presence_groups'],'observations':model.meta['represented_observations']}),flush=True)
    model.close()


if __name__=='__main__':prepare()
