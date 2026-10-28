"""Postcheck every selected publisher occurrence under the aligned policy."""

from collections import Counter
import csv
import json
import time

from bigyear_planner.util import dump,read,sha256
import audit_countability as retrieval_tools
from align_aba2024 import DATA,POLICY,record_decision
from benchmark_aba2024 import OUT,TAG


def verify():
    folder=OUT/TAG;route_path=folder/'route.json'
    if not read(folder/'validation.json')['valid']:
        raise ValueError('independent source/calendar/road validation failed')
    route=read(route_path)
    if route['taxonomy_policy']!=POLICY:
        raise ValueError('route policy differs')
    retrieval_tools.DATA=DATA
    retrieval_tools.fetch(route_path,'aligned-run')
    snapshot_path=DATA/'aligned-run-source-retrieval.json';snapshot=read(snapshot_path)
    taxa={(r['source_name'],r['source_taxon_id']):r for r in csv.DictReader((DATA/'taxonomy.csv').open())}
    name_to_tax={r['source_name']:r for r in taxa.values()}
    decisions=[]
    for row in retrieval_tools.occurrences(route):
        key=row['record'].removeprefix('GBIF:');item=snapshot['records'][key];raw=None
        if item.get('retrieved'):
            path=DATA/'sources/aligned-run-verbatim'/(key+'.json')
            if sha256(path)!=item['sha256']:
                raise ValueError('frozen publisher evidence changed')
            raw=read(path)
        tax=name_to_tax[row['source_name']]
        if tax['species_id']!=row['species_id']:
            raise ValueError('publisher source concept maps to different 2024 scoring unit')
        status,reason,reference=record_decision(row,raw,tax)
        decisions.append({**row,'decision':status,'reason':reason,'reference':reference})
    failures=[r for r in decisions if r['decision']!='supported']
    result={'valid':not failures,'route_sha256':sha256(route_path),'policy_sha256':sha256(DATA/'policy.json'),
        'retrieval_sha256':sha256(snapshot_path),'records_checked':len(decisions),
        'decisions':dict(Counter(r['decision'] for r in decisions)),
        'unresolved_or_excluded_credited':failures,'individual_wild_status_certified':False,
        'scope':'Every selected publisher name/concept/date/coordinate/state plus 2024 eligibility and population envelope; public review remains an explicit wild-occurrence proxy.'}
    dump(folder/'countability-source-validation.json',result)
    summary=read(OUT/'aba2024-aligned-benchmark-result.json')
    summary['publisher_source_postcheck']='passed' if not failures else 'needs review'
    dump(OUT/'aba2024-aligned-benchmark-result.json',summary)
    if failures:
        dump(OUT/'run-status.json',{'stage':'needs-attention','run':TAG,
            'message':'Search completed and travel/calendar audited, but selected publisher evidence needs review before accepting the score.'})
        raise ValueError(f'{len(failures)} selected records need review; see countability-source-validation.json')
    dump(OUT/'run-status.json',{'stage':'completed','run':TAG,'best_run':TAG,'best_species':route['species_total'],
        'message':f"{route['species_total']} ABA 2024 aligned modeled species. Independent travel/calendar audit and every selected publisher record passed; occurrence proxy remains explicit."})
    print(json.dumps({k:v for k,v in result.items() if k!='unresolved_or_excluded_credited'}),flush=True)
    from report_aba2024 import main as report
    report()


if __name__=='__main__':
    import sys
    if '--wait' in sys.argv:
        deadline=time.monotonic()+4*3600
        while True:
            status=read(OUT/'run-status.json')
            if status.get('run')==TAG:
                if status['stage']=='failed':
                    raise RuntimeError('benchmark failed; postcheck cancelled')
                if status['stage']=='completed' and (OUT/'aba2024-aligned-benchmark-result.json').exists():
                    break
            if time.monotonic()>deadline:
                raise TimeoutError('benchmark did not complete within four active hours')
            time.sleep(10)
    verify()
