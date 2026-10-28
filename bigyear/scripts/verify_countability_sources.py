"""Post-search check of every selected publisher record under the same policy."""

from collections import Counter
import csv
import json
import os

from bigyear_planner.util import dump,read,sha256
from audit_countability import DATA,OUT,occurrences,record_decision
from benchmark_countability import TAG


def main():
    route_path=OUT/TAG/'route.json';route=read(route_path)
    label=os.environ.get('BIGYEAR_COUNTABILITY_SOURCE_LABEL','rerun')
    retrieval_path=DATA/(label+'-source-retrieval.json')
    retrieval=read(retrieval_path)
    if retrieval['source_route_sha256']!=sha256(route_path):
        raise ValueError('post-search source snapshot belongs to another route')
    taxa={r['species_id']:r for r in csv.DictReader((DATA/'taxonomy.csv').open())}
    defaults={r['species_id']:(r['default_decision'],r['reason'],r['reference'])
              for r in csv.DictReader((DATA/'species-decisions.csv').open())}
    checked=[];failures=[]
    for row in occurrences(route):
        key=row['record'].removeprefix('GBIF:');snapshot=retrieval['records'].get(key,{})
        raw=None
        if snapshot.get('retrieved'):
            path=DATA/'sources'/(label+'-verbatim')/(key+'.json')
            if sha256(path)!=snapshot['sha256']:
                raise ValueError('post-search publisher snapshot changed')
            raw=read(path)
        status,reason,reference=record_decision(row,raw,taxa[row['species_id']],defaults[row['species_id']])
        item={**row,'decision':status,'reason':reason,'reference':reference}
        checked.append(item)
        if status!='supported':
            failures.append(item)
    result={'valid':not failures,'run':TAG,'route_sha256':sha256(route_path),
         'policy_sha256':sha256(DATA/'policy.json'),
         'retrieval_sha256':sha256(retrieval_path),
         'records_checked':len(checked),'decisions':dict(Counter(v['decision'] for v in checked)),
         'source_retrieval_complete':all(v.get('retrieved') for v in retrieval['records'].values()),
         'unresolved_or_excluded_credited':failures,
         'scope':'Every scored record checked against retrieved publisher metadata and the same countability rules; individual wild-status proxy remains explicit.'}
    dump(OUT/TAG/'countability-source-validation.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='unresolved_or_excluded_credited'}),flush=True)
    if failures:
        print(json.dumps({'failures':failures}),flush=True)
        raise ValueError('selected evidence needs countability refinement before publication')


if __name__=='__main__':
    main()
