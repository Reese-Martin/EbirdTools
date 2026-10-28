"""Run independent study arms sequentially; no cross-arm cache discoveries."""
import argparse
import os
import subprocess
import sys
import time

from bigyear_planner.util import ROOT,read,sha256


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--wait-control-pid',type=int)
    parser.add_argument('--arms',nargs='+',default=['nearby','tentpole','island','combined'])
    args=parser.parse_args();root=ROOT/'runs/lower48-eod-2024'
    if args.wait_control_pid:
        while True:
            try:os.kill(args.wait_control_pid,0)
            except ProcessLookupError:break
            time.sleep(5)
    control=root/'calendar-study-control-2024-v1'
    if not read(control/'validation.json')['valid']:raise ValueError('control audit failed')
    print({'stage':'control-verified','species':read(control/'route.json')['species_total']},flush=True)
    jobs=[('island-landing-diagnostic-2024-v1',[str(ROOT/'scripts/verify_island_landing.py')])]
    jobs += [(f'calendar-study-{arm}-2024-v1',[str(ROOT/'scripts/benchmark_calendar_study.py'),'--arm',arm]) for arm in args.arms]
    for tag,arguments in jobs:
        folder=root/tag
        if (folder/'route.json').exists():
            audit=read(folder/'validation.json')
            if not audit['valid'] or audit.get('route_sha256')!=sha256(folder/'route.json'):
                raise ValueError('existing study output is not positively audited: '+tag)
            print({'stage':'verified-existing','run':tag},flush=True);continue
        print({'stage':'starting','run':tag},flush=True)
        with (root/(tag+'.log')).open('w') as log:
            result=subprocess.run([sys.executable,*arguments],stdout=log,stderr=subprocess.STDOUT)
        if result.returncode:
            print((root/(tag+'.log')).read_text()[-5000:],flush=True)
            raise RuntimeError('study phase failed: '+tag)
        print({'stage':'completed','run':tag,'species':read(folder/'route.json')['species_total']},flush=True)


if __name__=='__main__':main()
