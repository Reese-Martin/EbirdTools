"""Build a lossless terrestrial catalog with exact dated charter alternatives."""
from bigyear_planner.joint_marine import build_joint
from bigyear_planner.util import ROOT


if __name__=='__main__':
    model=build_joint(ROOT/'data/lower48-aba2024-provisional-v3/model/model.json',
        ROOT/'data/marine-2024-charter-v1/charter-candidates.json',
        ROOT/'data/marine-2024-charter-v1/boat-access-score-holds.json',
        ROOT/'data/lower48-joint-mobility-2024-v1/model')
    try:
        print({'model':str(model.path),'locations':model.meta['locations'],
            'opportunities':len(model.records),'species':len(model.meta['species'])})
    finally:model.close()
