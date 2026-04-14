# Stability Analysis Agent — CLI 命令参考

本文档汇总 `tools/cli/main.py`（即 Agent 命令行入口）当前支持的参数及其作用。  
**说明**：CLI 为**单入口扁平参数**（无子命令），通过不同参数组合形成「完整分析 / 仅工具链 / 咨询 / 向量库运维」等用法。

**典型调用**（在项目 `main/` 目录下）：

```bash
python3 tools/cli/main.py [参数...]
```

---

## 1. 输入与数据源

| 参数 | 作用 |
|------|------|
| `--crash-log PATH` | 崩溃日志文件路径；使用 `-` 表示从 **标准输入 stdin** 读取。 |
| `--crash-log-dir DIR` | **批量模式**：指定包含多个崩溃日志的目录；须配合 `--skip-ai`、`--parse-only` 或 `--parse-log-only` 之一使用。 |
| `--library-dir DIR` | 符号解析用的**库文件目录**（供 `addr2line`/`atos` 等解析堆栈中的模块）。可为文件或目录。 |
| `--code-root DIR` | **代码根目录**，可多次指定，按顺序在多仓库场景中查找源码。 |
| `--config PATH` | 可选：Agent / LLM 等**配置文件路径**（如 `agent_config.json`）。 |

---

## 2. 分析流程模式（核心）

| 参数 | 作用 |
|------|------|
| （默认，无下方开关） | **完整流程**：崩溃日志解析 → 地址解析 → 代码上下文提取 → AI 分析（及可选向量库增强）。 |
| `--skip-ai` | **跳过 AI**，只运行前序工具链（解析 + 符号解析 + 代码内容等，具体以 Agent 实现为准）。 |
| `--parse-only` | 只执行**崩溃日志提取 + 地址解析**，不执行代码内容生成与 AI 分析。 |
| `--parse-log-only` | **仅第一步**：只执行崩溃日志解析（`crash_log_parser`），不进行地址解析及后续步骤；适合调试解析器。 |

**约束**：`--parse-only` 与 `--parse-log-only` **不能同时使用**。

---

## 3. 执行引擎与 AI 行为

| 参数 | 作用 |
|------|------|
| `--engine {direct,sequential,langgraph}` | 执行引擎：`direct`（默认，单次直连大模型）、`sequential`（兼容旧版 LangChain 流程）、`langgraph`（多轮增强分析）。 |
| `--optimized` | 使用**优化分析流程**（并行、压缩上下文等，具体以实现为准）。 |
| `--streaming` | 启用 **流式输出**（实时显示 AI 分析过程）。 |
| `--no-streaming` | 显式关闭流式，使用传统一次性响应。 |
| `--disable-vector-db` | **禁用向量数据库**（RAG）；优先级高，显式关闭。 |
| `--enable-vector-db` | **显式启用向量数据库**（在默认或 `--skip-ai` 等可能关闭向量库的场景下按需打开；可能明显增加耗时）。 |

---

## 4. 咨询模式（与崩溃文件无关）

| 参数 | 作用 |
|------|------|
| `--consultation` | 进入**咨询模式**：不向 Agent 传入崩溃日志，仅根据提示词对话。 |
| `--prompt TEXT` | 咨询提示词；**咨询模式下必填**。 |

---

## 5. 输出与落盘

| 参数 | 作用 |
|------|------|
| `--output-format {markdown,json,text}` | 终端/最终输出的格式，默认 `markdown`。 |
| `--output-dir DIR` | 默认落盘目录，默认 `cli_reports/`。 |
| `--output-file PATH` | 指定**单一输出文件**路径（优先级高于 `--output-dir`）。 |
| `--no-save-output` | **不保存**最终输出到文件（默认会写入带时间戳的文件）。 |
| `--no-save-final-output` | 不生成 `cli_reports` 下的**最终摘要文件**（run bundle 目录仍可能保留，除非同时 `--no-save-run-bundle`）。 |
| `--no-save-run-bundle` | **不生成 run bundle** 目录（默认会在 `cli_reports/<时间戳>_.../` 下保存各工具 JSON + AI 输出等）。 |
| `--save-run-extra` | 在 run bundle 中**额外写入调试信息**（如 `00_run_meta.json`）。 |
| `--save-raw-content` | 在 `01_crash_log_parser.json` 中保留 **`raw_content`** 字段（默认不保存以减小体积）。 |

---

## 6. 崩溃日志解析（第一步）相关

| 参数 | 作用 |
|------|------|
| `--filter-frames-by-library-dir` | 指定 `--library-dir` 时，**仅保留**能在该目录下匹配到库的堆栈帧（**默认开启**）。 |
| `--no-filter-frames-by-library-dir` | 关闭按库目录裁剪，保留解析到的**全部帧**（可能含大量系统库）。 |
| `--max-threads N` | 解析结果中最多保留的**线程块数量**（按出现顺序），默认 `4`。详见 **§6.1**（与 iOS `Crashed Thread` 语义正交）。 |
| `--max-primary-frames N` | **主线程（primary）**最多保留的帧数，默认 `50`。详见 **§6.1**。 |
| `--max-background-frames N` | **非主线程**最多保留的帧数，默认 `20`。详见 **§6.1**。 |
| `--crash-segment-index N` | 在已截取范围内，日志中存在多处 `backtrace:` 时解析第 **N** 段（1-based），默认 `1`。详见 **§6.1**（与「选崩溃线程块」为先后关系）。 |

### 6.1 崩溃线程、`--max-threads` 与 `--crash-segment-index`（语义区分）

三者解决的是**不同维度**的问题，不要混用含义：

| 概念 | 含义 | 典型生效平台 |
|------|------|----------------|
| **崩溃线程 / 主栈从哪来** | 由解析器按**日志格式**决定：例如 iOS 根据 `Crashed Thread: N` 截取 **`Thread N:`**（或 `Last Exception Backtrace` 等更高优先级段）；Android/HarmonyOS 带 `Tid:` 时按 Tid 块与 `Fault thread info:` 等决定 primary。 | iOS 分支、Android/HarmonyOS Tid 分支 |
| **`--max-threads` 等** | 限制 **`01` 里保留多少个线程块**、每块最多多少帧；用于控制 JSON 体积。 | **主要**在 **HarmonyOS/Android 且含 `Tid:`** 的多线程 dump 路径生效 |
| **`--crash-segment-index`** | 在**已经截取好的文本范围**内，按 **`backtrace:`** 标题分成多段时，取第 **N** 段；无 `backtrace:` 时整段算 1 段（典型 **Apple `.crash`** 即如此）。 | 与平台无强绑定，但与「多段 backtrace」日志最相关 |

**要点**：

- **iOS**：`Crashed Thread` 决定读 **`Thread N`** 那一块栈，**不是**由 `--max-threads` 决定；`--max-threads` 在该路径下**通常不改变**「选哪条崩溃线程」。
- **先后顺序**：先确定 `scope_content`（如 iOS 的崩溃线程块），再在块内应用 **`--crash-segment-index`**（仅当存在多段 `backtrace:` 时有意义）。
- **Android Tid 分块**：若使用 **`--crash-segment-index > 1`**，实现可能 **忽略**该参数并打日志告警，避免与 Tid 多线程语义冲突。
- **macOS**：若识别为 `macos`，可能与 iOS 的 `Crashed Thread` 切块**不一致**；需要 Apple 风格统一行为时请以源码/发布说明为准或向维护者反馈。

### 6.2 主崩溃函数选取优化（2026-04-09）

| 概念 | 说明 |
|------|------|
| **栈顶优先** | 在可提取源码上下文的候选帧中，按调用栈**自上而下**选择首个可用帧，不再跨帧按"崩溃行诊断分"择优，保证 `crash_summary` 与原始栈序语义一致。 |
| **默认跳过系统库** | 主崩溃帧筛选时优先排除 `libsystem` / `libdispatch` / `libobjc` / `libc++` / `UIKit` / `CoreFoundation` 等系统模块；若无业务候选帧，再回退到系统模块候选。 |
| **崩溃代码行限制** | 当 `resolved_line` 缺失、不可用，或落在已提取函数片段范围外时，**仅在当前崩溃函数体范围内**启发式重选 `crash_line_code` 与 `crash_line_number`，不再跳到其它函数。 |
| **`crash_line_note` 文案细化** | 根据 `from_add2line` / `from_log_deduce`、`rescue_reason`、是否函数内重选展示行等条件生成说明，明确区分"精确解析行"与"函数内启发式展示行（非指令级 PC）"。 |

效果：在"已符号化但缺 file:line"的 iOS 崩溃场景中，`03` 的 `crash_summary.function/node_id` 更稳定对齐栈顶业务函数。

---

## 7. 地址解析与代码提取（工具链）相关

| 参数 | 作用 |
|------|------|
| `--parse-lines N` | 限制 **add2line 解析**时从栈顶开始处理的帧数（用于降噪或加速）；默认不限制。 |
| `--code-context-timeout-sec SEC` | 第三步工具 `code_content_provider` 超时秒数（默认 **`240`**）；**`0`** 表示不限制；合法为 **`0`** 或 **`1～7200`**。超时后会中止重扫描/长循环并输出可解析 JSON（含 `code_context_phase_timed_out` 与可操作建议列表 `code_context_phase_timeout_avoidance_hints`），后续步骤可继续执行。该参数也用于替代此前的 `find-source` 超时判断。 |

---

## 8. 代码静态分析 / 代码解析器

| 参数 | 作用 |
|------|------|
| `--code-parser-backend {regex,tree_sitter,tree-sitter}` | 代码结构解析后端，默认 **tree-sitter**（与 `regex` 二选一）。 |
| `--exclude-dirs DIR ...` | 遍历代码时要**排除**的目录名列表（如 `test` `third_party`）。 |
| `--include-subdirs DIR ...` | **仅包含**这些子目录；未指定则包含全部（在实现允许范围内）。 |
| `--max-static-call-chain-depth N` | 静态调用链 `call_chain_from_code` 最大节点数（含崩溃函数），默认 `5`，至少 `1`。 |
| `--max-direct-callers N` | 「直接调用崩溃函数」候选最多 **N** 个，默认 `10`。 |
| `--max-shared-var-related-functions N` | 共享变量相关函数记录最多 **N** 条，默认 `10`。 |
| `--max-symbol-only-rescues N` | `02` 缺失 `resolved_file/resolved_line` 时，第三步按符号名兜底定位最多尝试 **N** 帧，默认 `5`；`0` 表示关闭该兜底。 |

### 8.1 `regex` 与 `tree-sitter`：适用场景说明

代码上下文（`code_content_provider`）依赖该参数决定**如何解析 C++ 结构**（函数边界、签名、片段等）。两者不是「谁绝对更好」，而是**速度与精度**的取舍。

| 后端 | 大致做法 | 更适合的场景 | 代价 / 注意 |
|------|----------|--------------|-------------|
| **tree-sitter**（**CLI 默认**） | 用语法树完整解析源码，在 AST 上取函数定义与范围 | 小中型代码根、希望**开箱即用更稳**；模板/宏/复杂换行较多、更看重**结构准确**时 | 对大仓库、多文件扫描时 **CPU 与耗时常明显更高**；依赖 `tree-sitter` / `tree-sitter-languages` |
| **regex** | 以行级模式匹配 + 启发式（如括号配对）抽取函数 | **`--code-root` 很大**（整仓引擎、地图 SDK 等）、**`--skip-ai` 只跑工具链**、优先要**几分钟内跑完**时 | 边界情况（重度宏、怪异格式）可能不如 tree-sitter；需接受偶发启发式误差 |

**为何 regex 往往更快**：tree-sitter 需对（大量）源文件做完整词法/语法分析；regex 路径多为字符串扫描与轻量规则，**单位文件成本更低**。大仓下第三步「代码上下文」若默认 tree-sitter，总耗时容易被解析阶段拉长。

**实践建议**：

- 默认不传参：保持 **tree-sitter**，与当前 CLI 默认值一致。
- 大仓 + 工具链/调试：显式加 **`--code-parser-backend regex`**，通常可显著缩短第三步耗时。
- 若某次结果中函数片段/签名明显异常，可改用 **tree-sitter** 对同一条崩溃再跑一遍对比。

`03_code_content_provider.json` 中的 **`code_parser_backend`** 字段会记录**本次实际使用**的后端，便于对照报告与命令行参数。

---

## 9. Patch 生成（可选）

| 参数 | 作用 |
|------|------|
| `--generate-patch` | 基于本次 AI 输出**生成 `fix.patch`**，写入 run bundle，供审阅或手动应用。 |
| `--apply-patch` | 在生成 patch 后尝试 **`git apply`**（含检查）；成功后可配合展示 diff（具体行为以实现为准）。 |

---

## 10. 向量数据库（RAG）运维

以下参数多数会**单独短路退出**（执行完即 `exit`，不跑崩溃分析）。

| 参数 | 作用 |
|------|------|
| `--vector-db-stats` | 打印向量数据库**统计信息**（JSON）。 |
| `--init-vector-db` | **初始化**向量数据库并加载崩溃知识库数据。 |
| `--add-sample-data` | 向向量库写入**示例 pattern/evidence**（用于测试）。 |
| `--vector-db-decay FLOAT` | 对 pattern **置信度衰减**（如 `0.01`）。 |
| `--vector-db-gc` | 执行向量库 **GC**，标记低质量 pattern；可配合下方阈值参数。 |
| `--gc-min-confidence FLOAT` | GC：最低置信度阈值，默认 `0.2`。 |
| `--gc-rejected-threshold INT` | GC：被拒绝次数阈值，默认 `5`。 |
| `--pattern-feedback ID` | 对指定 **pattern_id** 记录反馈（需配合 `--feedback-type`）。 |
| `--feedback-type {adopted,rejected}` | 反馈类型：采纳 / 拒绝。 |
| `--feedback-comment TEXT` | 反馈备注。 |
| `--export-vector-db [PATH]` | **导出**向量库快照 JSON；可省略路径使用自动路径。 |
| `--import-vector-db PATH` | 从 JSON **导入**向量库快照。 |

---

## 11. Daemon（本地 HTTP 服务）

| 参数 | 作用 |
|------|------|
| `--daemon URL` | 指定本地 daemon 基地址（如 `http://127.0.0.1:8765`）。**可用时**将分析请求委托给 daemon；不可用则**自动回退本地执行**。 |

**说明**：向量库相关独占子流程（统计、导入导出、GC、初始化等）**不会**走 daemon，仅在本地执行。

---

## 12. 参数组合速查

| 场景 | 建议参数 |
|------|----------|
| 完整分析 + 落盘 | `--crash-log ... --library-dir ... --code-root ...` |
| 只验证解析器 | `--parse-log-only --crash-log ...` |
| 解析 + 符号、不要 AI | `--parse-only --skip-ai`（或按项目习惯仅用 `--parse-only`） |
| 批量只解析日志 | `--crash-log-dir ... --parse-log-only` |
| 纯咨询 | `--consultation --prompt "..."` |
| 走 daemon | `--daemon http://127.0.0.1:8765 --crash-log ...` |
| 大代码仓、只要工具链、尽量快 | 在上一行基础上增加 `--code-parser-backend regex`（见 §8.1） |

---

## 13. 文档维护

- **实现来源**：`main/tools/cli/main.py` 中 `argparse` 定义及 `main()` 分支逻辑。
- **最后更新**：2026-04-09（v0.9.2）
- **版本对齐**：与 `CLI_RELEASE_NOTES_v0.9.2.md` 同步。
- 若参数增减或语义变更，请同步更新本文件。
