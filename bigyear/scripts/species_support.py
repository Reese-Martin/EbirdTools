"""Small exact annual support summary from every mapped national opportunity."""

from datetime import date, timedelta
import numpy as np

from bigyear_planner.compact_model import load
from bigyear_planner.util import ROOT, dump, sha256


def main():
    path = ROOT/'data/lower48-eod-2024/model/model.json'
    model = load(path)
    try:
        year = model.meta['config']['year']
        days = (date(year+1,1,1)-date(year,1,1)).days
        boundaries = np.searchsorted(model.records['day'], np.arange(days+1))
        daily = np.zeros((days,len(model.meta['species'])), dtype=np.int64)
        for day in range(days):
            lo,hi = map(int,boundaries[day:day+2])
            if lo==hi:
                continue
            begin = int(model.records[lo]['offset'])
            end = int(model.records[hi]['offset']) if hi<len(model.records) else len(model.species)
            daily[day] = np.bincount(model.species[begin:end], minlength=daily.shape[1])
        assert int(daily.sum())==model.meta['species_entries']
        rows = []
        for i,species in enumerate(model.meta['species']):
            observed = np.flatnonzero(daily[:,i])
            rows.append({**species,'reported_days':len(observed), 'location_date_supports':int(daily[:,i].sum()),
                         'first_date':str(date(year,1,1)+timedelta(days=int(observed[0]))),
                         'last_date':str(date(year,1,1)+timedelta(days=int(observed[-1])))})
        dump(ROOT/'runs/lower48-eod-2024/species-support.json',
             {'model_sha256':sha256(path),'species':rows,'supports_checked':int(daily.sum()),
              'interpretation':'Reported days and exact location/date supports across the complete eligible national cohort. These measure observation support, not abundance, biological rarity or migration speed.'})
        print({'species':len(rows),'supports_checked':int(daily.sum())})
    finally:
        model.close()


if __name__=='__main__':
    main()
