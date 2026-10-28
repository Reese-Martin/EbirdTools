"""Stage an authorized 2024 national EBD release; never starts routing/search.

Tar/ZIP/gzip inputs stay intact. Each successful stage is hash-bound for --resume.
All raw records are retained before countability and access decisions are made.
"""

import argparse
from pathlib import Path

from bigyear_planner.config import Config
from bigyear_planner.ebd import DEFAULT_POLICY, classify_land, prepare, validate_config, verify_policy
from bigyear_planner.ebd_archive import archive_scan, convert
from bigyear_planner.util import ROOT, dump, read, sha256


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path, help='authorized EBD tar/ZIP, .txt/.tsv or .gz')
    parser.add_argument('--release', required=True, help='release identifier from the actual download')
    parser.add_argument('--output', required=True, type=Path, help='new import directory')
    parser.add_argument('--member', help='EBD tar/ZIP member, if ambiguous')
    parser.add_argument('--sed-source', type=Path, help='optional SED file or same tar/ZIP as --source')
    parser.add_argument('--sed-member')
    parser.add_argument('--land-mask', type=Path,
                        default=ROOT/'build/land-mask/land-polygons-split-4326/land_polygons.shp')
    parser.add_argument('--policy-dir', type=Path, default=DEFAULT_POLICY)
    parser.add_argument('--config', type=Path, default=ROOT/'examples/ebd-2024-national.json')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--build-model', action='store_true', help='also build complete mmap candidates; no search')
    args = parser.parse_args(argv)
    config = Config.read(args.config)
    validate_config(config)
    verify_policy(args.policy_dir)
    inputs = [args.source, args.land_mask, args.policy_dir/'policy.json', args.config]
    if args.sed_source:
        inputs.append(args.sed_source)
    if args.land_mask.suffix == '.shp':
        inputs.append(args.land_mask.with_suffix('.shx'))
    binding = {'purpose': 'ebd-national-2024-import-v1', 'release': args.release,
               'member': args.member, 'sed_member': args.sed_member,
               'inputs': [{'path': str(p.resolve()), 'bytes': p.stat().st_size,
                           'mtime_ns': p.stat().st_mtime_ns} for p in inputs],
               'config': config.to_dict()}
    root = args.output.resolve()
    if root.exists():
        if not args.resume or read(root/'import-plan.json') != binding:
            raise ValueError('output exists or resume inputs differ; choose a new output')
    else:
        root.mkdir(parents=True)
        dump(root/'import-plan.json', binding)

    def archive_stage(source, target, kind, member):
        if (target/'manifest.json').exists():
            _, manifest = archive_scan(target)
            if sha256(source) != manifest['source_sha256']:
                raise ValueError('original source changed since the completed conversion')
            return manifest
        return convert(source, target, args.release, kind=kind, member=member,
                       resume=args.resume and target.exists())

    archive = root/'observations'
    manifest = archive_stage(args.source, archive, 'ebd', args.member)
    sed = None
    if args.sed_source:
        sed = root/'sampling'
        archive_stage(args.sed_source, sed, 'sed', args.sed_member)
    stage_file = root/'stages.json'
    stages = read(stage_file) if stage_file.exists() else {}
    land = root/'land.csv'
    if 'land' in stages:
        if sha256(land) != stages['land']:
            raise ValueError('completed land classification changed')
    else:
        if land.exists():
            raise ValueError('uncheckpointed land.csv; inspect it before moving it aside and resuming')
        classify_land(archive, args.land_mask, land)
        stages['land'] = sha256(land)
        dump(stage_file, stages)
    dataset = root/'dataset'
    if (dataset/'manifest.json').exists():
        from bigyear_planner.eod import attach_dataset
        from bigyear_planner.util import ManagedDuckDB

        with ManagedDuckDB() as con:
            attach_dataset(con, dataset)
        audit = read(dataset/'manifest.json')
    else:
        audit = prepare(archive, land, dataset, args.release, config, args.policy_dir, sed)
    if args.build_model:
        from bigyear_planner.compact_model import build

        model = build(dataset, root/'model', config)
        model.close()
    report = {'status': 'imported; review taxonomy/access before a new routing benchmark',
              'release': args.release, 'archive_rows': manifest['rows'],
              'archive_bytes': manifest['bytes'], 'statistics': audit['statistics'],
              'dataset': str(dataset), 'dataset_manifest_sha256': sha256(dataset/'manifest.json'),
              'walking_speed_kmh': 2.2, 'source_kept': str(args.source.resolve()),
              'search_started': False,
              'next': ['Inspect dataset/review.csv for release-specific taxonomy and held records.',
                       'Validate EBD marine/island access against exact dated observations.',
                       'Build complete candidates with --build-model, then benchmark county/state before national search.']}
    dump(root/'import-report.json', report)
    print(report)
    return report


if __name__ == '__main__':
    main()
