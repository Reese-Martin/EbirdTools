import csv
import gzip
import io
import json
from pathlib import Path
import tarfile
import zipfile

import duckdb
import pytest

from bigyear_planner import ebd_archive
from bigyear_planner.compact_model import build
from bigyear_planner.config import Config
from bigyear_planner.ebd import POLICY, classify_land, prepare, register_views
from bigyear_planner.ebd_archive import REQUIRED, archive_scan, convert
from bigyear_planner.eod import attach_dataset
from bigyear_planner.util import dump, read, sha256, write_csv


def observation(**changes):
    row = {n: '' for n in REQUIRED}
    row.update({'SCIENTIFIC NAME': 'Cardinalis cardinalis', 'TAXON CONCEPT ID': 'avibase-ABC1',
                'CATEGORY': 'species', 'OBSERVATION COUNT': 'X', 'COUNTRY CODE': 'US',
                'STATE CODE': 'US-CA', 'LOCALITY ID': 'L1', 'LOCALITY': 'Fixture point',
                'LATITUDE': '34.000000000000000001', 'LONGITUDE': '-119.00000000000000',
                'OBSERVATION DATE': '2024-02-29', 'SAMPLING EVENT IDENTIFIER': 'S1',
                'GLOBAL UNIQUE IDENTIFIER': 'TEST:1', 'APPROVED': '1', 'EXOTIC CODE': '',
                'REVIEWED': '0', 'GROUP IDENTIFIER': 'G1', 'PROTOCOL CODE': 'P20',
                'ALL SPECIES REPORTED': '0', 'SPECIES COMMENTS': 'Literal "quoted" text',
                'TRIP COMMENTS': '', 'DURATION MINUTES': '200', 'EFFORT DISTANCE KM': '25.40',
                'UNKNOWN FUTURE FIELD': 'keep me', '': ''})
    row.update(changes)
    return row


def raw_file(path, rows):
    names = sorted(set().union(*(set(r) for r in rows)) - {''}) + ['']
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=names, delimiter='\t', quoting=csv.QUOTE_NONE,
                                quotechar=None, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(rows)
    return path


def policy_fixture(root):
    root.mkdir()
    primary = {'source_name': 'Cardinalis cardinalis', 'source_taxon_id': 'avibase-avibase-ABC1',
               'species_id': 'aba2024:cardinal', 'common_name': 'Fixture Cardinal',
               'aba_eligible': '1', 'eod_presence_eligible': '1', 'eligible_states': '*',
               'reference': 'fictional test fixture', 'eligible_regions': '', 'score_tier': 'primary'}
    provisional = {**primary, 'source_name': 'Aix galericulata', 'source_taxon_id': 'avibase-avibase-ABC2',
                   'species_id': 'aba2024:mandarin', 'common_name': 'Fixture Mandarin',
                   'score_tier': 'provisional-A', 'eligible_regions': json.dumps([
                       {'state': 'US-CA', 'min_lat': 33, 'max_lat': 35, 'min_lon': -120,
                        'max_lon': -118, 'first': '2024-01-01', 'last': '2024-05-04'}])}
    held = {**primary, 'source_name': 'Held species', 'source_taxon_id': 'avibase-avibase-ABC3',
            'species_id': 'aba2024:held', 'score_tier': 'held', 'eod_presence_eligible': 'review'}
    write_csv(root/'taxonomy.csv', [primary, provisional, held], list(primary))
    dump(root/'source-retrieval.json', {'records': {}})
    dump(root/'policy.json', {'id': POLICY, 'decision_files': {'taxonomy.csv': sha256(root/'taxonomy.csv')},
                             'sources': {}, 'source_retrieval_sha256': sha256(root/'source-retrieval.json')})
    return root


@pytest.fixture
def pipeline(tmp_path):
    rows = [observation(), observation(), observation(**{'SAMPLING EVENT IDENTIFIER': 'S2'}),
            observation(**{'LATITUDE': '34.000000000000000002'}),
            observation(**{'SCIENTIFIC NAME': 'Aix galericulata', 'TAXON CONCEPT ID': 'avibase-ABC2', 'EXOTIC CODE': 'P'}),
            observation(**{'SCIENTIFIC NAME': 'Aix galericulata', 'TAXON CONCEPT ID': 'avibase-ABC2',
                           'EXOTIC CODE': 'P', 'OBSERVATION DATE': '2024-05-05'}),
            observation(**{'TAXON CONCEPT ID': 'avibase-new-split'}),
            observation(**{'EXOTIC CODE': 'X'}), observation(**{'APPROVED': '0'}),
            observation(**{'SCIENTIFIC NAME': 'Held species', 'TAXON CONCEPT ID': 'avibase-ABC3'}),
            observation(**{'LONGITUDE': '-125.0000000000000'}),
            observation(**{'SPECIES COMMENTS': 'captive bird in an aviary'}),
            observation(**{'LATITUDE': ''})]
    source = raw_file(tmp_path/'ebd.txt', rows)
    archive = tmp_path/'archive'
    convert(source, archive, 'fixture-release', min_free_bytes=0)
    mask = tmp_path/'land.geojson'
    dump(mask, {'type': 'Polygon', 'coordinates': [[[-120, 33], [-118, 33], [-118, 35],
                                                  [-120, 35], [-120, 33]]]})
    land = tmp_path/'land.csv'
    classify_land(archive, mask, land)
    config = Config(target_locations=10000000, activity_minutes=720, walking_speed_kmh=2.2,
                    threads=1, memory_limit='256MB')
    return source, archive, land, policy_fixture(tmp_path/'policy'), config, rows


@pytest.mark.parametrize('container', ['tsv', 'gzip', 'zip', 'zip-gzip-members',
                                     'tar', 'tar-gzip-members', 'tar-gzip-outer'])
def test_archive_retains_every_value_duplicate_and_coordinate(tmp_path, container):
    rows = [observation(), observation(), observation(**{'STATE CODE': 'US-DC', 'LATITUDE': '38.9'}),
            observation(**{'STATE CODE': 'US-AK'}), observation(**{'STATE CODE': 'US-HI'}),
            observation(**{'COUNTRY CODE': 'CA', 'STATE CODE': 'CA-BC'}),
            observation(**{'OBSERVATION DATE': '2023-12-31'})]
    source = raw_file(tmp_path/'ebd.txt', rows)
    if container == 'gzip':
        compressed = tmp_path/'ebd.txt.gz'
        with gzip.open(compressed, 'wb') as f:
            f.write(source.read_bytes())
        source = compressed
    elif container in {'zip', 'zip-gzip-members'}:
        compressed = tmp_path/'download.zip'
        with zipfile.ZipFile(compressed, 'w', zipfile.ZIP_DEFLATED) as z:
            if container == 'zip-gzip-members':
                z.writestr('folder/ebd.txt.gz', gzip.compress(source.read_bytes()))
            else:
                z.write(source, 'folder/ebd.txt')
            z.writestr('folder/ebd_sampling.txt', 'not the observation file')
            z.writestr('README.txt', 'download metadata: preserve inside original ZIP')
        source = compressed
    elif container.startswith('tar'):
        compressed = tmp_path/('download.tar.gz' if container == 'tar-gzip-outer' else 'download.tar')
        with tarfile.open(compressed, 'w:gz' if container == 'tar-gzip-outer' else 'w') as archive:
            body = source.read_bytes()
            name = 'folder/ebd.txt'
            if container == 'tar-gzip-members':
                body, name = gzip.compress(body), name+'.gz'
            for name, body in [(name, body), ('README.txt', b'preserve source metadata')]:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        source = compressed
    digest = sha256(source)
    archive = tmp_path/'archive'
    result = convert(source, archive, 'release', min_free_bytes=0)
    assert result['rows'] == 3 and result['input_rows'] == 7
    assert result['outside_scope_rows'] == 4 and result['verified_mismatches'] == 0
    assert sha256(source) == digest == result['source_sha256']
    sql, meta = archive_scan(archive)
    with duckdb.connect() as con:
        data = con.execute(f'SELECT * FROM {sql} ORDER BY _source_row').fetchall()
    for parsed, original in zip(data, rows[:3]):
        assert list(parsed[:-1]) == [original.get(n, '') if n != '_ebd_trailing_blank' else '' for n in meta['columns']]
    assert [d[-1] for d in data] == [1, 2, 3]
    assert not (archive/'current-window.tsv').exists()
    assert not (archive/'spill').exists()


def test_policy_views_hold_unresolved_and_keep_marine_for_access(pipeline, tmp_path):
    _, archive, land, policy, config, rows = pipeline
    dataset = tmp_path/'dataset'
    result = prepare(archive, land, dataset, 'fixture-release', config, policy)
    assert result['statistics']['year_geography_rows'] == len(rows)
    assert result['statistics']['dispositions']['eligible'] == 5
    assert result['statistics']['countable_species_before_access'] == 2
    assert sum(v['observations'] for v in result['statistics']['state_month_coverage']) == len(rows)
    assert result['statistics']['scoring_tiers_before_access'] == {'primary': 1, 'provisional-A': 1}
    assert result['statistics']['scoring_dispositions']['unmapped_taxon_review'] == 1
    with duckdb.connect() as con:
        evidence, obs = attach_dataset(con, dataset)
        assert con.execute(f'SELECT count(*) FROM {evidence}').fetchone()[0] == 5
        assert con.execute(f'SELECT sum(source_rows) FROM {obs}').fetchone()[0] == 5
        assert con.execute('SELECT count(*) FROM ebd_raw').fetchone()[0] == len(rows)
        assert con.execute("SELECT count(*) FROM ebd_countable_evidence WHERE access_disposition!='land'").fetchone()[0] == 1
        assert con.execute(f'SELECT DISTINCT event_id FROM {obs}').fetchall() == [('group:G1',)]
        assert con.execute(f'SELECT count(DISTINCT checklist_id) FROM {evidence}').fetchone()[0] == 2
    model = build(dataset, tmp_path/'model', config)
    try:
        assert model.meta['represented_observations'] == 5
        assert model.meta['locations'] == 2
        assert model.meta['species_entries'] == 3
        assert {model.locations[i]['latitude_text'] for i in range(2)} == {
            '34.000000000000000001', '34.000000000000000002'}
        assert model.meta['config']['walking_speed_kmh'] == 2.2
        assert not (model.root/'rows.parquet').exists()
    finally:
        model.close()


def test_sed_join_is_by_checklist_and_rejects_conflicts(pipeline, tmp_path):
    _, archive, land, policy, config, _ = pipeline
    sed_raw = raw_file(tmp_path/'sampling.txt', [observation(), observation()])
    sed = tmp_path/'sed'
    convert(sed_raw, sed, 'fixture-release', kind='sed', min_free_bytes=0)
    result = prepare(archive, land, tmp_path/'dataset', 'fixture-release', config, policy, sed)
    assert result['statistics']['year_geography_rows'] == 13
    assert result['statistics']['missing_sed_checklist_rows'] == 1
    conflicting = raw_file(tmp_path/'bad-sampling.txt', [observation(), observation(**{'GROUP IDENTIFIER': 'different'})])
    bad = tmp_path/'bad-sed'
    convert(conflicting, bad, 'fixture-release', kind='sed', min_free_bytes=0)
    with pytest.raises(ValueError, match='conflicting SED'):
        prepare(archive, land, tmp_path/'bad-dataset', 'fixture-release', config, policy, bad)


@pytest.mark.parametrize('changes', [{'OBSERVATION DATE': '2024-02-30'}, {'STATE CODE': ''},
                                    {'STATE CODE': 'US-XX'}, {'COUNTRY CODE': ''}])
def test_invalid_scope_fails_without_silent_loss(tmp_path, changes):
    source = raw_file(tmp_path/'bad.txt', [observation(**changes)])
    with pytest.raises(ValueError, match='invalid date or unresolved'):
        convert(source, tmp_path/'archive', 'release', min_free_bytes=0)
    assert not (tmp_path/'archive/manifest.json').exists()
    assert not (tmp_path/'archive/current-window.tsv').exists()


def test_malformed_tsv_is_not_ignored(tmp_path):
    source = raw_file(tmp_path/'bad.txt', [observation()])
    with source.open('a') as f:
        f.write('short\tbad\trow\n')
    with pytest.raises(duckdb.Error):
        convert(source, tmp_path/'archive', 'release', min_free_bytes=0)
    assert not (tmp_path/'archive/manifest.json').exists()


@pytest.mark.parametrize('container', ['tsv', 'tar-gzip-members'])
def test_resume_replays_windows_and_detects_tampering(tmp_path, monkeypatch, container):
    source = raw_file(tmp_path/'source.txt', [observation(**{'SPECIES COMMENTS': 'x'*600000}) for _ in range(4)])
    if container == 'tar-gzip-members':
        body = gzip.compress(source.read_bytes())
        packed = tmp_path/'download.tar'
        with tarfile.open(packed, 'w') as tar:
            info = tarfile.TarInfo('ebd.txt.gz')
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        source = packed
    archive = tmp_path/'archive'
    real_dump = ebd_archive.dump

    def interrupted(path, value):
        real_dump(path, value)
        if Path(path).name == 'progress.json' and len(value['windows']) == 1:
            raise OSError('simulated interruption after checkpoint')

    monkeypatch.setattr(ebd_archive, 'dump', interrupted)
    with pytest.raises(OSError, match='simulated interruption'):
        convert(source, archive, 'release', chunk_mib=1, min_free_bytes=0)
    assert len(read(archive/'progress.json')['parts']) == 1
    assert not (archive/'current-window.tsv').exists()
    monkeypatch.setattr(ebd_archive, 'dump', real_dump)
    manifest = convert(source, archive, 'release', chunk_mib=1, resume=True, min_free_bytes=0)
    assert manifest['rows'] == 4 and len(manifest['parts']) == 2
    part = archive/manifest['parts'][0]['name']
    part.write_bytes(part.read_bytes() + b'tampered')
    with pytest.raises(ValueError, match='part changed'):
        archive_scan(archive)


def test_policy_and_archive_changes_are_rejected(pipeline, tmp_path):
    _, archive, land, policy, config, _ = pipeline
    dataset = tmp_path/'dataset'
    prepare(archive, land, dataset, 'fixture-release', config, policy)
    (policy/'taxonomy.csv').write_text('changed')
    with duckdb.connect() as con, pytest.raises(ValueError, match='decision changed'):
        register_views(con, dataset, read(dataset/'manifest.json'))


def test_empty_cohort_is_inspectable_and_zip_members_are_explicit(tmp_path):
    source = raw_file(tmp_path/'source.txt', [observation(**{'STATE CODE': 'US-AK'})])
    convert(source, tmp_path/'empty', 'release', min_free_bytes=0)
    sql, manifest = archive_scan(tmp_path/'empty')
    assert manifest['rows'] == 0
    assert duckdb.sql(f'SELECT count(*) FROM {sql}').fetchone()[0] == 0
    zpath = tmp_path/'ambiguous.zip'
    with zipfile.ZipFile(zpath, 'w') as z:
        z.write(source, 'one.txt')
        z.write(source, 'two.txt')
    with pytest.raises(ValueError, match='select --member'):
        convert(zpath, tmp_path/'no', 'release', min_free_bytes=0)


def test_walking_and_candidate_reduction_cannot_change_silently(pipeline, tmp_path):
    _, archive, land, policy, _, _ = pipeline
    with pytest.raises(ValueError, match='2.2 km/h'):
        prepare(archive, land, tmp_path/'dataset', 'fixture-release', Config(), policy)


def test_ebd_route_independent_validation_includes_provisionals(pipeline, tmp_path):
    from bigyear_planner.demo import synthetic_roads
    from bigyear_planner.problem import build_problem
    from bigyear_planner.runner import run

    _, archive, land, policy, config, _ = pipeline
    dataset = tmp_path/'dataset'
    prepare(archive, land, dataset, 'fixture-release', config, policy, synthetic=True)
    problem_path = tmp_path/'problem.json'
    build_problem(dataset, problem_path, config)
    roads = tmp_path/'roads.sqlite'
    synthetic_roads(problem_path, tmp_path/'roads.csv', roads)
    result = run(problem_path, roads, tmp_path/'run', seconds=1)
    assert result['species_total'] == 2
    assert read(tmp_path/'run/validation.json')['valid']


def test_orchestrated_import_and_resume(pipeline, tmp_path):
    import importlib.util
    from bigyear_planner.util import ROOT

    spec = importlib.util.spec_from_file_location('import_ebd_2024', ROOT/'scripts/import_ebd_2024.py')
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    source, _, _, policy, config, rows = pipeline
    config_path = tmp_path/'config.json'
    dump(config_path, config.to_dict())
    argv = ['--source', str(source), '--release', 'fixture-release', '--output', str(tmp_path/'whole'),
            '--land-mask', str(tmp_path/'land.geojson'), '--policy-dir', str(policy),
            '--config', str(config_path), '--build-model']
    first = script.main(argv)
    second = script.main([*argv, '--resume'])
    assert first['dataset_manifest_sha256'] == second['dataset_manifest_sha256']
    assert first['archive_rows'] == len(rows) and not first['search_started']
    assert source.exists() and (tmp_path/'whole/model/model.json').exists()


def test_tiled_shapefile_land_matches_small_geojson(pipeline, tmp_path):
    import shapefile

    _, archive, land, _, _, _ = pipeline
    stem = tmp_path/'coast'
    with shapefile.Writer(str(stem), shapeType=shapefile.POLYGON) as writer:
        writer.field('name', 'C')
        # Clockwise ring, as required by the shapefile polygon convention.
        writer.poly([[[-120, 33], [-120, 35], [-118, 35], [-118, 33], [-120, 33]]])
        writer.record('fixture land')
    tiled = tmp_path/'tiled.csv'
    classify_land(archive, stem.with_suffix('.shp'), tiled)
    with land.open() as f:
        expected = {(r['locality_id'], r['latitude'], r['longitude']): r['on_land'] for r in csv.DictReader(f)}
    with tiled.open() as f:
        actual = {(r['locality_id'], r['latitude'], r['longitude']): r['on_land'] for r in csv.DictReader(f)}
    assert actual == expected


def test_tar_selects_ebd_and_sed_by_header_without_extracting(tmp_path):
    raw = raw_file(tmp_path/'source.txt', [observation()])
    sed_fields = sorted(ebd_archive.COMMON | {'GROUP IDENTIFIER'})
    sed_row = observation()
    sed_body = ('\t'.join(sed_fields)+'\n'+'\t'.join(sed_row[f] for f in sed_fields)+'\n').encode()
    source = tmp_path/'world.tar'
    with tarfile.open(source, 'w') as archive:
        for name, body in [('nested/world.txt.gz', gzip.compress(raw.read_bytes())),
                           ('nested/sampling.txt.gz', gzip.compress(sed_body)),
                           ('../../should-not-be-extracted.txt', b'nondata metadata')]:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo('link.txt.gz')
        link.type = tarfile.SYMTYPE
        link.linkname = '/not-a-real-host-path'
        archive.addfile(link)
    ebd = convert(source, tmp_path/'ebd', 'release', min_free_bytes=0)
    sed = convert(source, tmp_path/'sed', 'release', kind='sed', min_free_bytes=0)
    assert ebd['source']['member'] == 'nested/world.txt.gz'
    assert sed['source']['member'] == 'nested/sampling.txt.gz'
    assert ebd['rows'] == sed['rows'] == 1
    assert not (tmp_path/'nested').exists()
    with pytest.raises(ValueError, match='regular file'):
        convert(source, tmp_path/'link', 'release', member='link.txt.gz', min_free_bytes=0)


def test_tar_does_not_silently_choose_one_of_several_data_members(tmp_path):
    raw = raw_file(tmp_path/'source.txt', [observation()])
    source = tmp_path/'ambiguous.tar'
    with tarfile.open(source, 'w') as archive:
        archive.add(raw, arcname='first.txt')
        archive.add(raw, arcname='second.txt')
    with pytest.raises(ValueError, match='one unique --member'):
        convert(source, tmp_path/'ambiguous', 'release', min_free_bytes=0)
    result = convert(source, tmp_path/'explicit', 'release', member='second.txt', min_free_bytes=0)
    assert result['rows'] == 1 and result['source']['member'] == 'second.txt'
