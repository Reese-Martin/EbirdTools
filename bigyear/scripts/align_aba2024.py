"""Freeze a full-source crosswalk to the ABA's 2024 primary checklist.

The old conservative rarity-code screen is deliberately not an ABA rule. Public
publisher review remains an explicit occurrence proxy. Population envelopes are
documented operational assumptions, never official ABA population boundaries.
"""

from collections import Counter
import csv
from copy import deepcopy
import io
import json
import os
import re
import zipfile

from bigyear_planner.countability import region_supported
from bigyear_planner.eod import STATE_NAMES, attach_dataset
from bigyear_planner.util import ROOT, ManagedDuckDB, dump, read, sha256, write_csv
import audit_countability as previous

DATA = ROOT/'data/lower48-aba2024-aligned'
ORIGINAL = ROOT/'data/lower48-eod-2024'
POLICY = 'lower48-2024-publisher-reviewed-countability-aba816-v2'
CHECKLIST = 'https://www.aba.org/wp-content/uploads/2020/03/ABA_Checklist-8.16.csv.zip'
ANNUAL = 'https://www.aba.org/wp-content/uploads/2025/02/2024_ABA_CLC_Report.pdf'
REPATRIATED = 'https://www.aba.org/aba-area-reintroduced-indigenous-species/'
WBRC = 'https://wos.org/records/votingsummary/spring-2024/'

# Explicit synonyms, genus changes, and daughter concepts collapsing to ONE
# 2024 species. No geographic assignment to a modern split is inferred.
ALIASES = {
    'Anarhynchus montanus':'Anarhynchu montanus',  # Typo in the official 8.16 CSV.
    'Ardea ibis':'Bubulcus ibis',
    'Astur atricapillus':'Accipiter atricapillus',
    'Astur cooperii':'Accipiter cooperii',
    'Cyanocorax morio':'Psilorhinus morio',
    'Daptrius chimachima':'Milvago chimachima',
    'Larus smithsonianus':'Larus argentatus',
    'Larus vegae':'Larus argentatus',
    'Saxicola stejnegeri':'Saxicola maurus',
    'Spilopelia chinensis':'Streptopelia chinensis',
}


def zone(state, lat=(24.,50.), lon=(-125.,-66.), first='2024-01-01', last='2024-12-31', label=''):
    return dict(state='US-'+state,min_lat=lat[0],max_lat=lat[1],min_lon=lon[0],max_lon=lon[1],
                first=first,last=last,label=label)


# Compact cores within the regions explicitly named by ABA RSEC. These are
# conservative envelopes for search; not county-wide or nationwide approvals.
LA=zone('CA',(33.7,34.35),(-118.7,-117.6),label='Los Angeles urban region')
SD=zone('CA',(32.55,33.05),(-117.3,-116.9),label='San Diego urban region')
MIAMI=zone('FL',(25.5,26.0),(-80.5,-80.05),label='Miami / Kendall')
SFL=zone('FL',(24.4,27.3),(-82.,-79.8),label='South Florida')
PHOENIX=zone('AZ',(33.1,33.9),(-112.7,-111.5),label='Phoenix urban region')
HOUSTON=zone('TX',(29.4,30.2),(-95.9,-94.9),label='Houston urban region')
BAKERSFIELD=zone('CA',(35.2,35.55),(-119.3,-118.8),label='Bakersfield urban region')
LRGV=zone('TX',(25.8,26.7),(-99.15,-97.1),label='Lower Rio Grande Valley')


def population_regions(name):
    """None = native/general proxy; [] = unresolved mainland population."""
    rules = {
        'Alectoris chukar':[zone(s,label='Established western population (CO–CA–BC)')
                           for s in ['CA','OR','WA','ID','NV','UT','CO','WY']],
        'Perdix perdix':[zone(s,label='Established northern/western population')
                        for s in ['WI','MN','ND','SD','MT','WY','ID','WA','OR']],
        'Phasianus colchicus':[zone(s,label='Established population outside Southeast')
              for s in ['CA','OR','WA','ID','NV','UT','AZ','NM','CO','WY','MT','ND','SD','NE',
                        'KS','OK','MN','IA','MO','WI','IL','IN','MI','OH','PA','NY','NJ','DE',
                        'MD','CT','RI','MA','VT','NH','ME']],
        'Pavo cristatus':[LA,zone('FL',label='Established Florida population')],
        'Amazona finschi':[LA,SD],
        'Amazona viridigenalis':[LA,SD,LRGV,zone('FL',(26.,26.8),(-80.4,-80.),label='Fort Lauderdale–West Palm Beach')],
        'Psittacara erythrogenys':[LA,SD,MIAMI,zone('CA',(37.65,37.85),(-122.55,-122.3),label='San Francisco'),zone('FL',(26.,26.3),(-80.4,-80.),label='Fort Lauderdale')],
        'Psittacara mitratus':[LA,MIAMI,zone('CA',(37.3,37.5),(-122.15,-121.9),label='Sunnyvale')],
        'Psittacula krameri':[BAKERSFIELD,zone('FL',(26.,26.4),(-81.9,-81.6),label='Naples')],
        'Pycnonotus jocosus':[LA,MIAMI],
        'Acridotheres tristis':[SFL],
        'Agapornis roseicollis':[PHOENIX],
        'Aratinga nenday':[LA,zone('FL',label='Established Florida population')],
        'Brotogeris chiriri':[LA,MIAMI],
        'Brotogeris versicolurus':[MIAMI],
        'Icterus pectoralis':[MIAMI],
        'Psittacara holochlorus':[LRGV],
        'Cairina moschata':[LRGV,zone('FL',label='Established Florida population')],
        'Alopochen aegyptiaca':[LA,zone('FL',label='Established Florida population'),zone('TX',(29.3,30.6),(-99.,-97.2),label='Central Texas')],
        'Cygnus olor':[zone(s,label='Eastern/Great Lakes established population') for s in ['MN','WI','MI','IL','IN','OH','PA','NY','NJ','CT','RI','MA','DE','MD','VA','NC']],
        'Gallus gallus':[zone('FL',(24.5,24.6),(-81.82,-81.72),label='Key West'),zone('GA',(31.65,31.78),(-83.32,-83.2),label='Fitzgerald')],
        'Myiopsitta monachus':[zone('FL',label='Florida'),PHOENIX,HOUSTON,zone('TX',(32.5,33.2),(-97.7,-96.4),label='Dallas–Fort Worth'),zone('TX',(30.,30.6),(-98.,-97.3),label='Austin'),zone('TX',(29.2,29.8),(-98.9,-98.1),label='San Antonio'),zone('LA',(29.8,30.15),(-90.35,-89.8),label='New Orleans'),zone('IL',(41.5,42.2),(-88.,-87.5),label='Chicago'),zone('IN',(41.5,41.8),(-87.6,-87.1),label='Chicago region'),zone('NY',(40.45,41.),(-74.3,-73.6),label='New York City'),zone('CT',(40.95,41.3),(-73.75,-73.1),label='Southwest Connecticut')],
        'Lonchura punctulata':[zone('CA',(32.5,35.),(-120.8,-116.9),label='Southern California coast'),zone('CA',(35.,38.),(-122.6,-120.3),label='Central California coast'),HOUSTON,MIAMI,zone('FL',(30.2,30.7),(-87.6,-86.8),label='Pensacola')],
        'Spilopelia chinensis':[LA,BAKERSFIELD,zone('CA',(36.6,36.9),(-119.95,-119.55),label='Fresno')],
        'Streptopelia chinensis':[LA,BAKERSFIELD,zone('CA',(36.6,36.9),(-119.95,-119.55),label='Fresno')],
        'Porphyrio poliocephalus':[zone('FL',label='Established Florida population')],
        'Pycnonotus cafer':[HOUSTON],
        'Passer montanus':[zone('MO',(37.5,40.6),(-92.,-89.),label='Eastern Missouri'),zone('IL',(37.5,42.5),(-91.6,-89.),label='Western Illinois'),zone('IA',(40.3,42.6),(-92.,-90.),label='Eastern Iowa')],
        'Carduelis carduelis':[zone('IL',(41.8,42.5),(-89.,-87.5),label='Northeast Illinois established 2024 population'),zone('WI',(42.5,43.5),(-89.,-87.5),label='Southeast Wisconsin established 2024 population')],
        'Tetraogallus himalayensis':[zone('NV',(40.,41.),(-115.8,-114.9),label='Ruby Mountains')],
        'Gymnogyps californianus':[zone('CA',(33.5,37.5),(-122.5,-117.),label='Southern/Central California wild-hatching populations'),zone('AZ',(35.,37.),(-113.8,-110.8),label='Northern Arizona'),zone('UT',(37.,38.),(-113.8,-110.8),label='Southern Utah')],
        # ABA explicitly lists all three reintroduced crane populations as
        # countable, in addition to the wild migratory Aransas population.
        'Grus americana':None,
        'Falco femoralis':[zone('TX',(25.8,29.5),(-98.5,-96.),label='Coastal Texas wild-hatching population')],
        'Anser anser':[],  # Domestic forms cannot be separated in this archive.
        'Cygnus cygnus':[zone('WA',(48.4,48.65),(-122.55,-122.25),'2024-02-14','2024-03-17','WBRC WHOS-2024-1, Samish Flats')],
        # The San Diego flamingos and Los Angeles caracara have unresolved
        # provenance. These areas cannot gain credit merely through membership.
        'Phoenicopterus ruber':[zone('FL',label='Publicly reviewed native/vagrant Florida birds')],
        'Daptrius chimachima':[zone('FL',label='2024 Florida natural vagrant'),zone('TX',label='2024 Texas juvenile vagrant')],
    }
    if name in {'Columba livia',*previous.AREA_WIDE}:
        return None
    if name in rules:
        return rules[name]
    return [] if name in previous.INTRODUCED_TAXA else None


def checklist():
    with zipfile.ZipFile(DATA/'sources/ABA_Checklist-8.16.csv.zip') as archive:
        rows=csv.reader(io.StringIO(archive.read('ABA_Checklist-8.16.csv').decode('utf-8-sig')))
        result={}
        for r in rows:
            if len(r)!=6 or r[5] not in set('123456'):
                continue
            name=re.sub(r'\s*\([^)]*\)','',r[3]).strip()
            if name in result:
                raise ValueError('duplicate 2024 primary species')
            result[name]={'name':r[1].strip(),'scientific_name':name,'code':r[5]}
    # Official CSV and PDF contain 1,151 coded rows despite their 1,152 header.
    # Do not invent a species to reconcile a publisher's arithmetic discrepancy.
    if len(result)!=1151:
        raise ValueError('unexpected official 8.16 contents; inspect source')
    return result


def source_decision(tax):
    if tax['aba_eligible']=='0':
        return 'excluded',tax['review_basis'],tax['reference']
    if tax['eod_presence_eligible']!='1':
        return 'unresolved',tax['review_basis'],tax['reference']
    return 'supported',tax['review_basis'],tax['reference']


def record_decision(row, raw, tax):
    default=source_decision(tax)
    if default[0]=='excluded':
        return default
    result=previous.record_decision(row,raw,tax,('supported','publisher_reviewed_occurrence_proxy',previous.REVIEW))
    # A zoo is a checklist locality, not an individual captive flag. Wild native
    # birds and established feral populations can be reported on zoo grounds.
    if result[1]=='locality_requires_wild_status_review' and raw:
        locality=raw.get('locality',raw.get(previous.DWC+'locality',''))
        if not re.search(r'\baviary\b|\bcaptive\b|\bescape[ed]?\b|rehabilitation',locality,re.I):
            result=('supported','publisher_reviewed_occurrence_proxy_at_zoo_locality',previous.REVIEW)
    if result[0]!='supported':
        return result
    state_name=raw.get('stateProvince',raw.get(previous.DWC+'stateProvince'))
    state='US-'+next((s for s,n in STATE_NAMES.items() if n==state_name), 'unknown')
    if state != row['state']:
        return 'unresolved','publisher_state_mismatch',row['record']
    if default[0]!='supported' or not region_supported(tax,state,row['latitude'],row['longitude'],row['date']):
        return 'unresolved','population_or_provenance_not_resolved_here',tax['reference']
    return result[0],tax['review_basis']+'; '+result[1],tax['reference']


def freeze():
    if (DATA/'policy.json').exists():
        raise ValueError('frozen policy exists; use a new version for changes')
    approved=checklist()
    old=list(csv.DictReader((ORIGINAL/'taxonomy.csv').open()))
    rows=[]
    for tax in old:
        name=tax['source_name'];canonical=ALIASES.get(name,name);entry=approved.get(canonical)
        item={**tax,'species_id':'aba2024:'+canonical.replace(' ','_'),'policy_id':POLICY,'eligible_regions':''}
        if not entry:
            item.update(aba_eligible='0',eod_presence_eligible='0',review_basis='not_on_2024_primary_approved_set',reference=CHECKLIST)
        elif entry['code']=='6':
            item.update(aba_eligible='0',eod_presence_eligible='0',common_name=entry['name'],review_basis='historical_extinct_or_extirpated_no_2024_credit',reference=ANNUAL+'; '+previous.RULES)
        else:
            regions=population_regions(name)
            reason='2024_primary_member; publisher_public_review_proxy; no_extra_rarity_code_screen'
            if regions is not None:
                item['eligible_regions']=json.dumps(regions,separators=(',',':'))
                reason='2024_primary_member; documented_population_or_event_envelope; publisher_review_proxy'
            if regions==[]:
                reason='2024_primary_member; mainland_population_or_individual_provenance_unresolved'
            item.update(aba_eligible='1',eod_presence_eligible='review' if regions==[] else '1',common_name=entry['name'],
                review_basis=reason,reference=CHECKLIST+'; '+previous.REVIEW+'; '+previous.INTRODUCED+'; '+REPATRIATED+'; '+WBRC)
        item['aba_2024_scientific_name']=canonical
        item['aba_2024_code']=entry['code'] if entry else ''
        item['taxonomic_mapping']='explicit_synonym_or_2024_parent' if name in ALIASES else 'exact_2024_scientific_name'
        rows.append(item)
    write_csv(DATA/'taxonomy.csv',rows,list(rows[0]))
    by_name={r['source_name']:r for r in rows}
    evidence=[]
    for row in csv.DictReader((ROOT/'data/lower48-countability-2024/evidence-decisions.csv').open()):
        tax=by_name[row['source_name']]
        item={**row,'species_id':tax['species_id']}
        decision=record_decision({**item,'latitude':float(item['latitude']),'longitude':float(item['longitude'])},
            read(DATA/'sources/selected-verbatim'/(row['representative_gbifid']+'.json')),tax)
        item.update(zip(['decision','reason','reference'],decision));evidence.append(item)
    write_csv(DATA/'evidence-decisions.csv',evidence,list(evidence[0]))
    former=list(csv.DictReader((ROOT/'data/lower48-countability-2024/species-decisions.csv').open()))
    flagged=[]
    for oldrow in former:
        if oldrow.get('credited_in_baseline')!='True' or oldrow['decision']!='unresolved':
            continue
        tax=by_name[oldrow['source_name']];ev=[r for r in evidence if r['source_name']==oldrow['source_name']]
        decision='supported' if any(r['decision']=='supported' for r in ev) else 'unresolved'
        flagged.append(dict(name=oldrow['name'],source_name=oldrow['source_name'],species_id=tax['species_id'],
            previous='unresolved',selected_record_decision=decision,
            supported_records=sum(r['decision']=='supported' for r in ev),
            unresolved_records=sum(r['decision']=='unresolved' for r in ev),
            reason='; '.join(sorted({r['reason'] for r in ev})),reference=tax['reference']))
    if len(flagged)!=23:
        raise ValueError('original 23 cases not completely accounted for')
    write_csv(DATA/'first-23-decisions.csv',flagged,list(flagged[0]))
    ledger=[]
    for tax in rows:
        status,reason,reference=source_decision(tax)
        ledger.append({**tax,'default_decision':status,'reason':reason,'decision':status,
                       'reference':reference,'source_groups':tax['presence_rows']})
    write_csv(DATA/'species-decisions.csv',ledger,list(ledger[0]))
    policy={'id':POLICY,'year':2024,'taxonomic_standard':'ABA 8.16 official 2024 primary checklist, as cross-checked with 2024 annual committee report; whole-year retrospective scoring units.',
        'official_header_species':1152,'actual_coded_primary_rows':len(approved),
        'publisher_count_discrepancy':'Official CSV and PDF contain 1,151 coded species rows despite a 1,152 header. No species invented; absent concepts receive no credit.',
        'full_source_concepts':len(rows),'source_dispositions':dict(Counter(r['default_decision'] for r in ledger)),
        'first_23':dict(Counter(r['selected_record_decision'] for r in flagged)),
        'rules':['Score primary-approved 2024 taxa only; collapse later daughter taxa to one 2024 parent species.',
            'Rarity code never by itself excludes an approved species. Public eBird review is the same explicit quality/wild-occurrence proxy for native rare and common birds.',
            'Introduced taxa require documented countable mainland populations. Explicit geographic envelopes operationalize those populations with the publisher-review proxy; they are not official ABA boundaries or proof of individual provenance.',
            'Reintroduced populations qualify once wild hatching is documented under ABA Rule 2.B(vi); no individual wild-born requirement is invented.',
            'Graylag domestic provenance, unestablished populations, ambiguous records, historical extinct/extirpated exotics, and non-approved/provisional taxa receive zero credit.',
            'Zoo locality alone is not a captive flag. Explicit aviary, captive, escape or rehabilitation locality remains held in selected-record validation.',
            'All source observations and exact coordinate strings remain unchanged in original Parquet; scoring policy changes only eligibility.',
            'Occurrence/access/encounter timing and permissions remain modeled assumptions. This is not ABA certification.'],
        'certification':'ABA-list-aligned modeled score using an explicit publisher-review and population-envelope proxy; no individual wildness certification.',
        'population_envelopes':{r['source_name']:json.loads(r['eligible_regions']) for r in rows if r['eligible_regions']},
        'aliases':ALIASES,'baseline_route_sha256':read(DATA/'source-retrieval.json')['source_route_sha256'],
        'decision_files':{n:sha256(DATA/n) for n in ['taxonomy.csv','evidence-decisions.csv','species-decisions.csv','first-23-decisions.csv']},
        'sources':{p.name:sha256(p) for p in sorted((DATA/'sources').iterdir()) if p.is_file()},
        'source_retrieval_sha256':sha256(DATA/'source-retrieval.json')}
    dump(DATA/'policy.json',policy)
    print(json.dumps({k:policy[k] for k in ['id','full_source_concepts','source_dispositions','first_23','actual_coded_primary_rows']}),flush=True)


def prepare():
    policy=read(DATA/'policy.json')
    for name,digest in policy['decision_files'].items():
        if sha256(DATA/name)!=digest:
            raise ValueError('frozen decisions changed')
    dataset=DATA/'dataset';dataset.mkdir(exist_ok=True)
    manifest=deepcopy(read(ORIGINAL/'dataset/manifest.json'))
    manifest['taxonomy_policy']=POLICY
    manifest['countability']={'policy':POLICY,'policy_sha256':sha256(DATA/'policy.json'),
        'score':'2024 primary-approved species, explicit population rules and publisher-review proxy.'}
    for name in ['land.csv','scope.geojson','scope-points.csv']:
        if not (dataset/name).exists():os.link(ORIGINAL/'dataset'/name,dataset/name)
    for source,target in [('taxonomy.csv','taxonomy.csv'),('policy.json','countability-policy.json')]:
        (dataset/target).write_bytes((DATA/source).read_bytes())
    decisions=list(csv.DictReader((DATA/'evidence-decisions.csv').open()))
    projected=[{**{k:r[k] for k in ['representative_gbifid','source_name','source_taxon_id','date','decision','reason','reference']},
        'latitude':r['latitude_text'],'longitude':r['longitude_text']} for r in decisions]
    write_csv(dataset/'countability-evidence.csv',projected,list(projected[0]))
    manifest['inputs']['taxonomy']={'path':str(dataset/'taxonomy.csv'),'sha256':sha256(dataset/'taxonomy.csv')}
    manifest['limitations']+=policy['rules']
    manifest['outputs']={n:sha256(dataset/n) for n in ['taxonomy.csv','land.csv','scope.geojson','scope-points.csv','countability-evidence.csv','countability-policy.json']}
    dump(dataset/'manifest.json',manifest)
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='3GB'");con.execute('SET threads=2');con.execute("SET max_temp_directory_size='8GB'")
        con.execute('SET preserve_insertion_order=false')
        _,obs=attach_dataset(con,dataset,manifest)
        dispositions=dict(con.execute('SELECT disposition,count(*) FROM eod_classified GROUP BY 1').fetchall())
        species,locations,groups,observations=con.execute(f'SELECT count(DISTINCT species_id),count(DISTINCT location_id),count(*),sum(source_rows) FROM {obs}').fetchone()
        con.execute("COPY (SELECT v_scientificname AS source_name,disposition,count(*) AS source_groups,sum(source_record_count) AS represented_observations FROM eod_classified GROUP BY 1,2 ORDER BY 1,2) TO ? (HEADER)",[str(DATA/'full-source-dispositions.csv')])
    manifest['statistics']={**manifest['statistics'],'dispositions':dispositions,'annual_recorded_species_ceiling':species,
        'locations':locations,'eod_daily_presence_groups':groups,'represented_observations':observations}
    dump(dataset/'manifest.json',manifest)
    print(json.dumps({'stage':'dataset-ready','species_pool':species,'exact_locations':locations,'groups':groups,'observations':observations}),flush=True)


if __name__=='__main__':
    import sys
    {'freeze':freeze,'prepare':prepare}[sys.argv[1]]()
