#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bun_bin="${BUN_BIN:-bun}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/nightingale-uv-cache}"
cd "$root/backend"
FASTAPI_ENV=development uv run --frozen --no-sync python -c \
  "import app.main; import json; print(json.dumps(app.main.app.openapi()))" \
  > "$root/frontend/openapi.json"
cd "$root"
"$bun_bin" run --filter frontend generate-client
python scripts/normalize_generated_client.py
"$bun_bin" run --filter frontend lint
