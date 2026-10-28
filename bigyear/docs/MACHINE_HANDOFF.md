# Continue Big Year on another computer

Prepared October 28, 2026. The next task is to download the authorized EBD onto
the computer with sufficient storage and import the 2024 lower-48/DC cohort.
The real national EBD has not yet been imported or benchmarked. Existing EOD
experiments and the pending EBD import remain separate evidence sources.

## Clone and install

Use macOS or Linux with Python 3.11 or newer and a C++17 compiler. On Windows,
use Linux through WSL2; the planner uses Unix shell scripts and sockets.
Node.js is optional for the browser regression tests. No cloud VM is needed.

```sh
git clone https://github.com/Reese-Martin/EbirdTools.git
cd EbirdTools
python3 -m venv bigyear/.venv
bigyear/.venv/bin/python -m pip install --upgrade pip
bigyear/.venv/bin/python -m pip install -e './bigyear[dev,gis]'
bigyear/launch.sh build
```

Install the C++ command-line tools if needed: `xcode-select --install` on macOS,
or your Linux distribution's C++ build tools. The `gis` extra is required for
the existing shapefile land mask. The personal-data Streamlit visualizer has
its own installation instructions in the repository's root README.

## Restore the local EBD prerequisites

**A clone alone does not contain the frozen policy or land mask.** The private
handoff archive prepared on the original computer is:

```text
bigyear/build/handoff/ebd-2024-inputs.tar.gz
```

Transfer this file separately with a private file-transfer method or removable
storage. It is intentionally ignored by Git. It contains only the frozen
countability decisions and their required source evidence, the existing land
polygon shapefile and companions, and a checksum manifest. It excludes EOD/EBD
bulk downloads, derived datasets, models, route runs, and Python environments.

From the **new repository root**, restore it:

```sh
tar -xzf /path/to/ebd-2024-inputs.tar.gz
```

The archive restores these paths, which are the importer's defaults:

- `bigyear/data/lower48-aba2024-provisional-v3/`: policy, decision ledgers,
  source-retrieval manifest, and required frozen source snapshots.
- `bigyear/build/land-mask/land-polygons-split-4326/`: land polygons from the
  October 16, 2026 OSM snapshot, including `.shp`, `.shx`, and source/license notes.
- `bigyear/build/handoff/ebd-2024-inputs-manifest.json`: hashes for the restored
  input files, with repository-relative paths.

Verify every restored input before import. Run this from the repository root:

```sh
python3 - <<'PY'
import hashlib
import json
from pathlib import Path

manifest = json.loads(Path('bigyear/build/handoff/ebd-2024-inputs-manifest.json').read_text())
for item in manifest['files']:
    path = Path(item['path'])
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
            digest.update(block)
    assert path.stat().st_size == item['bytes'], path
    assert digest.hexdigest() == item['sha256'], path
print(f"Verified {len(manifest['files'])} handoff inputs")
PY
```

The importer additionally checks the policy's own frozen hashes. Keep the
existing policy rather than regenerating it from newer web pages: the reviewed
decisions are tied to a specific historical analysis.

## Download and import EBD

Keep the authorized download outside the checkout, or inside `bigyear/data/`.
Allow room for the compressed original, any separate Sampling Event Data, the
retained Parquet cohort, and temporary spill. The importer reads compressed
archives directly; it does not create a full decompressed world-text copy.
Neither worldwide runtime nor final subset size has been measured yet.

From the repository root:

```sh
bigyear/.venv/bin/python bigyear/scripts/import_ebd_2024.py \
  --source /path/to/authorized-world-EBD.tar \
  --release actual-EBD-release-label \
  --output bigyear/data/ebd-lower48-2024
```

Add `--sed-source /path/to/sampling-file` if SED is supplied. If the EBD and SED
are in the same archive, pass that archive to both options. Use the actual
release identifier, and add `--member` / `--sed-member` if member selection is
ambiguous. The output directory must be new; repeat with `--resume` after an
interruption with the same source files and options.

The initial import does not launch optimization. Review the import report,
taxonomy holds, and marine/island access before building the complete model.
See [EBD_2024.md](EBD_2024.md) for verification, resume behavior, and next steps.
See [CURRENT_SCENARIO.md](CURRENT_SCENARIO.md) for the shared twelve-hour activity
budget, two-hour visits, 2.2 km/h walking, and primary/provisional treatment.

## Check the installation

```sh
cd bigyear
.venv/bin/python -m pytest tests -q
node --test tests/*.test.mjs
./launch.sh demo --output runs/handoff-demo --seconds 5 --threads 2
./launch.sh validate runs/handoff-demo/run-2024
```

The tests and demo use fictional fixtures and need no observation download,
private policy bundle, or external routing service. Native routing tests need
permission to bind a local loopback socket. Some optional JavaScript checks
skip when real saved route paths have not been supplied.

## Existing EOD experiments and saved runs

The original computer retains approximately 47 GB under `bigyear/data/`,
5.6 GB under `bigyear/runs/`, and 1.6 GB under `bigyear/build/` as measured during
handoff. These are excluded from Git and have not been deleted. The EBD input
bundle covers the prerequisites for a fresh EBD import; it does not migrate
all earlier EOD experiments.

To continue an existing EOD search, transfer the required data, run artifacts,
and routing inputs separately. Many manifests bind absolute input paths and
hashes. Preserve the original paths where practical or rebuild the dataset and
model bindings using the preparation scripts. Do not rewrite hashes to bypass
validation. Rebuild native binaries and Python environments on the destination.

The benchmark JSON and historical documentation record earlier local paths and
results for provenance; they are not fresh-clone execution instructions.
