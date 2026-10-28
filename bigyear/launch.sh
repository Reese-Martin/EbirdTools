#!/usr/bin/env bash
set -euo pipefail
app_root="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -x "$app_root/.venv/bin/python" ]]; then
  python3 -m venv "$app_root/.venv"
  "$app_root/.venv/bin/python" -m pip install -e "${app_root}[dev]"
fi
export PATH="$app_root/.venv/bin:$PATH"
exec "$app_root/.venv/bin/python" -m bigyear_planner.cli "$@"
