"""Prepare the relaxed daily activity policy over the unchanged complete model."""

import argparse
from pathlib import Path

from bigyear_planner.daily_policy import prepare_mixed_activity
from bigyear_planner.util import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path,
                        default=ROOT/'data/calendar-strategy-study-2024-v1/island-model/model.json')
    parser.add_argument('--output', type=Path, default=ROOT/'data/shared-12h-2024-v1/model')
    parser.add_argument('--driving-minutes', type=int, choices=(480,720), default=720)
    args = parser.parse_args()
    print(prepare_mixed_activity(args.source, args.output, args.driving_minutes))


if __name__ == '__main__':
    main()
