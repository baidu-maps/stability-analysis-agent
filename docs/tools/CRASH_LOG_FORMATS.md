# Crash log input formats

This document describes what `--crash-log-file` / `--crash-log-content` / `--crash-log-dir` accept: **file extensions**, **how content is read**, and **which crash-report shapes** `crash_log_parser` can extract. Implementation lives under `tools/crash_parser/` (registry in `parsers.py`).

---

## 1. File path and extension

The CLI does **not** whitelist extensions. Any readable path works; parsing is **content-based**.

| Input | Supported |
|-------|-----------|
| File path (e.g. `.crash`, `.txt`, `.log`, `.json`, no extension) | Yes |
| `--crash-log-file -` (stdin) | Yes |
| UTF-8 / UTF-8-BOM / UTF-16 text | Yes (auto fallback) |
| RTF crash export | Yes (converted to plain text before parse) |
| Binary / invalid encoding | Best-effort decode with errors ignored |

**Not valid as input:** tool output such as `01_crash_log_parser.json` from a previous run (that schema is for reports, not raw logs).

---

## 2. How parsers are selected

1. Read file → string (`cli/main.py` → `_read_crash_log`).
2. Detect OS hints (`format_detect.detect_os_type`).
3. First matching parser in `parsers.PARSERS` wins (see priority in section 4).

The field `meta_info.log_format` in `01_crash_log_parser.json` records which parser handled the log.

---

## 3. Supported content formats (by `log_format`)

### 3.1 Apple / desktop text reports

| `log_format` | Typical source | Content markers |
|--------------|----------------|-----------------|
| `ios_apple_crash` | Xcode / macOS `.crash` | `Exception Type:`, `Crashed Thread:` |
| `ios_pre_parsed_symbolized` | Third-party symbolized export | `* SIGSEGV:`, dual-index stack lines |
| `ios_mach_tool_export` | KZp / similar tools | `Last Exception Backtrace`, `Crash Type: Mach` |
| `ios_freeze_report` | Watchdog / freeze sampling | `Freeze Type:`, 卡顿堆栈 |

Platforms: **iOS**, **macOS** (also `default` heuristics for Linux/Windows text with `segmentation fault`, etc.).

### 3.2 Android / HarmonyOS text reports

| `log_format` | Typical source | Content markers |
|--------------|----------------|-----------------|
| `android_harmony_tid` | tombstone / multi-thread dump | `Tid:` blocks |
| `harmony_stacktrace` | OpenHarmony log | `Stacktrace:` (no `Tid:`) |
| `android_logcat` | logcat / debuggerd | `Fatal signal`, `Cmdline:`, tombstone banner |

Native stack lines in text often use **`#NN pc 0xADDR /path/to/lib.so (symbol+off)`** (also parsed inside Harmony JSON `call_stack` fields).

### 3.3 Harmony crash platform JSON (single-line export)

| `log_format` | Typical source | Content markers |
|--------------|----------------|-----------------|
| `harmony_crash_diagnosis_json` | In-house / map crash platform export | Prefix `crashDiagnosis:` or `crashDiagnsis:` + JSON |

JSON structure (simplified):

- `body.attributed_stack.stack_frames[]` — symbolized frames (`frame_addr`, `image`, `local_symbol`, …)
- `body.stacks[].call_stack` — text `#NN pc` stacks (used when bundle/app libs appear here but not in `stack_frames`)

**Extension does not matter** (`.txt`, `.log`, `.json` are all fine if content matches).

**Not supported:** bare JSON object **without** the `crashDiagnosis:` / `crashDiagnsis:` prefix (use prefix wrapper or a generic JSON export below).

### 3.4 Third-party crash platform JSON exports

Module: `tools/crash_parser/platform_json_exports.py`  
Parser: `PlatformJsonExportParser`  
Requires **valid JSON** at file start (`{` or `[`), not the Harmony prefix line.

| `log_format` (adapter) | Platform / product | Typical JSON shape |
|------------------------|-------------------|---------------------|
| `sentry_event_json` | [Sentry](https://develop.sentry.dev/sdk/data-model/event-payloads/) | `exception.values[].stacktrace.frames`, `threads.values[].stacktrace.frames` |
| `firebase_crashlytics_json` | [Firebase Crashlytics](https://firebase.google.com/docs/crashlytics) REST / BigQuery export | `exceptions[].frames`, `threads[].frames`, `error[].frames` |
| `bugsnag_event_json` | [Bugsnag](https://docs.bugsnag.com/) notify payload | `events[].exceptions[].stacktrace` |
| `generic_json_stack_export` | Bugly-like / custom dashboards / wrapped exports | `threads[].frames`, `stack_frames`, nested frame arrays |

**Planned / community:** Bugly (腾讯), Umeng, domestic APM JSON often fits `generic_json_stack_export` if frames use common keys (`function`, `file`, `line`, `module`, `address`, `frame_addr`, …). Vendor-specific schemas can add a new adapter class without changing text parsers.

Sentry stack frames are stored **innermost-first**; the adapter reverses them to match analysis order.

---

## 4. Parser registration order (priority)

Higher entries are tried first (`tools/crash_parser/parsers.py`):

1. `IosPreParsedCrashParser`
2. `IosMachExportCrashParser`
3. `IosAppleCrashParser`
4. `IosFreezeReportParser`
5. `HarmonyCrashDiagnosisJsonParser`
6. `PlatformJsonExportParser`
7. `AndroidHarmonyTidCrashParser`
8. `HarmonyStacktraceCrashParser`
9. `AndroidLogcatCrashParser`
10. `DefaultCrashParser` (fallback)

---

## 5. Adding a new platform export

1. Add `PlatformJsonAdapter` subclass in `platform_json_exports.py` (or a dedicated module for large vendors).
2. Register it in `ADAPTERS` **before** `GenericJsonStackAdapter`.
3. Add unit tests under `test/agent_py_tool/test_platform_json_exports.py`.
4. Update this document and README tables.

Do **not** fold vendor JSON field names into `stack_extract.py` regex paths; keep JSON mapping isolated.

---

## 6. Related docs

- [CLI Commands Reference](../cli/CLI_COMMANDS_REFERENCE.md) — `--crash-log-file`, `--scope parse_log_only`
- [CLI Guide](../cli/CLI_GUIDE.md) — end-to-end examples
- Chinese summary: [CRASH_LOG_FORMATS.zh-CN.md](./CRASH_LOG_FORMATS.zh-CN.md)
