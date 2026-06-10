# Stability Analysis Agent — 参考手册

供外部 Agent 在需要细节时查阅。主入口见 [SKILL.md](./SKILL.md)。

## 环境要求

| 项目 | 说明 |
|------|------|
| Python | 最低 3.9；推荐 3.10–3.12 |
| `[rag]` 可选依赖 | 含 torch/transformers，建议 3.10–3.12 |
| 符号化 | macOS 自带 `atos`；Linux 需 `addr2line`（binutils） |
| LLM | 仅 `--scope full` 需要；OpenAI 兼容 API |

安装排错：`docs/cli/INSTALL_TROUBLESHOOTING.md`

## CLI 入口

| 方式 | 命令 |
|------|------|
| PyPI 安装后 | `sa-agent [参数...]` |
| 源码仓库 | `python3 cli/main.py [参数...]` |
| 交互向导 | `sa-agent`（无参数） |

## 核心参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `--crash-log PATH` | 是* | 崩溃日志；`-` 表示 stdin。不限后缀，按**内容**识别格式 |
| `--library-dir DIR` | 建议 | 含 `.dylib`/`.so` 及 `.dSYM` 等符号 |
| `--code-root DIR` | 可选 | 源码根，可重复指定多个 |
| `--scope` | 否 | 默认 `full` |
| `--engine` | 否 | `direct` / `langchain` / `langgraph`，默认 `direct` |
| `--prompt-mode` | 否 | `analysis`（默认）或 `fix`（偏补丁输出） |
| `--agent-loop` | 否 | `single` 或 `context_loop`（多轮补源码） |
| `--daemon URL` | 否 | 委托给 HTTP Daemon |
| `--output-format` | 否 | `markdown` / `json` / `text` |
| `--output-file PATH` | 否 | 写入文件而非 stdout |

\* `parse_log_only` 时可省略 `--library-dir` 和 `--code-root`。

## `--scope` 详解

| scope | 工具链 | LLM | 生成 05 提示词 |
|-------|--------|-----|----------------|
| `full` | parser + addr2line + code_content | 是 | 是 |
| `gen_prompt_only` | 同上 | 否 | 是 |
| `parse_stack_only` | parser + addr2line | 否 | 否 |
| `parse_log_only` | parser | 否 | 否 |

## `--prompt-mode` 与 `--agent-loop`

- `analysis`：偏证据与置信度，不强制输出修复代码；默认启用 `context_loop`（最多 3 轮）。
- `fix`：偏可编译补丁输出；默认 `single` 轮。
- `context_loop`：模型可请求更多函数源码后继续分析；见 `round_N/06_context_requests.json`。

## 支持的崩溃日志格式（摘要）

**文本类：** Apple `.crash`、Android logcat/tombstone、Harmony `Stacktrace:`/`Tid:`、native `#NN pc` 栈。

**JSON 类：**

| 平台 | `01` 中 `log_format` |
|------|----------------------|
| Harmony 崩溃平台 JSON | `harmony_crash_diagnosis_json` |
| Sentry | `sentry_event_json` |
| Firebase Crashlytics | `firebase_crashlytics_json` |
| Bugsnag | `bugsnag_event_json` |
| 通用 frames/stack_frames | `generic_json_stack_export` |

**注意：** 不要把上一轮输出的 `01_crash_log_parser.json` 当作 `--crash-log` 输入。

完整说明：`docs/tools/CRASH_LOG_FORMATS.zh-CN.md`

## 报告产物说明

输出根目录：`./cli_reports/<timestamp>/`（相对当前工作目录）。

| 文件 | scope 条件 | 内容 |
|------|------------|------|
| `01_crash_log_parser.json` | 总是（有 parse 结果时） | 信号、线程、帧、log_format |
| `02_add2line_resolver.json` | full / gen_prompt_only / parse_stack_only | 符号化堆栈 |
| `03_code_content_provider.json` | full / gen_prompt_only | 崩溃点源码片段 |
| `03b_code_location_trace.json` | 有 location_trace 时 | 定位审计旁路 |
| `04_memory_context.json` | full / gen_prompt_only | RAG 规则与向量命中 |
| `round_0/05_ai_prompt.md` | full / gen_prompt_only | LLM 输入提示词 |
| `round_0/06_ai_gen_res.md` | full 且 LLM 成功 | 模型分析输出 |
| `agent_rounds_summary.json` | context_loop 多轮时 | 各轮摘要 |
| `07_apply_ai_fixes.json` | 启用自动改码时 | 补丁应用结果 |
| `final_output.md` | 通常有 | 人类可读汇总 |

`05_ai_prompt.md` 默认由 `01`/`02`/`03` 拼装；仅当 `--include-memory-in-05` 且 `04` 非空时并入 RAG 段落。

## 配置路径

默认配置目录：`~/.config/stability-analysis-agent/`

| 文件 | 用途 |
|------|------|
| `agent_config.local.json` | LLM provider、API Key、模型 |
| `add2line_resolver_config.local.json` | addr2line/atos 搜索路径 |

环境变量覆盖符号化配置：

```bash
export STABILITY_AGENT_ADD2LINE_CONFIG_FILE="/abs/path/add2line_resolver_config.local.json"
```

## Daemon

```bash
sa-agent --daemon-server --host 127.0.0.1 --port 8765
sa-agent --daemon http://127.0.0.1:8765 --crash-log ... --library-dir ... --code-root ...
```

API 细节：`docs/cli/DAEMON_SERVER_GUIDE.md`

## Python 可编程 API

自 v1.2.4 起，推荐用 `cli/api.py` 在进程内调用（无需 subprocess）：

```python
from cli.api import run_from_interactive_state

state = {
    "crash_log": "/path/to/crash.log",
    "library_dir": "/path/to/libs",
    "code_roots": ["/path/to/source"],
    "scope": "gen_prompt_only",
}
raise SystemExit(run_from_interactive_state(state))
```

也可用 Tool System 直接编排 workflow（见 README 中 Python API 示例）。

## 与本仓库 Skill System 的区别

| | `stability-analysis-agent-skill/`（本目录） | `sa-agent skill install` |
|---|---|---|
| 受众 | Claude、Cursor 等外部 Agent | sa-agent 运行时扩展 |
| 安装位置 | `~/.claude/skills/` 等 | `~/.config/stability-analysis-agent/skills` |
| 内容 | 如何使用本 Agent 的说明与命令 | 第三方可执行 skill 包 |
| 依赖 | 需先 `pip install stability-analysis-agent` | 由 SkillManager 管理 |

框架文档见 `docs/skills/README.md`（Skill System 规范，非本能力包）。
