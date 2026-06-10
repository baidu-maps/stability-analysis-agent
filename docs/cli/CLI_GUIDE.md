# CLI 指南

本指南覆盖 `cli/main.py` 命令行入口的常见用法。完整参数列表见 [CLI_COMMANDS_REFERENCE.md](./CLI_COMMANDS_REFERENCE.md)。

`--crash-log` 不限文件后缀，支持 Apple `.crash`、Android/Harmony 文本栈、Harmony `crashDiagnosis` JSON、Sentry/Crashlytics/Bugsnag 等 JSON 导出；详见 [崩溃日志格式说明](../tools/CRASH_LOG_FORMATS.zh-CN.md)。

## 入口与定位

- **权威入口**：`cli/main.py`
- **主要能力**：
  - 崩溃分析（解析日志 + 地址解析 + 代码上下文 + 可选 AI）
  - 向量库运维命令（初始化、统计、导入导出、反馈、衰减、GC）
  - 插件扩展（`--plugin-module`）
  - Skill 管理（`sa-agent skill ...`）

## 安装依赖

- **Python**：最低 3.9，推荐 3.10–3.12（详见 [INSTALL_TROUBLESHOOTING.md](./INSTALL_TROUBLESHOOTING.md)）
- **核心**（解析 / 符号化 / LLM）：`pip install stability-analysis-agent` 或 `pipx install stability-analysis-agent`
- **含向量库 RAG**：`pip install "stability-analysis-agent[rag]"` 或 `pipx install "stability-analysis-agent[rag]"`

未安装 `[rag]` 时，崩溃分析主流程仍可用，相似案例向量检索会自动跳过。ML 栈导入失败、SSL、`nn` 未定义等见 [INSTALL_TROUBLESHOOTING.md](./INSTALL_TROUBLESHOOTING.md)。

## 快速开始

### 1) 完整分析

```bash
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir
```

> 交互模式（直接运行 `sa-agent`）支持快捷复跑：当存在最近一次分析记录时，菜单会显示 `5) 再次进行上一次分析`，可一键复用上次参数重跑。
> 菜单型选择支持上下键切换，回车确认（也兼容数字键）。
> 交互首屏不做自动环境检测；配置大模型/配置 addr2line 工具时会先做对应检测并展示结论。`2) 设置` 提供 **配置大模型 / 配置堆栈地址解析工具 / 高级选项（手动编辑配置文件、AI 推理模式切换、Agent 执行流程切换等）**；`3) 帮助` 提供 **全部命令参考（完整参数手册）/ 命令快速示例（最小可运行）**，方便随时查阅；`1) 快速开始分析` 路径保持精简。
> 参数采集完成后会直接执行，不再二次确认；执行前会提示“运行中按 `Ctrl+C` 可终止当前任务”。

### 2) gen_prompt_only 模式（推荐回归测试）

```bash
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
```

默认生成的 `round_0/05_ai_prompt.md` 使用 `--prompt-mode analysis`：提示词偏证据分析、置信度判断和“不足以定位时说明缺失证据”，不会强制模型必须输出修复代码。若需要回到补丁导向提示词，可显式指定：

```bash
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only \
  --prompt-mode fix
```

`--prompt-mode` 只控制提示词内容，不控制是否自动应用修复。`--scope full` 下是否尝试回写源码仍由 `--apply-ai-fixes` / `--no-apply-ai-fixes` 决定；如果模型没有输出可提取的完整修复代码，自动改码会自然跳过。

在弱归因或源码上下文不足的 case 中，可以启用轻量多轮上下文补充。该能力和 `--engine` 解耦，`direct` / `langchain` / `langgraph` 都可使用：

```bash
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope full \
  --prompt-mode analysis \
  --agent-loop context_loop \
  --max-agent-rounds 3
```

`context_loop` 首轮仍使用 `round_0/05_ai_prompt.md`。如果模型输出 `need_more_context=true` 和 `context_requests[]`，Agent 会按请求补充函数源码并生成 `round_1/05_ai_prompt.md` 继续询问。每轮输出保存在对应 `round_N/06_ai_gen_res.md`。

### 3) 解析模式

```bash
# 只做解析 + 地址解析（不需要 code_root）
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --scope parse_stack_only

# 只解析崩溃日志
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --scope parse_log_only
```

### 4) 指定引擎

```bash
python3 cli/main.py ... --engine direct
python3 cli/main.py ... --engine langchain
python3 cli/main.py ... --engine langgraph
```

### 5) 通过 Daemon 执行（推荐高频调用）

```bash
# 终端 A：启动 daemon
python3 daemon/server.py --host 127.0.0.1 --port 8765

# 终端 B：CLI 通过 daemon 执行
python3 cli/main.py --daemon http://127.0.0.1:8765 \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir
```

## 配置加载规则（LLM）

- 若传入 `--config` 且其中定义了 `llm`，优先使用 `--config`
- 否则固定读取：`~/.config/stability-analysis-agent/agent_config.local.json`
- 不再从当前工作目录或仓库内配置回退，避免多来源导致行为不确定
- 交互菜单中的 **「厂商」** 对应配置文件里的 **`active_provider`**：即当前启用哪一条 `providers` 下的配置。
- `active_provider` 的值必须是 `llm_config.providers` 下的某个 key（例如 `openai` / `deepseek`）；自定义网关等可另取键名（菜单里称「自定义厂商或配置标识名」）。

## 符号化工具（堆栈地址解析）

- **配置文件**：默认使用 `~/.config/stability-analysis-agent/add2line_resolver_config.local.json`（亦支持工作目录 `configs/` 下同名文件等候选路径；可用环境变量 `STABILITY_AGENT_ADD2LINE_CONFIG_FILE` 指向单一文件）。字段说明与示例见 `tools/configs/add2line_resolver_config.local.example.json` 与 [docs/tools/addr2line/README.md](../tools/addr2line/README.md)。
- **交互式配置**：`设置` → `配置堆栈地址解析工具` 中，**「自动获取（推荐）」** 会按本机探测结果写入 `tool_paths` / `environment_vars`；**「手动设置符号化工具绝对路径」** 可输入 **可执行文件路径**（如 `.../llvm-addr2line`）或 **含该工具的目录**，写入配置时统一为 `tool_paths` 中的目录项。
- **快速开始**：在需要符号化的流程里，会在拦截用户前**静默尝试**与「自动获取」一致的写入，便于首次使用即落盘。

### 厂商配置与请求格式（`llm_config` / `providers`）

- 配置示例见：`tools/configs/agent_config.local.example.json`
- 可将公共项放在 `llm_config.provider_defaults`（如 `auth_header`、`auth_prefix`、`request_timeout`），**各厂商条目**（`providers` 下的每个 key）仅覆盖差异字段。
- 每个厂商条目建议至少包含：
  - `model`
  - `base_url`
  - `auth_type`（`api_key` / `authorization` / `none`）
- 当前 AI 分析请求默认按 **OpenAI Chat Completions 兼容格式** 发送；`base_url` 使用配置原值（仅去除末尾 `/`），请填写完整请求地址。
- 可通过 `request_format` 字段标记该厂商所用协议的请求格式：
  - `openai_chat_completions_compatible`
  - `anthropic_messages_compatible`
  - `openai_responses_compatible`
  - `minimax_text_chatcompletion_v2_compatible`
  - `custom_unsupported_need_adapter`
- `base_url` 使用配置原值（仅去除末尾 `/`），建议始终填写完整 endpoint（例如 `.../v1/chat/completions` 或 `.../v1/messages`）。
- 若使用 Anthropic 协议网关，通常需要在对应厂商条目中显式配置：
  - `request_format: anthropic_messages_compatible`
  - `auth_header: x-api-key`
  - `auth_prefix: ""`
  - `base_url: .../v1/messages`
- 当某厂商所用协议非 OpenAI 兼容时，需要新增 adapter 后再启用（仅改配置不足以跑通）。

### 自定义厂商与示例文件说明

- `tools/configs/agent_config.local.example.json` 中的 `providers` **只保留**常用预置厂商键名，**不包含**仅占演示用的自定义键（例如历史上的 `your_new_provider`）。原因：用户若整份复制为 `~/.config/stability-analysis-agent/agent_config.local.json`，交互式 CLI 会把 `providers` 下**所有**键都列入「请选择大模型厂商」，多余的占位键会干扰选择。
- 需要自建兼容网关、Anthropic 协议网关或 OpenAI Responses 等时，请在本节下方「多协议最小配置示例」的 JSON 模板中**自选有意义的键名**（如 `my_gateway`、`my_responses_provider`），再按需合并进自己的 `agent_config.local.json`，并设置 `llm_config.active_provider` 指向该键。

### 多协议最小配置示例

```json
{
  "llm_config": {
    "active_provider": "openai",
    "providers": {
      "openai": {
        "api_key": "YOUR_OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o"
      }
    }
  }
}
```

OpenAI Chat Completions 兼容自建网关（`providers` 下的键名请自行命名，避免使用仅占位的 `your_*`，以免与交互菜单混淆）：

```json
{
  "llm_config": {
    "active_provider": "my_openai_compatible",
    "providers": {
      "my_openai_compatible": {
        "api_key": "YOUR_API_KEY",
        "request_format": "openai_chat_completions_compatible",
        "base_url": "https://your-provider.example.com/v1/chat/completions",
        "model": "your-model-name"
      }
    }
  }
}
```

```json
{
  "llm_config": {
    "active_provider": "my_claude_gateway",
    "providers": {
      "my_claude_gateway": {
        "request_format": "anthropic_messages_compatible",
        "auth_type": "api_key",
        "auth_header": "x-api-key",
        "auth_prefix": "",
        "api_key": "YOUR_ANTHROPIC_AUTH_TOKEN",
        "base_url": "https://your-gateway.example.com/v1/messages",
        "model": "Claude Haiku 4.5"
      }
    }
  }
}
```

```json
{
  "llm_config": {
    "active_provider": "my_responses_provider",
    "providers": {
      "my_responses_provider": {
        "request_format": "openai_responses_compatible",
        "api_key": "YOUR_API_KEY",
        "base_url": "https://your-provider.example.com/v1/responses",
        "model": "your-responses-model"
      }
    }
  }
}
```

## 向量数据库（RAG）命令

```bash
# 初始化（清空后写种子）
python3 cli/main.py --init-vector-db

# 统计
python3 cli/main.py --vector-db-stats

# 导出快照
python3 cli/main.py --export-vector-db

# 导入快照
python3 cli/main.py --import-vector-db /path/to/snapshot.json

# 记录反馈
python3 cli/main.py --pattern-feedback pattern_xxx --feedback-type adopted --feedback-comment "有效"

# 衰减与 GC
python3 cli/main.py --vector-db-decay 0.01
python3 cli/main.py --vector-db-gc --gc-min-confidence 0.2 --gc-rejected-threshold 5
```

## 任务终止与取消

```bash
# 本地 CLI 运行中：直接按 Ctrl+C

# daemon Run API 任务取消
python3 cli/main.py cancel <run_id>
python3 cli/main.py cancel <run_id> --daemon http://127.0.0.1:8765
```

## 故障排查建议

- **无 AI 输出**：
  - 检查 `--scope` 是否为 `full`（其它取值会跳过 LLM 调用）
  - 检查 `tools/configs/agent_config.local.json` 是否正确配置
- **向量检索未生效**：
  - 先看 `--vector-db-stats`
  - 检查依赖（例如 `chroma-hnsw-lib`）
- **输出路径问题**：
  - 使用 `--output-file` 指定落盘路径

## 相关文档

- [CLI_COMMANDS_REFERENCE.md](./CLI_COMMANDS_REFERENCE.md) - 完整参数参考
- [DAEMON_SERVER_GUIDE.md](./DAEMON_SERVER_GUIDE.md) - Daemon 服务指南
- [INTERACTIVE_CLI_DESIGN.md](./INTERACTIVE_CLI_DESIGN.md) - 交互式 CLI 设计方案
- [rag/README.md](../rag/README.md) - RAG 向量库说明
