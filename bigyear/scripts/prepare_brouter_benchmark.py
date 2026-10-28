"""Fetch pinned official BRouter sources, portable Java and West routing tiles."""
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import time
import zipfile

from bigyear_planner.util import ROOT, dump, read, sha256

BUILD = ROOT/'build/brouter-benchmark'
TILES = ROOT/'data/west-coast-routing-2024/brouter-tiles'
OUT = ROOT/'runs/west-coast-routing-2024'
TAG = 'v1.7.10'


def curl(url):
    return subprocess.check_output(['/usr/bin/curl', '-fLsS', '--retry', '2', '--max-time', '60', url])


def download(url, path, checksum=None, expected_bytes=None):
    if path.exists() and (expected_bytes is None or path.stat().st_size == expected_bytes):
        if checksum is None or sha256(path) == checksum:
            return
    staging = path.with_name(path.name+'.part')
    try:
        subprocess.run(['/usr/bin/curl', '-fLsS', '--retry', '2', '--max-time', '600',
                        '-o', str(staging), url], check=True)
        if expected_bytes is not None and staging.stat().st_size != expected_bytes:
            raise ValueError('download changed size: '+url)
        if checksum is not None and sha256(staging) != checksum:
            raise ValueError('download checksum differs: '+url)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def prepare():
    start = time.monotonic()
    BUILD.mkdir(parents=True, exist_ok=True)
    TILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    metadata = BUILD/'java-download.json'
    if not metadata.exists():
        value = json.loads(curl('https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=aarch64&image_type=jdk&os=mac&vendor=eclipse'))[0]
        dump(metadata, value)
    jdk = read(metadata)
    package = jdk['binary']['package']
    tar = BUILD/package['name']
    download(package['link'], tar, package['checksum'], package['size'])
    if not list(BUILD.glob('jdk-*/Contents/Home/bin/javac')):
        with tarfile.open(tar) as source:
            source.extractall(BUILD, filter='data')
    java_home = next(BUILD.glob('jdk-*/Contents/Home'))
    source_zip = BUILD/'brouter-v1.7.10.zip'
    download('https://codeload.github.com/abrensch/brouter/zip/refs/tags/'+TAG, source_zip)
    source_dir = BUILD/'brouter-1.7.10'
    if not source_dir.exists():
        with zipfile.ZipFile(source_zip) as archive:
            for name in archive.namelist():
                if not (BUILD/name).resolve().is_relative_to(BUILD.resolve()):
                    raise ValueError('unsafe source archive path')
            archive.extractall(BUILD)
    # The route-only server has no external runtime dependencies. Omit the
    # map-creation CLI entry point, which needs a separate OSM parsing library.
    modules = ['brouter-codec', 'brouter-core', 'brouter-expressions',
               'brouter-mapaccess', 'brouter-util', 'brouter-server']
    sources = [p for module in modules for p in (source_dir/module/'src/main/java').rglob('*.java')
               if p.name != 'BRouter.java']
    classes = BUILD/'classes'
    jar = BUILD/'brouter-1.7.10-routing.jar'
    if not jar.exists():
        classes.mkdir(exist_ok=True)
        subprocess.run([str(java_home/'bin/javac'), '--release', '17', '-d', str(classes),
                        *map(str, sources)], check=True)
        subprocess.run([str(java_home/'bin/jar'), '--create', '--file', str(jar),
                        '-C', str(classes), '.'], check=True)
    profiles = source_dir/'misc/profiles2'
    # Pin the current lookup dictionary along with the weekly prebuilt tiles.
    lookup = profiles/'lookups.dat'
    lookup_url = 'https://brouter.de/brouter/profiles2/lookups.dat'
    if not (BUILD/'lookup-download.json').exists():
        content = curl(lookup_url)
        lookup.write_bytes(content)
        dump(BUILD/'lookup-download.json', {'url': lookup_url, 'sha256': sha256(lookup)})
    index_url = 'https://brouter.de/brouter/segments4/'
    index = TILES/'download-index.html'
    if not index.exists():
        index.write_bytes(curl(index_url))
    entries = []
    for line in index.read_text().splitlines():
        match = re.search(r'>(W(\d+)_N(\d+)\.rd5)</a>\s+([^<]+?)\s+(\d+)\s*$', line)
        if not match:
            continue
        name, west, north, modified, size = match.groups()
        if -130 <= -int(west) < -100 and 30 <= int(north) < 55:
            path = TILES/name
            download(index_url+name, path, expected_bytes=int(size))
            entries.append({'file': name, 'bytes': path.stat().st_size,
                            'sha256': sha256(path), 'index_modified': modified.strip()})
            print(json.dumps({'tile': name, 'bytes': path.stat().st_size}), flush=True)
    if not entries:
        raise ValueError('no routing tiles matched the Western test rectangle')
    release = {'tag': TAG, 'commit': '4d2639af77ea5ed9c30d3e400764eb6f9e8522da',
               'source_zip_sha256': sha256(source_zip), 'jar_sha256': sha256(jar),
               'java': str(java_home/'bin/java'), 'java_version': jdk['version'],
               'jdk_sha256': package['checksum'], 'jar': str(jar), 'profiles': str(profiles),
               'profile': 'car-vario', 'profile_sha256': sha256(profiles/'car-vario.brf'),
               'profile_note': 'Bundled car-vario default vmax 90 km/h, beeline/offroad false; turn restrictions enabled by car default. Frozen before any road queries.',
               'lookup_sha256': sha256(lookup), 'tiles': entries,
               'tile_bytes': sum(e['bytes'] for e in entries), 'tile_directory': str(TILES),
               'tileset_sha256': hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
               'setup_seconds': time.monotonic()-start,
               'snapshot_caveat': 'Current weekly BRouter OSM tiles differ from January 2025 OSRM; agreement is not ground-truth accuracy.'}
    dump(OUT/'brouter-release.json', release)
    if classes.exists():
        shutil.rmtree(classes)
    tar.unlink(missing_ok=True)
    print(json.dumps({'ready': True, 'tiles': len(entries), 'tile_bytes': release['tile_bytes']}), flush=True)


if __name__ == '__main__':
    prepare()
