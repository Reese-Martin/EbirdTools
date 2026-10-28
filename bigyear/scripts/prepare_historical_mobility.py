"""Actual 2023 marine dates with the same modeled access as the 2024 scenario."""
import argparse
import os
import shutil

from bigyear_planner.compact_model import load
from bigyear_planner.island_access import build_catalog
from bigyear_planner.island_validate import audit_catalog
from bigyear_planner.joint_marine import build_joint
from bigyear_planner.util import ROOT, dump, read, sha256
import prepare_marine_charter as offshore
import build_marine_candidates as boats

DATA = ROOT/'data/lower48-eod-2023'
MARINE = DATA/'marine'
REFERENCE = ROOT/'data/marine-2024-charter-v1'


def main(stage):
    MARINE.mkdir(exist_ok=True)
    offshore.DATA = boats.DATA = MARINE
    offshore.PARENT = DATA/'dataset'
    # The 2024 construction reference chooses which options to construct;
    # it never supplies 2023 presence or dates. This matches the existing
    # partial marine construction, rather than claiming national completeness.
    for name in ['ports.json','boat-access-score-holds.json']:
        if not (MARINE/name).exists():
            shutil.copyfile(REFERENCE/name,MARINE/name)
    for name in ['sources/countries-ne-5.1.2.geojson']:
        dest = MARINE/name
        dest.parent.mkdir(exist_ok=True)
        if not dest.exists():
            os.link(REFERENCE/name,dest)
    (MARINE/'coasts').mkdir(exist_ok=True)
    for original in (REFERENCE/'coasts').iterdir():
        target = MARINE/'coasts'/original.name
        if not target.exists():
            os.link(original,target)
    if stage in ['offshore','all'] and not (MARINE/'manifest.json').exists():
        offshore.main()
    if stage in ['boats','all'] and not (MARINE/'charter-candidates.json').exists():
        boats.main()
    if stage in ['joint','all']:
        parent = load(DATA/'model/model.json')
        try:
            catalog = build_catalog(parent,MARINE/'charter-candidates.json',MARINE/'islands')
            errors = []
            audit_catalog(read(catalog),lambda ok,msg: None if ok else errors.append(msg))
            if errors:
                raise ValueError('island catalog audit failed: '+str(errors[:5]))
            # Explicit marine_data is needed because this catalog lives in a
            # subfolder; the raw offshore supplement is in its parent.
            content = read(catalog)
            content['marine_data'] = str(MARINE.resolve())
            dump(catalog,content)
            model = build_joint(parent.path,catalog,MARINE/'boat-access-score-holds.json',DATA/'joint-model')
            try:
                dump(DATA/'mobility-preparation.json',{'model':str(model.path),
                    'model_sha256':sha256(model.path),'locations':model.meta['locations'],
                    'opportunities':model.meta['opportunities'],'species':len(model.meta['species']),
                    'charters':len(content['trips']),'island_options':content['island_access']['added_options'],
                    'physical_roads_reused_from_2024':True,'observation_dates_retimed':False})
                print(read(DATA/'mobility-preparation.json'),flush=True)
            finally:
                model.close()
        finally:
            parent.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['offshore','boats','joint','all'])
    main(parser.parse_args().stage)
