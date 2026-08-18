# Dev Container / GitHub Codespaces

Lightweight environment for CLI, daemon, Web UI, and deterministic unit tests.

## What is included

- Python **3.11** (`devcontainers/python` Bookworm image)
- Editable install: `pip install -e ".[test]"`
- Port **8765** forwarded for `daemon/server.py` Web UI
- **Not** installed by default: full `[rag]` / torch (slow & large) — install manually if needed

## Open in Codespaces

1. On GitHub: **Code → Codespaces → Create codespace on …**
2. Wait for `post-create.sh` to finish
3. Smoke:

```bash
sa-agent --help
python -B -m unittest test.web.test_web_contract -v
python daemon/server.py --host 127.0.0.1 --port 8765
```

## Local VS Code / Cursor

Command Palette → **Dev Containers: Reopen in Container** (requires Dev Containers extension).

## Limits

Codespaces is **Linux**. macOS `atos` and private on-disk symbol trees for Map SDK cases still need a local machine (or the closed-source workspace). Use this container for open-source CLI / Web / CI-style work.
