---
name: stability-analysis-agent
description: >-
  使用 Stability Analysis Agent 分析 App 崩溃日志：结构化解析、addr2line/atos 符号化、
  源码上下文提取、可选 LLM 根因推理。在用户提供了 crash log、需要堆栈符号化、
  稳定性根因分析、或询问 sa-agent / stability-analysis-agent 用法时使用。
when_to_use: >-
  用户提供崩溃日志（.crash/.txt/.json 等）、需要符号化 native 堆栈、提取崩溃点源码、
  生成 AI 分析报告，或询问如何运行 stability-analysis-agent / sa-agent 时。
disable-model-invocation: false
---

# Stability Analysis Agent

教外部 Agent（Claude Code、Cursor 等）如何调用 **Stability Analysis Agent** 的能力。
本目录是**对外能力导出包**，不是 `sa-agent skill install` 的安装目录。

## 先决条件：安装 Python 包

任选一种方式安装 **stability-analysis-agent**（安装的是 CLI/工具链，不是本 Skill 目录）：

```bash
# PyPI（推荐）
pip install stability-analysis-agent

# 含 RAG 向量库（Python 3.10–3.12 推荐）
pip install "stability-analysis-agent[rag]"

# 隔离 CLI
pipx install stability-analysis-agent

# 从源码开发安装
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
pip install -e .
```

安装后可用 `sa-agent` 或 `python3 -m cli.main`（源码目录下）。

无 Python 环境时，可从 [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases) 下载预编译二进制 `StabilityAnalyzer`。

## 何时使用本 Skill

- 用户给了崩溃日志，需要**比直接丢给 LLM 更可靠**的分析（符号化 + 结构化解析 + 源码上下文）。
- 需要**无 LLM API Key** 跑通工具链（用 `--scope gen_prompt_only`）。
- 需要批量/脚本化分析，或读取标准化 JSON/Markdown 报告。

## 最小工作流（推荐顺序）

1. **确认输入**：`--crash-log`（必填）；符号化需 `--library-dir`；源码上下文需 `--code-root`（可多次指定）。
2. **选择 scope**（见下表）。
3. **执行命令**，报告写入 `./cli_reports/<timestamp>/`。
4. **读报告**：先看 `01` → `02` → `03`；AI 结论看 `round_0/06_ai_gen_res.md` 或 `final_output.md`。

### `--scope` 快速选择

| scope | 需要 LLM | 典型用途 |
|-------|----------|----------|
| `parse_log_only` | 否 | 只解析日志结构，无需库目录 |
| `parse_stack_only` | 否 | 解析 + maps + 符号化 + 04a 诊断（条件 04c/d/e） |
| `gen_prompt_only` | 否 | 完整工具链 + 生成 `06_ai_prompt.md`，**无需 API Key** |
| `full`（默认） | 是 | 完整分析 + AI 推理 |

## 常用命令

### 完整 AI 分析

```bash
sa-agent \
  --crash-log /path/to/crash.log \
  --library-dir /path/to/libs \
  --code-root /path/to/source
```

### 无 LLM Key：只生成提示词

```bash
sa-agent \
  --crash-log /path/to/crash.log \
  --library-dir /path/to/libs \
  --code-root /path/to/source \
  --scope gen_prompt_only
```

### 仅解析 + 符号化

```bash
sa-agent \
  --crash-log /path/to/crash.log \
  --library-dir /path/to/libs \
  --scope parse_stack_only
```

### 仓库内置 Demo（克隆后）

```bash
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
```

更多示例见 [examples.md](./examples.md)。

## 报告目录结构

运行后在 `./cli_reports/<timestamp>/` 生成：

| 文件 | 含义 |
|------|------|
| `01_crash_log_parser.json` | 结构化解析结果 |
| `02_memory_maps.json` | 内存映射（有则写） |
| `03_add2line_resolver.json` | 符号化后的堆栈 |
| `04a_crash_diagnosis.json` | 崩溃诊断（含证据罗盘） |
| `04b_code_content_provider.json` | 崩溃点源码上下文 |
| `04b2_code_location_trace.json` | 代码定位审计（旁路，默认不并入提示词） |
| `04c` / `04d` / `04e` | 条件旁路：ANR / 内存压力 / 日志时序 |
| `05_memory_context.json` | RAG 检索上下文 |
| `round_0/06_ai_prompt.md` | 发给 LLM 的提示词 |
| `round_0/07_ai_gen_res.md` | LLM 分析结果 |
| `final_output.md` | 终端可读汇总 |

## LLM 配置

`full` scope 需要 LLM。配置方式：

- 交互：`sa-agent` → 设置 → 配置大模型
- 本地文件：`~/.config/stability-analysis-agent/agent_config.local.json`
- 环境变量：`OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 等（视 provider 而定）

符号化工具配置：`~/.config/stability-analysis-agent/add2line_resolver_config.local.json`

## Daemon 模式（IDE / 高频场景）

```bash
# 终端 1：启动
sa-agent --daemon-server --host 127.0.0.1 --port 8765

# 终端 2：委托分析
sa-agent --daemon http://127.0.0.1:8765 \
  --crash-log /path/to/crash.log \
  --library-dir /path/to/libs \
  --code-root /path/to/source
```

## 外部 Agent 安装本 Skill

将本目录复制到目标 Agent 的 skill 目录，例如：

```bash
# Claude Code
cp -R stability-analysis-agent-skill ~/.claude/skills/stability-analysis-agent

# Cursor（项目级）
mkdir -p .cursor/skills
cp -R stability-analysis-agent-skill .cursor/skills/stability-analysis-agent
```

## 详细参考

- [reference.md](./reference.md) — 参数、配置、报告字段、Python API
- [examples.md](./examples.md) — 可复制命令集
- 仓库文档：`docs/cli/CLI_COMMANDS_REFERENCE.md`、`docs/tools/CRASH_LOG_FORMATS.zh-CN.md`
