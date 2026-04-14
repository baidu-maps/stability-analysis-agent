# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Stability Analysis Agent is an AI-driven crash log analysis toolchain with a multi-shell architecture:
- **Core** (Python): Analytics, parsing, address resolution, code extraction, RAG
- **CLI** (Python): Command-line entry point for scripting and debugging
- **Local Daemon** (Python/HTTP): Long-running process for streaming, cancellation, and reuse
- **VSCode Plugin** (TypeScript): Thin UI shell that delegates to daemon/Core

**Important**:
- **Stability Analysis Agent** (open source): flat package layout, all core modules (`agent/`, `tools/`, `rag/`, `cli/`, `daemon/`, etc.) are top-level directories.
- **Map SDK Crash Agent** (closed, enterprise-specific): sibling repo `../map-sdk-crash-agent/` — installs this package via `pip install -e .` and adds private configs/crash cases. Not part of this repo.

## Common Commands

### Run CLI Analysis

```bash
# Direct run (development)
python3 cli/main.py \
  --crash-log <path> --library-dir <path> --code-root <path>

# Skip AI, generate prompt file only (no LLM key required)
python3 cli/main.py \
  --crash-log <path> --library-dir <path> --code-root <path> \
  --skip-ai

# Parse log + addr2line only (no code-root required)
python3 cli/main.py \
  --crash-log <path> --library-dir <path> \
  --parse-only

# Parse crash log only
python3 cli/main.py \
  --crash-log <path> \
  --parse-log-only

# Via daemon (recommended for VSCode / repeated runs)
python3 cli/main.py --daemon http://127.0.0.1:8765 \
  --crash-log <path> --library-dir <path> --code-root <path>
```

### Start Daemon

```bash
python3 daemon/server.py --host 127.0.0.1 --port 8765
```

### Run Tests

```bash
# AI Agent tool tests
cd test/agent_py_tool
python3 test_code_content_provider.py
python3 test_stop_functionality.py
python3 test_vector_db.py

# LLM connection tests
cd test/llm
python3 test_llm_connection.py --all

# VSCode integration test (30s timeout)
AI_STABILITY_TEST_TIMEOUT_SECONDS=30 python3 test/test_vscode_ai_agent_integration.py

# Fast mode (skip AI analysis)
AI_STABILITY_TEST_FAST=1 python3 test/llm/test_vscode_simulation.py
```

### Package Installation

```bash
# Install as editable package (required before first run)
pip install -e .
```

### Demo (no LLM key required)

```bash
# Run with bundled demo case
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --skip-ai
# Output: cli_reports/<timestamp>/01~03 JSON + round_0/05_ai_final_tip.txt
```

## Architecture

### Tool Chain Flow
1. `crash_log_parser` - Parses crash logs, extracts stack addresses and metadata
2. `add2line_resolver` - Resolves addresses to function names/line numbers using addr2line/atos
3. `code_content_provider` - Extracts source code context around crash points
4. `AI Agent` (LangGraph) - Generates fix suggestions, optionally enhanced with RAG

### Key Directories
- `agent/` - AI Agent implementation (LangGraph-based)
- `tools/` - Tool implementations (parser, resolver, code provider)
- `rag/` - Vector database integration (ChromaDB)
- `cli/main.py` - CLI entry point
- `daemon/server.py` - HTTP daemon with streaming support
- `tool_system/` - Tool registration and dispatch
- `skills/` - Skill definitions
- `prompts/` - Prompt templates
- `examples/` - Demo crash cases (mac / ios / multithread)
- `test/` - Test suite

### Configuration
- `tools/configs/agent_config.json` - LLM provider template (no keys, safe to commit)
- `tools/configs/agent_config.local.json` - Local overrides with real keys (gitignored)
- `tools/configs/add2line_resolver_config.json` - Toolchain path template
- `tools/configs/add2line_resolver_config.local.json` - Local toolchain paths (gitignored)
- Environment variables: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, etc.

## Documentation Rules

**IMPORTANT**: New Markdown documentation must be placed in `docs/` subdirectories:
- Architecture / design: `docs/architecture/`
- CLI usage: `docs/cli/`
- Tool implementation: `docs/tools/`
- Developer guides: `docs/dev/`
- Do NOT create `.md` files in `test/`, `tools/`, `cli_reports/`, or repo root (except standard files like README, CHANGELOG, etc.)
