"""Trace previously unresolved exact-point access with a longer car query budget."""
import json
import time

from bigyear_planner.compact_model import load
from bigyear_planner.routing_resolution import ResolvedJointRoads
from bigyear_planner.joint_seed import seed_verified_joint
from bigyear_planner.mobility import FlightModel
from bigyear_planner.targeted_exploration import TargetedExplorer
from bigyear_planner.util import dump,read
from benchmark_joint_mobility import DATA,NATIONAL,HOLDS,OUT
from evaluate_lower48 import guarded,router


TAG='routing-query-diagnostic-v3'
BASE=OUT/'targeted-regions-2024-v3'


def main():
    out=OUT/TAG;out.mkdir(exist_ok=True);work=DATA/(TAG+'-cache')
    base=load(DATA/'model/model.json');records=[]
    try:
        with guarded(TAG),router(TAG,120):
            model=FlightModel(base,NATIONAL/'flights-2024')
            roads=ResolvedJointRoads(model,NATIONAL/'roads/release.json',work,query_timeout_seconds=135,
                access_holds_path=HOLDS,source_access_holds_path=DATA/'source-visit-holds.json')
            try:
                if not roads.access_migration:roads.migrate_cache(DATA/(BASE.name+'-cache'))
                current,binding=seed_verified_joint(model,roads,BASE);dump(out/'incumbent-binding.json',binding)
                targets=[v for v in read(BASE/'gap-analysis.json')['targeted_species'] if v['outcome'].startswith('Routing unresolved')]
                explorer=TargetedExplorer(model,None,out,targets)
                original=roads.request;label=''
                with (out/'requests.jsonl').open('w') as log:
                    def request(coords,match=False,radius=1):
                        started=time.monotonic();entry={'label':label,'coordinates':coords,'radius':radius,'matching_only':match}
                        try:
                            value=original(coords,match=match,radius=radius)
                            entry['successful']=value is not None
                            return value
                        except Exception as error:
                            entry.update(successful=False,error=str(error));raise
                        finally:
                            entry['seconds']=time.monotonic()-started;log.write(json.dumps(entry)+'\n');log.flush()
                    roads.request=request
                    for target,s in zip(targets,explorer.target_indices):
                        head=explorer.heads(current,s)[0];idx=int(base.records[head]['location'])
                        candidates=[(idx,'observation')]+[(model.meta['locations']+int(i),model.airports[i]['code']) for i in roads.nearest_airports(idx)[:3]]
                        for location,kind in candidates:
                            label=target['species']+' / '+kind
                            entry={'target':target['species'],'head':head,'location':location,'kind':kind,
                                'coordinate':list(map(float,model.coords[location]))}
                            try:entry['anchor']=list(roads.access(location));entry['resolved']=True
                            except Exception as error:entry.update(resolved=False,error=str(error))
                            records.append(entry);dump(out/'access-diagnostic.json',records)
                            print(json.dumps(entry),flush=True)
            finally:roads.close()
    finally:base.close()


if __name__=='__main__':main()
