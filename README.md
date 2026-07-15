<h1 align="center">Stability Analysis Agent</h1>
<p align="center">
  <strong>🐛 When your app crashes, sa-agent fixes it end-to-end — from the bug ticket to the verified, packaged fix.</strong><br>
  <sub>An open-source Agent for <b>app-stability repair</b>. It solves stability problems class by class; <b>Crash fix is what ships first</b>. ANR / OOM / Freeze / memory leak follow the same framework as they mature.</sub>
</p>
<p align="center">
  <a href="https://pypi.org/project/stability-analysis-agent/"><img src="https://img.shields.io/pypi/v/stability-analysis-agent.svg" alt="PyPI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python"></a>
  <a href="https://pypi.org/project/stability-analysis-agent/#files"><img src="https://img.shields.io/badge/wheel-py3--none--any-success.svg" alt="Wheel"></a>
  <a href="./CONTRIBUTING.md"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"></a>
  <a href="./stability-analysis-agent-skill/"><img src="https://img.shields.io/badge/skills-claude%20code%20%7C%20cursor-purple.svg" alt="Skill Pack"></a>
</p>
<p align="center">
  <b>English</b> | <a href="./README.zh-CN.md">简体中文</a>
</p>

---

### What this is — and what isn't

`Stability Analysis Agent` is an **app-stability repair framework**. It treats
crashes, ANRs, OOMs, freezes, memory leaks, watchdog kills — every class of
stability problem — as a first-class repair target.

It does **not** ship generic-prompt tooling. The Agent reads a real crash log,
runs the native toolchain (`addr2line` / `atos`), reads source context, and
**auto-fixes** the offending code with backup. Then it hands control off to
the rest of the closed loop (verify / package / ship).

It does **not** ship every workflow at once. The framework is open-ended
long-term, but the first production scope is **Crash fix**. ANR, OOM and Freeze
are on the roadmap — when those land they get their own dedicated workflows
under the same framework, not a v2 product. Today: **Crash in, Crash fixed,
Crash shipped**.

## Why we are not another AI coding tool

| | Cursor / Copilot / Claude Code | Stability Analysis Agent |
|---|---|---|
| **What it does to a crash log** | Reads it like any other text — analyzes, sometimes suggests a fix | **Auto-fixes** it: parse → symbolize → read source → produce a patch → apply locally with backup |
| **Native toolchain (`addr2line` / `atos`)** | Cannot run them | First-class integration — addresses get resolved before any LLM call |
| **Knowledge accumulation** | Stateless across conversations | RAG rule table + vector DB, patterns improve over time |
| **Multi-step reasoning** | Single prompt, one shot | LangGraph state machine — Agent can request more context and re-invoke tools |
| **Bug-tracker integration** | None out of the box | `bug-platform-fetcher` Skill wires in any ticket system |
| **Verifies the fix before shipping** | ❌ you stop at "looks right" | ✅ `automation-testing` Skill preset runs your test runner |
| **Ship automation** | ❌ | ✅ `cicd-pipeline` Skill preset drives build/sign/publish |
| **End-to-end auto-fix loop** | ❌ | ✅ Ticket → auto-fix → verify → ship, all on one Python package |
| **Extensibility** | Prompt only | Tool + Workflow + Skill system, with `extensions/` for local plugins |

> **Scope of "auto-fix"**, in this repo:
> `parse → symbolize → extract code context → propose patch → apply locally with backup`. After that the Agent **hands off** to a human or to the closed-loop Skills (verify / package) — it does not push to `main`, open PRs, or bypass code review. The Loop is open, not headless.

### "Crash fix" today, more tomorrow

Current scope = **Crash fix**. Supported crash kinds include null pointer,
div-zero, abort, double free, deadlocks / race conditions / atomic failures,
stack overflow, OOM-on-crash dumps, etc. The closed loop below describes
how we wire Crash fix.

Planned next (each as its own workflow under the same framework, no v2):

- **ANR** analysis — Android `am_anr`, iOS watchdog, Harmony AppFreeze.
- **OOM / memory** analysis — heap snapshot diffing.
- **Freeze / hang** detection — stack sampling + thread state.

[Full roadmap →](#roadmap)

## Closed-Loop Workflow

The four Skill presets shipped in `v1.2.8` are the **backbone** of Crash-fix
auto-repair — not a collection of disconnected features. Wire them together,
and `sa-agent` becomes an end-to-end stability engineering agent.

```
                ┌──────────────────────  AUTO-FIX  LOOP  ──────────────────────┐
                │                                                                  │
                │                                                                  │
  ① Bug Fetcher      ② Auto-Fixer            ③ Verifier              ④ Packager │
   bug-platform-      sa-agent                automation-              cicd-     │
   fetcher-skill      (Direct / LangChain /    testing-skill          pipeline- │
   (your backend)     LangGraph)              (your test runner)     skill      │
                                                                                │
       ⬇                  ⬇                    ⬇                      ⬇    │
   ticket ID      →   parse + symbolize   →   tests / smoke      →   build      │
   crash log          code context           regression            publish      │
   library dir        patch + auto-apply     pass / fail            artifact    │
                                          (with backup)                              │
                                                                                │
                │  Each stage is independently runnable. The repair loop happens  │
                │  when you stitch them together. Today this is manual / CI; the │
                │  open framework is the integration point for full automation.   │
                │                                                                  │
                └──────────────────────────────────────────────────────────────────┘
```

### Pick what you need

| You need to… | Jump to |
|---|---|
| Auto-fix the crash log you already have | [Quick Start — 60 seconds](#quick-start) |
| Pull a ticket from your bug tracker and let the Agent auto-fix | `4) 根据缺陷管理平台自动修复` (run `bug-platform-fetcher`) → `1) 快速开始修复` |
| Verify the fix with your project's tests | `6) 自动验证修复结果` (run `automation-testing`) |
| Ship a fixed artifact to CI / a release channel | `7) 自动生成修复后的新包` (run `cicd-pipeline`) |
| Wire the whole loop as one Skill | [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md) |

### Stand-up commands

```bash
# ① fetch the crash from your ticket system
sa-agent skill init bug-platform-fetcher-skill ./bug-platform-fetcher-skill \
                   --preset bug-platform-fetcher
sa-agent skill install ./bug-platform-fetcher-skill

# ③ verify the fix
sa-agent skill init automation-testing-skill ./automation-testing-skill \
                   --preset automation-testing
sa-agent skill install ./automation-testing-skill

# ④ ship the artifact
sa-agent skill init cicd-pipeline-skill ./cicd-pipeline-skill \
                   --preset cicd-pipeline
sa-agent skill install ./cicd-pipeline-skill

# run them end-to-end via the Skill CLI
sa-agent skill run bug-platform-fetcher-skill --input '{"ticket_id":"MY-123"}' --json
sa-agent --crash-log <log> --library-dir <dir> --code-root <dir>      # ② auto-fix
sa-agent skill run automation-testing-skill --input '{"build":{...}}' --json
sa-agent skill run cicd-pipeline-skill --input '{"artifact":{...}}'    --json
```

The presets ship as skeletons — only `SKILL.md` + `skill.json`. You fill in the platform-specific code; `sa-agent` does the rest. See [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md) and [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md).

### Pick what you need to know next

- 🙋 **"I just want to auto-fix a crash log."** → [Quick Start](#quick-start) — 60 seconds, no LLM key required.
- 🛠 **"I want to script `sa-agent` from my own tool / IDE / CI."** → [Python API](#python-api) + [Daemon Mode](#daemon-mode).
- 🧩 **"I want to extend the agent with my own Tool / Workflow / Skill."** → [For Developers](#for-developers--four-ways-to-contribute).

## Quick Start

### Prerequisites

- Binary usage: no Python runtime required
- **Python version**: minimum **3.9**; **recommended 3.10–3.12** (primary CI coverage)
  - Core only (parse + symbolize + auto-fix without LLM): 3.9+ is generally fine
  - With `[rag]` (torch / transformers): prefer **3.10–3.12**; 3.9 may hit ML stack issues
  - On macOS, prefer **Homebrew / pyenv** Python over python.org installers without CA setup (SSL)
- (Optional) `atos` (macOS, built-in) or `addr2line` (Linux, via binutils) for symbolization

### Install and Launch (Recommended)

**Option A — `pip` (venv or system environment)**

```bash
# Install (for Mainland China, add -i https://pypi.tuna.tsinghua.edu.cn/simple)
pip install stability-analysis-agent

# With vector DB / similar-case RAG (recommended for full experience)
pip install "stability-analysis-agent[rag]"

# Open the interactive wizard
sa-agent
```

**Option B — `pipx` (isolated CLI, no global site-packages pollution)**

```bash
# Install pipx first: https://pipx.pypa.io/
pipx install stability-analysis-agent
# Or with RAG (large download, slower first install)
pipx install "stability-analysis-agent[rag]"

sa-agent --help
```

**Option C — prebuilt binary**: see "Use Prebuilt CLI Binary" below.

See [docs/cli/INSTALL_TROUBLESHOOTING.md](./docs/cli/INSTALL_TROUBLESHOOTING.md) for Python versions, SSL, pipx, `transformers` / `nn` errors, etc.

> The UX is intentionally Claude CLI-like: arrow-key menus, grouped "More options", clear back paths, concise confirmations. The top-level menu follows `q (exit) → 1 (quick start) → 4 / 5 / 6 / 7 (closed-loop presets) → 2 (settings) → 3 (help)` — no deep sub-menus, one keypress for any common action.

### Demo: Auto-Fix a Crash (60 seconds)

Use the bundled demo case to experience the auto-fix path end-to-end:

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
sa-agent
```

In the wizard, choose `快速开始修复（推荐）`, then enter:

```text
crash_log  -> examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash
library_dir -> examples/crash_cases/demo_basic/lib/mac
code_root  -> examples/crash_cases/demo_basic/code_dir
```

The CLI prints an execution plan and runs automatically. In AI mode it
parses, symbolizes, extracts code context, produces a patch, and applies
it locally with backup. To auto-fix your own case, run `sa-agent` and
input your own paths through the same flow.

> 🎥 Want to try the full closed loop? Pick the demo case above first, then
> drop into menu item `4` and explore `automation-testing` / `cicd-pipeline`
> after the auto-fix completes.

## Key Features

| Feature | Description |
|---------|-------------|
| **End-to-End Auto-Fix Loop** | Crash → auto-fix (parse + symbolize + patch + apply) → verify → ship, stitched from 4 `Skill` presets |
| **Crash-Fix Focus, Framework-Depth** | Ships Crash fix first; ANR / OOM / Freeze plug into the same framework as dedicated workflows |
| **Address Symbolization** | Resolves raw addresses to function names & line numbers via `addr2line` / `atos` (runs *before* the LLM) |
| **Structured Log Parsing** | Auto-detects iOS / Android / macOS / Linux / Windows; classifies Crash / ANR / OOM / freeze; extracts signal, threads, key frames |
| **Source Code Context** | Reads source files around the crash site so the Agent sees real code, not just addresses |
| **RAG Knowledge Base** | Rule table (fast path) + vector retrieval (ChromaDB) with feedback loop |
| **`sa-agent` Auto-Fix Core** | Direct / LangChain / LangGraph engines — all run the same auto-fix pipeline, no LLM needed for the deterministic parts |
| **Tool + Workflow System** | Pluggable architecture — register custom tools and workflows via config or decorators |
| **Skill System** | Discover / install / lint / init / run Claude-style `SKILL.md` skills, or bridge them into the existing Tool / Workflow runtime |
| **`extensions/` plugins** | Drop-in user-level Tool / Workflow directory at `~/.config/stability-analysis-agent/extensions/` |
| **External Agent Skill Pack** | Bundled [`stability-analysis-agent-skill/`](./stability-analysis-agent-skill/) — teach Claude Code / Cursor how to call `sa-agent` |
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
                            │ Tool + Workflow +  │
                            │     Skill         │
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
                            ┌─────────▼───────────────────┐
                            │  sa-agent core (Auto-Fix)    │
                            │  ┌───────────────────────┐  │
                            │  │  Direct / LangChain   │  │
                            │  │  / LangGraph engine   │  │
                            │  └──────────┬────────────┘  │
                            │             │                │
                            │        ┌────▼─────┐          │
                            │        │   RAG    │          │
                            │        │ rules +  │          │
                            │        │ vectors  │          │
                            │        └────┬─────┘          │
                            │             │                │
                            │        ┌────▼─────┐          │
                            │        │   LLM    │ (auto-fix│
                            │        │  patch   │  only)   │
                            │        └──────────┘          │
                            └─────────────────────────────────┘

   ┌─── Skill System (plugins & closed-loop presets) ─────────────────┐
   │                                                                  │
   │  bug-platform-fetcher ──▶ automation-testing ──▶ cicd-pipeline   │
   │        (① Fetch)                  (③ Verify)             (④ Ship)  │
   │                                                                  │
   │  cli/main.py → SkillManager → SkillRuntime → extensions/         │
   └──────────────────────────────────────────────────────────────────┘
```

**Auto-Fix Pipeline:**

```
Crash Log → Parse → Symbolize → Read Source → Propose Patch → Apply Locally (with backup)
                                  ▲                              │
                                  │         RAG (rules + vectors) │
                                  └────────   Request more context ┘
                                                            ↓
                                                       Fix Report + Patched Files
```

> For detailed architecture diagrams, see [docs/architecture/ARCHITECTURE_DIAGRAM.md](./docs/architecture/ARCHITECTURE_DIAGRAM.md).

## Skill System (sa-agent Runtime Extensions)

The `skill_system/` package and `sa-agent skill …` subcommands add a **pluggable extension layer** *inside* `sa-agent` (distinct from the external-agent skill pack above). A Skill is a directory containing a Claude-style `SKILL.md` plus an optional machine-readable `skill.json`; installed skills can be discovered at startup, rendered as prompt snippets, or bridged into the existing **Tool / Workflow** runtime.

### CLI Subcommands

```bash
# Discover, list, show
sa-agent skill list [--skill-dir PATH]… [--json]
sa-agent skill show <name> [--json]

# Validate
sa-agent skill lint <path-to-skill-dir> [--json]

# Install / uninstall (directories or .zip archives)
sa-agent skill install <source-dir-or.zip> [--target-root PATH] [--overwrite]
sa-agent skill uninstall <name> [--target-root PATH]

# Scaffold a new skill (claude-style prompt or workflow/tool/plugin)
sa-agent skill init <name> <target-dir> [--type prompt|workflow|tool|plugin] \
                   [--preset automation-testing|cicd-pipeline|bug-platform-fetcher]

# Execute: render prompt, or invoke exported workflow/tool
sa-agent skill run <name> [args…] [--input path/to/input.json] [--json]
```

### Closed-Loop Skill Presets

Three `--preset` scaffolds cover the **fetch → auto-fix → verify → ship** loop:

| Preset | Purpose | When to use |
|--------|---------|-------------|
| `bug-platform-fetcher` | Pull a ticket ID and download the matching crash log + debug symbols | Right **before** auto-fix — feeds `crash_log` / `library_dir` paths |
| `automation-testing` | Run automated tests / smoke / regression checks on a repaired artifact | Right **after** the Agent has produced and applied a fix |
| `cicd-pipeline` | Package, build, publish or hand off the repaired artifact | Right **after** a fix has been verified |

```bash
sa-agent skill init bug-platform-fetcher-skill ./bug-platform-fetcher-skill --preset bug-platform-fetcher
sa-agent skill init automation-testing-skill  ./automation-testing-skill  --preset automation-testing
sa-agent skill init cicd-pipeline-skill      ./cicd-pipeline-skill      --preset cicd-pipeline

sa-agent skill install ./bug-platform-fetcher-skill
sa-agent skill install ./automation-testing-skill
sa-agent skill install ./cicd-pipeline-skill
```

In the interactive `sa-agent` wizard, the top-level menu exposes **three flat entries** for these presets — `4) 根据缺陷管理平台自动修复`, `6) 自动验证修复结果`, `7) 自动生成修复后的新包` — each showing the recommended init/install commands and the current install status (`sa-agent skill show …`).

> The public build ships only **empty skeletons** for these presets. *No* Jira / iCafe / WorkTile / 飞书 / 自建系统 API call is made by this repository. Real platform integrations live in your own Skill packages. See [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md) and [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md).

### Discovery and Install Paths

- Default install root: `~/.config/stability-analysis-agent/skills` (override with `--skill-home` or `STABILITY_AGENT_SKILL_HOME`).
- On startup, `sa-agent` also discovers skills in:
  - `~/.claude/skills`
  - `./.claude/skills` (current working directory)
  - `<repo>/.claude/skills`
  - any extra path passed via `--skill-dir` or `STABILITY_AGENT_SKILL_DIRS` (PATH-list separator).
- Supported packaging formats: a skill **directory** or a `.zip` archive whose top level is a skill directory.

### Bridging Skills into Tool / Workflow

`skill.json` declares an `entrypoint` plus an `exports` array:

| `entrypoint` | Runtime behavior |
|--------------|------------------|
| `prompt` | Render `SKILL.md`, substitute `$ARGUMENTS` / `$SKILL_NAME` / `$SKILL_DIR`, return a prompt string |
| `workflow:<name>` | Call the workflow registered via `exports.kind = workflow` |
| `tool:<name>` | Call the tool registered via `exports.kind = tool` |

A workflow/tool skill can register its exports back into the `tool_system` registry so the existing executor (`ConfigDrivenExecutor` / LangGraph routes) can pick them up:

```json
{
  "id": "crash-analysis-skill",
  "command_name": "crash-analysis",
  "type": "workflow",
  "entrypoint": "workflow:crash_analysis",
  "exports": [
    {
      "kind": "workflow",
      "ref": "my_package.my_skill:CrashAnalysisWorkflow",
      "name": "crash_analysis",
      "priority": "CUSTOM",
      "force_override": false,
      "enabled": true
    }
  ]
}
```

For a complete end-to-end example (install → lint → run), see [docs/skills/README.md](./docs/skills/README.md) and [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md).

## For Developers — Four Ways to Contribute

If you've ever shipped a plugin to a popular tool, you can ship a Plugin to `sa-agent`. There are four well-trodden paths; pick the one closest to your team's expertise.

| You want to… | Where to start | What you'll ship |
|---|---|---|
| **Wrap an internal symbol server** (Tool) | [`extensions/tools/example_tool.py`](./extensions/tools/example_tool.py) | A `BaseTool` subclass + `@register_tool(priority=…)` decorator — `sa-agent` auto-discovers it from your `~/.config/stability-analysis-agent/extensions/` drop |
| **Integrate your team's ticket system** (Skill) | preset `bug-platform-fetcher` + [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md) | A Skill that returns `{crash_log, library_dir, ticket_id, ...}` JSON — wire it to Jira, WorkTile, 飞书, 自建系统, or anything that issues tickets |
| **Plug in your test runner** (Skill preset) | preset `automation-testing` + [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md) | A Skill that wraps your project's test command / smoke check, returning `pass / fail` |
| **Replace or extend the auto-fix core** (Workflow) | [docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md) | A `BaseWorkflow` subclass registered with `@register_workflow(priority=Priority.CUSTOM)` — same plugin dir, no wrapper |

> All four paths share the same plugin convention: drop a Python file or directory where `sa-agent` looks for it, and it's live. No SDK, no registry, no fork required.
> See [CONTRIBUTING.md](./CONTRIBUTING.md) for branch / DCO / sign-off conventions.

## Use with External AI Agents (Claude / Cursor)

If you already use **Claude Code**, **Cursor**, or similar AI coding tools, install the bundled skill pack so the agent knows how to call this toolchain (symbolization, structured reports, `--scope`, etc.) — instead of guessing commands or pasting raw logs only.

This is **not** the same as `sa-agent skill install` (runtime extensions for sa-agent). The pack lives at [`stability-analysis-agent-skill/`](./stability-analysis-agent-skill/) and is copied into **your external agent's** skill directory.

**Step 1 — install the Python package** (provides `sa-agent`):

```bash
pip install stability-analysis-agent
# or: pipx install stability-analysis-agent
```

**Step 2 — install the skill pack** into your agent:

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cp -R stability-analysis-agent/stability-analysis-agent-skill ~/.claude/skills/stability-analysis-agent
```

For **Cursor** (project-level example):

```bash
mkdir -p .cursor/skills
cp -R stability-analysis-agent/stability-analysis-agent-skill .cursor/skills/stability-analysis-agent
```

After that, ask your agent to auto-fix a crash with Stability Analysis Agent — it should propose `sa-agent` commands, pick the right `--scope`, and read `cli_reports/<timestamp>/` outputs.

| Resource | Description |
|----------|-------------|
| [SKILL.md](./stability-analysis-agent-skill/SKILL.md) | Main entry for external agents |
| [examples.md](./stability-analysis-agent-skill/examples.md) | Copy-paste command examples |
| [reference.md](./stability-analysis-agent-skill/reference.md) | Flags, reports, config paths |
| [docs/skills/README.md](./docs/skills/README.md) | sa-agent Skill System (runtime extensions) |
| [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md) | `automation-testing` / `cicd-pipeline` closed-loop templates |
| [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md) | `bug-platform-fetcher` template |

> **No LLM key?** The skill documents `--scope gen_prompt_only` — full parse + symbolize + code context + prompt file, without calling an LLM. (`auto-fix` itself requires an LLM; the prompt-only mode skips the LLM step and just emits structured analysis.)

## Roadmap

We're building this in the open. Here's where we are — and where the framework, not just the Agent, is heading.

| Milestone | Status | First shipped |
|---|---|---|
| **Framework** | | |
| Tool + Workflow registration framework | ✅ GA | v1.1 |
| Python API / Daemon mode | ✅ GA | v1.2.2 |
| RAG `[rag]` extra (ChromaDB + sentence-transformers) | ✅ GA | v1.2.6 |
| `extensions/` plugin discovery + example Tool / Workflow | ✅ GA | v1.2.7 |
| **Auto-Fix Core** | | |
| Crash auto-fix (parse + symbolize + patch + apply) | ✅ GA | v1.0 (core), v1.2.8 (closed-loop presets) |
| **`bug-platform-fetcher` / `automation-testing` / `cicd-pipeline` presets** | ✅ GA | v1.2.8 |
| **Same framework, new stability class** | | |
| ANR auto-fix (Android `am_anr`, iOS watchdog, Harmony AppFreeze) | 🚧 in design | next minor |
| OOM / memory auto-fix (heap snapshot diffing) | 📋 planned | next minor |
| Freeze / hang auto-fix (stack sampling + thread state) | 📋 planned | TBD |
| **Community presets** | | |
| engine-build, iOS-XCUITest, Hypium (more Skill presets) | 🎯 community-driven | open |
| Sentry, Bugsnag, Azure DevOps, Linear, Jira Cloud, 飞书 (more bug platforms) | 🎯 community-driven | open |

For the long-form version (deferred ideas, RFCs, design notes), see [docs/ROADMAP.md](./docs/ROADMAP.md).

## Compatible Platforms & Runtimes

| Layer | Supported |
|---|---|
| **Stability class (current scope)** | Crash — null pointer, div-zero, abort, double free, deadlocks / race conditions / atomic failures, stack overflow, OOM-on-crash dumps, etc. |
| **Stability class (planned)** | ANR, OOM / memory, Freeze / hang |
| **OS** | macOS · iOS · Android · Harmony · Linux · Windows |
| **Crash log formats** | Apple `.crash` · Android logcat / tombstone · Harmony `Stacktrace:` · native `#NN pc` · JSON exports from Sentry, Firebase Crashlytics, Bugsnag, Bugly, 自建 APM, etc. |
| **Python** | 3.9 · 3.10 · 3.11 · 3.12 |
| **LLM providers** | Any OpenAI-compatible endpoint: OpenAI · DeepSeek · 文心 / ERNIE · GLM · Qwen · llama.cpp · vLLM |
| **Symbolizers** | `addr2line` (Linux) · `atos` (macOS) · DWARF `.dSYM` |
| **External agents** | Claude Code · Cursor · 任何支持 `~/.claude/skills/` 的 Agent |

Full matrix and how to add a new adapter: [docs/tools/CRASH_LOG_FORMATS.md](./docs/tools/CRASH_LOG_FORMATS.md).

## Other Ways (Advanced)

### Programmatic API (embedding / enterprise wrappers)

Since **v1.2.4**, the wheel includes a stable Python surface in [`cli/api.py`](./cli/api.py), for example `execute_analysis`, `build_parser`, `collect_interactive_run_state`, `interactive_state_to_argv`, `run_from_interactive_state`, and `run_cli_main`. Use it to drive the same pipeline from custom menus or automation without `subprocess`. See [`CHANGELOG.md`](./CHANGELOG.md).

For skill system integrators, the public surface is exposed via [`skill_system/`](./skill_system/):

```python
from skill_system import (
    SkillManager, SkillRuntime,
    load_skill_bundle, parse_skill_directory,
    available_skill_presets, write_skill_scaffold,
)

manager = SkillManager()        # uses the standard discovery roots
runtime = SkillRuntime(manager)

# Scaffold + write a starter skill
write_skill_scaffold("./my-skill", "my-skill", preset="automation-testing")

# Render prompt-style skill
prompt = runtime.render("my-skill", arguments="issue-123 json").prompt

# Execute workflow/tool skill against a JSON payload
result = runtime.execute("crash-analysis-skill", input_payload={
    "crash_log": "...", "library_dir": "./lib", "code_root": "./code"
})
```

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
| `--crash-log` | Yes | Path to the crash log file (any extension; content-based parsing — see [Crash log formats](./docs/tools/CRASH_LOG_FORMATS.md)) |
| `--library-dir` | Yes* | Directory with libraries (`.dylib`/`.so`) and debug symbols (`.dSYM`) |
| `--code-root` | No | Source code root for reading code context |
| `--scope <value>` | No | Agent run scope (default `full`). One of `full` / `gen_prompt_only` / `parse_stack_only` / `parse_log_only`. See below. |
| `--daemon <url>` | No | Delegate to a running daemon instance |

\* Not required when using `--scope parse_log_only`.

### `--scope` values

| Value | Behavior |
|-------|----------|
| `full` (default) | Parse + symbolize + read source + LLM auto-fix (produces a patch and applies it locally with backup). |
| `gen_prompt_only` | Run the full toolchain but skip the LLM call; emit a reusable prompt file. |
| `parse_stack_only` | Only parse + symbolize. `--code-root` not needed. |
| `parse_log_only` | Only parse the crash log. Neither `--library-dir` nor `--code-root` is needed. |

### Supported crash log files and platforms

**File extensions:** not restricted — `.crash`, `.txt`, `.log`, `.json`, or no suffix all work if the **content** matches a known format. You can also pass `-` for stdin. RTF exports are converted to plain text automatically.

**Text reports (examples):** Apple `.crash`, iOS freeze/Mach exports, Android logcat/tombstone, Harmony `Stacktrace:` / `Tid:` dumps, native `#NN pc` stacks.

**JSON exports:**

| Platform / shape | `log_format` (in `01` report) |
|------------------|-------------------------------|
| Harmony crash platform (`crashDiagnosis:` / `crashDiagnsis:` + JSON, incl. `#NN pc` in `body.stacks`) | `harmony_crash_diagnosis_json` |
| [Sentry](https://sentry.io/) event JSON | `sentry_event_json` |
| [Firebase Crashlytics](https://firebase.google.com/docs/crashlytics) event JSON | `firebase_crashlytics_json` |
| [Bugsnag](https://www.bugsnag.com/) event JSON | `bugsnag_event_json` |
| Other dashboards (Bugly-like, custom APM) with `frames` / `stack_frames` arrays | `generic_json_stack_export` |

Full matrix, parser priority, and how to add adapters: **[docs/tools/CRASH_LOG_FORMATS.md](./docs/tools/CRASH_LOG_FORMATS.md)**

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

Then enter `设置` -> `配置大模型` / `配置堆栈地址解析工具`. Checks and guidance run contextually in flow. For stack symbolization: **Auto-detect (recommended)** and **Manually set absolute path to the symbolizer** (executable or directory containing it). When you choose **Quick start** and the run needs symbolization, the CLI also tries the same silent auto-write as **Auto-detect** first to avoid repeating setup.

Default local config directory:

```bash
~/.config/stability-analysis-agent/
```

- `agent_config.local.json` for LLM vendor selection (`active_provider` key), credentials, and model
- `add2line_resolver_config.local.json` for symbolizer search paths (`tool_paths` = directories; optional `environment_vars` for toolchain roots such as NDK, often filled by auto-detect)

If you prefer manual editing, edit these files directly in that directory.

### Advanced: add2line config override

You can override add2line config file location via environment variable:

```bash
export STABILITY_AGENT_ADD2LINE_CONFIG_FILE="/abs/path/add2line_resolver_config.local.json"
```

## Project Structure

```
stability-analysis-agent/
├── agent/              # Auto-Fix core engine (LangGraph state machine)
├── cli/                # CLI entry point
├── daemon/             # HTTP daemon (streaming, SSE)
├── tools/              # Tool implementations (parser, resolver, code provider)
│   └── configs/        # Configuration templates
├── tool_system/        # Tool + Workflow registration & dispatch framework
├── extensions/         # User-pluggable Tool / Workflow drop-in (auto-discovered)
│   ├── tools/          #   Tool examples (extensions/tools/example_tool.py)
│   └── workflows/      #   Workflow examples (extensions/workflows/example_workflow.py)
├── skill_system/       # Skill discovery, install, runtime bridge (CLI subcommands)
│   ├── cli.py          # `sa-agent skill …` argparse subparser
│   ├── manager.py      # SkillManager: discover / install / lint / register
│   ├── runtime.py      # SkillRuntime: render prompt / execute workflow / execute tool
│   ├── templates.py    # `available_skill_presets()` (3 presets) + scaffold writer
│   ├── models.py       # SkillBundle / SkillExport / SkillRunResult dataclasses
│   └── parser.py       # `SKILL.md` + `skill.json` parser
├── workflows/          # Workflow definitions (crash auto-fix)
├── rag/                # RAG: rule store + vector index (ChromaDB) + metadata
├── prompts/            # Prompt templates for LLM auto-fix
├── protocol/           # Unified request/response protocol
├── examples/           # Bundled crash cases
│   └── crash_cases/
│       ├── demo_basic/         # NullPtr, DivZero, Abort, DoubleFree, etc.
│       └── demo_multithread/   # Race condition, deadlock, atomic failure, etc.
├── test/               # Test suite
├── stability-analysis-agent-skill/  # External agent skill pack (Claude / Cursor)
└── docs/               # Documentation
```

## Documentation

| Topic | Link |
|-------|------|
| CLI Guide | [docs/cli/CLI_GUIDE.md](./docs/cli/CLI_GUIDE.md) |
| CLI Commands Reference | [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md) |
| Daemon Server Guide | [docs/cli/DAEMON_SERVER_GUIDE.md](./docs/cli/DAEMON_SERVER_GUIDE.md) |
| External Agent Skill Pack | [stability-analysis-agent-skill/](./stability-analysis-agent-skill/) |
| Skill System (sa-agent runtime) | [docs/skills/README.md](./docs/skills/README.md) |
| Closed-Loop Skill Templates | [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md) |
| Bug Platform Fetcher Template | [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md) |
| Skill Template Reference | [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md) |
| PyPI Release Scripts | [docs/scripts/PYPI_RELEASE_SCRIPTS.md](./docs/scripts/PYPI_RELEASE_SCRIPTS.md) |
| Roadmap (long-form) | [docs/ROADMAP.md](./docs/ROADMAP.md) |
| System Architecture | [docs/architecture/README.md](./docs/architecture/README.md) |
| Architecture Diagram | [docs/architecture/ARCHITECTURE_DIAGRAM.md](./docs/architecture/ARCHITECTURE_DIAGRAM.md) |
| Tool System Overview | [docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md](./docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md) |
| Tool Extension Guide | [docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md) |
| Workflow System | [docs/workflows/WORKFLOWS.md](./docs/workflows/WORKFLOWS.md) |
| RAG Vector Database | [docs/rag/README.md](./docs/rag/README.md) |
| Crash Demos | [docs/crash_cases/README.md](./docs/crash_cases/README.md) |
| Crash log formats & platforms | [docs/tools/CRASH_LOG_FORMATS.md](./docs/tools/CRASH_LOG_FORMATS.md) |

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

# Skill System (parser, install, lint, runtime bridge, preset guardrails)
python3 test/skill_system/test_skill_system.py

# CLI report-paths helpers + extensions/ auto-discovery
python3 test/cli/test_report_paths.py
```

## FAQ

**Q: What does "auto-fix" actually mean — does the Agent push to `main` and ship without me?**
No. In this repo, "auto-fix" means: `parse → symbolize → read source → propose a patch → apply locally with backup`. After that, control returns to you or to the closed-loop Skills (verify / package). The Agent does **not** open PRs, merge to main, or bypass code review. The Auto-Fix Loop is open, not headless. ([#why-we-are-not-another-ai-coding-tool](#why-we-are-not-another-ai-coding-tool) has the same disclaimer.)

**Q: Symbolization failed?**
Ensure `--library-dir` contains the binary files (`.dylib` / `.so`) along with their debug symbols (`.dSYM` directories or DWARF info). In interactive mode, use **Settings → Configure stack symbolization tools** with **Auto-detect** or **Manually set absolute path to the symbolizer** (executable or directory). You can also edit `~/.config/stability-analysis-agent/add2line_resolver_config.local.json` (see `tools/configs/add2line_resolver_config.local.example.json`).

**Q: The LLM step failed (or I have no LLM key). Can I still use `sa-agent`?**
Yes. Use `--scope gen_prompt_only` to run the full toolchain (parse + symbolize + read source) without calling the LLM. The structured JSON output is useful on its own — paste it into a chat or hand it to a human reviewer.

**Q: Code context extraction returns empty?**
Ensure `--code-root` points to the source directory that contains the files listed in the symbolized stack trace.

**Q: Will ANR / OOM / Freeze be supported soon?**
They sit on the same framework as Crash auto-fix; each gets its own dedicated workflow as it matures. See [Roadmap](#roadmap) and [docs/ROADMAP.md](./docs/ROADMAP.md). Crash ships first because the toolchain (parse + symbolize + LLM patch) is already production-grade there.

**Q: How do I use this from Claude Code or Cursor?**
Install the Python package (`pip install stability-analysis-agent`), then copy [`stability-analysis-agent-skill/`](./stability-analysis-agent-skill/) into your agent's skill directory (e.g. `~/.claude/skills/stability-analysis-agent`). See [Use with External AI Agents](#use-with-external-ai-agents-claude--cursor) above.

**Q: How do I add my own Skill (custom verify step, custom CI, custom bug-tracker, etc.) inside `sa-agent`?**
Use `sa-agent skill init <name> ./<dir> --preset bug-platform-fetcher|automation-testing|cicd-pipeline` to scaffold a closed-loop starter, or `sa-agent skill init <name> ./<dir>` for a blank prompt/workflow/tool skill. After filling in your logic, install it with `sa-agent skill install ./<dir>` and verify with `sa-agent skill list` / `sa-agent skill show <name>`. For skills that should plug into the executor, declare `entrypoint` and `exports` in `skill.json` — see [Skill System](#skill-system-sa-agent-runtime-extensions) above and [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md).

**Q: Does `sa-agent` call Jira / iCafe / WorkTile / 飞书 APIs out of the box?**
No. The `bug-platform-fetcher` Skill preset is an **empty skeleton**. It defines the contract (what to download, what JSON shape to return) but ships zero platform-specific code. You bring your own integration in a private package — see [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md).

**Q: Where does this fit between `sa-agent` and `bd-sa-agent`?**
This repo (`stability-analysis-agent`) is the open-source core (framework + Crash auto-fix + Skill plugins). The closed-source `bd-sa-agent` is an enterprise-specific wrapper that adds internal LLM providers, internal bug-tracker backends, and a packaged binary release. The closed-loop Skill system was designed so that **any** such integration can be built as a Skill on top of this open core — without forking it.

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](./CONTRIBUTING.md) before submitting a PR.

```bash
# All commits require DCO sign-off
git commit -s -m "feat: describe your change"
```

The most useful first PRs are usually:

- A new **bug-platform-fetcher** adapter for your team's tracker (open it as `extensions/bug-platform/<vendor>-fetcher/` and submit it upstream if your team's tracker is broadly useful).
- A new **automation-testing** preset for your test runner (pytest / XCTest / GTest / Hypium / adb shell).
- A new **crash log format** adapter for an APM you ship to (`tools/crash_log_parser/` — see [docs/tools/CRASH_LOG_FORMATS.md](./docs/tools/CRASH_LOG_FORMATS.md)).
- A new **stability class auto-fix** (ANR / OOM / Freeze) — most ambitious, see [Roadmap](#roadmap).

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
  If this project helped you auto-fix even one crash, consider giving it a <b>Star</b> — it helps other teams find us.<br>
  📣 Star <b>and</b> file an Issue if you'd like to see a stability class supported (ANR, OOM, Freeze) or a platform wired in (Sentry, Bugsnag, ADO, Linear, Jira Cloud, 飞书, 自建…) — that signal shapes the [roadmap](./docs/ROADMAP.md).
</p>
