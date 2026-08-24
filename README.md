<h1 align="center">Stability Analysis Agent</h1>
<p align="center">
  <strong>🐛 When your app crashes <em>or freezes</em>, sa-agent turns the log into evidence — then into a fix.</strong><br>
  <sub>An open-source Agent for <b>app-stability repair</b>. Deterministic toolchain first (registers · ANR · memory · business path), LLM patch second. <b>Crash auto-fix ships today</b>; ANR / OOM / Freeze analysis already runs on the same pipeline.</sub>
</p>
<p align="center">
  <a href="https://pypi.org/project/stability-analysis-agent/"><img src="https://img.shields.io/pypi/v/stability-analysis-agent.svg" alt="PyPI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python"></a>
  <a href="https://pypi.org/project/stability-analysis-agent/#files"><img src="https://img.shields.io/badge/wheel-py3--none--any-success.svg" alt="Wheel"></a>
  <a href="./CONTRIBUTING.md"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"></a>
  <a href="./CHANGELOG.md"><img src="https://img.shields.io/badge/Maintained-yes-success.svg" alt="Maintained"></a>
  <a href="./stability-analysis-agent-skill/"><img src="https://img.shields.io/badge/skills-claude%20code%20%7C%20cursor-purple.svg" alt="Skill Pack"></a>
</p>
<p align="center">
  <b>English</b> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <sub>
    <b>Maintenance:</b> actively maintained ·
    <b>latest:</b> <a href="https://pypi.org/project/stability-analysis-agent/1.3.3/">v1.3.3</a>
    (daemon graceful shutdown, bounded SSE queues, finished-run TTL) ·
    meaningful changes → <a href="https://github.com/baidu-maps/stability-analysis-agent/releases">GitHub Releases</a>
    (aim for about monthly when there is substance; not a calendar SLA) ·
    see <a href="./CHANGELOG.md">CHANGELOG</a>
  </sub>
</p>

---

### What this is — and what isn't

`Stability Analysis Agent` is an **app-stability repair framework**. It treats
crashes, ANRs, OOMs, freezes, memory pressure, watchdog kills — every class of
stability problem — as a first-class analysis (and eventually repair) target.

It does **not** ship generic-prompt tooling. The Agent reads a real crash /
AppFreeze / ANR log, runs the native toolchain (`addr2line` / `atos`),
builds a **deterministic evidence chain** (PC → symbols → optional disassembly →
registers → ANR hotspots / EventHandler → memory clues → pre-crash business path),
then — for Crash — **auto-fixes** the offending code with backup. Verify /
package / ship stay in the closed-loop Skills.

It does **not** claim every stability class is fully auto-fixed yet.
**Crash auto-fix is production-ready.** ANR / Freeze / memory-pressure /
timeline diagnosis already land as structured reports (`04a`–`04e`) and feed
the LLM prompt — dedicated auto-fix workflows for those classes keep maturing
under the same framework, not as a separate “v2 product”.

## Why we are not another AI coding tool

| | Cursor / Copilot / Claude Code | Stability Analysis Agent |
|---|---|---|
| **What it does to a crash / ANR log** | Reads it like any other text — analyzes, sometimes suggests a fix | **Tool-first**: parse → symbolize → evidence compass → (Crash) patch + apply with backup |
| **Native toolchain (`addr2line` / `atos`)** | Cannot run them | First-class — addresses resolve *before* any LLM call |
| **Registers / fault address / near-null** | Guess from the log text | Deterministic register & fault-pattern diagnosis (`04a`) |
| **ANR / AppFreeze / freeze** | Paste the traces and hope | Dedicated ANR workflow: hotspots, EventHandler queue, IPC hints (`04c`) |
| **“What was the user doing?”** | Manual logcat archaeology | Pre-crash timeline + business-path extraction (`04e`) |
| **Knowledge accumulation** | Stateless across conversations | RAG rule table + vector DB, patterns improve over time |
| **Multi-step reasoning** | Single prompt, one shot | LangGraph state machine — Agent can request more context and re-invoke tools |
| **Bug-tracker integration** | None out of the box | `bug-platform-fetcher` Skill wires in any ticket system |
| **Verifies the fix before shipping** | ❌ you stop at "looks right" | ✅ `automation-testing` Skill preset runs your test runner |
| **Ship automation** | ❌ | ✅ `cicd-pipeline` Skill preset drives build/sign/publish |
| **End-to-end auto-fix loop** | ❌ | ✅ Ticket → auto-fix → verify → ship, all on one Python package |
| **Extensibility** | Prompt only | Tool + Workflow + Skill system, with `extensions/` for local plugins |

> **Scope of "auto-fix"**, in this repo:
> `parse → symbolize → extract code context → propose patch → apply locally with backup`. After that the Agent **hands off** to a human or to the closed-loop Skills (verify / package) — it does not push to `main`, open PRs, or bypass code review. The Loop is open, not headless.

### What you get today

| Layer | Status | What you can run |
|-------|--------|------------------|
| **Crash auto-fix** | ✅ GA | Null deref, abort, double-free, races, stack overflow, … → patch + apply |
| **Crash evidence diagnosis** | ✅ GA | Registers, maps, optional PC disassembly, evidence compass (`04a`) |
| **ANR / AppFreeze / freeze analysis** | ✅ GA (analysis) | Auto-route to `anr_freeze_analysis`; hotspots + EventHandler (`04c`) |
| **Memory pressure / OOM clues** | ✅ GA (sidepath) | Log-side RSS/PSS/heap hints + fault-mode match (`04d`); heap-diff auto-fix still planned |
| **Pre-crash business path** | ✅ GA (sidepath) | logcat / HiLog / ASI timeline → lifecycle & click path (`04e`) |
| **ANR / OOM / Freeze auto-fix** | 🚧 maturing | Same framework; patch workflows land class-by-class |

[Full roadmap →](#roadmap)

## Closed-Loop Workflow

The three Skill presets shipped in `v1.2.8` (`bug-platform-fetcher`,
`automation-testing`, `cicd-pipeline`) are the **backbone** of Crash-fix
auto-repair — not a collection of disconnected features. Wire them together
with `sa-agent`, and you get an end-to-end stability engineering loop.

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
| Diagnose ANR / AppFreeze without `.so` or an API key | `--scope parse_stack_only` → see [Evidence-driven diagnosis](#evidence-driven-diagnosis-why-developers-stick-around) |
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

- 🙋 **"I just want to auto-fix a crash log."** → [Quick Start](#quick-start) — 60 seconds, no LLM key required for diagnosis.
- 🔬 **"I want registers / ANR / business path without calling the LLM."** → [Evidence-driven diagnosis](#evidence-driven-diagnosis-why-developers-stick-around).
- 🖥 **"I want a browser UI for one-click full fix (local dev)."** → [Local Web UI](#local-web-ui).
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

### Local Web UI

A **local browser shell** for developers who already have `library_dir` and `code_root` on disk. It is an internal/open-source convenience layer — not the final enterprise “upload log only” product.

```bash
# From a source checkout (or after pip install with daemon entry)
python3 daemon/server.py --host 127.0.0.1 --port 8765
open http://127.0.0.1:8765/
```

| Area | What it does |
|------|----------------|
| **Main** | Paste a crash log path or full log → **Run full pipeline fix** (`scope=full`, `apply_ai_fixes=true`, same reports as CLI) |
| **Sidebar · Workspace** | Save `library_dir` + `code_roots` to `~/.config/stability-analysis-agent/web_preferences.json` |
| **Sidebar · Installed Skills** | List skills under `~/.config/.../skills`; enable/disable; install from a local path or zip |
| **After a successful fix** | Optional **Save to vector DB** card (`POST /runs/<id>/vector-db/commit`); CLI prompts the same choice (default: skip) |

Parameter sweeps (`gen_prompt_only`, `parse_stack_only`, …) stay on the CLI. See [Web UI Guide](./docs/cli/WEB_UI_GUIDE.md) and [Daemon Server Guide](./docs/cli/DAEMON_SERVER_GUIDE.md).

## Key Features

| Feature | Description |
|---------|-------------|
| **End-to-End Auto-Fix Loop** | Crash → auto-fix (parse + symbolize + patch + apply) → verify → ship, stitched from Skill presets |
| **Three-Level Root Cause Library** | 68 fault-mode rules with L1→L2→L3 classification (type → mechanism → specific cause); deterministic matching *before* any LLM call |
| **Evidence Grading (Tier 1–5)** | Every conclusion carries a confidence label: detector report (HIGH) > register+address (HIGH) > multi-stack (MEDIUM) > single feature (LOW) > speculation (LOW) |
| **Signal Sub-Code Semantics** | SEGV_MAPERR, SEGV_ACCERR, BUS_ADRALN, FPE_INTDIV, ILL_ILLOPC — 20+ sub-codes decoded to human-readable root-cause hints at parse time |
| **Crash Address Pattern Analysis** | Near-zero → null pointer; 0x6b6b → UAF (freed-memory fill); 0xDEADBEEF → debug poison; stack/heap region classification |
| **Register Correlation** | Extracts ARM64/ARM32/x86_64 register dumps; detects NULL registers, UAF patterns, crash-address matches |
| **Stack Layer Classification** | Separates crash frame / first non-runtime / first app frame — prevents system-frame misattribution |
| **Selective Knowledge Loading** | Module→knowledge-domain routing (14 mappings); RAG only searches relevant patterns, reducing noise |
| **Deterministic Pre-Analysis** | `null_pointer` / `abort` / `divide_by_zero` / `stack_overflow` / `ASan report` confirmed with 100% confidence *before* LLM |
| **Responsibility Attribution** | Per-platform path rules (Android/iOS/HarmonyOS/macOS/Linux) classify modules as application / system / vendor / third-party |
| **Business Flow Analysis** | Pre-crash logcat/HiLog/syslog → operation path inference (lifecycle → network → database → user_action → crash) |
| **EventHandler + Binder Chain** | ANR queue-depth analysis + IPC call-graph traversal + deadlock-cycle detection |
| **Stack Hotspot Statistics** | Function frequency counting, blocking-indicator detection (mutex/futex/IO), repeated call-pattern discovery |
| **Optional Disassembly** | `llvm-objdump` / `objdump` wrapper — PC-nearby instructions, access direction, involved registers (only when binary provided) |
| **Structured Report Schema** | 7-section output format enforced on LLM: fault info → 3-level root cause → evidence chain → confidence → responsibility → fix → follow-up |
| **Crash + ANR on one CLI** | Auto `log_kind` routing: Crash → `crash_analysis`; AppFreeze / ANR traces → `anr_freeze_analysis`; mixed cases handled by confidence-based primary/secondary |
| **Address Symbolization** | `addr2line` / `atos` resolve raw addresses to function + line *before* any LLM call |
| **Structured Log Parsing** | iOS / Android / macOS / Linux / Windows / Harmony; Crash · ANR · OOM · Freeze classification |
| **RAG Knowledge Base** | Rule table (fast path) + vector retrieval (ChromaDB); after a successful apply-fix, **opt-in** write to the local vector store (CLI confirm / Web button; not automatic) |
| **Tool + Workflow + Skill** | Pluggable tools/workflows + Claude-style skills + `extensions/` drop-ins |
| **External Agent Skill Pack** | Teach Claude Code / Cursor to call `sa-agent` correctly |
| **Multiple Interfaces** | CLI, HTTP Daemon (SSE), Local Web UI, Python API |

### Evidence-driven diagnosis (why developers stick around)

Paste a log. Get **structured reports**, not a wall of model prose. Even with
`--scope parse_stack_only` and **no library / no LLM key**, you still get
actionable JSON:

| Report | What it answers |
|--------|-----------------|
| `01` parse | Signal + sub-code semantics, threads, `log_kind` (crash / app_freeze / anr_trace / oom…), **address pattern analysis** |
| `02` maps | Memory map / module layout when present |
| `03` symbolize | Function + file:line (or skipped cleanly without `.so`), **stack layer classification** (crash frame / non-runtime / app frame) |
| **`04a` crash diagnosis** | **Three-level root cause** (L1→L2→L3), fault pattern, **registers**, near-null, optional **disassembly**, **evidence compass** (PC → symbol → insn → reg), **deterministic facts**, **evidence grade (Tier 1–5)** |
| **`04c` ANR / Freeze** | Stack hotspots, **EventHandler** queue (incl. Harmony AppFreeze dump), **Binder/IPC chain** (deadlock detection), blocking indicators |
| **`04d` memory pressure** | RSS/PSS/heap/FD clues + leak-mode keyword match (sidepath / `--force-memory-analysis`) |
| **`04e` business path** | Pre-crash logcat / HiLog / ASI timeline → **lifecycle & operation path inference** (*what the user was doing*) |
| **`04c`/`04f`/`04g`/`04h` sidecars** | Native crash hints + stack layering · AppFreeze Binder/system-stress · API error-code knowledge · JS/ArkTS fault modes |
| `05` RAG memory | **Fault-mode library match** (68 rules) + evidence grade + knowledge-domain routing + similar patterns |
| `06` / `07` | Structured 7-section report (when LLM engaged): fault info → 3-level root cause → evidence chain → confidence → responsibility → fix → follow-up |

```bash
# Crash evidence only — no code-root, no API key
sa-agent --crash-log ./app.crash --library-dir ./lib --scope parse_stack_only

# Harmony AppFreeze / Android ANR — no .so required
sa-agent --crash-log ./appfreeze.txt --scope parse_stack_only

# Force memory / timeline sidepaths on a rich dump
sa-agent --crash-log ./crashInfos.txt --scope parse_stack_only \
  --force-memory-analysis --force-timeline-analysis
```

Design notes: [docs/architecture/fault_mode_library.md](./docs/architecture/fault_mode_library.md) ·
CLI report layout: [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md).

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

**Diagnosis + Auto-Fix Pipeline:**

```
Crash / ANR / AppFreeze Log
        │
        ▼
   Parse (01) ──log_kind──▶ crash_analysis  or  anr_freeze_analysis
        │          │
        │          ├─ Signal sub-code semantics (SEGV_MAPERR → UAF/OOB hint)
        │          └─ Address pattern analysis (near-zero / 0x6b / poison)
        │
        ▼
   Maps (02) → Symbolize (03) → Stack Layer Classification
        │                          (crash_frame / non_runtime / app_frame)
        │
        ├─▶ 04a evidence  (registers · fault analysis · disasm · evidence compass)
        │       ├─ Deterministic Analyzer (null ptr / abort / SIGFPE = 100% facts)
        │       ├─ Three-Level Fault Mode Matcher (68 rules: L1→L2→L3)
        │       ├─ Evidence Grader (Tier 1–5, HIGH/MEDIUM/LOW)
        │       └─ Responsibility Attribution (app / system / vendor / 3rd-party)
        │
        ├─▶ 04c ANR       (hotspots · EventHandler queue · Binder/IPC deadlock)
        ├─▶ 04d memory    (pressure / OOM clues · leak fault-modes)  [sidepath]
        └─▶ 04e timeline  (business-path inference from logcat/HiLog) [sidepath]
        │
        ▼
   Code context (04b) → RAG (05: selective knowledge routing) → LLM (06: 7-section report) → Apply (07)
                              ▲
                              └── request more context (context_loop)
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

After that, ask your agent to auto-fix a crash with Stability Analysis Agent — it should propose `sa-agent` commands, pick the right `--scope`, and read `reports/<timestamp>/` outputs.

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

**Release cadence:** the project is **actively maintained**. We publish a PyPI / GitHub Release when there is a meaningful batch of fixes or features — typically on the order of **about once a month** when the tree is moving, and quieter when it is not. We do **not** promise a fixed calendar date. Track progress in [`CHANGELOG.md`](./CHANGELOG.md) and [Releases](https://github.com/baidu-maps/stability-analysis-agent/releases); PR review expectations are in [`CONTRIBUTING.md`](./CONTRIBUTING.md).

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
| ANR / AppFreeze / freeze **analysis** (hotspots, EventHandler, IPC) | ✅ GA | v1.3.0 |
| Memory-pressure / OOM **clues** (log-side `04d`) | ✅ GA (sidepath) | v1.3.0 |
| Pre-crash business-path / timeline (`04e`) | ✅ GA (sidepath) | v1.3.0 |
| Crash evidence compass + registers + optional disassembly (`04a`) | ✅ GA | v1.3.0 |
| ANR / Freeze **auto-fix** (patch apply) | 🚧 maturing | next minors |
| OOM / memory **auto-fix** (heap snapshot diff) | 📋 planned | next minors |
| **Community presets** | | |
| engine-build, iOS-XCUITest, Hypium (more Skill presets) | 🎯 community-driven | open |
| Sentry, Bugsnag, Azure DevOps, Linear, Jira Cloud, 飞书 (more bug platforms) | 🎯 community-driven | open |

For the long-form version (deferred ideas, RFCs, design notes), see [docs/ROADMAP.md](./docs/ROADMAP.md).

## Compatible Platforms & Runtimes

| Layer | Supported |
|---|---|
| **Stability class (auto-fix)** | Crash — null pointer, div-zero, abort, double free, deadlocks / races / atomics, stack overflow, … |
| **Stability class (analysis)** | ANR / AppFreeze / freeze · memory pressure / OOM clues · pre-crash business path · register / disasm evidence |
| **Stability class (planned auto-fix)** | ANR patch · heap-diff OOM fix · deeper Freeze auto-repair |
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
| `full` (default) | Parse + maps + symbolize + diagnosis family (`04a` + conditional `04c`/`04d`/`04e`) + code + LLM auto-fix. |
| `gen_prompt_only` | Same toolchain through prompt file; skip LLM. |
| `parse_stack_only` | Parse + maps + symbolize + diagnosis (`04a` / ANR `04c` …). No `--code-root` / LLM. Ideal for ANR dumps without `.so`. |
| `parse_log_only` | Parse only (`01`, incl. `log_kind`). |

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

The daemon provides **streaming output (SSE)**, **process reuse** (no cold start), **task cancellation**, and hosts the **local Web UI** — ideal for IDE integration, the browser shell, and high-frequency analysis:

```bash
# Start the daemon
sa-agent --daemon-server --host 127.0.0.1 --port 8765
# or: python3 daemon/server.py

# Open the local Web UI
open http://127.0.0.1:8765/

# Analyze via daemon (CLI)
sa-agent --daemon http://127.0.0.1:8765 \
  --crash-log <crash-log> --library-dir <lib-dir> --code-root <code-root>
```

> See [Daemon Server Guide](./docs/cli/DAEMON_SERVER_GUIDE.md) and [Web UI Guide](./docs/cli/WEB_UI_GUIDE.md).

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

Default local config directory (installed CLI):

```bash
~/.config/stability-analysis-agent/
```

- `agent_config.local.json` for LLM vendor selection (`active_provider` key), credentials, and model
- `add2line_resolver_config.local.json` for symbolizer search paths (`tool_paths` = directories; optional `environment_vars` for toolchain roots such as NDK, often filled by auto-detect)

Templates live in the repo under [`configs/`](./configs/) (e.g. `agent_config.local.example.json`). For editable checkouts, the loader prefers `STABILITY_AGENT_CONFIG_DIR` if set, otherwise `<repo>/configs/agent_config.local.json`. Do not commit `*.local.json` with real keys.

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
├── daemon/             # HTTP daemon (streaming, SSE, Web UI host, web preferences)
├── web/                # Local Web UI static assets (one-click full fix + workspace + Skills)
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
├── test/               # Test suite (cli / daemon / web / rag / ai_regression / …)
├── .github/workflows/  # CI, optional AI regression, PyPI publish
├── .devcontainer/      # Codespaces / VS Code Dev Container (lightweight, no full [rag])
├── stability-analysis-agent-skill/  # External agent skill pack (Claude / Cursor)
└── docs/               # Documentation
```

## Documentation

| Topic | Link |
|-------|------|
| CLI Guide | [docs/cli/CLI_GUIDE.md](./docs/cli/CLI_GUIDE.md) |
| CLI Commands Reference | [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md) |
| Daemon Server Guide | [docs/cli/DAEMON_SERVER_GUIDE.md](./docs/cli/DAEMON_SERVER_GUIDE.md) |
| Local Web UI | [docs/cli/WEB_UI_GUIDE.md](./docs/cli/WEB_UI_GUIDE.md) |
| Testing (unit, AI regression, Web/Daemon, CI) | [docs/testing/README.md](./docs/testing/README.md) |
| PyPI Release (scripts + GitHub Actions) | [docs/scripts/PYPI_RELEASE_SCRIPTS.md](./docs/scripts/PYPI_RELEASE_SCRIPTS.md) |
| Codespaces / Dev Container | [.devcontainer/README.md](./.devcontainer/README.md) |
| External Agent Skill Pack | [stability-analysis-agent-skill/](./stability-analysis-agent-skill/) |
| Skill System (sa-agent runtime) | [docs/skills/README.md](./docs/skills/README.md) |
| Closed-Loop Skill Templates | [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md) |
| Bug Platform Fetcher Template | [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md) |
| Skill Template Reference | [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md) |
| AI code regression (redirect) | [docs/testing/AI_REGRESSION.md](./docs/testing/AI_REGRESSION.md) |
| Roadmap (long-form) | [docs/ROADMAP.md](./docs/ROADMAP.md) |
| System Architecture | [docs/architecture/README.md](./docs/architecture/README.md) |
| Architecture Diagram | [docs/architecture/ARCHITECTURE_DIAGRAM.md](./docs/architecture/ARCHITECTURE_DIAGRAM.md) |
| Fault modes · evidence · ANR / memory / timeline | [docs/architecture/fault_mode_library.md](./docs/architecture/fault_mode_library.md) |
| Tool System Overview | [docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md](./docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md) |
| Tool Extension Guide | [docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md) |
| Workflow System | [docs/workflows/WORKFLOWS.md](./docs/workflows/WORKFLOWS.md) |
| RAG Vector Database | [docs/rag/README.md](./docs/rag/README.md) |
| Crash Demos | [docs/crash_cases/README.md](./docs/crash_cases/README.md) |
| Crash log formats & platforms | [docs/tools/CRASH_LOG_FORMATS.md](./docs/tools/CRASH_LOG_FORMATS.md) |

## Testing

Full guide: **[docs/testing/README.md](./docs/testing/README.md)** (unit tests, AI regression, Web/Daemon contracts, GitHub Actions).

**Pre-commit (no LLM)** — same suite as [`.github/workflows/ci.yml`](./.github/workflows/ci.yml):

```bash
python3 -B -m unittest \
  test.ai_regression.test_runner \
  test.cli.test_report_paths \
  test.cli.test_vector_db_commit_prompt \
  test.rag.test_case_writer \
  test.daemon.test_build_cli_cmd \
  test.daemon.test_skills_api \
  test.daemon.test_run_lifecycle \
  test.daemon.test_vector_db_commit_api \
  test.daemon.test_web_preferences \
  test.skill_system.test_installed_skills_runtime \
  test.web.test_web_contract
```

**GitHub Actions:**

| Workflow | When | What |
|----------|------|------|
| [CI](./.github/workflows/ci.yml) | PR / push to `main`/`master` | Deterministic suite (Python matrix) + toolchain spot checks |
| [AI Regression](./.github/workflows/ai-regression.yml) | Manual run, or PR label `ai-regression` | Real LLM repair regression (needs API secret) |
| [Publish PyPI](./.github/workflows/publish-pypi.yml) | Tag `v*`, or manual | Build + Trusted Publishing upload (gated by the deterministic suite) |

**Codespaces:** [`.devcontainer/`](./.devcontainer/) — `pip install -e ".[test]"` on create; open via **Code → Codespaces**.

**Spot checks:**

```bash
python3 test/tool_system/test_regression.py
python3 test/skill_system/test_skill_system.py
python3 test/llm/test_llm_connection.py --provider openai   # needs API key
```

**Release (real LLM, code regression):**

```bash
python3 scripts/run_ai_regression.py --case test/ai_regression/cases/demo_basic_nullptr.json
# After Web/daemon changes, also:
python3 scripts/run_ai_regression.py --case test/ai_regression/cases/demo_basic_nullptr.json --entrypoint daemon
```

See also [docs/testing/AI_REGRESSION.md](./docs/testing/AI_REGRESSION.md) and [docs/testing/WEB_DAEMON_TESTS.md](./docs/testing/WEB_DAEMON_TESTS.md).

## FAQ

**Q: What does "auto-fix" actually mean — does the Agent push to `main` and ship without me?**
No. In this repo, "auto-fix" means: `parse → symbolize → read source → propose a patch → apply locally with backup`. After that, control returns to you or to the closed-loop Skills (verify / package). The Agent does **not** open PRs, merge to main, or bypass code review. The Auto-Fix Loop is open, not headless. ([#why-we-are-not-another-ai-coding-tool](#why-we-are-not-another-ai-coding-tool) has the same disclaimer.)

**Q: Symbolization failed?**
Ensure `--library-dir` contains the binary files (`.dylib` / `.so`) along with their debug symbols (`.dSYM` directories or DWARF info). In interactive mode, use **Settings → Configure stack symbolization tools** with **Auto-detect** or **Manually set absolute path to the symbolizer** (executable or directory). You can also edit `~/.config/stability-analysis-agent/add2line_resolver_config.local.json` (see `configs/add2line_resolver_config.local.example.json`).

**Q: The LLM step failed (or I have no LLM key). Can I still use `sa-agent`?**
Yes. Use `--scope gen_prompt_only` for a reusable prompt, or `--scope parse_stack_only` for diagnosis-only JSON (`04a` / `04c` / …) with **zero** API key. Structured reports are useful on their own — paste into a chat or hand to a reviewer.

**Q: Code context extraction returns empty?**
Ensure `--code-root` points to the source directory that contains the files listed in the symbolized stack trace.

**Q: Do you support ANR / OOM / Freeze?**
**Analysis: yes.** AppFreeze / Android ANR traces auto-route to the ANR workflow (`04c`); memory-pressure and pre-crash timeline are sidepaths (`04d` / `04e`). **Auto-fix patches** for those classes are still maturing — Crash auto-fix is what ships as GA today. See [What you get today](#what-you-get-today) and [Roadmap](#roadmap).

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
  📣 Star <b>and</b> file an Issue if you want deeper ANR/OOM auto-fix, a new APM adapter, or a bug-platform Skill (Sentry, Bugsnag, ADO, Linear, Jira Cloud, 飞书, 自建…) — that signal shapes the [roadmap](./docs/ROADMAP.md).
</p>
