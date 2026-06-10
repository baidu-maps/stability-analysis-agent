<h1 align="center">Stability Analysis Agent</h1>
<p align="center">
  <strong>面向 App 稳定性的 AI Agent — 从崩溃日志到根因定位，一步到位</strong><br>
  <sub>Crash · ANR · OOM · Freeze 分析 | addr2line / atos 符号化 | LangGraph AI Agent | RAG 知识库</sub>
</p>
<p align="center">
  <a href="https://pypi.org/project/stability-analysis-agent/"><img src="https://img.shields.io/pypi/v/stability-analysis-agent.svg" alt="PyPI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python"></a>
  <a href="./CONTRIBUTING.md"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"></a>
</p>
<p align="center">
  <a href="./README.md">English</a> | <b>简体中文</b>
</p>

---

**Stability Analysis Agent** 是一个开源的、面向 **App 稳定性分析** 的统一 AI Agent 框架，支持扩展到 **崩溃（Crash）、ANR（应用无响应）、OOM（内存溢出）、卡死（Freeze / Watchdog Kill）** 等场景。当前首个成熟落地场景为 **Crash（闪退）分析**；ANR、卡顿与内存治理能力正在持续演进中。给它一份稳定性日志，它会自动完成**解析、符号化、代码提取、根因推理和修复建议生成**。支持 **iOS、Android、macOS、Linux、Windows**，内置 `addr2line` / `atos` 集成、LangGraph 多轮推理和 RAG 知识库（ChromaDB）。

### 为什么不直接把日志丢给 AI 编程工具？

通用 AI 编程工具（Cursor、Copilot、Claude Code 等）可以阅读崩溃日志，但在稳定性分析上存在硬伤：

- **原始地址无法解析** — AI 工具无法调用 `addr2line` / `atos`，它看到的是 `0x1a2b3c` 而不是 `MyClass::process() at main.cpp:42`。
- **日志噪音大** — 数百行系统栈帧淹没真正的关键信息，LLM 把 token 浪费在无关上下文上。
- **没有领域记忆** — 每次对话从零开始，分析过的模式（崩溃签名、ANR 死锁堆栈、OOM 规律）无法沉淀。

本 Agent 针对性地解决这三个问题：

| | AI 编程工具 | Stability Analysis Agent |
|---|---|---|
| **地址符号化** | 无法调用原生工具 | 内置 `addr2line` / `atos` 集成 |
| **日志解析** | 看到原始文本，噪音高 | 结构化解析，提取信号类型、线程、关键帧；自动分类 Crash / ANR / OOM / Freeze |
| **知识沉淀** | 无状态，每次从零开始 | RAG：规则表 + 向量数据库，模式持续积累 |
| **工作流** | 单次 prompt，一轮对话 | 多步 Agent，支持条件分支和多轮推理 |
| **可扩展性** | 只能改 prompt | Tool + Workflow + Skill 系统，配置驱动 |

### Agent 引擎

三种执行模式，适配不同场景：

| 模式 | 引擎 | 适用场景 |
|------|------|----------|
| **Direct** | 单次 prompt 拼装 | 快速、简单，无框架依赖 |
| **LangChain** | LangChain Agent | 灵活的工具调用 + 思维链 |
| **LangGraph** | LangGraph 状态机 | 多轮推理，Agent 可主动请求更多上下文并重新调用工具 |

通过 `--engine direct|langchain|langgraph` 切换。三种模式共享同一套工具链和 RAG 知识库。

**无需 LLM API Key** 即可运行核心工具链（解析 + 符号化 + 代码提取）。需要 AI 分析时，接入任意 OpenAI 兼容模型（GPT、DeepSeek、文心一言、GLM 等）即可。

## 核心特性

| 特性 | 说明 |
|------|------|
| **多步 AI Agent** | LangGraph / LangChain / Direct — 支持条件分支和多轮推理 |
| **地址符号化** | 通过 `addr2line` / `atos` 将原始地址转换为函数名和行号 |
| **结构化日志解析** | 自动识别 iOS / Android / macOS / Linux / Windows，分类 Crash、ANR、OOM、Freeze，提取信号、线程、关键帧 |
| **源码上下文提取** | 自动提取崩溃点附近的代码片段 |
| **RAG 知识库** | 规则表（快速路径）+ 向量检索（ChromaDB），支持反馈闭环 |
| **Tool + Workflow 系统** | 可插拔架构 — 通过配置或装饰器注册自定义工具和工作流 |
| **Skill 系统** | 安装 Claude 兼容 skill，支持提示词技能或桥接为工具 / 工作流 |
| **对外 Agent 能力包** | 仓库自带 [`stability-analysis-agent-skill/`](./stability-analysis-agent-skill/) — 教 Claude Code、Cursor 等外部 Agent 如何安装并调用 `sa-agent` |
| **多种接入方式** | CLI、HTTP Daemon（流式 / SSE）、Python API |

## 架构

```
                  ┌──────────┐   ┌──────────┐   ┌──────────┐
                  │   CLI    │   │  Daemon  │   │  Python  │
                  │          │   │  (HTTP)  │   │   API    │
                  └────┬─────┘   └────┬─────┘   └────┬─────┘
                       │              │              │
                       └──────────────┼──────────────┘
                                      │
                            ┌─────────▼─────────┐
                            │ Tool + Workflow + │
                            │      Skill        │
                            └─────────┬─────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
   ┌────────────┐            ┌────────────┐            ┌────────────┐
   │  崩溃日志   │            │   地址     │            │   代码     │
   │   解析器    │            │  符号化器   │            │  提取器    │
   └────────────┘            └────────────┘            └────────────┘
                                      │
                            ┌─────────▼─────────┐
                            │    AI Agent       │
                            │  ┌─────────────┐  │
                            │  │  LangGraph  │  │
                            │  │  状态机      │  │
                            │  │             │  │
                            │  └──────┬──────┘  │
                            │         │         │
                            │    ┌────▼────┐    │
                            │    │   RAG   │    │
                            │    │ 规则 +   │    │
                            │    │ 向量检索  │    │
                            │    └────┬────┘    │
                            │         │         │
                            │    ┌────▼────┐    │
                            │    │   LLM   │    │
                            │    └─────────┘    │
                            └───────────────────┘
```

**Agent 分析流程：**

```
崩溃日志 → 解析 → 符号化 → 代码提取
                                 ↓
                         RAG（规则 + 向量检索）
                                 ↓
                           LLM 推理 ←──→ 请求更多上下文（多轮）
                                 ↓
                             修复报告
```

> 详细架构图请参阅 [docs/architecture](./docs/architecture/ARCHITECTURE_DIAGRAM.md)。

## 快速开始

### 环境要求

- 二进制使用：无需 Python 运行时
- **Python 版本**：最低 **3.9**；**推荐 3.10–3.12**（依赖与 CI 主要在此区间验证）
  - 仅核心能力（解析 + 符号化 + LLM）：3.9+ 通常可用
  - 含 `[rag]`（torch / transformers 等）：建议 **3.10–3.12**；3.9 可能遇到 ML 栈组合问题
  - macOS 建议优先使用 **Homebrew / pyenv** 安装的 Python，避免官方安装包未配置 CA 导致 SSL 失败
- （可选）符号化工具：`atos`（macOS 自带）或 `addr2line`（Linux，来自 binutils）

### 安装并启动（推荐）

**方式 A — pip（venv 或系统环境）**

```bash
# 安装（中国大陆可加 -i https://pypi.tuna.tsinghua.edu.cn/simple）
pip install stability-analysis-agent

# 含向量库 / 相似案例 RAG（推荐完整体验）
pip install "stability-analysis-agent[rag]"

# 进入交互向导
sa-agent
```

**方式 B — pipx（隔离 CLI，不污染全局 site-packages）**

```bash
# 先安装 pipx：https://pipx.pypa.io/
pipx install stability-analysis-agent
# 或含 RAG（体积较大、首次安装较慢）
pipx install "stability-analysis-agent[rag]"

sa-agent --help
```

**方式 C — 预编译二进制**：见下方「使用预编译 CLI 二进制」。

安装排错（Python 版本、SSL、pipx、`transformers`/`nn` 报错等）见 [docs/cli/INSTALL_TROUBLESHOOTING.md](./docs/cli/INSTALL_TROUBLESHOOTING.md)。

> 交互体验对标 Claude CLI：支持上下键菜单、分组化“设置 / 帮助”、可返回路径和关键步骤确认。  
> 大多数场景可在终端向导内完成“配置 + 分析 + AI 修复建议”全流程。

## Demo：交互式 AI 修复（Crash）

使用内置 Demo 快速体验“终端交互 + AI 完整链路”：

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
sa-agent
```

在向导中选择 `快速开始分析（推荐）`，然后输入：

```text
crash_log   -> examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash
library_dir -> examples/crash_cases/demo_basic/lib/mac
code_root   -> examples/crash_cases/demo_basic/code_dir
```

CLI 会先输出执行计划，再自动执行。AI 模式下将完成解析、符号化、代码上下文提取和 LLM 推理，并可回写修复建议（含备份）。

分析你自己的崩溃日志同样使用 `sa-agent` 交互输入路径即可。输出位于 `./cli_reports/<timestamp>/`。

## 在 Claude / Cursor 等外部 Agent 中使用

若你已在用 **Claude Code**、**Cursor** 等 AI 编程工具，可安装仓库自带的对外能力包，让外部 Agent 知道如何调用本工具链（符号化、结构化报告、`--scope` 等），而不是仅凭猜测拼命令或只粘贴原始日志。

这与 `sa-agent skill install`（给 sa-agent 运行时安装扩展）**不是一回事**。能力包位于 [`stability-analysis-agent-skill/`](./stability-analysis-agent-skill/)，需复制到**外部 Agent 自己的** skill 目录。

**步骤 1 — 安装 Python 包**（提供 `sa-agent` 命令）：

```bash
pip install stability-analysis-agent
# 或：pipx install stability-analysis-agent
```

**步骤 2 — 安装能力包**到外部 Agent：

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cp -R stability-analysis-agent/stability-analysis-agent-skill ~/.claude/skills/stability-analysis-agent
```

**Cursor**（项目级示例）：

```bash
mkdir -p .cursor/skills
cp -R stability-analysis-agent/stability-analysis-agent-skill .cursor/skills/stability-analysis-agent
```

安装后，可让外部 Agent「用 Stability Analysis Agent 分析崩溃日志」——它应能给出 `sa-agent` 命令、选择合适的 `--scope`，并读取 `cli_reports/<timestamp>/` 下的报告。

| 资源 | 说明 |
|------|------|
| [SKILL.md](./stability-analysis-agent-skill/SKILL.md) | 外部 Agent 主入口 |
| [examples.md](./stability-analysis-agent-skill/examples.md) | 可复制命令示例 |
| [reference.md](./stability-analysis-agent-skill/reference.md) | 参数、报告、配置路径 |
| [docs/skills/README.md](./docs/skills/README.md) | sa-agent Skill System（运行时扩展机制） |

> **没有 LLM Key？** 能力包内说明可使用 `--scope gen_prompt_only` — 完整解析 + 符号化 + 代码上下文 + 提示词文件，不调用 LLM。

## 其它方式（高级）

### 以 Python 集成（可编程接口）

自 **v1.2.4** 起，PyPI 包提供稳定模块 [`cli/api.py`](./cli/api.py)，例如 `execute_analysis`、`build_parser`、`collect_interactive_run_state`、`interactive_state_to_argv`、`run_from_interactive_state`、`run_cli_main` 等，便于企业包装器或自动化脚本在进程内调用与 `sa-agent` 相同的分析链路，而无需 `subprocess`。变更说明见 [`CHANGELOG.md`](./CHANGELOG.md)。

### 使用预编译 CLI 二进制（无需 Python）

从 [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases) 下载最新二进制。压缩包与目录名随版本变化，请以实际 Release 文件名为准：

```bash
unzip StabilityAnalyzer-v1.2.4-mac-arm64.zip
cd output/cli_release/stability_analyzer_cli/v1.2.4-mac-arm64
./StabilityAnalyzer
```

### 开发者源码安装

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
pip install -e .
sa-agent
```

> `pip install -e .` 主要用于开发场景，同时也会暴露本地 `sa-agent` 命令。

### CLI 参数说明

| 参数 | 必须 | 说明 |
|------|------|------|
| `--crash-log` | 是 | 崩溃日志文件路径（不限后缀，按内容识别格式，见 [崩溃日志格式说明](./docs/tools/CRASH_LOG_FORMATS.zh-CN.md)） |
| `--library-dir` | 是* | 库文件目录，包含 `.dylib`/`.so` 及调试符号（`.dSYM`） |
| `--code-root` | 否 | 源码根目录，用于提取崩溃点代码上下文 |
| `--scope <value>` | 否 | Agent 执行流程范围（默认 `full`），取值 `full` / `gen_prompt_only` / `parse_stack_only` / `parse_log_only`，详见下方。 |
| `--daemon <url>` | 否 | 委托给运行中的 Daemon 实例 |

\* 使用 `--scope parse_log_only` 时不需要。

### `--scope` 取值说明

| 取值 | 行为 |
|------|------|
| `full`（默认） | 解析 + 符号化 + 取代码上下文 + AI 推理（含可选自动改码）。 |
| `gen_prompt_only` | 完整工具链，但不调用 LLM，仅生成可复用的提示词文件。 |
| `parse_stack_only` | 仅解析 + 符号化，无需 `--code-root`。 |
| `parse_log_only` | 仅解析崩溃日志，`--library-dir` 与 `--code-root` 都可省略。 |

### 支持的崩溃日志文件与平台导出

**文件后缀：** 不做白名单限制 — `.crash`、`.txt`、`.log`、`.json` 或无后缀均可，关键看**文件内容**是否匹配已知格式；也支持 `--crash-log -` 从 stdin 读取。RTF 导出会先转为纯文本。

**文本类（示例）：** Apple `.crash`、iOS 卡顿/Mach 导出、Android logcat/tombstone、Harmony `Stacktrace:` / `Tid:` dump、native 文本栈 `#NN pc 0x地址 /path/lib.so`。

**JSON 类导出：**

| 平台 / 形态 | `01` 报告中的 `log_format` |
|-------------|---------------------------|
| Harmony 崩溃平台（`crashDiagnosis:` / `crashDiagnsis:` + JSON，含 `body.stacks[].call_stack` 的 `#NN pc`） | `harmony_crash_diagnosis_json` |
| [Sentry](https://sentry.io/) 事件 JSON | `sentry_event_json` |
| [Firebase Crashlytics](https://firebase.google.com/docs/crashlytics) 事件 JSON | `firebase_crashlytics_json` |
| [Bugsnag](https://www.bugsnag.com/) 事件 JSON | `bugsnag_event_json` |
| Bugly / 友盟 / 自建 APM 等（`frames` / `stack_frames` 常见字段） | `generic_json_stack_export` |

完整列表、解析器优先级与扩展方式：**[docs/tools/CRASH_LOG_FORMATS.zh-CN.md](./docs/tools/CRASH_LOG_FORMATS.zh-CN.md)** · [English](./docs/tools/CRASH_LOG_FORMATS.md)

## Daemon 模式

Daemon 提供**流式输出（SSE）**、**进程复用**（免冷启动）和**任务取消**功能，适合 IDE 集成和高频分析场景：

```bash
# 启动 Daemon
sa-agent --daemon-server --host 127.0.0.1 --port 8765

# 通过 Daemon 分析
sa-agent --daemon http://127.0.0.1:8765 \
  --crash-log <崩溃日志> --library-dir <库目录> --code-root <源码目录>
```

> 完整 HTTP API 参考请查看 [Daemon 服务指南](./docs/cli/DAEMON_SERVER_GUIDE.md)。

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

## 配置 LLM 与符号化工具

推荐通过交互向导配置：

```bash
sa-agent
```

进入后在 `设置` 中选择 `配置大模型` / `配置堆栈地址解析工具`，流程内会自动检测并给出引导。堆栈符号化向导为 **「自动获取」** 与 **「手动设置符号化工具绝对路径」**（可填可执行文件或工具所在目录）；选择「快速开始分析」且流程需要符号化时，会先静默尝试与「自动获取」相同的写入，减少重复配置。

默认本地配置目录：

```bash
~/.config/stability-analysis-agent/
```

- `agent_config.local.json`：配置大模型 **厂商 / 密钥 / 模型**（对应 `llm_config.active_provider` 与 `llm_config.providers`）
- `add2line_resolver_config.local.json`：配置符号化工具搜索路径（`tool_paths` 为工具所在目录；可选 `environment_vars` 为 NDK/LLVM 等安装根，常由自动获取写入）

若你偏好手动编辑，也可直接修改以上配置文件。

高级可选模式（通过 `--scope`）：
- `--scope gen_prompt_only`（完整工具链，跳过 LLM，仅生成提示词）
- `--scope parse_stack_only`（仅解析 + 符号化）
- `--scope parse_log_only`（仅解析日志）

### 高级：add2line 配置路径覆盖

可通过环境变量显式指定 add2line 配置文件路径：

```bash
export STABILITY_AGENT_ADD2LINE_CONFIG_FILE="/绝对路径/add2line_resolver_config.local.json"
```

## 项目结构

```
stability-analysis-agent/
├── agent/              # AI Agent 引擎（LangGraph 状态机）
├── cli/                # CLI 入口
├── daemon/             # HTTP Daemon（流式、SSE）
├── tools/              # 工具实现（解析器、符号化、代码提取）
│   └── configs/        # 配置模板
├── tool_system/        # Tool + Workflow 注册与调度框架
├── skill_system/       # Skill 发现、安装、运行时桥接
├── workflows/          # Workflow 定义（崩溃分析）
├── rag/                # RAG：规则存储 + 向量索引（ChromaDB）+ 元数据
├── prompts/            # LLM 分析提示词模板
├── protocol/           # 统一请求/响应协议
├── examples/           # 内置崩溃案例
│   └── crash_cases/
│       ├── demo_basic/         # NullPtr、DivZero、Abort、DoubleFree 等
│       └── demo_multithread/   # 竞态条件、死锁、原子操作失败等
├── test/               # 测试套件
├── stability-analysis-agent-skill/  # 对外 Agent 能力包（Claude / Cursor 等）
└── docs/               # 文档
```

## 文档导航

| 主题 | 链接 |
|------|------|
| CLI 使用指南 | [docs/cli/CLI_GUIDE.md](./docs/cli/CLI_GUIDE.md) |
| CLI 参数参考 | [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md) |
| Daemon 服务指南 | [docs/cli/DAEMON_SERVER_GUIDE.md](./docs/cli/DAEMON_SERVER_GUIDE.md) |
| 对外 Agent 能力包 | [stability-analysis-agent-skill/](./stability-analysis-agent-skill/) |
| Skill 系统（sa-agent 运行时） | [docs/skills/README.md](./docs/skills/README.md) |
| PyPI 发布脚本指南 | [docs/scripts/PYPI_RELEASE_SCRIPTS.md](./docs/scripts/PYPI_RELEASE_SCRIPTS.md) |
| 系统架构 | [docs/architecture/README.md](./docs/architecture/README.md) |
| 架构图 | [docs/architecture/ARCHITECTURE_DIAGRAM.md](./docs/architecture/ARCHITECTURE_DIAGRAM.md) |
| Tool System 概览 | [docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md](./docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md) |
| 工具扩展指南 | [docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md) |
| Workflow 系统 | [docs/workflows/WORKFLOWS.md](./docs/workflows/WORKFLOWS.md) |
| RAG 向量数据库 | [docs/rag/README.md](./docs/rag/README.md) |
| 崩溃示例 | [docs/crash_cases/README.md](./docs/crash_cases/README.md) |
| 崩溃日志格式与平台支持 | [docs/tools/CRASH_LOG_FORMATS.zh-CN.md](./docs/tools/CRASH_LOG_FORMATS.zh-CN.md) |

## 测试

```bash
# 回归测试
python3 test/tool_system/test_regression.py

# LLM 连接测试
python3 test/llm/test_llm_connection.py --provider openai

# 代码内容提取测试
python3 test/agent_py_tool/test_code_content_provider.py

# 向量数据库测试
python3 test/agent_py_tool/test_vector_db.py
```

## 常见问题

**Q：符号化失败？**
确保 `--library-dir` 包含二进制文件（`.dylib` / `.so`）及其调试符号（`.dSYM` 目录或 DWARF 信息）。交互式 CLI 中可在 `设置 → 配置堆栈地址解析工具` 使用 **自动获取**，或 **手动设置符号化工具绝对路径**（可执行文件或工具所在目录）；亦可编辑 `~/.config/stability-analysis-agent/add2line_resolver_config.local.json`（参见 `tools/configs/add2line_resolver_config.local.example.json`）。

**Q：LLM 调用失败？**
检查 API Key 是否正确设置。快速验证：`python3 test/llm/test_llm_connection.py --provider openai`

**Q：代码上下文提取为空？**
确保 `--code-root` 指向的源码目录包含符号化堆栈中引用的文件。

**Q：不配置 LLM Key 能用吗？**
可以。使用 `--scope gen_prompt_only` 即可运行完整工具链（解析 + 符号化 + 代码提取），跳过 LLM 调用并生成可复用提示词，结构化 JSON 输出本身就对问题定位很有帮助。

**Q：如何在 Claude Code 或 Cursor 里使用？**
先安装 Python 包（`pip install stability-analysis-agent`），再将 [`stability-analysis-agent-skill/`](./stability-analysis-agent-skill/) 复制到外部 Agent 的 skill 目录（例如 `~/.claude/skills/stability-analysis-agent`）。详见上文 [在 Claude / Cursor 等外部 Agent 中使用](#在-claude--cursor-等外部-agent-中使用)。

## 贡献

欢迎贡献代码！提交前请阅读 [CONTRIBUTING.md](./CONTRIBUTING.md)。

```bash
# 所有提交需包含 DCO 签名
git commit -s -m "feat: 描述你的改动"
```

## 许可证

[Apache License 2.0](./LICENSE)

## 联系方式

| 渠道 | 链接 |
|------|------|
| GitHub Issues | [提交 Bug 或功能建议](https://github.com/baidu-maps/stability-analysis-agent/issues) |
| 邮箱 | [hong9988.dev@gmail.com](mailto:hong9988.dev@gmail.com) |

**维护者：**

| 姓名 | GitHub | 邮箱 |
|------|--------|------|
| liuhong | [@liuhong996](https://github.com/liuhong996) | hong9988.dev@gmail.com |

---

<p align="center">
  如果这个项目对你有帮助，欢迎点个 <b>Star</b> 支持一下！
</p>
