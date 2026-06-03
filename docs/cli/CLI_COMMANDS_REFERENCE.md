# Stability Analysis Agent — CLI 命令参考

本文档与仓库根目录下的 **`cli/main.py`**（Tool System 统一入口）保持一致。  
CLI 为**扁平参数**（含少量管理子命令）：崩溃分析主流程与向量库运维类参数通过**是否携带向量库相关开关**区分；携带向量库运维参数时会**执行完即退出**，不跑崩溃分析。另提供 `config/profile/cancel` 子命令用于配置管理、会话模板与 daemon 任务取消。

**入口与典型工作目录**（在仓库根目录执行）：

```bash
python3 cli/main.py [参数...]
```

---

## 1. 崩溃分析主流程（常用）

### 1.1 参数一览

| 参数 | 是否必填 | 作用 |
|------|----------|------|
| `--crash-log PATH` | 分析时**必填**（向量库子命令除外） | 崩溃日志文件路径；`-` 表示从 **stdin** 读取。 |
| `--library-dir DIR` | 建议填写 | 符号库目录，供 `add2line_resolver`（如 `atos` / `addr2line`）解析堆栈。 |
| `--code-root DIR` | 建议填写 | 代码根目录；可**多次**指定，在多根目录下查找源码。 |
| `--config PATH` | 否 | `SystemConfig` JSON 文件；不指定时使用内置默认工具链 + `crash_analysis` 工作流。 |
| `--scope {full,prompt_only,parse_only,parse_log_only}` | 否 | Agent 执行流程范围，默认 `full`。详见下方“`--scope` 取值”。 |
| `--engine {direct,langchain,langgraph}` | 否 | 传给 `LLMConfig` 的引擎标记，默认 `direct`。 |
| `--output-format {markdown,json,text}` | 否 | 终端打印/写入 `--output-file` 的格式，默认 `markdown`。 |
| `--output-file PATH` | 否 | 将上述格式结果写入文件；不指定则打印到 **stdout**。 |

#### `--scope` 取值

| 取值 | 工具链装配 | 是否调用 LLM | 是否生成提示词文件 |
|------|------------|--------------|--------------------|
| `full`（默认） | parser + addr2line + code_content_provider | 是 | 是（同时作为 LLM 输入） |
| `prompt_only` | parser + addr2line + code_content_provider | 否 | 是（落盘到 `round_0/05_ai_final_tip.txt`） |
| `parse_only` | parser + addr2line | 否 | 否 |
| `parse_log_only` | parser | 否 | 否 |

### 1.2 传给工作流的问题上下文（内部字段）

以下参数会进入 `crash_analysis` 的 `problem` 字典，供 RAG 相关逻辑使用：

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `--vector-db-path` | `./vector_db` | 向量数据库目录。 |
| `--vector-db-max-results` | `3` | 向量检索最大条数。 |
| `--rule-confidence-threshold` | `0.85` | 规则高置信阈值。 |

### 1.3 最小示例（完整分析 + AI）

```bash
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir
```

LLM 密钥与默认厂商/模型来自 **`tools/configs/agent_config.json`**；若存在 **`tools/configs/agent_config.local.json`**，则与 `cli/main.py` 中 `_load_agent_config_file()` 行为一致：**仅读取 local 文件**（不与其合并）。智谱等在实现上通过 **OpenAI 兼容客户端**调用，因此需在环境中安装 **`openai`** Python 包（见 `pyproject.toml` / `requirements.txt`）。

---

## 2. AI 自动改码与备份（`cli/main.py` 扩展）

在 `--scope full`、分析成功且 LLM 适配器可用时，下列行为生效。

| 参数 | 默认 | 作用 |
|------|------|------|
| `--apply-ai-fixes` / `--no-apply-ai-fixes` | **开启** | 是否在首轮 AI 分析后，再经一次结构化调用，将候选函数**回写到 `--code-root` 内源码**。关闭后只分析、不改文件。 |
| `--backup-original-sources` / `--no-backup-original-sources` | **开启** | 回写前是否在当次 **`cli_reports/<run>/original_sources/`** 下保存**改前**源码副本。若工程已由 Git 管理、习惯用 `git checkout` 撤销，可加 **`--no-backup-original-sources`** 省略磁盘备份。 |

**说明**：

- 改码范围受限于本次 `code_content_provider` 图节点中的**函数签名 + 片段**，避免模型改到未出现在上下文中的路径。
- 每次成功跑完主流程后，会在仓库 **`cli_reports/<时间戳>_analysis_<scope>_<engine>_<crash名>/`** 下写入解析 JSON、（若有）AI 文本、改码结果等（其中 `<scope>` 取自 `--scope`，例如 `analysis_full`、`analysis_prompt_only`）；终端仍会打印摘要，并在 stderr 提示报告目录路径。

---

## 3. 输出与 `cli_reports` 落盘

| 行为 | 说明 |
|------|------|
| 终端 / `--output-file` | 与 `--output-format` 一致，为面向阅读的摘要（markdown/json/text）。 |
| `cli_reports/.../` | 每次分析成功后会尽量写入：`01_crash_log_parser.json`（所有 scope）、`02_add2line_resolver.json`（`full`/`prompt_only`/`parse_only`）、`03_code_content_provider.json`（`full`/`prompt_only`）、`round_0/05_ai_final_tip.txt`（`full`/`prompt_only`）、`round_0/06_ai_res.txt`（仅 `full`）。若执行了改码逻辑，还会写入 `06_fix_extract_debug.json`（提取阶段：含 `fix_plan` 与覆盖率统计）与 `06_apply_ai_fixes.json`（应用结果）。`final_output.md` 仅在 `--scope full` 下生成，且内容与 `round_0/06_ai_res.txt` 保持一致（均为模型原始返回）。 |

---

## 4. 向量数据库（RAG）运维（独占子流程）

以下任一参数出现时，CLI **只执行对应向量库逻辑并退出**，**不会**再要求 `--crash-log` 或跑崩溃分析：

| 参数 | 作用 |
|------|------|
| `--init-vector-db` | 清空并初始化向量库，写入种子数据。 |
| `--vector-db-stats` | 打印向量库统计（JSON）。 |
| `--export-vector-db [PATH]` | 导出快照；省略路径时使用默认路径（见实现）。 |
| `--import-vector-db PATH` | 从快照 JSON 导入（upsert）。 |
| `--pattern-feedback ID` | 记录 pattern 反馈（须配合 `--feedback-type`）。 |
| `--feedback-type {adopted,rejected}` | 反馈类型。 |
| `--feedback-comment TEXT` | 反馈备注。 |
| `--vector-db-decay FLOAT` | 置信度衰减。 |
| `--vector-db-gc` | 模式治理（低置信/高拒绝等）。 |
| `--gc-min-confidence FLOAT` | GC 最低置信阈值，默认 `0.2`。 |
| `--gc-rejected-threshold INT` | GC 拒绝次数阈值，默认 `5`。 |

依赖 RAG 相关模块；若环境缺少依赖，CLI 会报错退出（见 `cli/main.py` 中 `_run_vector_db_command`）。

---

## 5. 第三方扩展

| 参数 / 环境变量 | 作用 |
|-----------------|------|
| `--plugin-module MODULE` | 可重复；模块需提供 `register_all(registry)` 或 `register(registry)`。 |
| `STABILITY_AGENT_PLUGIN_MODULES` | 逗号分隔模块名，效果与多次 `--plugin-module` 类似。 |

---

## 6. 管理子命令

| 子命令 | 作用 |
|--------|------|
| `config` | 进入/执行配置相关命令。 |
| `profile` | 管理会话模板（list/show/use/save/delete）。 |
| `cancel` | 取消 daemon 中正在运行的任务。 |

### 6.1 取消 daemon 任务

```bash
# 取消默认 daemon（http://127.0.0.1:8765）中的任务
python3 cli/main.py cancel <run_id>

# 指定 daemon 地址
python3 cli/main.py cancel <run_id> --daemon http://127.0.0.1:8765
```

---

## 7. 参数组合速查

| 场景 | 建议参数 |
|------|----------|
| 完整分析 + 默认改码 + 默认备份 | `--crash-log ... --library-dir ... --code-root ...` |
| 分析且改码，但不要磁盘备份（Git 撤销） | 在上行基础上加 `--no-backup-original-sources` |
| 只分析、不改源码 | `--no-apply-ai-fixes` |
| 只要工具链、不要 LLM | `--scope prompt_only` |
| 仅解析 + 符号化 | `--scope parse_only` |
| 仅解析崩溃日志 | `--scope parse_log_only` |
| 向量库统计 | `--vector-db-stats` |
| 初始化向量库 | `--init-vector-db` |

---

## 8. 文档维护

- **实现来源**：仓库根目录 `cli/main.py` 中 `build_parser()` 与 `main()`。
- **最后更新**：2026-04-20（与当前 Tool System CLI 行为对齐）。
- 若增减参数或改变 `cli_reports` / 改码语义，请同步更新本文件。

**说明**：若你曾在旧版本文档中见到 `tools/cli/main.py`、`--daemon` 等条目，**当前本仓库以 `cli/main.py` 为准**；其他入口（如 `agent/ai_stability_agent.py`、daemon）的参数不在此文件覆盖范围内。
