# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

## [1.5.5] - 2026-10-16

### Changed

- 上下文构建：第一轮保留崩溃栈与写路径上的函数全文，不在锚点上且超过 6000 字符的函数只留签名；后续轮用「已发送源码索引」替换已发过的函数体，并附上上一轮分析作为工作记忆，避免同一份源码被完整重发。
- 分析输出预算只升不降：下限 12288、抬升 16384，不再把 4096 判成截断后降到 2048 并把同一份提示完整生成多遍。实际输出超过请求上限时视为网关未截断，不再重试。
- JSON 提取按括号配对扫描，忽略正文和 cpp 代码块里的花括号；优先取带 `agent_can_fetch_more` 的控制契约，改码计划用 `prefer_key=edits` 选取，结构化报告不再被尾部控制对象覆盖。

### Added

- 新增单测 `test/harness/test_agent_output_parser.py`，并补充上下文压缩与分析重试预算用例。

## [1.5.4] - 2026-10-16

### Added

- 新增 `services/code_location_cache.py`：代码定位（04b）结果跨运行磁盘缓存。同一卡片重复分析、各 `code_root` 的 git commit 与定位输入未变时直接复用，省去约 89s 重算；worktree 路径不同时按 code_root 下标做哨兵替换后再写回当前路径。环境变量 `SA_DISABLE_CODE_LOCATION_CACHE` 可关闭。
- 上下文字符预算支持环境变量 `SA_EVIDENCE_MAX_CHARS` 覆盖，便于验证发给 LLM 的上下文容量对耗时的影响。
- 新增单测 `test/tools/test_code_location_cache.py`，并补充 Daemon 对 `--no-use-ctags-index` 的转发用例。

### Changed

- ctags 函数索引默认开启（CLI `--use-ctags-index`、Daemon `RunRequest`、代码定位与仓库搜索）。未安装 universal-ctags 时自动回退 rg；`--no-use-ctags-index` 可显式关闭。
- 代码定位工具显式传入 `use_ctags_index=false` 时关闭索引，不再只在为真时透传。

## [1.5.1] - 2026-10-12

### Added

- `services/code_locator.py`：新增“类体内联方法定位”——日志无 `Class::method` 文本时，按方法名在 class/struct 体内定位内联方法定义（仅深度为 1 的直接成员，排除嵌套调用）。
- 新增单测 `test_in_class_method_locator`、`test_glued_so_build_id`、`test_changed_files_from_applied`，并补充 `test_agent_context_loop` 上下文装箱用例。

### Changed

- 上下文引擎（`services/context_engine.py` 等）重构为“整段装箱 + 字符预算”：契约 / 身份 / 崩溃摘要 / 任务为强制段落必整段保留；可选段落只发放得下的完整段落，放不下整段省略并给出说明；先前已定位源码在容量允许时整段重发；引入 prompt ledger——只有真正写入提示的符号才计为“模型已看见”。
- 上下文轮询：本轮一个源码请求都没定位到、或强制段落超出容量时停止，不再向模型发送残缺上下文。
- 改码从已有分析文本提取补丁、不调用模型，因此不再预扣 LLM 调用次数，避免已写出的修复在落盘前被预算直接拒绝。
- 精简改码安全校验（`services/safety_verification.py`）。

### Fixed

- 精简栈中 BuildId 紧贴 `.so`（如 `libBaiduMapSDK_map_v7_6_6.so[<buildId>]` 或 `.so [arm64-v8a::<buildId>]`）时，模块名只保留 `.so` 文件名，不再把 BuildId 并入模块名（`tools/crash_parser/stack_extract.py` / `android.py`）。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.5.1。

## [1.5.0] - 2026-09-21

### Added

- 新增 `knowledge_plane/`：Codebase Knowledge Pack 加载、路由、轻量检索与 ingest；与既有 RAG 规则/案例统一写入 `05_memory_context.json` / `memory_context`。
- 新增 `rag/retrieval_fusion.py`：规则、Pack、历史案例统一排序、去重与字符预算裁剪。
- CLI / Daemon / Web：Knowledge Pack 相关参数与偏好（`--knowledge-pack-dir` / `--knowledge-pack` / `--knowledge-max-hits` / `--knowledge-max-chars` / `--domain-knowledge`）。
- 新增 `tools/lifecycle_code_search_tool.py`，旁路落盘 `04b3_lifecycle_evidence.json`。
- 新增 `services/evidence_contract.py` 与协议侧 `source_identity` 字段，修复管道对源码身份不安全时门禁阻断。
- 修复管道支持可选 `syntax_build` 验证结果归一化；改码/定位/上下文引擎与 TOCTOU 写侧门禁增强。

### Changed

- Wheel 打包纳入 `knowledge_plane` 子包。
- 架构 / RAG / CLI 命令参考文档补充 Knowledge Pack 与融合检索说明。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.5.0。

## [1.4.1] - 2026-09-14

### Fixed

- Daemon：请求/协议类 ``ValueError``（含已移除字段如 ``skip_ai`` / ``code_root``、非法 ``engine``）统一映射为 HTTP **400** JSON，避免 Handler 未捕获导致客户端挂起 / HTTP 000。
- 报告标题层级：``final_output.md`` / 结构化分析报告改为 ``##``/``###``/``####``，避免 Markdown 预览中相对正文字号过大；Daemon 截取报告时同时识别新旧标题标记。

### Changed

- Prompt 结构化报告模板章节降级为 ``###``/``####``，并明确禁止使用 ``#``/``##`` 作为章节标题。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.4.1。

## [1.4.0] - 2026-09-14

### Added

- `parse_stack_only`：生成可读的 `final_output.md`（三列表格：原日志行号 / 原始堆栈地址 / addr2line 解析结果），与 `full` 一样落盘人类可读报告。
- 新增 `services/stack_resolve_report.py` 及单测，供 CLI / 报告产物复用。

### Changed

- Daemon `/result` 报告截取标题增加 `# 堆栈地址解析结果`，与全流程 `# 崩溃分析结果` 对齐。
- CLI 产物清单与主产物路径：`parse_stack_only` 优先指向 `final_output.md`。

### Documentation

- CLI 命令参考中 `final_output.md` 适用范围更新为 `full` / `parse_stack_only`。
- README / PyPI 发布说明中的最新版本号更新为 1.4.0。

## [1.3.11] - 2026-09-10

### Added

- 代码上下文：崩溃行实参求值被调（`arg_eval`）展开与跨文件被调解析，优先保留全局 buffer / `c_str` 等生命周期风险片段。
- Daemon `/status`、`/result`：从报告目录聚合 `completion_reason` / `stage_*` / `fix_notes` / `failure_detail`，便于数字员工可读失败原因。

### Fixed

- 自动改码：函数签名匹配更稳健（模板/`std::`/多行签名/NAMESPACE 宏短名），降低错抽或整段放弃。
- Context 预算压缩：崩溃行 / 同步 / 写路径等 pin 段按优先级装箱，避免高价值上下文被末尾一刀切裁掉。
- 修复管道：`changed_files` 与 applied 结果对齐；相关单测补充。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.11。

## [1.3.10] - 2026-09-09

### Fixed

- 自动改码提取：`class`/`struct` 骨架围栏不再整段放弃 fast-path；跳过骨架后继续提取后续完整
  `Class::method` / 析构函数围栏（修复「先类声明再函数」导致 0 edits）。
- 「需要修改的函数」解析：兼容「### 6.1 直接修复」等非正式段落（`修改Xxx()`、析构叙述、独立函数围栏签名）。
- Context-loop：修复代码含带占位的 class 骨架或臆造 `m_*Mutex`/`m_*Lock` 时强制续一轮纠正格式；
  无可用锁时应写入「需人工处理」，禁止把臆造锁当作可自动应用补丁。

### Changed

- Prompt（fix 模式）：明确禁止「修复代码」输出 class/struct 骨架；新增成员一律进「需人工处理」。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.10。

## [1.3.9] - 2026-09-09

### Fixed

- 自动改码写回：用 `newline=''` 读写以保留原文 CRLF/LF；替换函数时把 LLM 缩进对齐到原函数 Tab/空格风格；避免 `*/` 与函数签名挤到同一行。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.9。

## [1.3.8] - 2026-09-09

### Fixed

- Daemon `_emit_trace_handoff` 构造 `RunEvent` 时补上 `run_id`，避免产出 `runtime_trace` 的成功任务被标成 `status=error`。
- HTTP `_prepare_http_run_body` 在提供非空 `icafe_card_id` 时不再强制 `crash_log_content`；`_run_fingerprint` 纳入 `icafe_card_id`，避免不同卡片幂等指纹碰撞。
- `_build_cli_cmd` 在无日志来源时不再追加空的 `--crash-log-file ""`，避免企业层误判「调用方已自带日志」。
- Context-loop：接受 `file`→`read_file`、`medium`→`normal` 等常见别名；无效请求不再误标成「缺少 context_requests」合同错误；协议失败后的终局若已给出最终分析则记为 `model_final`，避免仅因 `invalid_schema` 阻断改码管道。提示词明确禁止 `type=file` / `priority=medium`，并区分 Markdown 修复示例与 `next_action.propose_fix`。
- 自动改码：类骨架优先从头文件构建并落盘 `member_fields`；`apply_fix_plan` 接线成员白名单门禁，拒绝臆造的 `m_*`（含同步原语）；对已有 `Lock/Unlock` 成员拒绝改用未出现的 `ScopedLock`；prompt 禁止假设新 mutex/lock 成员，并要求 TOCTOU 评估写路径；共享变量源码配额优先保留写/删路径。
- 类成员解析：剥离 `Type m_foo; /**< ... */` 行尾注释后再识别字段，避免 `CNMutex` 等声明漏入 `member_fields`；prompt 对大类骨架增加同步/共享指针声明摘录并截断完整骨架，避免锁名被裁掉。
- TOCTOU 终局写路径完整性门禁：当 prompt 含「写路径函数源码（压缩保活）」且分析主张共享状态竞态，
  但修复代码未给出写路径完整函数体时，context-loop 强制续一轮补齐（避免只改读侧就收工）。
  仅认「修复建议/修复代码」章节内的带同步原语函数体；根因证据区的 cpp 块不算已提交补丁。
- Context-loop / EvidenceStore 预算压缩共用保活逻辑：钉住「关键同步与共享成员 / 写路径函数源码」于「# 崩溃分析任务」前，避免 `select_prompt` 先裁掉锁名后再进 ContextEngine。
- 自动改码：拒绝源文件未出现的 `AddRef`/`Release`/`Retain` 等臆造生命周期 API；臆造 `m_*Mutex` 仍由成员白名单拒绝。
- 写路径保活优先崩溃共享变量的 assign 边（如置空路径），避免无关误报写边占满配额。
- 共享变量检索：赋值点优先扫描（泛化 `m_x=` / `->m_x=` / delete|free，变量名来自崩溃点而非硬编码）；`assign`/`write`/`delete` 分类修正；截断与早停写路径优先；「共享变量写」纳入 prompt must 标签，强制完整函数体进改码上下文。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.8。

## [1.3.7] - 2026-09-08

### Added

- **`--bypass-repair-gate` / HTTP `bypass_repair_gate`**：默认 `true`，跳过 04 诊断 repair_gate，使 probable/低置信度在 `apply_ai_fixes=true` 时也可进入自动改码；`--no-bypass-repair-gate` / `false` 恢复 confirmed+高置信度门控。
- **简化运行时验证协议**：`enable_runtime_verification` / `skip_runtime_verification` / `run_issue_verification` / `run_regression_verification` 与本地 issue/regression 脚本执行器（`services/simple_verification.py`）；默认不跑复现/回归脚本。
- **改码安全检查**：`services/safety_verification.py` 校验补丁落盘与同步完整性（与运行时验证开关独立）。

### Changed

- 改码管道与 harness/decide 判定、CLI/Daemon 参数透传对齐上述开关；相关文档与单测同步。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.7。
- Daemon / CLI 参考补充 `bypass_repair_gate` 与运行时验证字段。

## [1.3.6] - 2026-09-04

### Added

- **Harness alignment phase 2**: `services/feedback_analyze.py` (verify/judge failure → bounded analyze revision); `services/context_observation_resolver.py` (`memory_pattern`, `verification_log`, `trace_snippet`); `11_judge.json`; `record_run_memory()` case commit audit; Skill `permissions` → `PolicyEngine`; evaluation manifest `expected_decision` / `expected_judge_verdict` / `max_invalid_context_requests`.
- **Harness alignment**: `services/context_loop_contract.py` unifies multi-round LLM protocol; verification presets + discover candidates; Web checkpoint/candidate UI; offline evaluation matrix in CI; round-level analyze replay (`analyze:round:N`).

### Changed

- **`--scope full` default**: enables `context_loop` with up to 3 rounds unless `--agent-loop single`.
- Default runtime budgets: `--max-llm-calls=8`, `--max-tool-calls=32`, `--max-runtime-seconds=3600` (0 still means unlimited when explicitly set).
- `_build_prompt_final_tip` context_loop sections sourced from contract module.

### Documentation

- [CONTEXT_LOOP_CONTRACT.md](docs/architecture/CONTEXT_LOOP_CONTRACT.md), [HARNESS_MIGRATION.md](docs/cli/HARNESS_MIGRATION.md)

## [1.3.5] - 2026-08-24

### Changed

- **RAG / SQLite**：元数据写入不再依赖 SQLite UPSERT（`ON CONFLICT`），CentOS 7 系统库 3.7.17 可写知识库；`[rag]` extra 增加 `pysqlite3-binary`，包导入时尽量顶替旧 `sqlite3`。
- **Daemon 远程入参**：`POST /runs` 省略 `apply_ai_fixes` 时默认为 `false`；空日志 **400** `crash_log_content_required`；非法 `output_format` **400** `invalid_output_format`。`--deny-local-path-fields` 拒绝指向服务端本地路径的字段（**400** `forbidden_field`）。未预期异常 **500** `internal_error`。`--access-log` 输出 method/path/状态。
- **符号化探测**：HarmonyOS 显式配置的 `tool_paths` 不再被 Android affinity 规则丢掉；`config doctor` 按平台打印实际 `primary_tool`。
- **维护命令**：非 TTY 下 `--check-llm` 不再等待回车。
- **阶段 4 参数**：CLI / HTTP 可传 `--max-symbol-only-rescues`、`--max-crash-caller-search-files`。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.5。

## [1.3.4] - 2026-08-24

### Changed

- **Daemon 请求体上限**：JSON body 在 `read` 前按 `Content-Length` 校验，默认 16 MiB；超限 **413** `payload_too_large`（`--max-body-bytes` / `STABILITY_AGENT_DAEMON_MAX_BODY_BYTES`）。非法 JSON 统一为 **400** `invalid_json`。`GET /health` 增加 `max_body_bytes`。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.4。
- Daemon 指南补充请求体上限与 413。

## [1.3.3] - 2026-08-24

### Changed

- **Daemon 优雅停机**：`SIGTERM`/`SIGINT` 后拒绝新的 `POST /runs`（**503** `shutting_down`），取消 queued/running 任务，并等待 CLI 子进程退出（`--shutdown-wait`，默认 90 秒，环境变量 `STABILITY_AGENT_DAEMON_SHUTDOWN_WAIT_SEC`）。systemd 的 `TimeoutStopSec` 须大于该等待时间。
- **Daemon 内存边界**：每任务 SSE 队列默认 256 条、满则丢最旧（`--event-queue-max` / `STABILITY_AGENT_DAEMON_EVENT_QUEUE_MAX`）；结束态任务默认 6 小时后从内存淘汰（`--run-ttl` / `STABILITY_AGENT_DAEMON_RUN_TTL_SEC`，`0` 表示不淘汰），并清理对应幂等 key。
- **`GET /health`**：增加 `shutting_down`、`runs_retained`。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.3。
- Daemon 指南补充停机、队列上界与任务 TTL。

## [1.3.2] - 2026-08-23

### Changed

- **Daemon HTTP 与 CLI 对齐**：`POST /runs` 的 `RunRequest` 透传分析主流程参数；省略字段时沿用 CLI argparse 默认值（不再覆盖 `max_agent_rounds` / `streaming` 等）。新增 `max_stack_frames_symbol_enrich`（默认 8）与 `max_stack_frames_in_prompt`（默认 4）。同类兄弟函数 `--max-sibling-member-functions` 默认改为 0。
- **提示词栈帧裁剪**：栈顶符号补全默认 8 帧，提示词纳入工程栈帧默认 4 帧；可通过 CLI / HTTP 覆盖（范围 2～16）。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.2。
- Daemon / CLI 参考文档同步 HTTP 字段与默认值语义。

## [1.3.1] - 2026-08-20

### Changed

- **向量库写库**：从分析报告构建案例时同时识别 `08_apply_ai_fixes.json` 与 `07_apply_ai_fixes.json`，兼容新旧报告编号。
- **崩溃解析与修复门禁**：增强 abort/heap 相关解析与改码守卫，减少误修系统库/无效补丁。
- **诊断与提示词**：收紧 prompt 上下文过滤、C++ crash 线索与 LLM 路由策略。

### Documentation

- README / PyPI 发布说明中的最新版本号更新为 1.3.1。

## [1.3.0] - 2026-08-18

### Added

- **Daemon/Web 并发改码隔离**：自动改码任务使用独立 Git worktree，生成 workspace 清单和 patch，避免并发任务互相覆盖源码。
- **本地 Web UI 与专项诊断能力**：补充 ANR/AppFreeze、内存压力、业务路径及平台专项诊断旁路。

### Changed

- **报告目录改名**：默认产物根目录由 `cli_reports/` 改为 `reports/`（CLI / Daemon / Web 共用）。首次访问时自动将现有 `cli_reports/` 迁移到 `reports/`；可通过 `STABILITY_AGENT_REPORT_DIR` 覆盖。兼容旧函数名 `summarize_cli_reports` / `clear_cli_reports`。

## [1.2.9] - 2026-08-06

### Added

- **证据驱动诊断流水线**：崩溃证据诊断（`04a`）、ANR/Freeze（`04c`）、内存压力/OOM 线索（`04d`）、崩溃前业务时序（`04e`）；含证据罗盘（PC → 符号 → 反汇编 → 寄存器）。
- **`log_kind` 强分类与 ANR 专用 workflow**：AppFreeze / ANR traces 自动路由 `anr_freeze_analysis`；支持 `--force-anr-analysis` / `--force-memory-analysis` / `--force-timeline-analysis` / `--force-disassembly`。
- **确定性与平台增强**：三级故障模式库、证据分级、信号子码语义、地址特征模式、寄存器关联、栈分层、模块知识路由、责任归属、热点栈统计、反汇编门控、鸿蒙 EventHandler dump 解析、Binder/IPC 弱提示。
- **报告编号对齐**：`01` 解析 → `02` maps → `03` 符号化 → `04a`–`04e` 诊断族 → `05` 向量记忆 → `round_*/06` 提示词 / `07` AI 结果 → `08` 改码。

### Changed

- **`parse_stack_only`**：包含 maps + 符号化 + 诊断族（不再仅解析/符号化）；`04e` 默认仅在检测到 logcat/HiLog/ASI 等业务日志信号时运行。
- **配置目录**：示例与本地配置从 `tools/configs/` 迁至仓库根目录 `configs/`。`agent_config.local.json` / `add2line_resolver_config.local.json` 继续被 `.gitignore` 忽略，勿提交密钥。
- **LLM 配置加载**：`STABILITY_AGENT_CONFIG_DIR`（若设置）优先；否则开源源码树读 `<仓库根>/configs/agent_config.local.json`；安装后的 CLI 读 `~/.config/stability-analysis-agent/agent_config.local.json`。

### Documentation

- `README.md` / `README.en.md`：证据链能力、维护状态与发版节奏（有意义则发版，活跃期约每月一档，非日历 SLA）。
- `docs/cli/CLI_COMMANDS_REFERENCE.md`、`docs/architecture/fault_mode_library.md`、Skill 包参考文档同步新报告编号与 scope 语义。

## [1.2.8] - 2026-07-15

### Added

- **Bug Platform Fetcher Skill 模板**：新增内置 `--preset bug-platform-fetcher`，对应一级菜单「4) 根据缺陷管理平台自动修复（基于 bug-platform-fetcher-skill）」。预设只生成空骨架（`SKILL.md` + `skill.json`），**绝不调用任何具体平台 API**（iCafe / Jira / WorkTile / 飞书 / 自建系统都由下游团队在自己仓库里实现）。详见 `docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md`。
- **一级菜单调整**：把闭源 `bd-sa-agent` 的「输入 iCafe 编号自动修复」抽象为通用「根据缺陷管理平台自动修复」，并将其放到一级菜单"快速开始修复（推荐）"正下方（数字键 4）。这与一键"退出置顶"、设置 / 帮助置末的约定一致。
- **CLI 文档**：`docs/cli/INTERACTIVE_CLI_DESIGN.md` 与 `docs/cli/CLI_GUIDE.md` 已同步增补新菜单项及其与闭源版的边界（"开源仓库**不**带任何具体平台 API 调用"）。
- **测试**：`test/skill_system/test_skill_system.py::test_bug_platform_fetcher_preset_is_generic_template` 断言该预设元数据与生成的 `SKILL.md` 中**绝不包含** `icafe-cli` / `uuap.baidu` / `bcebos` / `baidu-int.com` / `UGate` 等内网 API 痕迹，防止以后被人无意中塞进具体平台实现。

### Changed

- 顶部一级菜单文案统一：「快速开始分析」→「快速开始修复」、「再次进行上一次分析」→「再次进行上一次修复」（与闭源版用语对齐）。

## [1.2.7] - 2026-07-15

### Added

- **`extensions/` 扩展框架**：仓库自带 `extensions/tools/example_tool.py` 与 `extensions/workflows/example_workflow.py` 两个可运行模板，覆盖 `@register_tool` / `@register_workflow` 装饰器、`ToolDefinition` / `WorkflowDefinition`、以及 `WorkflowContext.execute_tool()` 等接入方式；`extensions.register_all()` 在 CLI 启动时被自动调用，逐级扫描仓库自带扩展、`~/.config/stability-analysis-agent/extensions/`、`<cwd>/.stability-analysis-agent/extensions/`、`STABILITY_AGENT_EXT_DIRS` 追加目录，以及 Python 入口点 `stability_analysis_agent.tools` / `stability_analysis_agent.workflows`。详见 `docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md`。
- **设置菜单新增缓存与自检**：增加「查看本地缓存（cli_reports 占用）」与「清理本地缓存（cli_reports）」两个菜单项，背后由新模块 `cli/report_paths.py` 提供（`format_bytes / summarize_cli_reports / clear_cli_reports / print_cli_reports_overview`），可按"全部 / 仅最近 N 份"清理 `cli_reports/`。
- **`cli/upgrade.run_upgrade_check_interactive`**：设置菜单中的「检查更新（升级 sa-agent 到最新版）」接入公网 PyPI 公开发布渠道，探测 `pip / pipx / 编辑模式 / 预编译二进制` 安装方式，推荐与具体环境匹配的升级命令，可一键 `pip install -U "stability-analysis-agent[rag]"`。

### Documentation

- `docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md`：补充「仓库级示例 `extensions/`」、「用户级扩展发现」、「Python 入口点发布扩展包」三个段落，及设置菜单新动作的说明。
- `docs/README.md`：在文档目录结构下新增「仓库自带扩展（`extensions/`）」段。
- `README.md` / `README.en.md`：同步 Skill System / 闭环 Skill 模板章节的扩展机制。
- 新增 `test/cli/test_report_paths.py`：覆盖 `format_bytes` 单位换算、`summarize / clear_cli_reports` 在正常、缺失目录、`preview_limit=1` 下的行为，以及 example 模板与用户级扩展自动发现。

## [1.2.6] - 2026-06-03

### Changed

- **PyPI 依赖**：核心包不再捆绑 RAG/ML 栈；向量检索与相似案例能力改为可选 extra `pip install "stability-analysis-agent[rag]"`（版本区间与 `requirements-rag.txt` 对齐）。
- **打包**：wheel/sdist 纳入 `services` 子包；核心依赖新增 `ripgrep`。
- **Interactive CLI（符号化工具）**：从「设置 → 配置堆栈地址解析工具」完成手动路径或自动写入后，仅保留一次「按回车继续」，避免与内层保存成功面板重复。

## [1.2.5] - 2026-05-14

### Changed

- **Interactive CLI（堆栈符号化配置）**：移除「从 shell 读取环境变量 KEY 并写入配置」的向导菜单；手动配置改为 **「手动设置符号化工具绝对路径」**（可输入可执行文件或其所在目录）。在需要符号化的 **快速开始** 流程中，会先静默尝试与 **自动获取** 相同的配置写入，减少重复操作。文档与 `add2line_resolver_config.local.example.json` 已同步说明 `tool_paths` / `environment_vars` 含义。
- **Interactive CLI（大模型文案）**：菜单与检测摘要中面向用户的 **provider** 统一改为 **厂商** 等中文表述（配置 JSON 字段名 `active_provider` / `providers` 仍保持不变）。
- **Documentation**：`README.md` / `README.en.md`、`docs/cli/CLI_GUIDE.md`、`docs/cli/INTERACTIVE_CLI_DESIGN.md` 与 `tools/configs/agent_config.local.example.json` 中面向用户的 LLM 说明已与「厂商」用语对齐（英文 README 使用 *vendor*；脚本参数 `--provider` 等接口名不变）。
- **Interactive CLI（大模型向导）**：交互提示将裸写 `base_url` 改为「接口请求地址」，并在说明中保留 JSON 字段名 `base_url` 以便对照配置文件。
- **LLM 示例配置**：`tools/configs/agent_config.local.example.json` 移除仅占位的 `your_*` 自定义厂商条目，避免用户复制到 `agent_config.local.json` 后在厂商菜单中出现无关项；说明与 JSON 模板见 `docs/cli/CLI_GUIDE.md`。

## [1.2.2] - 2026-05-07

### Added

- **`cli.api`**: stable programmatic entry points for embedding and enterprise wrappers (`build_parser`, `execute_analysis`, `collect_interactive_run_state`, `interactive_state_to_argv`, `parse_analysis_args`, `run_from_interactive_state`, `run_cli_main`).
- **`bd-sa-agent` / closed-workspace note**: downstream packages should avoid shipping a top-level `cli` package that shadows this library’s `cli` module.
- **Enterprise LLM menu (opt-in)**: when env `STABILITY_AGENT_BD_ENTERPRISE` is set to `1`/`true`/`yes`/`on`, the interactive “配置大模型” flow adds **「配置百度内部API-KEY」** (under “重新设置”, and also when LLM is not yet configured). Default `sa-agent` behavior is unchanged.

### Changed

- Interactive CLI: streamlined confirmations, optional LLM connectivity check, Ctrl+C handling, and related UX tweaks.
- Renamed public API: `execute_analysis`, `collect_interactive_run_state` (formerly private `_execute_analysis`, `_collect_interactive_run_state`).

### Fixed

- LLM `base_url` handling when configured with a trailing `/chat/completions` path (avoids duplicated path segments with OpenAI-compatible clients).
