# Stability Analysis Agent

**An open-source domain Agent for app stability engineering: built on a governed Agent Harness and informed by open-source AI coding tools' context management, multi-round investigation, tool invocation, permission control, checkpoint recovery, and verification-loop practices, it turns Crash, ANR, and OOM incident materials into auditable, verifiable root-cause analyses and code-fix results.**

[简体中文](./README.md) | **English**

[![PyPI](https://img.shields.io/pypi/v/stability-analysis-agent.svg)](https://pypi.org/project/stability-analysis-agent/)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](./LICENSE)

## Why Stability Problems Need a Dedicated Agent

Stability logs are noisy, address-heavy, and spread across disconnected sources. General-purpose AI coding tools usually need a developer to organize the incident materials before they can reason about them. A dedicated Agent turns those materials into usable diagnostic evidence.

| Stability problem | Limitation of general-purpose AI coding tools | What this Agent provides |
|---|---|---|
| Noisy logs hide the important thread and call stack | Developers must manually filter the relevant crash information | Structured parsing of exception type, crash thread, and key frames |
| Native stacks contain memory addresses only | The model cannot infer a function or source location from an address alone | Address symbolization using matching symbol files |
| The crash location is not always the root cause | Surface-level stack reading leads to guesses | A Crash evidence chain built from the fault address, registers, and call relationships |
| Logs, symbols, and source are separate | Developers must collect and paste context by hand | Automatic correlation and source-context extraction |
| Similar failures happen repeatedly | Analysis knowledge is lost between conversations | Rules and vector-memory retrieval for similar cases |

The project also adopts engineering practices from open-source AI coding tools: multi-round context retrieval, structured tool calls, context budgeting, permission controls, checkpoint recovery, failure feedback, and verification. These capabilities are constrained by the stability-analysis workflow; this is not an unrestricted autonomous coding agent.

## Installation

### Recommended

Requires Python 3.9+. Python 3.10-3.12 is recommended. The default installation includes similar-case retrieval through RAG.

```bash
pip install stability-analysis-agent
```

For mainland China network environments, add a pip mirror to the command. For installation failures, Python versions, SSL, and ML dependency issues, see [Installation and Dependency Troubleshooting](./docs/cli/INSTALL_TROUBLESHOOTING.md).

## Quick Start

### Run the Bundled Demo

Clone the repository, install it, and start the interactive CLI. Choose `快速开始修复（推荐）` (Quick Start Fix), follow the prompts to configure an LLM and a stack symbolizer, and enter the bundled Demo paths. The flow parses the log, symbolizes the stack, builds the evidence chain, extracts source context, asks the AI to analyze the issue, and can apply a source fix subject to review, approval, and verification.

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
pip install -e .
sa-agent
```

When the CLI starts, focus on this menu path for the first run:

```text
Select an operation
> 1) 快速开始修复（推荐）
  2) 设置
  3) 帮助
  q) 退出
```

Then follow this flow:

```text
1) 快速开始修复（推荐）
  -> If no model is configured, enter 大模型与路由设置
  -> If no symbolizer is found, enter 配置堆栈地址解析工具
  -> Enter the Crash log, symbol directory, and source directory
  -> Confirm the execution plan and start analysis and source repair
```

The bundled Demo uses these paths:

```text
Crash log:      examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash
Symbol dir:     examples/crash_cases/demo_basic/lib/mac
Source dir:     examples/crash_cases/demo_basic/code_dir
```

### View the Final Result

The Demo's null-pointer Crash is located and, when repair is enabled and approved, fixed. The source changes from:

```cpp
int* p = nullptr;
*p = 42;
```

to:

```cpp
int* p = nullptr;
if (p != nullptr) {
    *p = 42;
} else {
    std::cerr << "错误: 尝试解引用空指针" << std::endl;
}
```

The same run also generates a developer-readable final report:

```text
reports/<timestamp>/final_output.md
```

The report answers:

- What happened?
- What is the root cause?
- Why is that conclusion supported?
- Which source code needs to change?
- What fix was applied?
- What additional material is still needed?

Its main sections are:

- `故障基本信息`: exception type, signal, crash thread, platform, and crash module
- `三级根因定位`: problem category, triggering mechanism, and specific root cause
- `证据链`: fault address, symbolized frames, source evidence, call relationships, and thread information
- `置信度与证据等级`: confidence and supporting evidence
- `责任归属`: the responsible module, function, or code area
- `修复建议`: code-level fixes and necessary defensive measures
- `需补充材料`: logs, source, or runtime information still needed
- `总结`: root cause, fix result, and follow-up recommendations

The run also preserves auditable process artifacts:

- `09_evidence.json`: evidence-store snapshot
- `00_runtime_trace.json`: model, tool, policy, and lifecycle events
- `context_session.json`: multi-round context session and request ledger
- `09_verification.json`: verification result and approval record
- `10_decide.json`: aggregated repair decision
- `11_judge.json`: deterministic harness judgment, when enabled

## How the Agent Works

The runtime combines a deterministic stability-analysis pipeline with a governed Agent Harness:

```text
incident materials
  -> parse / symbolize / specialized diagnosis
  -> evidence store
  -> bounded context investigation
  -> analysis and repair plan
  -> patch and diff review
  -> explicit verification
  -> decide / judge
```

In `context_loop` mode, the model can request functions, fields, references, callers, or cross-file context. Each run is bounded by round, LLM-call, tool-call, and time budgets. Workspace writes and executable verification commands are subject to policy and explicit approval. Verification failures remain failures and may trigger rollback or a bounded reanalysis/edit cycle.

## Supported Platforms and Capability Boundaries

The built-in Crash analysis pipeline covers iOS, macOS, Android, HarmonyOS, Linux, and Windows. It provides one consistent flow for log parsing, stack symbolization, evidence analysis, source-context extraction, and optional AI-powered repair. Each platform is connected through its own log adapters and symbolization tools; see [Crash Log Formats](./docs/tools/CRASH_LOG_FORMATS.md) for the supported input formats.

The core boundary is not simply whether the Agent can read a platform name. It depends on whether a suitable log adapter, symbolization tool, and analysis Workflow are available. When an adapter is missing, third parties can add a Tool, Workflow, or Skill without changing the core execution framework.

The project also includes dedicated analysis components for ANR, OOM, Jank, and JavaScript/ArkTS. Details about those capabilities and extension points are documented in [Diagnostic Tools](./docs/tools/), the [Tool System Extension Guide](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md), and the [Skill System](./docs/skills/README.md).

The Agent is designed for stability engineering, not unrestricted autonomous coding. It does not implicitly execute discovered build, test, or reproduce commands, access paths outside the configured workspace, loop indefinitely after verification failures, or treat an unsuccessful reproduction as proof of a successful fix. Multi-agent collaboration and open-ended autonomous tool planning are not current promises.

## Troubleshooting and Contact

- For usage questions, bugs, and feature requests, open a [GitHub Issue](https://github.com/baidu-maps/stability-analysis-agent/issues).
- For installation, CLI, and extension questions, start with [Installation and Dependency Troubleshooting](./docs/cli/INSTALL_TROUBLESHOOTING.md), the [CLI Guide](./docs/cli/CLI_GUIDE.md), and the [Tool System Extension Guide](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md).
- For security vulnerabilities, follow the [Security Policy](./SECURITY.md) and contact the maintainer privately. Do not disclose details in a public issue.
- When filing an issue, include the version, operating system, command, log format, and a sanitized report. Do not upload API keys or unredacted production data.

Maintainer: [@liuhong996](https://github.com/liuhong996) | [hong9988.dev@gmail.com](mailto:hong9988.dev@gmail.com)

Version history and important changes are tracked in [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases) and [CHANGELOG.md](./CHANGELOG.md). The project is licensed under [Apache-2.0](./LICENSE); contribution and DCO requirements are documented in [CONTRIBUTING.md](./CONTRIBUTING.md).

## Documentation Map

| What you need | Documentation |
|---|---|
| Installation, dependencies, and environment failures | [docs/cli/INSTALL_TROUBLESHOOTING.md](./docs/cli/INSTALL_TROUBLESHOOTING.md) |
| Common CLI usage | [docs/cli/CLI_GUIDE.md](./docs/cli/CLI_GUIDE.md) |
| Complete CLI parameters | [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md) |
| Local Web panel | [docs/cli/WEB_UI_GUIDE.md](./docs/cli/WEB_UI_GUIDE.md) |
| Crash log formats | [docs/tools/CRASH_LOG_FORMATS.md](./docs/tools/CRASH_LOG_FORMATS.md) |
| C++, ANR, OOM, JS, and Jank diagnostics | [docs/tools/](./docs/tools/) |
| Skill and extension system | [docs/skills/README.md](./docs/skills/README.md) |
| System architecture | [docs/architecture/README.md](./docs/architecture/README.md) |
| Agent Runtime, Harness lifecycle, and safety boundaries | [AGENT_RUNTIME_LIFECYCLE.md](./docs/architecture/AGENT_RUNTIME_LIFECYCLE.md) and [HARNESS_MIGRATION.md](./docs/cli/HARNESS_MIGRATION.md) |
| Multi-round context investigation and request protocol | [CONTEXT_LOOP_CONTRACT.md](./docs/architecture/CONTEXT_LOOP_CONTRACT.md) |
| Daemon, Run API, and event streaming | [DAEMON_SERVER_GUIDE.md](./docs/cli/DAEMON_SERVER_GUIDE.md) |
| Post-fix build, test, and verification loop | [VERIFICATION_PROVIDERS.md](./docs/tools/VERIFICATION_PROVIDERS.md) |
| Testing, regression, and Harness evaluation | [docs/testing/README.md](./docs/testing/README.md) and [EVALUATION_MATRIX.md](./docs/testing/EVALUATION_MATRIX.md) |
| Roadmap | [docs/ROADMAP.md](./docs/ROADMAP.md) |
| License and contribution requirements | [LICENSE](./LICENSE) and [CONTRIBUTING.md](./CONTRIBUTING.md) |

## Where to Go Next

- First run: follow the [bundled Demo](#run-the-bundled-demo).
- Have your own Crash log: replace the Demo log, symbol, and source paths with your own.
- Integrate with a team workflow: read the [Skill System](./docs/skills/README.md) and [CLI Guide](./docs/cli/CLI_GUIDE.md).
- Understand the implementation: read the [System Architecture Overview](./docs/architecture/README.md).

If this project helps you diagnose or fix a stability issue, consider giving it a [Star on GitHub](https://github.com/baidu-maps/stability-analysis-agent). It helps more developers discover the project and gives us useful feedback for future platform and capability improvements.
