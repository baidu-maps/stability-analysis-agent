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
- 否则自动读取：
  1. `tools/configs/agent_config.local.json`（存在则独占）
  2. `tools/configs/agent_config.json`
- `default_provider` 支持 `openai` / `zhipu_bigmodel` / `deepseek` / `baidu_qianfan`

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
- [rag/README.md](../rag/README.md) - RAG 向量库说明
