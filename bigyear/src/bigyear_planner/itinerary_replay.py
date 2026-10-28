"""Identity-bound dated itinerary replay against the complete exact-point model.

An EOD match proves date/location/species support, not that a particular person
saw a bird. Observer identity must come from a supplied, verified input manifest.
Unmatched points remain unmatched; they are never snapped to a nearby sighting.
"""
import csv
from datetime import date
import math
from pathlib import Path
import re

import numpy as np

from .config import Config
from .util import read,sha256,write_csv,dump

FIELDS=['date','latitude','longitude','state','scientific_name','checklist_id','observer_id',
        'start_time','duration_minutes','reviewed']


def rows(path,observer_id):
    with Path(path).open(newline='',encoding='utf-8-sig') as stream:
        first=stream.readline();stream.seek(0)
        reader=csv.DictReader(stream,delimiter='\t' if '\t' in first else ',')
        for line,row in enumerate(reader,2):
            ebd='OBSERVATION DATE' in row
            observer=row.get('OBSERVER ID' if ebd else 'observer_id','')
            if observer_id not in re.split(r'[,;|\s]+',observer.strip()):continue
            if ebd:
                yield {'date':row['OBSERVATION DATE'],'latitude':row['LATITUDE'],'longitude':row['LONGITUDE'],
                  'state':row['STATE CODE'],'scientific_name':row['SCIENTIFIC NAME'],
                  'checklist_id':row.get('SAMPLING EVENT IDENTIFIER',''),'observer_id':observer,
                  'start_time':row.get('TIME OBSERVATIONS STARTED',''),
                  'duration_minutes':row.get('DURATION MINUTES',''),'reviewed':row.get('APPROVED',''),
                  'line':line}
            else:yield {**row,'line':line}


def source_match(model,visits):
    """One bounded SQLite scan, with exact numeric equality and state identity."""
    db=model.locations.db
    db.execute('DROP TABLE IF EXISTS temp.replay_points')
    db.execute('CREATE TEMP TABLE replay_points (state TEXT,lat REAL,lon REAL,PRIMARY KEY(state,lat,lon)) WITHOUT ROWID')
    db.executemany('INSERT OR IGNORE INTO replay_points VALUES (?,?,?)',[(r['state'],float(r['latitude']),float(r['longitude'])) for r in visits])
    matches={}
    for idx,state,lat,lon in db.execute("""SELECT l.idx,p.state,p.lat,p.lon FROM locations l
        JOIN replay_points p ON json_extract(l.metadata,'$.state')=p.state
        AND json_extract(l.metadata,'$.lat')=p.lat AND json_extract(l.metadata,'$.lon')=p.lon"""):
        matches.setdefault((state,lat,lon),[]).append(int(idx))
    db.execute('DROP TABLE replay_points')
    return matches


def replay(model,manifest_path,output,evaluator=None):
    manifest=read(manifest_path)
    if not manifest.get('identity_reference') or not manifest.get('observer_id'):
        raise ValueError('verified observer ID and identity reference are required; a name alone is insufficient')
    path=Path(manifest['itinerary_path']).expanduser().resolve()
    if sha256(path)!=manifest.get('itinerary_sha256'):raise ValueError('itinerary input changed')
    cfg=Config(**model.meta['config'])
    source=list(rows(path,manifest['observer_id']))
    if not source:raise ValueError('no rows match the verified observer ID')
    valid=[]
    for r in source:
        d=date.fromisoformat(r['date']);lat=float(r['latitude']);lon=float(r['longitude'])
        if not all(math.isfinite(v) for v in [lat,lon]) or not -90<=lat<=90 or not -180<=lon<=180:
            raise ValueError('invalid exact coordinates at input line '+str(r['line']))
        if not cfg.first_date<=d<=cfg.last_date:continue
        r['day']=(d-cfg.first_date).days;valid.append(r)
    if not valid:raise ValueError('no dated observations in the configured calendar')
    original=getattr(model,'original',model)
    matches=source_match(original,valid)
    # Current taxonomy supplies explicit historical parent/synonym mapping.
    taxonomy={r['source_name']:r['species_id'] for r in csv.DictReader((Path(model.meta['dataset'])/'taxonomy.csv').open())}
    species={r['id']:i for i,r in enumerate(model.meta['species'])}
    report=[];selected=set();seen=set()
    for r in valid:
        mapped=taxonomy.get(r['scientific_name']);sid=species.get(mapped)
        locations=matches.get((r['state'],float(r['latitude']),float(r['longitude'])),[])
        lo,hi=np.searchsorted(original.records['day'],[r['day'],r['day']+1])
        candidates=np.flatnonzero(np.isin(original.records['location'][lo:hi],locations))+lo
        supported=[]
        for i in candidates:
            o=original.opportunity(int(i))
            if sid is not None and sid in o['species']:supported.append(int(i))
        reviewed=r.get('reviewed','') in ['1','true','True']
        status=('unreviewed_input' if not reviewed else 'taxon_not_scored' if sid is None else
                'exact_date_location_species_supported' if supported else
                'no_exact_model_location' if not locations else 'no_exact_dated_species_support')
        if supported and reviewed:
            selected.add(supported[0]);seen.add(sid)
        report.append({**{k:r.get(k,'') for k in FIELDS},'status':status,
           'model_opportunity':supported[0] if supported and reviewed else '',
           'scoring_species_id':mapped or ''})
    ids=sorted(selected)
    score=evaluator.evaluate(ids,timeout=60) if evaluator is not None and ids else None
    # Within-day input chronology must be checked separately: canonical order
    # in the model is not an assertion of the person's real visit order.
    summary={'subject':manifest.get('subject'),'observer_id':manifest['observer_id'],
      'identity_reference':manifest['identity_reference'],'itinerary_sha256':sha256(path),
      'model_sha256':sha256(model.path),'input_rows':len(source),'in_calendar_rows':len(valid),
      'matched_species':len(seen),'matched_opportunities':len(ids),'selected':ids,
      'status_counts':{s:sum(r['status']==s for r in report) for s in sorted({r['status'] for r in report})},
      'modeled_calendar_replay':score,
      'exact_recordholder_replay_complete':False,
      'limitations':['Exact numeric coordinates and dates only; no nearby-point or date substitution.',
        'Model evaluation uses canonical intra-day order. It does not certify the supplied timed chronology.',
        'No marine/boat access, permissions or time zones inferred from missing fields.',
        'EOD absence is a data/policy coverage gap, not proof the person did not see the bird.']}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    write_csv(output/'replay-observations.csv',report,list(report[0]))
    dump(output/'replay.json',summary)
    return summary


def template(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    write_csv(output/'dated-itinerary.csv',[],FIELDS)
    dump(output/'manifest.template.json',{'subject':'Ezekiel Dobson, Lower 48, 2024',
      'observer_id':None,'identity_reference':None,'itinerary_path':str(output/'dated-itinerary.csv'),
      'itinerary_sha256':None,'status':'pending_input',
      'required':'Verified observer identity and dated EBD/personal-export rows; no substitute itinerary.'})
    dump(output/'replay-status.json',{'status':'pending_input','recordholder':'Ezekiel Dobson',
       'working_target':759,'replay_performed':False,
       'missing':['Verified observer ID','Dated itinerary/checklists with exact coordinates',
                  'Departure/arrival times and marine/island trip evidence for strict timed replay']})
