#!/usr/bin/env bash
# publish_pypi.sh — publish built distributions to TestPyPI or PyPI
#
# Usage:
#   bash scripts/pypi_release/publish_pypi.sh --test
#   bash scripts/pypi_release/publish_pypi.sh --prod
#
# Auth:
#   export TWINE_USERNAME="__token__"
#   export TWINE_PASSWORD="<pypi-token>"
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="$ROOT_DIR/output/pypi_dist"
TARGET="test"

usage() {
  cat <<'EOF'
Usage: publish_pypi.sh [--test|--prod]

Options:
  --test   Upload to TestPyPI (default)
  --prod   Upload to PyPI
  -h       Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)
      TARGET="test"
      shift
      ;;
    --prod)
      TARGET="prod"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${TWINE_USERNAME:-}" || -z "${TWINE_PASSWORD:-}" ]]; then
  echo "ERROR: TWINE_USERNAME / TWINE_PASSWORD must be set." >&2
  exit 1
fi

bash "$ROOT_DIR/scripts/pypi_release/build_python_dist.sh"

if [[ "$TARGET" == "test" ]]; then
  echo "==> Uploading to TestPyPI..."
  python3 -m twine upload --repository testpypi "$DIST_DIR"/*
else
  echo "==> Uploading to PyPI..."
  python3 -m twine upload "$DIST_DIR"/*
fi

echo "==> Publish complete ($TARGET)."

