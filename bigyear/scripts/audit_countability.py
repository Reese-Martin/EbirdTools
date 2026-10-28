"""Freeze public source evidence and explicit, conservative scoring decisions.

Supported means supported under the documented modeling policy, never a claim
that a hypothetical observer actually encountered the bird or an ABA certification.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

from bigyear_planner.util import ROOT, dump, read, sha256, write_csv

DATA = Path(os.environ.get('BIGYEAR_COUNTABILITY_DATA',str(ROOT/'data/lower48-countability-2024'))).resolve()
OUT = ROOT/'runs/lower48-eod-2024'
BASE = OUT/'regional-flights-900s-600s'
DWC = 'http://rs.tdwg.org/dwc/terms/'
POLICY = 'lower48-2024-publisher-reviewed-countability-v1'
RULES = 'https://www.aba.org/aba-recording-rules-and-interpretations/'
INTRODUCED = 'https://www.aba.org/aba-area-introduced-species/'
REVIEW = 'https://support.ebird.org/en/support/solutions/articles/48001078113'
TBRC = 'https://www.texasbirds.org/media/1962/tos_bulletin_2024_issue-57.pdf'
CBRC = 'https://www.californiabirds.org/queryDatabase.asp?STARTDATE=&county=&enddate=&partial=on&sort=date&species=curlew+sandpiper'
RIARC = 'https://www.birdobserver.org/Issues/2026/October-2026/seventh-report-of-the-rhode-island-avian-records-committee'

# Non-native taxa needing population evidence. Native North American taxa also
# listed on the ABA introductions page are not treated as globally non-native.
# Widely established House Sparrow, European Starling and Eurasian Collared-Dove
# have an explicit ABA-Area-wide population rule below.
INTRODUCED_TAXA = set('''Acridotheres tristis|Agapornis roseicollis|Alectoris chukar|
Alopochen aegyptiaca|Amazona finschi|Amazona oratrix|Amazona viridigenalis|
Aratinga nenday|Brotogeris chiriri|Brotogeris versicolurus|Cairina moschata|
Carduelis carduelis|Chrysolophus pictus|Columba livia|Cygnus olor|Estrilda astrild|
Euodice cantans|Francolinus francolinus|Gallus gallus|Garrulax canorus|
Icterus pectoralis|Leiothrix lutea|Lonchura atricapilla|Lonchura malacca|
Lonchura punctulata|Melopsittacus undulatus|Myiopsitta monachus|
Numida meleagris|Ortygornis pondicerianus|Paroaria capitata|Paroaria coronata|
Passer domesticus|Passer montanus|Pavo cristatus|Perdix perdix|
Phasianus colchicus|Ploceus cucullatus|Porphyrio poliocephalus|
Psittacara erythrogenys|Psittacara holochlorus|Psittacara mitratus|
Psittacula krameri|Pternistis erckelii|Pycnonotus cafer|Pycnonotus jocosus|
Serinus canaria|Sicalis flaveola|Spilopelia chinensis|Streptopelia chinensis|
Streptopelia decaocto|Streptopelia roseogrisea|Sturnus vulgaris|
Tetraogallus himalayensis|Uraeginthus bengalus|Zosterops japonicus|
Zosterops simplex|Crithagra mozambica'''.replace('\n','').split('|'))
AREA_WIDE = {'Passer domesticus','Sturnus vulgaris','Streptopelia decaocto'}
REINTRODUCTIONS = {'Gymnogyps californianus','Grus americana'}

# These are manually reviewed event matches, not general approvals of a rare
# species throughout Texas. Only source records at the named location and within
# the published accepted interval are eligible for an evidence override.
EVENTS = [
 ('Pachyramphus major','Cameron','resaca de la palma','2023-11-25','2024-03-23','2023-112'),
 ('Rupornis magnirostris','Cameron','resaca de la palma','2023-11-07','2024-03-23','2023-107'),
 ('Strix virgata','Starr','santa margarita','2023-11-18','2024-12-31','2023-122'),
 ('Tigrisoma mexicanum','Starr','santa margarita|salineño|salineno','2023-11-12','2024-03-18','2023-123'),
 ('Cyanocorax morio','Starr','santa margarita|salineño|salineno','2023-03-03','2024-12-31','2023-20'),
 ('Pachyramphus aglaiae','Cameron','resaca de la palma','2023-11-10','2024-04-23','2023-121'),
 ('Pachyramphus aglaiae','Starr','santa margarita','2023-03-11','2024-10-05','2023-19'),
 ('Basileuterus culicivorus','Hidalgo','frontera','2023-09-16','2024-03-01','2023-165'),
 ('Periporphyrus celaeno','Hidalgo','frontera','2023-12-02','2024-04-26','2023-164'),
 ('Cyanocompsa parellina','Bexar','san antonio|macarthur','2024-01-14','2024-03-12','2024-06'),
]
OTHER_EVENTS = [
 ('California','Calidris ferruginea','San Diego','south bay salt works|south san diego bay',
     '2024-07-13','2024-07-23','CBRC_2024-061',CBRC),
 ('Rhode Island','Anser brachyrhynchus','Washington','trustom pond',
     '2024-11-25','2024-12-07','RIARC_2024-3',RIARC),
]

# County is corroboration of an individually inspected selected occurrence, not
# a claim that an entire county is an established population's exact boundary.
# These overrides apply only to the explicit GBIF IDs in the evidence CSV.
POPULATIONS = {
 'Acridotheres tristis':('Florida',{'Miami-Dade','Broward','Monroe','Palm Beach'},'South Florida; established by 1997'),
 'Amazona viridigenalis':('Texas',{'Hidalgo','Cameron','Willacy'},'Lower Rio Grande Valley; established before 1980'),
 'Amazona finschi':('California',{'Los Angeles'},'Los Angeles region; established by 1992'),
 'Brotogeris chiriri':('Florida',{'Miami-Dade'},'Miami region; established by 1995'),
 'Brotogeris versicolurus':('Florida',{'Miami-Dade'},'Miami region; established before 1980'),
 'Icterus pectoralis':('Florida',{'Miami-Dade'},'Miami region; established before 1980'),
 'Psittacara holochlorus':('Texas',{'Hidalgo','Cameron','Willacy'},'Lower Rio Grande Valley; established before 1980'),
 'Agapornis roseicollis':('Arizona',{'Maricopa'},'Phoenix region; established by 2002'),
}


def occurrences(route):
    rows = []
    for day in route['days']:
        for stop in day['stops']:
            loc = route['locations'][stop['location']]
            for species in stop['species']:
                for evidence in stop['evidence'][species]:
                    rows.append({'species_id':species,'date':day['date'],'state':loc['state'],
                         'latitude':stop['lat'],'longitude':stop['lon'],
                         'latitude_text':loc['name'][4:].split(',')[0],
                         'longitude_text':loc['name'][4:].split(',')[1],
                         'record':evidence['record'],'source_name':evidence['taxon'],
                         'opportunity':route['search']['selected_model_opportunities'][stop['opportunity']]})
    return rows


def fetch(route_path=None,label=None):
    route_path=Path(route_path) if route_path else BASE/'route.json'
    if label and not re.fullmatch('[a-z0-9-]+',label):
        raise ValueError('invalid evidence snapshot label')
    rows = occurrences(read(route_path))
    ids = sorted({r['record'].removeprefix('GBIF:') for r in rows})
    folder = DATA/'sources'/((label+'-verbatim') if label else 'selected-verbatim');folder.mkdir(parents=True,exist_ok=True)
    snapshot_path=DATA/((label+'-source-retrieval.json') if label else 'source-retrieval.json')
    if snapshot_path.exists():
        previous=read(snapshot_path)
        if previous['source_route_sha256']!=sha256(route_path):
            raise ValueError('evidence snapshot label already belongs to another route; use a fresh label')
        if set(previous['records'])==set(ids) and all(v.get('retrieved') for v in previous['records'].values()):
            for key,item in previous['records'].items():
                if sha256(folder/(key+'.json'))!=item['sha256']:
                    raise ValueError('frozen source evidence changed')
            print(json.dumps({'complete':len(ids),'reused_immutable_snapshot':True}),flush=True)
            return

    def get(key):
        if not re.fullmatch(r'\d+',key):
            raise ValueError('invalid public record ID')
        target = folder/(key+'.json')
        if label and not target.exists():
            for cache_name in ['selected-verbatim','rerun-verbatim']:
                previous=DATA/'sources'/cache_name/(key+'.json')
                if previous.exists() and previous!=target:
                    os.link(previous,target);break
        if not target.exists():
            temporary = target.with_suffix('.part')
            result = subprocess.run(['/usr/bin/curl','--fail','--location','--silent','--show-error',
                 '--retry','2','--max-time','35','https://api.gbif.org/v1/occurrence/'+key+'/verbatim',
                 '--output',str(temporary)],capture_output=True,text=True)
            if result.returncode:
                temporary.unlink(missing_ok=True)
                return key,{'retrieved':False,'reason':result.stderr[-500:]}
            record = read(temporary)
            if str(record.get('key'))!=key:
                raise ValueError('GBIF ID differs from request')
            temporary.replace(target)
        return key,{'retrieved':True,'sha256':sha256(target)}

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(get,key) for key in ids]
        for future in as_completed(jobs):
            key,result = future.result();results[key] = result
            if len(results)%100==0:
                print(json.dumps({'stage':'source-records','complete':len(results),'total':len(ids)}),flush=True)
    dump(snapshot_path,{'retrieved_at':datetime.now(timezone.utc).isoformat(),
         'source_route_sha256':sha256(route_path),'records':results})
    print(json.dumps({'complete':len(results),'retrieved':sum(v['retrieved'] for v in results.values())}),flush=True)


class Text(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[];self.hidden=0

    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:
            self.hidden+=1

    def handle_endtag(self,tag):
        if tag in {'script','style'}:
            self.hidden=max(0,self.hidden-1)

    def handle_data(self,data):
        if not self.hidden:
            self.parts.append(data)


def text(path):
    parser=Text();parser.feed(Path(path).read_text())
    return re.sub(r'\s+',' ',unescape(' '.join(parser.parts)))


def general_decision(name,code,historical):
    if code=='6':
        return 'excluded','historical_extinction_or_extirpated_population',RULES
    if not historical:
        return 'unresolved','historical_taxonomy_mapping',RULES
    if name in AREA_WIDE:
        return 'supported','publisher_reviewed_area_wide_established_population',INTRODUCED+'; '+REVIEW
    if name in INTRODUCED_TAXA:
        return 'unresolved','introduced_population_or_provenance',INTRODUCED
    if name in REINTRODUCTIONS:
        return 'unresolved','reintroduction_individual_provenance',RULES
    if code in {'3','4','5'}:
        return 'unresolved','rarity_needs_independent_event_corroboration',RULES
    return 'supported','publisher_reviewed_native_species',REVIEW+'; '+RULES


def record_decision(row,raw,taxon,default):
    decision,reason,reference = default
    if decision=='excluded':
        return default
    if raw is None:
        return 'unresolved','public_source_metadata_unavailable',row['record']
    get=lambda key:raw.get(DWC+key,raw.get(key))
    actual_date=f"{int(get('year') or 0):04d}-{int(get('month') or 0):02d}-{int(get('day') or 0):02d}"
    try:
        valid=(str(raw.get('key'))==row['record'].removeprefix('GBIF:')
           and raw.get('datasetKey')=='4fa7b334-ce0d-4e88-aaae-2e0c138d049e'
           and get('scientificName')==row['source_name']
           and get('taxonConceptID')==taxon['source_taxon_id']
           and get('basisOfRecord')=='HumanObservation' and get('occurrenceStatus')=='PRESENT'
           and actual_date==row['date'] and get('country')=='United States'
           and (float(get('decimalLatitude')),float(get('decimalLongitude')))==(row['latitude'],row['longitude']))
    except (ValueError,TypeError):
        valid=False
    if not valid:
        return 'unresolved','publisher_metadata_mismatch',row['record']
    locality=(get('locality') or '').lower()
    if re.search(r'\bzoo\b|\baviary\b|\bcaptive\b|\bescape[ed]?\b|rehabilitation',locality):
        return 'unresolved','locality_requires_wild_status_review',RULES
    if decision=='supported':
        return default
    name=row['source_name']
    for scientific,county,place,first,last,event in EVENTS:
        if (name==scientific and get('stateProvince')=='Texas' and get('county')==county
             and re.search(place,locality) and first<=row['date']<=last):
            return 'supported','accepted_TBRC_event_'+event,TBRC
    for state,scientific,county,place,first,last,event,source in OTHER_EVENTS:
        if (name==scientific and get('stateProvince')==state and get('county')==county
             and re.search(place,locality) and first<=row['date']<=last):
            return 'supported','accepted_event_'+event,source
    if name in POPULATIONS:
        state,counties,population=POPULATIONS[name]
        if get('stateProvince')==state and get('county') in counties:
            return 'supported','reviewed_population_match: '+population,INTRODUCED
    return default


def decide():
    route=read(BASE/'route.json');rows=occurrences(route)
    retrieval=read(DATA/'source-retrieval.json')
    if retrieval['source_route_sha256']!=sha256(BASE/'route.json'):
        raise ValueError('retrieved evidence belongs to another route')
    if 'All eBird data that are publicly available have gone through' not in text(DATA/'sources/ebird-data-use.html'):
        raise ValueError('publisher review-process statement missing')
    old=ROOT/'data/lower48-eod-2024'
    taxonomy=list(csv.DictReader((old/'dataset/taxonomy.csv').open()))
    tax_by_id={r['species_id']:r for r in taxonomy}
    with zipfile.ZipFile(ROOT/'data/crosswalk-review-2026-10-15/sources/ABA_Checklist-8.19.csv.zip') as archive:
        aba={r[3]:r for r in csv.reader(io.StringIO(archive.read('ABA_Checklist-8.19.csv').decode('utf-8-sig')))
             if len(r)==6 and r[5] in {'1','2','3','4','5','6'}}
    historical=text(DATA/'sources/aba-clc-july2024.html')+' '+(DATA/'sources/ABA_Checklist-8.16.txt').read_text()
    aliases={'Ardea ibis':'Bubulcus ibis','Larus smithsonianus':'Larus argentatus'}
    defaults={};species_rows=[]
    for sp in route['species']:
        tax=tax_by_id[sp['id']];name=tax['source_name'];a=aba[name]
        membership=(name in historical or aliases.get(name,'<none>') in historical
                    or sp['name'].replace('-',' ') in historical.replace('-',' '))
        status,reason,ref=general_decision(name,a[5],membership)
        defaults[sp['id']]=(status,reason,ref)
        species_rows.append({'species_id':sp['id'],'name':sp['name'],'source_name':name,
             'aba_code':a[5],'historical_mapping_supported':membership,'default_decision':status,
             'reason':reason,'reference':ref,'individual_approval':'not present in EOD',
             'individual_wild_status':'not present in EOD; native/widespread established records use explicit publisher-review proxy'})
    evidence=[]
    for row in rows:
        key=row['record'].removeprefix('GBIF:')
        result=retrieval['records'].get(key,{})
        raw=None
        if result.get('retrieved'):
            path=DATA/'sources/selected-verbatim'/(key+'.json')
            if sha256(path)!=result['sha256']:
                raise ValueError('retrieved publisher evidence changed')
            raw=read(path)
        tax=tax_by_id[row['species_id']]
        status,reason,ref=record_decision(row,raw,tax,defaults[row['species_id']])
        evidence.append({**row,'representative_gbifid':key,'source_taxon_id':tax['source_taxon_id'],
             'decision':status,'reason':reason,'reference':ref,
             'locality':(raw or {}).get(DWC+'locality',''),'county':(raw or {}).get(DWC+'county',''),
             'publisher_source_sha256':result.get('sha256',''),
             'source_observation_id':(raw or {}).get(DWC+'occurrenceID',''),
             'individual_wild_status':'not supplied; explicit policy proxy, not independently certified'})
    totals=Counter();selected_ids=set()
    for item in species_rows:
        selected=[v for v in evidence if v['species_id']==item['species_id']]
        if not selected:
            item.update(credited_in_baseline=False,decision=item['default_decision'],supported_records=0,unresolved_records=0)
            continue
        selected_ids.add(item['species_id'])
        statuses=Counter(v['decision'] for v in selected)
        status='supported' if statuses['supported'] else ('unresolved' if statuses['unresolved'] else 'excluded')
        item.update(credited_in_baseline=True,decision=status,supported_records=statuses['supported'],
             unresolved_records=statuses['unresolved'],selected_records=len(selected),
             selected_reasons='; '.join(sorted({v['reason'] for v in selected})))
        totals[status]+=1
    if len(selected_ids)!=626:
        raise ValueError('audit does not account for all 626 credited species')
    write_csv(DATA/'species-decisions.csv',species_rows,list(dict.fromkeys(k for r in species_rows for k in r)))
    write_csv(DATA/'evidence-decisions.csv',evidence,list(evidence[0]))
    for tax in taxonomy:
        if tax['species_id'] in defaults:
            status,reason,ref=defaults[tax['species_id']]
            tax.update(aba_eligible='0' if status=='excluded' else ('1' if status=='supported' else 'review'),
                 eod_presence_eligible='1' if status=='supported' else ('0' if status=='excluded' else 'review'),
                 review_basis=reason,reference=ref,policy_id=POLICY)
    write_csv(DATA/'taxonomy.csv',taxonomy,list(taxonomy[0]))
    policy={'id':POLICY,'year':2024,'baseline_species':626,'baseline_decisions':dict(totals),
       'baseline_route':str(BASE/'route.json'),'baseline_route_sha256':sha256(BASE/'route.json'),
       'rules':[
        'Score supported evidence only; unresolved and excluded evidence receive zero credit.',
        'Native code-1/2 records use the documented public eBird review process as a quality and wild-occurrence proxy. This is a stated modeling assumption, not individual wild-status certification.',
        'Code-3/4/5 records require an independently documented matching event. This additional conservative screen is our modeling policy, not an ABA rule declaring rarities uncountable.',
        'Introduced species need explicit ABA population support; only selected reviewed population records are overrides, except three explicitly ABA-Area-wide established taxa.',
        'Code-6 historical extinct/extirpated taxa receive no 2024 credit.',
        'Reintroduced condors/cranes without individual provenance remain unresolved.',
        'Unavailable or conflicting publisher metadata is unresolved; GBIF crawl status is never interpreted as an eBird approval decision.',
        'All exact locations, dates and observations remain in the original source and complete candidate catalog. Only scoring eligibility changes.',
        'Other catalog species use the same default screen; unaudited rare/introduced records cannot gain credit in the rerun.',
        'Bird position, actual encounter time, permissions and ethical access cannot be independently reconstructed from EOD; the original perfect-hindsight and access assumptions remain.'
       ],'taxonomic_standard':'Frozen ABA 8.19 species identities; historical membership checked against ABA 8.16 (2024) with explicit name/genus aliases; historical introduced-population restrictions enforced.',
       'certification':'A conservative, reproducible modeled score. No official ABA certification or proof of every individual bird\'s wild status.',
       'decision_files':{name:sha256(DATA/name) for name in ['species-decisions.csv','evidence-decisions.csv','taxonomy.csv']},
       'sources':{p.name:sha256(p) for p in sorted((DATA/'sources').iterdir()) if p.is_file()},
       'source_retrieval_sha256':sha256(DATA/'source-retrieval.json'),
       'curated_events':[dict(zip(['scientific_name','county','locality_regex','from','through','TBRC_record'],v)) for v in EVENTS],
       'other_curated_events':[dict(zip(['state','scientific_name','county','locality_regex','from','through','record','source'],v)) for v in OTHER_EVENTS],
       'reviewed_population_rules':{k:[s,sorted(c),p] for k,(s,c,p) in POPULATIONS.items()}}
    dump(DATA/'policy.json',policy)
    print(json.dumps({'stage':'decisions','baseline':dict(totals),'evidence_records':len(evidence)}),flush=True)


def main(stage,route_path=None,label=None):
    DATA.mkdir(parents=True,exist_ok=True)
    if stage=='fetch':
        fetch(route_path,label)
    elif stage=='decide':
        decide()
    else:
        raise ValueError('audit policy has not been frozen')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('stage',choices=['fetch','decide'])
    parser.add_argument('--route',type=Path)
    parser.add_argument('--label')
    args=parser.parse_args()
    main(args.stage,args.route,args.label)
