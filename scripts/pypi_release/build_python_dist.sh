#!/usr/bin/env bash
# build_python_dist.sh — build and validate Python distributions for PyPI
#
# Usage:
#   bash scripts/pypi_release/build_python_dist.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="$ROOT_DIR/output/pypi_dist"

echo "==> repo : $ROOT_DIR"
echo "==> dist : $DIST_DIR"

python3 -m pip install -U pip --quiet
python3 -m pip install -U build twine --quiet

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

cd "$ROOT_DIR"
python3 -m build --outdir "$DIST_DIR"
python3 -m twine check "$DIST_DIR"/*

echo "==> Build complete:"
ls -lh "$DIST_DIR"

