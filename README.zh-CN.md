<p align="center">
  <h1 align="center">Stability Analysis Agent</h1>
  <p align="center">
    <strong>面向 App 稳定性的 AI Agent — 从崩溃日志到根因定位，一步到位</strong>
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
</p>

---

**Stability Analysis Agent** 是一个专为 **App 崩溃分析** 打造的 AI Agent。给它一份崩溃日志，它会自动完成**解析、符号化、代码提取、根因推理和修复建议生成**。

### 为什么不直接把日志丢给 AI 编程工具？

通用 AI 编程工具（Cursor、Copilot、Claude Code 等）可以阅读崩溃日志，但在稳定性分析上存在硬伤：

- **原始地址无法解析** — AI 工具无法调用 `addr2line` / `atos`，它看到的是 `0x1a2b3c` 而不是 `MyClass::process() at main.cpp:42`。
- **日志噪音大** — 数百行系统栈帧淹没真正的关键信息，LLM 把 token 浪费在无关上下文上。
- **没有领域记忆** — 每次对话从零开始，分析过的崩溃模式无法沉淀。

本 Agent 针对性地解决这三个问题：

| | AI 编程工具 | Stability Analysis Agent |
|---|---|---|
| **地址符号化** | 无法调用原生工具 | 内置 `addr2line` / `atos` 集成 |
| **日志解析** | 看到原始文本，噪音高 | 结构化解析，提取信号类型、线程、关键帧 |
| **知识沉淀** | 无状态，每次从零开始 | RAG：规则表 + 向量数据库，模式持续积累 |
| **工作流** | 单次 prompt，一轮对话 | 多步 Agent，支持条件分支和多轮推理 |
| **可扩展性** | 只能改 prompt | Tool + Skill 插件系统，配置驱动 |

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
| **结构化崩溃解析** | 自动识别 iOS / Android / macOS / Linux / Windows，提取信号、线程、关键帧 |
| **源码上下文提取** | 自动提取崩溃点附近的代码片段 |
| **RAG 知识库** | 规则表（快速路径）+ 向量检索（ChromaDB），支持反馈闭环 |
| **Tool + Skill 系统** | 可插拔架构 — 通过配置或装饰器注册自定义工具和技能 |
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
                            │   Tool + Skill    │
                            │   (tool_system)   │
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
- 源码使用：Python 3.9+
- （可选）符号化工具：`atos`（macOS 自带）或 `addr2line`（Linux，来自 binutils）

### 1. 通过 PyPI 安装（推荐）

```bash
# 安装（中国大陆可加 -i https://pypi.tuna.tsinghua.edu.cn/simple）
pip install stability-analysis-agent

# 查看帮助
sa-agent --help

# 初始化本地配置（交互式引导配置大模型密钥、addr2line/atos 工具路径等）
sa-agent config init

# 自检配置完整性
sa-agent config doctor
```

> 安装后的配置文件保存在 `~/.config/stability-analysis-agent/` 目录下：
> - `agent_config.local.json`：大模型 provider / API key / model
> - `add2line_resolver_config.local.json`：addr2line / atos 工具路径
>
> 即使不初始化配置，也可以通过 `--skip-ai` 运行完整非 AI 工具链。
>
> PyPI 包默认包含完整运行依赖（向量数据库、tree-sitter、LangGraph 链路）。
>
> 升级命令：`pip install -U stability-analysis-agent`

### 2. 使用预编译 CLI 二进制（无需 Python）

从 [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases) 下载最新二进制后执行：

```bash
# 以 v1.1.0 macOS arm64 包为例
unzip StabilityAnalyzer-v1.1.0-mac-arm64.zip
cd output/cli_release/stability_analyzer_cli/v1.1.0-mac-arm64

chmod +x StabilityAnalyzer

# 若 macOS Gatekeeper 拦截启动（未签名二进制）
xattr -d com.apple.quarantine StabilityAnalyzer

./StabilityAnalyzer --help

# 可选：安装到 ~/.local/bin，命令名为 sa-agent（Release 压缩包内自带 install.sh）
chmod +x install.sh
./install.sh
# 然后可直接: sa-agent --help
```

### 3. 开发者源码安装

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
pip install -e .
```

> `pip install -e .` 主要用于开发场景，同时也会暴露本地 `sa-agent` 命令。

### 4. 运行内置 Demo（无需 API Key）

通过 PyPI 安装（`pip install stability-analysis-agent`）或源码安装（`pip install -e .`）后，克隆仓库获取内置 Demo 样例，然后运行：

```bash
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --skip-ai
```

输出保存在当前工作目录下的 `./cli_reports/<timestamp>/`，包含结构化 JSON 报告。

### 5. 分析你自己的崩溃日志

```bash
sa-agent \
  --crash-log <你的崩溃日志> \
  --library-dir <库文件和符号目录> \
  --code-root <源码根目录>
```

> 添加 `--skip-ai` 跳过 AI 分析，或使用 `--parse-only` 仅执行解析 + 符号化。

### CLI 参数说明

| 参数 | 必须 | 说明 |
|------|------|------|
| `--crash-log` | 是 | 崩溃日志文件路径 |
| `--library-dir` | 是* | 库文件目录，包含 `.dylib`/`.so` 及调试符号（`.dSYM`） |
| `--code-root` | 否 | 源码根目录，用于提取崩溃点代码上下文 |
| `--skip-ai` | 否 | 跳过 AI 分析，只跑工具链（解析 + 符号化 + 代码提取） |
| `--parse-only` | 否 | 仅解析 + 符号化（无需 `--code-root`） |
| `--parse-log-only` | 否 | 仅解析崩溃日志（无需 `--library-dir`） |
| `--daemon <url>` | 否 | 委托给运行中的 Daemon 实例 |

\* 使用 `--parse-log-only` 时不需要。

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
    ToolAndSkillRegistry, SystemConfig, SkillConfig,
    ConfigDrivenExecutor, register_all_tools_and_skills
)

registry = ToolAndSkillRegistry()
register_all_tools_and_skills(registry)

config = SystemConfig(
    skills=[SkillConfig(name="crash_analysis", enabled=True)]
)
executor = ConfigDrivenExecutor(registry, config, llm_adapter=None)

result = executor.execute_skill("crash_analysis", {
    "crash_log": open("crash.crash").read(),
    "library_dir": "./lib",
    "code_root": "./code"
})
print(result)
```

## 配置 LLM 与符号化工具

AI 分析为**可选功能**。即使不初始化配置，也可以通过 `--skip-ai` 运行完整非 AI 工具链。

通过 PyPI 安装后，推荐使用以下命令配置与自检：

```bash
sa-agent config init
sa-agent config path
sa-agent config doctor
```

默认本地配置目录：

```bash
~/.config/stability-analysis-agent/
```

- `agent_config.local.json`：配置大模型 provider/key/model
- `add2line_resolver_config.local.json`：配置 addr2line/atos 工具路径

若在 `config init` 中选择手动编辑，直接编辑以上文件即可。

### 高级：环境变量覆盖

可通过环境变量显式指定配置文件路径：

```bash
export STABILITY_AGENT_CONFIG_FILE="/绝对路径/agent_config.local.json"
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
├── tool_system/        # Tool + Skill 注册与调度框架
├── skills/             # Skill 定义（崩溃分析）
├── rag/                # RAG：规则存储 + 向量索引（ChromaDB）+ 元数据
├── prompts/            # LLM 分析提示词模板
├── protocol/           # 统一请求/响应协议
├── examples/           # 内置崩溃案例
│   └── crash_cases/
│       ├── demo_basic/         # NullPtr、DivZero、Abort、DoubleFree 等
│       └── demo_multithread/   # 竞态条件、死锁、原子操作失败等
├── test/               # 测试套件
└── docs/               # 文档
```

## 文档导航

| 主题 | 链接 |
|------|------|
| CLI 使用指南 | [docs/cli/CLI_GUIDE.md](./docs/cli/CLI_GUIDE.md) |
| CLI 参数参考 | [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md) |
| Daemon 服务指南 | [docs/cli/DAEMON_SERVER_GUIDE.md](./docs/cli/DAEMON_SERVER_GUIDE.md) |
| PyPI 发布脚本指南 | [docs/scripts/PYPI_RELEASE_SCRIPTS.md](./docs/scripts/PYPI_RELEASE_SCRIPTS.md) |
| 系统架构 | [docs/architecture/README.md](./docs/architecture/README.md) |
| 架构图 | [docs/architecture/ARCHITECTURE_DIAGRAM.md](./docs/architecture/ARCHITECTURE_DIAGRAM.md) |
| Tool System 概览 | [docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md](./docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md) |
| 工具扩展指南 | [docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md) |
| Skill 系统 | [docs/skills/SKILLS.md](./docs/skills/SKILLS.md) |
| RAG 向量数据库 | [docs/rag/README.md](./docs/rag/README.md) |
| 崩溃示例 | [docs/crash_demos/README.md](./docs/crash_demos/README.md) |

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
确保 `--library-dir` 包含二进制文件（`.dylib` / `.so`）及其调试符号（`.dSYM` 目录或 DWARF 信息）。

**Q：LLM 调用失败？**
检查 API Key 是否正确设置。快速验证：`python3 test/llm/test_llm_connection.py --provider openai`

**Q：代码上下文提取为空？**
确保 `--code-root` 指向的源码目录包含符号化堆栈中引用的文件。

**Q：不配置 LLM Key 能用吗？**
可以。使用 `--skip-ai` 即可运行完整工具链（解析 + 符号化 + 代码提取），输出的结构化 JSON 本身就对问题定位很有帮助。

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
