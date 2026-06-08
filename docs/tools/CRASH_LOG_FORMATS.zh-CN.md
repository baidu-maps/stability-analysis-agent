# 崩溃日志输入格式说明

本文说明 `--crash-log` 支持哪些**文件类型**、**读取方式**，以及 `crash_log_parser` 能识别哪些**平台/格式**的崩溃内容。实现位于 `tools/crash_parser/`（注册表见 `parsers.py`）。

---

## 1. 文件路径与后缀

CLI **不按后缀白名单过滤**，只要能读成文本即可；能否解析成功取决于**内容格式**。

| 输入方式 | 是否支持 |
|----------|----------|
| 任意文件路径（如 `.crash`、`.txt`、`.log`、`.json`、无后缀） | 支持 |
| `--crash-log -`（stdin） | 支持 |
| UTF-8 / UTF-8-BOM / UTF-16 | 支持（自动回退） |
| RTF 崩溃导出 | 支持（先转纯文本再解析） |
| 二进制 / 乱码 | 尽力解码，可能丢字 |

**不能作为输入：** 上一轮工具产出的 `01_crash_log_parser.json`（那是报告 schema，不是原始崩溃日志）。

---

## 2. 解析器如何选择

1. 读文件得到字符串（`cli/main.py` → `_read_crash_log`）
2. 检测 OS 线索（`format_detect.detect_os_type`）
3. 按 `parsers.PARSERS` 顺序，**第一个** `can_handle` 为真的解析器处理

`01_crash_log_parser.json` 里 `meta_info.log_format` 会记录命中的格式 id。

---

## 3. 支持的内容格式（按 `log_format`）

### 3.1 Apple / 桌面文本报告

| `log_format` | 常见来源 | 内容特征 |
|--------------|----------|----------|
| `ios_apple_crash` | Xcode / macOS `.crash` | `Exception Type:`、`Crashed Thread:` |
| `ios_pre_parsed_symbolized` | 第三方已符号化导出 | `* SIGSEGV:`、双序号栈行 |
| `ios_mach_tool_export` | KZp 等工具 | `Last Exception Backtrace`、`Crash Type: Mach` |
| `ios_freeze_report` | Watchdog / 卡顿采样 | `Freeze Type:`、卡顿堆栈 |

平台：**iOS**、**macOS**（Linux/Windows 文本含 `segmentation fault` 等可走 `default`）。

### 3.2 Android / HarmonyOS 文本报告

| `log_format` | 常见来源 | 内容特征 |
|--------------|----------|----------|
| `android_harmony_tid` | tombstone / 多线程 dump | 含 `Tid:` 块 |
| `harmony_stacktrace` | OpenHarmony 日志 | `Stacktrace:`（无 `Tid:`） |
| `android_logcat` | logcat / debuggerd | `Fatal signal`、`Cmdline:`、tombstone 分隔线 |

文本 native 栈常见：**`#NN pc 0x地址 /path/lib.so (符号+偏移)`**（Harmony JSON 的 `call_stack` 字段也走同一套解析）。

### 3.3 Harmony 崩溃平台 JSON（含单行导出）

| `log_format` | 常见来源 | 内容特征 |
|--------------|----------|----------|
| `harmony_crash_diagnosis_json` | 地图/业务崩溃平台导出 | 前缀 `crashDiagnosis:` 或 `crashDiagnsis:` + JSON |

JSON 要点：

- `body.attributed_stack.stack_frames[]` — 平台标注的**崩溃/归因线程**（`thread_id` / `thread_name`）；输出 `01` 中 `is_crash_thread: true`，`is_main_thread: true`（UI/进程主线程）
- `body.stacks[].call_stack` — 其它工作线程文本 `#NN pc` 栈；全量模式下 `is_crash_thread: false`，`is_main_thread: false`
- **线程提取模式**（`meta_info.harmony_extraction_mode`）：
  - `full_by_threads`：已配置 `--library-dir`，且崩溃线程栈内**无**命中该目录库文件 → **不限制**线程数，输出归因崩溃线程（`is_crash_thread: true`）+ 全部 `body.stacks[]` 工作线程（本 case 约 160+1 条）
  - `selective`：否则沿用精选策略（`crash` + `primary` + 少量 `background` + 可选 `aggregated_app_libs`，受 `--max-threads` 限制）
- `meta_info.crash_thread_id` / `crash_thread_name`：来自 `attributed_stack`（与 `process_id` 可能相同，语义为崩溃线程）
- `threads[].thread_index`：对 `body.stacks[]` 导出的线程为数组下标（0-based）；归因崩溃线程来自 `attributed_stack`，**无**对应下标，值为 `null`
- `threads[].is_crash_thread` / `is_main_thread`：是否平台归因崩溃线程、是否主(UI)线程（未知为 `null`）

**02 多线程输出**（`add2line_resolver`）：

- `resolved_threads[]`：与 `01.threads[]` 对齐（`tid` / `name` / `thread_index` / `is_crash_thread` / `is_main_thread` / `frames`）
- `resolved_threads[]`：按线程分组的符号化结果（**推荐阅读**）
- `resolved_threads[]`：按线程分组的符号化结果（单栈路径也包装为单元素列表）。下游通过 `flatten_resolved_frames_from_stack` 扁平化；读取磁盘旧版 02 时可回退 `resolved_frames[]`。
- 根级 `crash_thread_id` / `crash_thread_name` / `crash_thread_is_main_thread` / `crash_thread_has_business_frames`：保存平台归因崩溃线程事实；即使崩溃线程因系统库过滤未出现在 `resolved_threads[]`，下游仍可区分“崩溃线程是谁、是否主线程、是否有业务库帧”。
- 仅含 `library_dir` 内 so（`libace_compatible.z.so` 等系统/平台库不写入 02）
- `crash_thread_id` / `frame_count_total` / `frame_count_resolvable`：统计与归因线程 ID
- `max_frames` 预算按 **崩溃线程 → 含 library_dir 库帧的线程** 优先消耗
- **03 代码图输出**：`graph.nodes[]` 继续全局去重；线程归属写在 `graph.edges[]`、`graph.call_chain_from_add2line[]` 和顶层 `thread_context[]` 中（`thread_id` / `is_crash_thread` / `is_main_thread` / `has_business_frames`）。`05_ai_prompt.md` 据此按线程展示函数调用关系，并区分“平台归因崩溃线程”和“当前业务分析入口线程”。
- `01` 中 `frames[].raw_log_line` 为**原始崩溃日志文件**的 1-based 行号：在格式化 JSON 里 `call_stack` 常整段挤在同一行（如第 53 行），则该栈内各 `#NN pc` 帧会显示相同行号

**后缀无关**（`.txt` / `.log` / `.json` 均可，只要内容匹配）。

**当前不支持：** 无前缀的裸 JSON `{...}`（需带 `crashDiagnosis:` 前缀，或改用下方通用 JSON 适配）。

### 3.4 第三方崩溃平台 JSON 导出

模块：`tools/crash_parser/platform_json_exports.py`  
解析器：`PlatformJsonExportParser`  
要求文件内容为**合法 JSON**（以 `{` 或 `[` 开头），且**不是** Harmony 的 `crashDiagnosis:` 前缀行。

| `log_format`（适配器） | 平台/产品 | 典型 JSON 结构 |
|------------------------|-----------|----------------|
| `sentry_event_json` | [Sentry](https://develop.sentry.dev/sdk/data-model/event-payloads/) | `exception.values[].stacktrace.frames`、`threads.values[].stacktrace.frames` |
| `firebase_crashlytics_json` | [Firebase Crashlytics](https://firebase.google.com/docs/crashlytics) REST / BigQuery | `exceptions[].frames`、`threads[].frames`、`error[].frames` |
| `bugsnag_event_json` | [Bugsnag](https://docs.bugsnag.com/) | `events[].exceptions[].stacktrace` |
| `generic_json_stack_export` | Bugly / 友盟 / 自建 APM 等 | `threads[].frames`、`stack_frames`、嵌套帧数组 |

**说明：** 腾讯 Bugly、友盟等国内平台若无专用适配器，只要帧字段含 `function` / `file` / `line` / `module` / `address` / `frame_addr` 等常见键，常可由 `generic_json_stack_export` 兜底。新厂商建议单独加 `PlatformJsonAdapter`，勿改 `stack_extract` 正则。

Sentry 的 `frames` 为**栈顶在内**顺序，适配器会反转为分析常用顺序。

---

## 4. 解析器注册优先级

顺序越高越先匹配（`tools/crash_parser/parsers.py`）：

1. `IosPreParsedCrashParser`
2. `IosMachExportCrashParser`
3. `IosAppleCrashParser`
4. `IosFreezeReportParser`
5. `HarmonyCrashDiagnosisJsonParser`
6. `PlatformJsonExportParser`
7. `AndroidHarmonyTidCrashParser`
8. `HarmonyStacktraceCrashParser`
9. `AndroidLogcatCrashParser`
10. `DefaultCrashParser`（兜底）

---

## 5. 扩展新平台导出

1. 在 `platform_json_exports.py` 增加 `PlatformJsonAdapter` 子类（大厂商可独立模块）。
2. 在 `ADAPTERS` 中注册，放在 `GenericJsonStackAdapter` **之前**。
3. 在 `test/agent_py_tool/test_platform_json_exports.py` 增加用例。
4. 同步更新本文与 README。

JSON 字段映射与文本栈解析（`stack_extract` / `platform_threads`）保持分离，便于维护。

---

## 6. 相关文档

- [CLI 参数参考](../cli/CLI_COMMANDS_REFERENCE.md)
- [CLI 使用指南](../cli/CLI_GUIDE.md)
- English: [CRASH_LOG_FORMATS.md](./CRASH_LOG_FORMATS.md)
