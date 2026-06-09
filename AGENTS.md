# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

Stability Analysis Agent is an AI-driven crash log analysis toolchain with a multi-shell architecture:
- **Core** (Python): Analytics, parsing, address resolution, code extraction, RAG
- **CLI** (Python): Command-line entry point for scripting and debugging
- **Local Daemon** (Python/HTTP): Long-running process for streaming, cancellation, and reuse
- **VSCode Plugin** (TypeScript): Thin UI shell that delegates to daemon/Core

**Important**:
- **Stability Analysis Agent** (open source): flat package layout, all core modules (`agent/`, `tools/`, `rag/`, `cli/`, `daemon/`, etc.) are top-level directories.
- **Stability Analysis Agent** (closed, enterprise-specific): sibling repo `../stability-analysis-agent/` — installs this package via `pip install -e .` and adds private configs/crash cases. Not part of this repo.

## Common Commands

### Run CLI Analysis

```bash
# Direct run (development) — full scope (default)
python3 cli/main.py \
  --crash-log <path> --library-dir <path> --code-root <path>

# gen_prompt_only: full toolchain but skip LLM, generate prompt file only (no LLM key required)
python3 cli/main.py \
  --crash-log <path> --library-dir <path> --code-root <path> \
  --scope gen_prompt_only

# parse_stack_only: parse log + addr2line only (no code-root required)
python3 cli/main.py \
  --crash-log <path> --library-dir <path> \
  --scope parse_stack_only

# parse_log_only: parse crash log only
python3 cli/main.py \
  --crash-log <path> \
  --scope parse_log_only

# Via daemon (recommended for VSCode / repeated runs)
python3 cli/main.py --daemon http://127.0.0.1:8765 \
  --crash-log <path> --library-dir <path> --code-root <path>
```

### Scope values

`--scope` controls how deep the agent runs (default `full`):

- `full`: parse + symbolize + extract code context + LLM analysis (and optional auto-fix).
- `gen_prompt_only`: full toolchain, but skip LLM call; produces a reusable prompt file.
- `parse_stack_only`: only parse + symbolize.
- `parse_log_only`: only parse the crash log.

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
pip install -e ".[rag]"

# PyPI: core only vs with vector/RAG stack
pip install stability-analysis-agent
pip install "stability-analysis-agent[rag]"
```

Install troubleshooting: `docs/cli/INSTALL_TROUBLESHOOTING.md`  
RAG ML pins: `requirements-rag.txt` / `[rag]` extra (`numpy<2`, `torch>=2.4`, `transformers<4.52`, etc.)

### Demo (no LLM key required)

```bash
# Run with bundled demo case
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
# Output: cli_reports/<timestamp>/01~03 (+ optional 03b/04) JSON + round_0/05_ai_prompt.md
# 03b = 03b_code_location_trace.json（03 代码定位审计，非 repo_search）
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
- `workflows/` - Workflow definitions
- `prompts/` - Prompt templates
- `examples/` - Demo crash cases (mac / ios / multithread)
- `test/` - Test suite

### `05_ai_prompt.md` generation (do not change without explicit request)

**Unless the user’s task or prompt explicitly asks to change it, do not modify how `round_0/05_ai_prompt.md` is built.** Keep the current assembly logic; do not inject new sections or extra content into this file from new tools, sidecar reports, or ad-hoc prompt appendices.

- **Producer**: `workflows/crash_analysis_workflow.py` → `_build_prompt_final_tip()`; CLI writes `result["final_tip"]` via `cli/main.py` → `_write_cli_report()`.
- **Inputs today**: structured content from `01` / `02` / `03` (parse, symbolize, code context) using the existing section order and wording in `_build_prompt_final_tip`.
- **Optional merge into `05`**: only `memory_context` from `04_memory_context.json` when non-empty (from `vector_memory_retriever`). If `memory_context` is empty, do not add a “规则与经验模式参考” block.
- **Do not merge into `05`**: `03b_code_location_trace.json`、LangGraph `repo_search_results`，或其它工具旁路产出，除非用户明确要求写入提示词。
- **Sidecar artifacts**（`03b_code_location_trace.json` 等）仅供调试与二次消费；不得默认 alter `05`。

When adding features (new tools, RAG, repo search, LangGraph nodes), wire them to separate report files or state/TOOL_OUTPUT—not into `05`—unless the user clearly asks to change `05`’s structure or contents.

### Configuration
- `tools/configs/agent_config.json` - LLM provider template (no keys, safe to commit)
- `tools/configs/agent_config.local.json` - Local overrides with real keys (gitignored)
- `tools/configs/add2line_resolver_config.local.example.json` - Example local add2line config (safe paths)
- `~/.config/stability-analysis-agent/add2line_resolver_config.local.json` - Local toolchain paths (gitignored); resolver loads this filename from several candidate locations (see `tools/add2line_resolver_tool.py`)
- Environment variables: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, etc.

## Documentation Rules

**IMPORTANT**: New Markdown documentation must be placed in `docs/` subdirectories:
- Architecture / design: `docs/architecture/`
- CLI usage: `docs/cli/`
- Tool implementation: `docs/tools/`
- Developer guides: `docs/scripts/`
- Do NOT create `.md` files in `test/`, `tools/`, `cli_reports/`, or repo root (except standard files like README, CHANGELOG, etc.)
