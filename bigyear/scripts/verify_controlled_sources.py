"""Postcheck complete selected publisher records without changing frozen policy."""
import argparse
from collections import Counter
import csv
import os
from pathlib import Path
import time

from bigyear_planner.util import ROOT,read,dump,sha256,write_csv
import audit_countability as source
from align_aba2024 import record_decision


def verify(folder,label):
    route=read(folder/'route.json');data=Path(read(folder/'problem.json')['dataset']).parent
    if not read(folder/'validation.json')['valid']:raise ValueError('route travel/calendar audit failed')
    target=data/'sources'/(label+'-verbatim');target.mkdir(exist_ok=True)
    # Reuse complete frozen raw records by ID; every payload is checked below.
    for row in source.occurrences(route):
        key=row['record'].removeprefix('GBIF:');destination=target/(key+'.json')
        if not destination.exists():
            for root in [data/'sources',ROOT/'data/lower48-aba2024-aligned/sources']:
                found=next((p for p in root.glob('*-verbatim/'+key+'.json')),None)
                if found is not None:os.link(found,destination);break
    source.DATA=data;source.fetch(folder/'route.json',label)
    retrieval=data/(label+'-source-retrieval.json');snapshot=read(retrieval)
    taxonomy={t['source_name']:t for t in csv.DictReader((data/'taxonomy.csv').open())}
    decide=record_decision
    if any('score_tier' in t for t in taxonomy.values()):
        from reconcile_2024_rules import record_decision as decide
    decisions=[]
    for row in source.occurrences(route):
        key=row['record'].removeprefix('GBIF:');item=snapshot['records'][key]
        raw=None
        if item.get('retrieved'):
            path=target/(key+'.json')
            if sha256(path)!=item['sha256']:raise ValueError('frozen raw occurrence changed')
            raw=read(path)
        tax=taxonomy[row['source_name']]
        if tax['species_id']!=row['species_id']:raise ValueError('2024 taxon mapping differs')
        status,reason,reference=decide(row,raw,tax)
        decisions.append({**row,'decision':status,'reason':reason,'reference':reference,
            'score_tier':tax.get('score_tier','primary')})
    failures=[r for r in decisions if r['decision']!='supported']
    result={'valid':not failures,'route_sha256':sha256(folder/'route.json'),
        'policy_sha256':sha256(data/'policy.json'),'retrieval_sha256':sha256(retrieval),
        'records_checked':len(decisions),'decisions':dict(Counter(r['decision'] for r in decisions)),
        'score_tiers':dict(Counter(r['score_tier'] for r in decisions)),
        'unresolved_or_excluded_credited':failures,'individual_wild_status_certified':False}
    dump(folder/'countability-source-validation.json',result)
    write_csv(folder/'source-decisions.csv',decisions,list(decisions[0]))
    if (folder/'result.json').exists():
        summary=read(folder/'result.json');summary['publisher_source_postcheck']='passed' if not failures else 'needs review';dump(folder/'result.json',summary)
    print({'folder':str(folder),'records':len(decisions),'failures':len(failures)},flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);parser.add_argument('--label',required=True);parser.add_argument('--wait',action='store_true')
    args=parser.parse_args()
    if args.wait:
        deadline=time.monotonic()+3600
        while not (args.folder/'validation.json').exists():
            if time.monotonic()>deadline:raise RuntimeError('run did not finish for source audit')
            time.sleep(5)
    result=verify(args.folder,args.label)
    if not result['valid']:raise ValueError('selected publisher occurrences remain held; see source decisions')
