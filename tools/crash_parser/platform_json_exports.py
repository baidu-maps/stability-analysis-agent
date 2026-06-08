#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三方崩溃平台 JSON 导出适配器。

这些平台没有统一 schema：Sentry、Crashlytics、Bugsnag 等都会把线程、
exception、stacktrace 放在不同层级。本模块只负责把常见 JSON 形态归一化为
CrashAnalysisResult；文本崩溃日志仍由原有 parser 处理。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from tools.crash_parser.meta import extract_crash_info, extract_meta_info
from tools.crash_parser.types import (
    CrashAnalysisResult,
    CrashInfo,
    CrashParseOptions,
    MetaInfo,
    StackFrame,
    ThreadStack,
    _maybe_filter_threads_by_library_dir,
    _thread_layer_summary,
)


@dataclass
class JsonStackCandidate:
    adapter_id: str
    frames: List[Dict[str, Any]]
    thread_name: Optional[str] = None
    thread_id: Optional[str] = None
    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    platform: Optional[str] = None
    app_version: Optional[str] = None
    process_name: Optional[str] = None
    timestamp: Optional[str] = None
    reverse_frames: bool = False


class PlatformJsonAdapter:
    adapter_id = "platform_json_export"

    def can_handle(self, doc: Any) -> bool:
        return False

    def extract(self, doc: Any) -> Optional[JsonStackCandidate]:
        return None


def try_load_platform_json_document(content: str) -> Optional[Any]:
    """加载纯 JSON 导出；非 JSON / crashDiagnosis 前缀文本交给其它 parser。"""
    stripped = (content or "").strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def is_platform_json_export(content: str) -> bool:
    doc = try_load_platform_json_document(content)
    return _select_adapter(doc) is not None if doc is not None else False


def _dict_get(obj: Any, *keys: str) -> Any:
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def _get_path(obj: Any, path: Sequence[str]) -> Any:
    cur = obj
    for part in path:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _first_list(*values: Any) -> Optional[List[Any]]:
    for value in values:
        if isinstance(value, list) and value:
            return value
    return None


def _platform_to_os_type(platform: Optional[str]) -> str:
    p = (platform or "").lower()
    if "ios" in p or "apple" in p:
        return "ios"
    if "android" in p:
        return "android"
    if "harmony" in p or "ohos" in p:
        return "harmonyos"
    if "mac" in p or "darwin" in p:
        return "macos"
    if "linux" in p:
        return "linux"
    if "win" in p:
        return "windows"
    return "unknown"


def _platform_to_language(platform: Optional[str]) -> str:
    p = (platform or "").lower()
    if p in ("javascript", "node", "nodejs", "browser"):
        return "javascript"
    if p in ("java", "kotlin", "android"):
        return "java"
    if p in ("objc", "swift", "ios", "cocoa"):
        return "objc"
    if p in ("native", "c", "cpp", "cocoa-native"):
        return "cpp"
    return "unknown"


def _module_from_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    text = str(path).strip()
    if not text:
        return None
    if "/" in text:
        return os.path.basename(text) or text
    return text


def _normalize_address(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("0x") or text.startswith("0X"):
        return "0x" + text[2:]
    if all(c in "0123456789abcdefABCDEF" for c in text):
        return f"0x{text}"
    return text


def _line_number(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    return line if line > 0 else None


def _classify_library(module: Optional[str], in_app: Optional[bool]) -> str:
    if in_app is True:
        return "app"
    if in_app is False:
        return "system"
    m = (module or "").lower()
    if not m:
        return "unknown"
    if "/system/" in m or m.startswith(("libc.", "libsystem_", "ld-musl", "libart.")):
        return "system"
    if "libapp_" in m or "/bundle/libs/" in m:
        return "app"
    return "unknown"


def _frame_from_json(item: Dict[str, Any], index: int, platform: Optional[str]) -> StackFrame:
    addr = _dict_get(
        item,
        "address",
        "instruction_addr",
        "instructionAddress",
        "frame_addr",
        "pc",
        "symbolAddress",
    )
    function = _dict_get(
        item,
        "function",
        "func",
        "method",
        "symbol",
        "symbol_name",
        "name",
        "raw_function",
    )
    file_path = _dict_get(item, "file", "filename", "abs_path", "path")
    module = _dict_get(
        item,
        "module",
        "package",
        "library",
        "image",
        "object_name",
        "binary",
        "binary_image",
    )
    module_name = _module_from_path(str(module)) if module else _module_from_path(str(file_path))
    line = _dict_get(item, "line", "lineno", "lineNumber", "line_number")
    offset = _dict_get(item, "offset", "instruction_offset", "image_offset")
    in_app = _dict_get(item, "in_app", "inApp", "in_project", "inProject")
    if isinstance(in_app, str):
        in_app = in_app.strip().lower() in ("1", "true", "yes")

    language = _platform_to_language(platform)
    layer = "native" if language in ("cpp", "objc", "swift", "unknown") else language
    return StackFrame(
        frame_number=index,
        address=_normalize_address(addr),
        function=str(function).strip() if function else None,
        file=str(file_path).strip() if file_path else None,
        line=_line_number(line),
        raw_log_line=None,
        module=module_name,
        offset=str(offset).strip() if offset not in (None, "") else None,
        stack_type=None,
        library_type=_classify_library(module_name or str(module or ""), in_app if isinstance(in_app, bool) else None),
        layer=layer,
        language=language,
        subsystem=None,
    )


def _has_frame_shape(items: Any) -> bool:
    if not isinstance(items, list) or not items:
        return False
    dict_items = [i for i in items if isinstance(i, dict)]
    if not dict_items:
        return False
    hits = 0
    for item in dict_items[:5]:
        keys = set(item.keys())
        if keys & {
            "function",
            "func",
            "method",
            "symbol",
            "file",
            "filename",
            "abs_path",
            "line",
            "lineno",
            "lineNumber",
            "module",
            "library",
            "image",
            "address",
            "instruction_addr",
            "frame_addr",
        }:
            hits += 1
    return hits >= min(2, len(dict_items))


class SentryJsonAdapter(PlatformJsonAdapter):
    adapter_id = "sentry_event_json"

    def can_handle(self, doc: Any) -> bool:
        if not isinstance(doc, dict):
            return False
        if "exception" in doc and isinstance(doc.get("exception"), dict):
            return True
        return "event_id" in doc and ("threads" in doc or "stacktrace" in doc)

    def extract(self, doc: Any) -> Optional[JsonStackCandidate]:
        platform = str(doc.get("platform") or "")
        exc_values = _get_path(doc, ("exception", "values"))
        if isinstance(exc_values, list):
            for exc in reversed(exc_values):
                frames = _get_path(exc, ("stacktrace", "frames"))
                if _has_frame_shape(frames):
                    return JsonStackCandidate(
                        adapter_id=self.adapter_id,
                        frames=frames,
                        exception_type=str(_dict_get(exc, "type", "mechanism") or "") or None,
                        exception_message=str(_dict_get(exc, "value", "message") or "") or None,
                        platform=platform,
                        app_version=str(_dict_get(doc, "release", "dist") or "") or None,
                        process_name=str(_dict_get(doc, "transaction", "culprit") or "") or None,
                        timestamp=str(doc.get("timestamp") or "") or None,
                        reverse_frames=True,
                    )
        threads = _get_path(doc, ("threads", "values"))
        if isinstance(threads, list):
            for thread in threads:
                frames = _get_path(thread, ("stacktrace", "frames"))
                if _has_frame_shape(frames):
                    return JsonStackCandidate(
                        adapter_id=self.adapter_id,
                        frames=frames,
                        thread_name=str(_dict_get(thread, "name")) or None,
                        thread_id=str(_dict_get(thread, "id")) or None,
                        platform=platform,
                        app_version=str(_dict_get(doc, "release", "dist") or "") or None,
                        process_name=str(_dict_get(doc, "transaction", "culprit") or "") or None,
                        timestamp=str(doc.get("timestamp") or "") or None,
                        reverse_frames=True,
                    )
        frames = _get_path(doc, ("stacktrace", "frames"))
        if _has_frame_shape(frames):
            return JsonStackCandidate(
                adapter_id=self.adapter_id,
                frames=frames,
                platform=platform,
                app_version=str(_dict_get(doc, "release", "dist") or "") or None,
                timestamp=str(doc.get("timestamp") or "") or None,
                reverse_frames=True,
            )
        return None


class CrashlyticsJsonAdapter(PlatformJsonAdapter):
    adapter_id = "firebase_crashlytics_json"

    def can_handle(self, doc: Any) -> bool:
        if not isinstance(doc, dict):
            return False
        markers = ("eventId", "event_id", "bundleOrPackage", "crashlyticsSdkVersion")
        return any(k in doc for k in markers) and ("threads" in doc or "exceptions" in doc or "error" in doc)

    def extract(self, doc: Any) -> Optional[JsonStackCandidate]:
        platform = str(_dict_get(doc, "platform", "operatingSystemDisplayVersion") or "")
        exceptions = doc.get("exceptions")
        if isinstance(exceptions, list):
            for exc in reversed(exceptions):
                frames = _first_list(exc.get("frames"), _get_path(exc, ("stacktrace", "frames")))
                if _has_frame_shape(frames):
                    return JsonStackCandidate(
                        adapter_id=self.adapter_id,
                        frames=frames,
                        exception_type=str(_dict_get(exc, "type", "exceptionType", "name") or "") or None,
                        exception_message=str(_dict_get(exc, "message", "reason") or "") or None,
                        platform=platform,
                        app_version=str(_dict_get(doc, "version", "appVersion", "buildVersion") or "") or None,
                        process_name=str(_dict_get(doc, "bundleOrPackage", "bundle_id", "appId") or "") or None,
                        timestamp=str(_dict_get(doc, "eventTime", "timestamp") or "") or None,
                    )
        threads = doc.get("threads")
        if isinstance(threads, list):
            sorted_threads = sorted(
                threads,
                key=lambda t: 0 if isinstance(t, dict) and t.get("crashed") else 1,
            )
            for thread in sorted_threads:
                if not isinstance(thread, dict):
                    continue
                frames = _first_list(thread.get("frames"), _get_path(thread, ("stacktrace", "frames")))
                if _has_frame_shape(frames):
                    return JsonStackCandidate(
                        adapter_id=self.adapter_id,
                        frames=frames,
                        thread_name=str(_dict_get(thread, "name", "threadName")) or None,
                        thread_id=str(_dict_get(thread, "id", "threadId")) or None,
                        platform=platform,
                        app_version=str(_dict_get(doc, "version", "appVersion", "buildVersion") or "") or None,
                        process_name=str(_dict_get(doc, "bundleOrPackage", "bundle_id", "appId") or "") or None,
                        timestamp=str(_dict_get(doc, "eventTime", "timestamp") or "") or None,
                    )
        errors = doc.get("error")
        if isinstance(errors, list):
            for err in reversed(errors):
                frames = err.get("frames") if isinstance(err, dict) else None
                if _has_frame_shape(frames):
                    return JsonStackCandidate(
                        adapter_id=self.adapter_id,
                        frames=frames,
                        exception_type=str(_dict_get(err, "error_type", "type") or "") or None,
                        platform=platform or "ios",
                        app_version=str(_dict_get(doc, "version", "appVersion", "buildVersion") or "") or None,
                        process_name=str(_dict_get(doc, "bundleOrPackage", "bundle_id", "appId") or "") or None,
                        timestamp=str(_dict_get(doc, "eventTime", "timestamp") or "") or None,
                    )
        return None


class BugsnagJsonAdapter(PlatformJsonAdapter):
    adapter_id = "bugsnag_event_json"

    def can_handle(self, doc: Any) -> bool:
        if not isinstance(doc, dict):
            return False
        if "events" in doc and isinstance(doc.get("events"), list):
            return True
        return "exceptions" in doc and ("notifier" in doc or "severity" in doc)

    def extract(self, doc: Any) -> Optional[JsonStackCandidate]:
        event = doc
        if isinstance(doc.get("events"), list) and doc["events"]:
            event = next((e for e in doc["events"] if isinstance(e, dict)), {})
        exceptions = event.get("exceptions")
        if not isinstance(exceptions, list):
            return None
        app = event.get("app") if isinstance(event.get("app"), dict) else {}
        device = event.get("device") if isinstance(event.get("device"), dict) else {}
        platform = str(_dict_get(app, "type") or _dict_get(device, "osName", "os_name") or "")
        for exc in exceptions:
            if not isinstance(exc, dict):
                continue
            frames = exc.get("stacktrace")
            if _has_frame_shape(frames):
                return JsonStackCandidate(
                    adapter_id=self.adapter_id,
                    frames=frames,
                    exception_type=str(_dict_get(exc, "errorClass", "type", "name") or "") or None,
                    exception_message=str(_dict_get(exc, "message") or "") or None,
                    platform=platform,
                    app_version=str(_dict_get(app, "version", "versionCode") or "") or None,
                    process_name=str(_dict_get(event, "context") or "") or None,
                    timestamp=str(_dict_get(event, "receivedAt", "timestamp") or "") or None,
                )
        return None


class GenericJsonStackAdapter(PlatformJsonAdapter):
    adapter_id = "generic_json_stack_export"

    def can_handle(self, doc: Any) -> bool:
        return self.extract(doc) is not None

    def extract(self, doc: Any) -> Optional[JsonStackCandidate]:
        if isinstance(doc, dict):
            candidate = self._from_known_paths(doc)
            if candidate:
                return candidate
        frames = _find_frame_list(doc)
        if frames:
            platform = str(_dict_get(doc, "platform", "os", "os_type") or "") if isinstance(doc, dict) else ""
            return JsonStackCandidate(
                adapter_id=self.adapter_id,
                frames=frames,
                platform=platform,
                app_version=str(_dict_get(doc, "app_version", "version") or "") if isinstance(doc, dict) else None,
                process_name=str(_dict_get(doc, "process_name", "app_name", "bundle_id") or "") if isinstance(doc, dict) else None,
            )
        return None

    def _from_known_paths(self, doc: Dict[str, Any]) -> Optional[JsonStackCandidate]:
        threads = doc.get("threads")
        if isinstance(threads, list):
            for thread in threads:
                if not isinstance(thread, dict):
                    continue
                frames = _first_list(thread.get("frames"), thread.get("stack_frames"))
                if _has_frame_shape(frames):
                    return JsonStackCandidate(
                        adapter_id=self.adapter_id,
                        frames=frames,
                        thread_name=str(_dict_get(thread, "name", "thread_name")) or None,
                        thread_id=str(_dict_get(thread, "tid", "id", "thread_id")) or None,
                        platform=str(_get_path(doc, ("meta_info", "os_type")) or _dict_get(doc, "platform", "os_type") or ""),
                        process_name=str(_get_path(doc, ("meta_info", "process_name")) or _dict_get(doc, "process_name") or "") or None,
                    )
        frames = _first_list(doc.get("frames"), doc.get("stack_frames"))
        if _has_frame_shape(frames):
            return JsonStackCandidate(
                adapter_id=self.adapter_id,
                frames=frames,
                platform=str(_dict_get(doc, "platform", "os_type") or ""),
                process_name=str(_dict_get(doc, "process_name", "app_name", "bundle_id") or "") or None,
            )
        return None


def _iter_child_values(obj: Any) -> Iterable[Any]:
    if isinstance(obj, dict):
        return obj.values()
    if isinstance(obj, list):
        return obj
    return ()


def _find_frame_list(obj: Any, *, depth: int = 0) -> Optional[List[Dict[str, Any]]]:
    if depth > 8:
        return None
    if _has_frame_shape(obj):
        return [i for i in obj if isinstance(i, dict)]
    for child in _iter_child_values(obj):
        found = _find_frame_list(child, depth=depth + 1)
        if found:
            return found
    return None


ADAPTERS: List[PlatformJsonAdapter] = [
    SentryJsonAdapter(),
    CrashlyticsJsonAdapter(),
    BugsnagJsonAdapter(),
    GenericJsonStackAdapter(),
]


def _select_adapter(doc: Any) -> Optional[PlatformJsonAdapter]:
    for adapter in ADAPTERS:
        if adapter.can_handle(doc):
            return adapter
    return None


def _build_thread(candidate: JsonStackCandidate, opts: CrashParseOptions) -> ThreadStack:
    items = list(candidate.frames)
    if candidate.reverse_frames:
        items = list(reversed(items))
    max_frames = max(1, int(opts.max_primary_frames or 50))
    frames = [
        _frame_from_json(item, idx, candidate.platform)
        for idx, item in enumerate(items[:max_frames])
        if isinstance(item, dict)
    ]
    return ThreadStack(
        tid=candidate.thread_id,
        name=candidate.thread_name,
        thread_index=0,
        is_crash_thread=True,
        is_main_thread=None,
        frames=frames,
        **_thread_layer_summary(frames),
    )


def _build_crash_info(content: str, candidate: JsonStackCandidate) -> CrashInfo:
    base = extract_crash_info(content)
    exception_type = candidate.exception_type or base.exception_type
    message = candidate.exception_message or ""
    crash_reason = base.crash_reason
    if crash_reason == "unknown":
        crash_reason = message or exception_type or "platform json crash"
    lang = _platform_to_language(candidate.platform)
    category = base.category
    if category is None:
        category = "js_exception" if lang == "javascript" else "native_crash"
    return replace(
        base,
        exception_type=exception_type,
        crash_reason=crash_reason,
        category=category,
        primary_language=lang if lang != "unknown" else base.primary_language,
    )


def _build_meta_info(content: str, candidate: JsonStackCandidate) -> MetaInfo:
    base = extract_meta_info(content)
    os_type = _platform_to_os_type(candidate.platform) if candidate.platform else base.os_type
    return replace(
        base,
        os_type=os_type,
        app_version=candidate.app_version or base.app_version,
        timestamp=candidate.timestamp or base.timestamp,
        platform=candidate.platform or base.platform,
        process_name=candidate.process_name or base.process_name,
        log_format=candidate.adapter_id,
    )


def parse_platform_json_export(
    content: str,
    debug: bool = False,
    *,
    options: Optional[CrashParseOptions] = None,
) -> CrashAnalysisResult:
    opts = options if options is not None else CrashParseOptions()
    doc = try_load_platform_json_document(content)
    adapter = _select_adapter(doc)
    candidate = adapter.extract(doc) if adapter else None
    if candidate is None:
        return CrashAnalysisResult(
            threads=[],
            crash_info=extract_crash_info(content),
            meta_info=replace(extract_meta_info(content), log_format="platform_json_export"),
            raw_content=content if opts.save_raw_content else "",
            parse_status="error",
            crash_backtrace_sum_count=0,
            crash_backtrace_index_set=max(1, int(opts.crash_segment_index)),
        )

    thread = _build_thread(candidate, opts)
    threads = [thread] if thread.frames else []
    os_type = _platform_to_os_type(candidate.platform)
    threads, removed, lib_applied = _maybe_filter_threads_by_library_dir(threads, os_type, opts)

    meta_info = _build_meta_info(content, candidate)
    meta_info = replace(
        meta_info,
        thread_count_total=1,
        thread_count_extracted=len(threads),
        library_dir_frame_filter_applied=lib_applied if lib_applied else None,
        frames_removed_by_library_dir_filter=removed if lib_applied else None,
    )
    total_frames = sum(len(t.frames) for t in threads)
    return CrashAnalysisResult(
        threads=threads,
        crash_info=_build_crash_info(content, candidate),
        meta_info=meta_info,
        raw_content=content if opts.save_raw_content else "",
        parse_status="ok" if total_frames > 0 else "error",
        crash_backtrace_sum_count=1 if total_frames > 0 else 0,
        crash_backtrace_index_set=max(1, int(opts.crash_segment_index)),
    )
