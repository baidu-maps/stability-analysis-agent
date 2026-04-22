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

**Stability Analysis Agent** is an open-source AI Agent purpose-built for **app stability analysis** — covering **crashes, ANR (Application Not Responding), OOM (Out of Memory), freezes / watchdog kills**, and more. Feed it a stability log, and it will **parse, symbolize, extract code, reason about the root cause, and generate fix suggestions** — automatically. Supports **iOS, Android, macOS, Linux, and Windows** with built-in `addr2line` / `atos` integration, LangGraph multi-turn reasoning, and a RAG knowledge base (ChromaDB).

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

### 1. Install via PyPI (Recommended)

```bash
# Install (for Mainland China, add -i https://pypi.tuna.tsinghua.edu.cn/simple)
pip install stability-analysis-agent

# Verify installation
sa-agent --help

# Initialize local config (interactive wizard for LLM keys, addr2line/atos paths, etc.)
sa-agent config init

# Check config completeness
sa-agent config doctor
```

> Config files are saved in `~/.config/stability-analysis-agent/`:
> - `agent_config.local.json` — LLM provider / API key / model
> - `add2line_resolver_config.local.json` — addr2line / atos tool paths
>
> Even without config initialization, you can run the full non-AI toolchain with `--skip-ai`.
>
> The PyPI package includes full runtime dependencies (vector DB, tree-sitter, and LangGraph chain).
>
> Upgrade with: `pip install -U stability-analysis-agent`

### 2. Use Prebuilt CLI Binary (No Python Required)

Download the latest binary from [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases), then run:

```bash
# Example for v1.1.0 macOS arm64 package
unzip StabilityAnalyzer-v1.1.0-mac-arm64.zip
cd output/cli_release/stability_analyzer_cli/v1.1.0-mac-arm64

chmod +x StabilityAnalyzer

# If macOS Gatekeeper blocks launch (unsigned binary)
xattr -d com.apple.quarantine StabilityAnalyzer

./StabilityAnalyzer --help

# Optional: install a stable command name into ~/.local/bin (also ships in release zips)
chmod +x install.sh
./install.sh
# then: sa-agent --help
```

### 3. Developer Setup (from Source)

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
pip install -e .
```

> `pip install -e .` is intended for development workflows. It also exposes the `sa-agent` command locally.

### 4. Run the Built-in Demo (No API Key Needed)

After installing via PyPI (`pip install stability-analysis-agent`) or from source (`pip install -e .`), clone the repo to get the bundled demo cases, then run:

```bash
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --skip-ai
```

Output is saved to `./cli_reports/<timestamp>/` (under your current working directory) with structured JSON reports.

### 5. Analyze Your Own Crash Log

```bash
sa-agent \
  --crash-log <your-crash-log> \
  --library-dir <path-to-libs-and-symbols> \
  --code-root <path-to-source-code>
```

> Add `--skip-ai` to skip AI analysis, or `--parse-only` to only parse + symbolize.

### CLI Options

| Flag | Required | Description |
|------|----------|-------------|
| `--crash-log` | Yes | Path to the crash log file |
| `--library-dir` | Yes* | Directory with libraries (`.dylib`/`.so`) and debug symbols (`.dSYM`) |
| `--code-root` | No | Source code root for extracting code context |
| `--skip-ai` | No | Skip AI — run toolchain only (parser + resolver + code provider) |
| `--parse-only` | No | Parse + symbolize only (no `--code-root` needed) |
| `--parse-log-only` | No | Parse crash log only (no `--library-dir` needed) |
| `--daemon <url>` | No | Delegate to a running daemon instance |

\* Not required when using `--parse-log-only`.

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

AI analysis is **optional**. You can still run full non-AI toolchain with `--skip-ai` without any initialization.

For AI analysis and add2line customization after PyPI install, use:

```bash
sa-agent config init
sa-agent config path
sa-agent config doctor
```

Default local config directory:

```bash
~/.config/stability-analysis-agent/
```

- `agent_config.local.json` for LLM provider/key/model
- `add2line_resolver_config.local.json` for addr2line/atos tool paths

If you choose manual editing in `config init`, edit these files directly in that directory.

### Advanced: Environment overrides

You can still override config file locations via environment variables:

```bash
export STABILITY_AGENT_CONFIG_FILE="/abs/path/agent_config.local.json"
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
| Crash Demos | [docs/crash_demos/README.md](./docs/crash_demos/README.md) |

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
Yes. Use `--skip-ai` to run the full toolchain (parse + symbolize + extract code). The structured JSON output is useful on its own for triage and debugging.

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
