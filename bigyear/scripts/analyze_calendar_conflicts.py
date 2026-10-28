import argparse
from pathlib import Path

from bigyear_planner.calendar_conflicts import analyze
from bigyear_planner.util import ROOT

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args()
    analyze(ROOT/'data/lower48-joint-mobility-2024-v1/model/model.json',
        ROOT/'data/lower48-eod-2024/roads/release.json',args.output)
