# CLI 指南

本指南覆盖 `cli/main.py` 命令行入口的常见用法。完整参数列表见 [CLI_COMMANDS_REFERENCE.md](./CLI_COMMANDS_REFERENCE.md)。

## 入口与定位

- **权威入口**：`cli/main.py`
- **主要能力**：
  - 崩溃分析（解析日志 + 地址解析 + 代码上下文 + 可选 AI）
  - 向量库运维命令（初始化、统计、导入导出、反馈、衰减、GC）
  - 插件扩展（`--plugin-module`）

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
> 交互首屏不做自动环境检测；配置大模型/配置 addr2line 工具时会先做对应检测并展示结论。`2) 更多选项` 同时提供命令参考（单屏说明常用参数与子命令作用）、手动输入命令示例，以及 **高级选项（手动编辑配置文件、执行引擎切换、AI 模式开关、执行范围切换等）**，`1) 快速开始分析` 路径保持精简。
> 参数采集完成后会直接执行，不再二次确认；执行前会提示“运行中按 `Ctrl+C` 可终止当前任务”。

### 2) 跳过 AI（推荐回归测试）

```bash
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --skip-ai
```

### 3) 解析模式

```bash
# 只做解析 + 地址解析（不需要 code_root）
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --parse-only

# 只解析崩溃日志
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --parse-log-only
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
- `active_provider` 指向当前启用的 provider
- `active_provider` 的值必须是 `llm_config.providers` 下的某个 key（例如 `openai` / `deepseek`）

### Provider 配置与请求格式

- 配置示例见：`tools/configs/agent_config.local.example.json`
- 可将公共项放在 `llm_config.provider_defaults`（如 `auth_header`、`auth_prefix`、`request_timeout`），各 provider 仅覆盖差异字段。
- 每个 provider 建议至少包含：
  - `model`
  - `base_url`
  - `auth_type`（`api_key` / `authorization` / `none`）
- 当前 AI 分析请求默认按 **OpenAI Chat Completions 兼容格式** 发送；`base_url` 使用配置原值（仅去除末尾 `/`），请填写完整请求地址。
- 可通过 `request_format` 字段标记 provider 的协议类型：
  - `openai_chat_completions_compatible`
  - `anthropic_messages_compatible`
  - `openai_responses_compatible`
  - `minimax_text_chatcompletion_v2_compatible`
  - `custom_unsupported_need_adapter`
- `base_url` 使用配置原值（仅去除末尾 `/`），建议始终填写完整 endpoint（例如 `.../v1/chat/completions` 或 `.../v1/messages`）。
- 若使用 Anthropic 协议网关，通常需要在 provider 中显式配置：
  - `request_format: anthropic_messages_compatible`
  - `auth_header: x-api-key`
  - `auth_prefix: ""`
  - `base_url: .../v1/messages`
- 当 provider 非 OpenAI 兼容协议时，需要新增 adapter 后再启用（仅改配置不足以跑通）。

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
  - 检查 `--skip-ai` 是否开启
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
