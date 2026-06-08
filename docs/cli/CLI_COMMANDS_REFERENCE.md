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
| `--crash-log PATH` | 分析时**必填**（向量库子命令除外） | 崩溃日志文件路径；`-` 表示从 **stdin** 读取。不限后缀（`.crash` / `.txt` / `.log` / `.json` 等），按**内容**选解析器。详见 [崩溃日志格式说明](../tools/CRASH_LOG_FORMATS.zh-CN.md)。 |
| `--library-dir DIR` | 建议填写 | 符号库目录，供 `add2line_resolver`（如 `atos` / `addr2line`）解析堆栈。 |
| `--code-root DIR` | 建议填写 | 代码根目录；可**多次**指定，在多根目录下查找源码。 |
| `--config PATH` | 否 | `SystemConfig` JSON 文件；不指定时使用内置默认工具链 + `crash_analysis` 工作流。 |
| `--scope {full,gen_prompt_only,parse_stack_only,parse_log_only}` | 否 | Agent 执行流程范围，默认 `full`。详见下方“`--scope` 取值”。 |
| `--prompt-mode {analysis,fix}` | 否 | `05_ai_prompt.md` / LLM 输入的提示词输出模式，默认 `analysis`。详见下方“`--prompt-mode` 取值”。 |
| `--agent-loop {single,context_loop}` | 否 | Agent 编排模式。未指定时随 `--prompt-mode`：`analysis`→`context_loop`，其它→`single`。`context_loop` 允许模型请求补充函数源码并继续多轮分析，独立于 `--engine`。 |
| `--max-agent-rounds N` | 否 | `context_loop` 最多 LLM 轮数。默认：`analysis` 模式 `3` 轮，其它模式 `1` 轮；显式指定时以参数为准（硬上限 `8`）。 |
| `--max-context-requests-per-round N` | 否 | `context_loop` 每轮最多处理的源码补充请求数，默认 `5`，硬上限 `16`。 |
| `--engine {direct,langchain,langgraph}` | 否 | 传给 `LLMConfig` 的引擎标记，默认 `direct`。 |
| `--output-format {markdown,json,text}` | 否 | 终端打印/写入 `--output-file` 的格式，默认 `markdown`。 |
| `--output-file PATH` | 否 | 将上述格式结果写入文件；不指定则打印到 **stdout**。 |

#### `--scope` 取值

| 取值 | 工具链装配 | 是否调用 LLM | 是否生成提示词文件 |
|------|------------|--------------|--------------------|
| `full`（默认） | parser + addr2line + code_content_provider | 是 | 是（同时作为 LLM 输入） |
| `gen_prompt_only` | parser + addr2line + code_content_provider | 否 | 是（落盘到 `round_0/05_ai_prompt.md`） |
| `parse_stack_only` | parser + addr2line | 否 | 否 |
| `parse_log_only` | parser | 否 | 否 |

#### `--prompt-mode` 取值

`--prompt-mode` 只控制 `round_0/05_ai_prompt.md` 和 LLM 输入中的“输出契约”，不控制是否自动应用修复。自动应用修复仍由 `--apply-ai-fixes` 和模型输出中是否存在可提取的完整修复代码共同决定。

| 取值 | 行为 |
|------|------|
| `analysis`（默认） | 偏证据分析与置信度判断：要求模型说明证据是否足够、区分结论与推断，不强制输出修复代码。 |
| `fix` | 偏补丁输出：要求模型列出需要修改的函数，并输出完整可替换、可编译的修复代码。 |

#### `--agent-loop` 取值

`--agent-loop` 控制 Agent 是否允许多轮补充上下文，和 `--engine` 解耦：`direct`、`langchain`、`langgraph` 都可以使用 `context_loop`。

| 取值 | 行为 |
|------|------|
| （省略） | 随 `--prompt-mode`：`analysis`→`context_loop`，`fix` 等→`single`。 |
| `single` | 只调用一次 LLM；`round_0/05_ai_prompt.md` 作为输入，`round_0/06_ai_gen_res.md` 保存模型输出。 |
| `context_loop` | 当模型在输出中返回 `need_more_context=true` 和 `context_requests[]` 时，Agent 按请求定位函数源码，构造下一轮 prompt 继续询问，直到无请求或达到 `--max-agent-rounds`。 |

### 1.2 传给工作流的问题上下文（内部字段）

以下参数会进入 `crash_analysis` 的 `problem` 字典，供 RAG 相关逻辑使用：

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `--vector-db-path` | `./vector_db` | 向量数据库目录。 |
| `--vector-db-max-results` | `3` | 向量检索最大条数。 |
| `--vector-db-record-usage` | 关闭 | 分析时检索是否累加 `hit_count`（默认**只读**，避免污染库；显式加此 flag 才写入）。 |
| `--include-memory-in-05` / `--no-include-memory-in-05` | 关闭 | 是否将向量库检索得到的「规则与经验模式参考」并入 `05_ai_prompt.md` / LLM 输入；默认只写 `04_memory_context.json`，不并入 05。 |
| `--rule-confidence-threshold` | `0.85` | 规则高置信阈值。 |

### 1.3 最小示例（完整分析 + AI）

```bash
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir
```

LLM 密钥与默认厂商/模型来自 **`tools/configs/agent_config.json`**；若存在 **`tools/configs/agent_config.local.json`**，则与 `cli/main.py` 中 `_load_agent_config_file()` 行为一致：**仅读取 local 文件**（不与其合并）。智谱等在实现上通过 **OpenAI 兼容客户端**调用，因此需在环境中安装 **`openai`** Python 包（见 `pyproject.toml` / `requirements.txt`）。

### 1.4 支持的崩溃日志格式（摘要）

| 类别 | 示例 | `01` 中 `meta_info.log_format` |
|------|------|--------------------------------|
| Apple 文本 | `.crash`、`Exception Type:` | `ios_apple_crash` 等 |
| Android / Harmony 文本 | logcat、`Tid:`、`Stacktrace:`、`#NN pc` | `android_logcat` / `android_harmony_tid` / `harmony_stacktrace` |
| Harmony 平台 JSON 单行 | `crashDiagnosis:` / `crashDiagnsis:` + JSON | `harmony_crash_diagnosis_json` |
| Sentry / Crashlytics / Bugsnag JSON | 平台导出 event JSON | `sentry_event_json` / `firebase_crashlytics_json` / `bugsnag_event_json` |
| 其它 JSON 栈数组 | Bugly、自建 APM | `generic_json_stack_export` |

- **不能**把上一轮输出的 `01_crash_log_parser.json` 当作 `--crash-log` 输入。
- 完整说明与扩展方式：[../tools/CRASH_LOG_FORMATS.zh-CN.md](../tools/CRASH_LOG_FORMATS.zh-CN.md)

---

## 2. AI 自动改码与备份（`cli/main.py` 扩展）

在 `--scope full`、分析成功且 LLM 适配器可用时，下列行为生效。

| 参数 | 默认 | 作用 |
|------|------|------|
| `--apply-ai-fixes` / `--no-apply-ai-fixes` | **开启** | 是否在首轮 AI 分析后，再经一次结构化调用，将候选函数**回写到 `--code-root` 内源码**。关闭后只分析、不改文件。 |
| `--backup-original-sources` / `--no-backup-original-sources` | **开启** | 回写前是否在当次 **`cli_reports/<run>/original_sources/`** 下保存**改前**源码副本。若工程已由 Git 管理、习惯用 `git checkout` 撤销，可加 **`--no-backup-original-sources`** 省略磁盘备份。 |

**说明**：

- 改码范围受限于本次 `code_content_provider` 图节点中的**函数签名 + 片段**，避免模型改到未出现在上下文中的路径。
- `--prompt-mode` 不会干涉是否尝试自动改码：即使 `--prompt-mode analysis`，只要 `--apply-ai-fixes` 开启且模型输出中存在可提取的完整修复代码，后续仍会尝试应用；如果提取不到修复代码，则自然跳过。
- 每次成功跑完主流程后，会在仓库 **`cli_reports/<时间戳>_analysis_<scope>_<engine>_<crash名>/`** 下写入解析 JSON、（若有）AI 文本、改码结果等（其中 `<scope>` 取自 `--scope`，例如 `analysis_full`、`analysis_gen_prompt_only`）；终端仍会打印摘要，并在 stderr 提示报告目录路径。

---

## 3. 输出与 `cli_reports` 落盘

| 行为 | 说明 |
|------|------|
| 终端 / `--output-file` | 与 `--output-format` 一致，为面向阅读的摘要（markdown/json/text）。 |
| `cli_reports/.../` | 每次分析成功后会尽量写入：`01_crash_log_parser.json`（所有 scope）、`02_add2line_resolver.json`（`full`/`gen_prompt_only`/`parse_stack_only`）、`03_code_content_provider.json`（`full`/`gen_prompt_only`）、`03b_code_location_trace.json`（`full`/`gen_prompt_only`，`03` 生成过程中的代码定位审计轨迹，**仅供调试/二次消费，不写入** `05_ai_prompt.md`）、`04_memory_context.json`（`full`/`gen_prompt_only`，`vector_memory_retriever` **只读**检索快照；默认不并入 `05`，仅在 `--include-memory-in-05` 时追加到提示词，且**默认不回写**向量库）、`round_0/05_ai_prompt.md`（`full`/`gen_prompt_only`）、`round_0/06_ai_gen_res.md`（仅 `full`）。若启用 `--agent-loop context_loop` 且产生多轮，会继续写入 `round_1/`、`round_2/` 等目录，每轮包含 `05_ai_prompt.md`、`06_ai_gen_res.md` 与可选 `06_context_requests.json`，同时写入 `agent_rounds_summary.json`。若执行了改码逻辑，还会写入 `07b_fix_extract_debug.json.json`（提取阶段：含 `fix_plan` 与覆盖率统计）与 `07_apply_ai_fixes.json`（应用结果）。`final_output.md` 仅在 `--scope full` 下生成，且内容为最终一轮模型原始返回。 |

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
| 提示词偏证据分析（默认） | `--prompt-mode analysis` |
| 提示词要求完整修复代码 | `--prompt-mode fix` |
| 只要工具链、不要 LLM | `--scope gen_prompt_only` |
| 仅解析 + 符号化 | `--scope parse_stack_only` |
| 仅解析崩溃日志 | `--scope parse_log_only` |
| 向量库统计 | `--vector-db-stats` |
| 初始化向量库 | `--init-vector-db` |

---

## 8. 文档维护

- **实现来源**：仓库根目录 `cli/main.py` 中 `build_parser()` 与 `main()`。
- **最后更新**：2026-06-04（补充崩溃日志格式说明链接）。
- 若增减参数或改变 `cli_reports` / 改码语义，请同步更新本文件。

**说明**：若你曾在旧版本文档中见到 `tools/cli/main.py`、`--daemon` 等条目，**当前本仓库以 `cli/main.py` 为准**；其他入口（如 `agent/ai_stability_agent.py`、daemon）的参数不在此文件覆盖范围内。
