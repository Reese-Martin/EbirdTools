"""Prepare input templates, or audit a supplied identity-bound dated itinerary."""
import argparse
from pathlib import Path

from bigyear_planner.compact_model import load
from bigyear_planner.itinerary_replay import replay,template

parser=argparse.ArgumentParser()
parser.add_argument('--template',type=Path)
parser.add_argument('--manifest',type=Path)
parser.add_argument('--model',type=Path)
parser.add_argument('--output',type=Path)
args=parser.parse_args()
if args.template:template(args.template)
else:
    if not all([args.manifest,args.model,args.output]):parser.error('--manifest, --model and --output are required')
    model=load(args.model)
    try:replay(model,args.manifest,args.output)
    finally:model.close()
