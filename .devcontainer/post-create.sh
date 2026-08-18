#!/usr/bin/env bash
# Lightweight Codespaces / Dev Container bootstrap (no full [rag] / torch by default).
set -euo pipefail

cd "$(dirname "$0")/.."

python -m pip install -U pip
python -m pip install -e ".[test]"

# ripgrep is declared as a Python dep; ensure the CLI binary exists for repo_search demos.
if ! command -v rg >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq ripgrep
fi

echo ""
echo "==> Dev container ready"
echo "    sa-agent --help"
echo "    python -B -m unittest test.web.test_web_contract test.daemon.test_build_cli_cmd -v"
echo "    python daemon/server.py --host 127.0.0.1 --port 8765   # then open forwarded :8765"
echo ""
echo "    Optional RAG stack (large): pip install -e '.[rag]'"
echo ""
