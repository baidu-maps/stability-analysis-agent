<h1 align="center">Stability Analysis Agent</h1>
<p align="center">
  <strong>An AI Agent for App Stability — from crash log to root cause in one step</strong><br>
  <sub>Crash · ANR · OOM · Freeze analysis | addr2line / atos symbolizer | LangGraph AI Agent | RAG knowledge base</sub>
</p>
<p align="center">
  <a href="https://pypi.org/project/stability-analysis-agent/"><img src="https://img.shields.io/pypi/v/stability-analysis-agent.svg" alt="PyPI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python"></a>
  <a href="./CONTRIBUTING.md"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"></a>
</p>
<p align="center">
  <b>English</b> | <a href="./README.zh-CN.md">简体中文</a>
</p>

---

**Stability Analysis Agent** is an open-source AI Agent framework for **app stability analysis**, designed to evolve across **crash, ANR (Application Not Responding), OOM (Out of Memory), and freeze / watchdog** scenarios. The first production-ready scenario today is **crash analysis**; ANR, freeze, and memory-focused workflows are under active evolution. Feed it a stability log, and it will **parse, symbolize, extract code, reason about the root cause, and generate fix suggestions** — automatically. Supports **iOS, Android, macOS, Linux, and Windows** with built-in `addr2line` / `atos` integration, LangGraph multi-turn reasoning, and a RAG knowledge base (ChromaDB).

### Why not just paste the log into an AI coding tool?

General-purpose AI coding tools (Cursor, Copilot, Claude Code, etc.) can read a crash log, but they hit hard limits on stability analysis:

- **Raw addresses are meaningless** — AI tools cannot run `addr2line` / `atos`; they see `0x1a2b3c` instead of `MyClass::process() at main.cpp:42`.
- **Stability logs are noisy** — hundreds of system frames drown the real signal; without structured parsing, the LLM wastes tokens on irrelevant context.
- **No domain memory** — every conversation starts from scratch; learned patterns (crash signatures, ANR deadlock traces, OOM heuristics) are lost.

This Agent solves all three:

| | AI Coding Tool | Stability Analysis Agent |
|---|---|---|
| **Address symbolization** | Cannot run native tools | Built-in `addr2line` / `atos` integration |
| **Log parsing** | Sees raw text, high noise | Structured parser extracts signal, threads, key frames; classifies crash / ANR / OOM / freeze |
| **Knowledge accumulation** | Stateless, starts from zero | RAG: rule table + vector DB, patterns improve over time |
| **Workflow** | Single-prompt, one-shot | Multi-step Agent with conditional multi-turn reasoning |
| **Extensibility** | Prompt-only | Tool + Workflow plugin system, config-driven |

### Agent Engine

Three execution modes to fit different needs:

| Mode | Engine | Best for |
|------|--------|----------|
| **Direct** | One-shot prompt assembly | Fast, simple, no framework dependency |
| **LangChain** | LangChain Agent | Flexible tool calling with chain-of-thought |
| **LangGraph** | LangGraph state machine | Multi-turn reasoning, the Agent can request more context and re-invoke tools |

Select via `--engine direct|langchain|langgraph`. All modes share the same tool chain and RAG knowledge base.

**No LLM API key required** to run the core toolchain (parsing + symbolization + code extraction). Plug in any OpenAI-compatible model (GPT, DeepSeek, ERNIE, GLM, etc.) when you're ready for AI analysis.

## Key Features

| Feature | Description |
|---------|-------------|
| **Multi-Step AI Agent** | LangGraph / LangChain / Direct — multi-turn reasoning with conditional branching |
| **Address Symbolization** | Resolves raw addresses to function names & line numbers via `addr2line` / `atos` |
| **Structured Log Parsing** | Auto-detects iOS / Android / macOS / Linux / Windows; classifies crash, ANR, OOM, freeze; extracts signal, threads, key frames |
| **Source Code Context** | Extracts code snippets around crash points |
| **RAG Knowledge Base** | Rule table (fast path) + vector retrieval (ChromaDB) with feedback loop |
| **Tool + Workflow System** | Pluggable architecture — register custom tools and workflows via config or decorators |
| **Multiple Interfaces** | CLI, HTTP Daemon (streaming / SSE), Python API |

## Architecture

```
                  ┌──────────┐   ┌──────────┐   ┌──────────┐
                  │   CLI    │   │  Daemon  │   │  Python  │
                  │          │   │  (HTTP)  │   │   API    │
                  └────┬─────┘   └────┬─────┘   └────┬─────┘
                       │              │              │
                       └──────────────┼──────────────┘
                                      │
                            ┌─────────▼─────────┐
                            │   Tool + Workflow │
                            │   (tool_system)   │
                            └─────────┬─────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
   ┌────────────┐            ┌────────────┐            ┌────────────┐
   │  Crash Log │            │  Address   │            │    Code    │
   │   Parser   │            │ Symbolizer │            │  Provider  │
   └────────────┘            └────────────┘            └────────────┘
                                      │
                            ┌─────────▼─────────┐
                            │    AI Agent       │
                            │  ┌─────────────┐  │
                            │  │  LangGraph  │  │
                            │  │  State      │  │
                            │  │  Machine    │  │
                            │  └──────┬──────┘  │
                            │         │         │
                            │    ┌────▼────┐    │
                            │    │   RAG   │    │
                            │    │ Rules + │    │
                            │    │ Vectors │    │
                            │    └────┬────┘    │
                            │         │         │
                            │    ┌────▼────┐    │
                            │    │   LLM   │    │
                            │    └─────────┘    │
                            └───────────────────┘
```

**Agent Pipeline:**

```
Crash Log → Parse → Symbolize → Extract Code
                                      ↓
                              RAG (rules + vectors)
                                      ↓
                                LLM Reasoning ←──→ Request More Context (multi-turn)
                                      ↓
                                 Fix Report
```

> For detailed architecture diagrams, see [docs/architecture](./docs/architecture/ARCHITECTURE_DIAGRAM.md).

## Quick Start

### Prerequisites

- Binary usage: no Python runtime required
- Source usage: Python 3.9+
- (Optional) `atos` (macOS, built-in) or `addr2line` (Linux, via binutils) for symbolization

### Install and Launch (Recommended)

```bash
# Install (for Mainland China, add -i https://pypi.tuna.tsinghua.edu.cn/simple)
pip install stability-analysis-agent

# Open the interactive wizard
sa-agent
```

> The UX is intentionally Claude CLI-like: arrow-key menus, grouped "More options", clear back paths, and concise confirmations.  
> In most cases, you can finish configuration + analysis + AI fix flow directly in the terminal.

## Demo: Interactive AI Fix (Crash)

Use the bundled demo case to experience the end-to-end AI path:

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
sa-agent
```

In the wizard, choose `快速开始分析（推荐）`, then enter:

```text
crash_log  -> examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash
library_dir -> examples/crash_cases/demo_basic/lib/mac
code_root  -> examples/crash_cases/demo_basic/code_dir
```

The CLI prints an execution plan and runs automatically. In AI mode, it performs parse + symbolize + code-context extraction + LLM reasoning, and can apply fix suggestions with backup.

To analyze your own case, run `sa-agent` and input your own paths using the same flow.

## Other Ways (Advanced)

### Programmatic API (embedding / enterprise wrappers)

Since **v1.2.4**, the wheel includes a stable Python surface in [`cli/api.py`](./cli/api.py), for example `execute_analysis`, `build_parser`, `collect_interactive_run_state`, `interactive_state_to_argv`, `run_from_interactive_state`, and `run_cli_main`. Use it to drive the same pipeline from custom menus or automation without `subprocess`. See [`CHANGELOG.md`](./CHANGELOG.md).

### Use Prebuilt CLI Binary (No Python Required)

Download the latest binary from [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases). Zip/folder names are versioned; use names from the release you downloaded.

```bash
unzip StabilityAnalyzer-v1.2.4-mac-arm64.zip
cd output/cli_release/stability_analyzer_cli/v1.2.4-mac-arm64
./StabilityAnalyzer
```

### Developer Setup (from Source)

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
pip install -e .
sa-agent
```

> `pip install -e .` is intended for development workflows and also exposes the local `sa-agent` command.

### CLI Options

| Flag | Required | Description |
|------|----------|-------------|
| `--crash-log` | Yes | Path to the crash log file |
| `--library-dir` | Yes* | Directory with libraries (`.dylib`/`.so`) and debug symbols (`.dSYM`) |
| `--code-root` | No | Source code root for extracting code context |
| `--scope <value>` | No | Agent run scope (default `full`). One of `full` / `prompt_only` / `parse_only` / `parse_log_only`. See below. |
| `--daemon <url>` | No | Delegate to a running daemon instance |

\* Not required when using `--scope parse_log_only`.

### `--scope` values

| Value | Behavior |
|-------|----------|
| `full` (default) | Parse + symbolize + extract code context + LLM analysis (with optional auto-fix). |
| `prompt_only` | Run the full toolchain but skip the LLM call; emit a reusable prompt file. |
| `parse_only` | Only parse + symbolize. `--code-root` not needed. |
| `parse_log_only` | Only parse the crash log. Neither `--library-dir` nor `--code-root` is needed. |

## Daemon Mode

The daemon provides **streaming output (SSE)**, **process reuse** (no cold start), and **task cancellation** — ideal for IDE integration and high-frequency analysis:

```bash
# Start the daemon
sa-agent --daemon-server --host 127.0.0.1 --port 8765

# Analyze via daemon
sa-agent --daemon http://127.0.0.1:8765 \
  --crash-log <crash-log> --library-dir <lib-dir> --code-root <code-root>
```

> See [Daemon Server Guide](./docs/cli/DAEMON_SERVER_GUIDE.md) for the full HTTP API reference.

## Python API

```python
from tool_system import (
    ToolAndWorkflowRegistry, SystemConfig, WorkflowConfig,
    ConfigDrivenExecutor, register_all_tools_and_workflows
)

registry = ToolAndWorkflowRegistry()
register_all_tools_and_workflows(registry)

config = SystemConfig(
    workflows=[WorkflowConfig(name="crash_analysis", enabled=True)]
)
executor = ConfigDrivenExecutor(registry, config, llm_adapter=None)

result = executor.execute_workflow("crash_analysis", {
    "crash_log": open("crash.crash").read(),
    "library_dir": "./lib",
    "code_root": "./code"
})
print(result)
```

## LLM and Tool Configuration

For LLM and add2line setup, use the interactive wizard:

```bash
sa-agent
```

Then enter `设置` -> `配置大模型` / `配置堆栈地址解析工具`. Checks and guidance run contextually in flow.

Default local config directory:

```bash
~/.config/stability-analysis-agent/
```

- `agent_config.local.json` for LLM provider/key/model
- `add2line_resolver_config.local.json` for addr2line/atos tool paths

If you prefer manual editing, edit these files directly in that directory.

Optional advanced run modes (via `--scope`):
- `--scope prompt_only` (full toolchain, skip LLM, emit prompt file)
- `--scope parse_only` (parse + symbolize only)
- `--scope parse_log_only` (parse log only)

### Advanced: add2line config override

You can override add2line config file location via environment variable:

```bash
export STABILITY_AGENT_ADD2LINE_CONFIG_FILE="/abs/path/add2line_resolver_config.local.json"
```

## Project Structure

```
stability-analysis-agent/
├── agent/              # AI Agent engine (LangGraph state machine)
├── cli/                # CLI entry point
├── daemon/             # HTTP daemon (streaming, SSE)
├── tools/              # Tool implementations (parser, resolver, code provider)
│   └── configs/        # Configuration templates
├── tool_system/        # Tool + Workflow registration & dispatch framework
├── workflows/          # Workflow definitions (crash analysis)
├── rag/                # RAG: rule store + vector index (ChromaDB) + metadata
├── prompts/            # Prompt templates for LLM analysis
├── protocol/           # Unified request/response protocol
├── examples/           # Bundled crash cases
│   └── crash_cases/
│       ├── demo_basic/         # NullPtr, DivZero, Abort, DoubleFree, etc.
│       └── demo_multithread/   # Race condition, deadlock, atomic failure, etc.
├── test/               # Test suite
└── docs/               # Documentation
```

## Documentation

| Topic | Link |
|-------|------|
| CLI Guide | [docs/cli/CLI_GUIDE.md](./docs/cli/CLI_GUIDE.md) |
| CLI Commands Reference | [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md) |
| Daemon Server Guide | [docs/cli/DAEMON_SERVER_GUIDE.md](./docs/cli/DAEMON_SERVER_GUIDE.md) |
| PyPI Release Scripts | [docs/scripts/PYPI_RELEASE_SCRIPTS.md](./docs/scripts/PYPI_RELEASE_SCRIPTS.md) |
| System Architecture | [docs/architecture/README.md](./docs/architecture/README.md) |
| Architecture Diagram | [docs/architecture/ARCHITECTURE_DIAGRAM.md](./docs/architecture/ARCHITECTURE_DIAGRAM.md) |
| Tool System Overview | [docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md](./docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md) |
| Tool Extension Guide | [docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md) |
| Workflow System | [docs/workflows/WORKFLOWS.md](./docs/workflows/WORKFLOWS.md) |
| RAG Vector Database | [docs/rag/README.md](./docs/rag/README.md) |
| Crash Demos | [docs/crash_cases/README.md](./docs/crash_cases/README.md) |

## Testing

```bash
# Regression tests
python3 test/tool_system/test_regression.py

# LLM connection test
python3 test/llm/test_llm_connection.py --provider openai

# Code content provider test
python3 test/agent_py_tool/test_code_content_provider.py

# Vector database test
python3 test/agent_py_tool/test_vector_db.py
```

## FAQ

**Q: Symbolization failed?**
Ensure `--library-dir` contains the binary files (`.dylib` / `.so`) along with their debug symbols (`.dSYM` directories or DWARF info).

**Q: LLM call failed?**
Verify your API key is set correctly. Quick check: `python3 test/llm/test_llm_connection.py --provider openai`

**Q: Code context extraction returns empty?**
Ensure `--code-root` points to the source directory that contains the files listed in the symbolized stack trace.

**Q: Can I use this without an LLM key?**
Yes. Use `--scope prompt_only` to run the full toolchain (parse + symbolize + extract code) without calling the LLM. The structured JSON output is useful on its own for triage and debugging.

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](./CONTRIBUTING.md) before submitting a PR.

```bash
# All commits require DCO sign-off
git commit -s -m "feat: describe your change"
```

## License

[Apache License 2.0](./LICENSE)

## Contact

| Channel | Link |
|---------|------|
| GitHub Issues | [Report a bug or request a feature](https://github.com/baidu-maps/stability-analysis-agent/issues) |
| Email | [hong9988.dev@gmail.com](mailto:hong9988.dev@gmail.com) |

**Maintainer:**

| Name | GitHub | Email |
|------|--------|-------|
| liuhong | [@liuhong996](https://github.com/liuhong996) | hong9988.dev@gmail.com |

---

<p align="center">
  If this project helps you, please consider giving it a <b>Star</b>!
</p>
