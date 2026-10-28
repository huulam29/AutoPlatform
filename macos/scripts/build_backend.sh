#!/usr/bin/env bash
# Bundle the agentline engine for the Mac app:
#
#   macos/build/backend/python/   relocatable CPython (python-build-standalone via uv)
#                                 with agentline[server] installed into it
#   macos/build/backend/web/      the built editor (web/dist)
#
#   macos/scripts/build_backend.sh            # build (reuses the Python if already there)
#   macos/scripts/build_backend.sh --clean    # start from scratch
#
# The app runs: backend/python/bin/python3 -m agentline.cli serve ...
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/macos/build/backend"
PYVER="${AGENTLINE_PYTHON_VERSION:-3.11}"

if [[ "${1:-}" == "--clean" ]]; then
  rm -rf "$OUT"
fi
mkdir -p "$OUT" "$ROOT/macos/build/dist"

# 1. A relocatable Python: python-build-standalone, managed by uv.
if [[ ! -x "$OUT/python/bin/python3" ]]; then
  echo "==> Python $PYVER (python-build-standalone via uv)"
  uv python install "$PYVER"
  PYBIN="$(uv python find "$PYVER" --managed-python)"
  # uv's install dir is a symlink to the versioned one: resolve it and copy the real files,
  # never install into uv's shared interpreter.
  PYHOME="$(cd "$(dirname "$PYBIN")/.." && pwd -P)"
  rm -rf "$OUT/python"
  mkdir -p "$OUT/python"
  cp -R "$PYHOME/." "$OUT/python/"
  # trim what the app never needs
  rm -rf "$OUT/python/lib/python$PYVER/test" "$OUT/python/lib/python$PYVER/idlelib" \
         "$OUT/python/lib/python$PYVER/tkinter" "$OUT/python/lib/python$PYVER/turtledemo"
fi
if [[ -L "$OUT/python" ]]; then
  echo "error: $OUT/python is a symlink; run with --clean" >&2
  exit 1
fi
PY="$OUT/python/bin/python3"
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version'

# 2. agentline + the server extra, installed into that Python.
echo "==> agentline wheel"
rm -f "$ROOT"/macos/build/dist/agentline-*.whl
(cd "$ROOT" && uv build --wheel -o macos/build/dist >/dev/null)
WHEEL="$(ls "$ROOT"/macos/build/dist/agentline-*.whl | head -1)"
echo "==> install $(basename "$WHEEL")[server]"
uv pip install --python "$PY" --break-system-packages --reinstall-package agentline \
  "${WHEEL}[server]" >/dev/null

# 3. The editor.
echo "==> editor (web/dist)"
# Rebuilt when missing, asked for, or older than any web source: a stale dist ships an old UI
# against a new engine (phase 49's install commands were missing from the app that way).
stale=""
if [[ -f "$ROOT/web/dist/index.html" ]]; then
  stale="$(find "$ROOT/web/src" "$ROOT/web/index.html" "$ROOT/web/package-lock.json" \
    "$ROOT/web/vite.config.ts" -newer "$ROOT/web/dist/index.html" -print -quit 2>/dev/null)"
fi
if [[ ! -f "$ROOT/web/dist/index.html" || -n "$stale" || "${AGENTLINE_REBUILD_WEB:-0}" == "1" ]]; then
  [[ -n "$stale" ]] && echo "    web/dist is older than $(basename "$stale"): rebuilding"
  (cd "$ROOT/web" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null)
fi
rm -rf "$OUT/web"
cp -R "$ROOT/web/dist" "$OUT/web"

# 4. Smoke check: the bundled interpreter runs the CLI and has the server deps.
find "$OUT/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
"$PY" -m agentline.cli --help >/dev/null
"$PY" -c 'import fastapi, uvicorn, ruamel.yaml, agentline; print("agentline", agentline.__version__)'
du -sh "$OUT" | awk '{print "==> backend bundle: " $1 " at '"$OUT"'"}'
