#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Harmony / OpenHarmony ``crashDiagnosis`` / ``crashDiagnsis`` 单行 JSON 崩溃导出。

与文本栈解析（stack_extract / platform_threads）分离：JSON 字段映射在本模块完成，
避免在通用正则栈提取中误匹配 JSON 子串。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.crash_parser.meta import extract_crash_info, extract_meta_info
from tools.crash_parser.stack_extract import extract_stack_frames
from tools.thread_display import normalize_harmony_thread_fields
from tools.thread_display import normalize_harmony_thread_fields
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

logger = logging.getLogger(__name__)

CRASH_DIAGNOSIS_PREFIX_RE = re.compile(r"^crashDiag\w*sis\s*:\s*", re.IGNORECASE)
_SIG_NAME_RE = re.compile(r"(SIG[A-Z0-9_]+)", re.IGNORECASE)
_SIG_NUM_RE = re.compile(r"SIG[A-Z0-9_]+\((\d+)", re.IGNORECASE)
_HARMONY_BUILD_ID_SUFFIX_RE = re.compile(r"\s+\[::[0-9a-fA-F]+\]\s*$")
_PC_LINE_HINT_RE = re.compile(r"#\d+\s+pc\s+[0-9a-fA-Fx]+", re.IGNORECASE)
_PC_FRAME_LINE_RE = re.compile(
    r"#(\d+)\s+pc\s+([0-9a-fA-Fx]+)\s+(\S+)",
    re.IGNORECASE,
)
_BUNDLE_LIB_PATH_HINTS = ("/bundle/libs/", "/data/storage/")

_SIGNAL_REASON_MAP = {
    "SIGSEGV": "segmentation fault",
    "SIGABRT": "abort",
    "SIGILL": "illegal instruction",
    "SIGBUS": "bus error",
    "SIGFPE": "divide by zero",
    "SIGTRAP": "trap",
    "SIGALRM": "timeout",
}


def is_harmony_crash_diagnosis_json(content: str) -> bool:
    """是否为 ``crashDiagnosis: { ... }`` 单行/短文本 JSON 导出。"""
    stripped = (content or "").lstrip()
    if not CRASH_DIAGNOSIS_PREFIX_RE.match(stripped):
        return False
    doc = try_load_crash_diagnosis_document(content)
    if not doc:
        return False
    call_stack, _ = _select_native_call_stack(doc)
    return _primary_stack_frame_entries(doc) is not None or bool(call_stack)


def try_load_crash_diagnosis_document(content: str) -> Optional[Dict[str, Any]]:
    """去掉前缀后解析 JSON；失败返回 None。"""
    stripped = (content or "").strip()
    m = CRASH_DIAGNOSIS_PREFIX_RE.match(stripped)
    if not m:
        return None
    body = stripped[m.end() :].strip()
    if not body.startswith("{"):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.debug("crashDiagnosis JSON 解析失败: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def _call_stack_has_pc_lines(text: str) -> bool:
    return bool(_PC_LINE_HINT_RE.search(text or ""))


def _json_entries_have_bundle_lib(entries: List[Dict[str, Any]]) -> bool:
    for ent in entries:
        img = str(ent.get("image") or "")
        if any(h in img for h in _BUNDLE_LIB_PATH_HINTS) or "/libapp_" in img:
            return True
    return False


def _parse_pc_frame_index_map(call_stack: str) -> Dict[int, Tuple[str, str]]:
    """从 call_stack 文本解析 ``#N -> (addr, module_path)``。"""
    out: Dict[int, Tuple[str, str]] = {}
    for line in call_stack.splitlines():
        m = _PC_FRAME_LINE_RE.search(line.strip())
        if not m:
            continue
        out[int(m.group(1))] = (m.group(2), m.group(3))
    return out


def _crash_native_stack_bonus(call_stack: str) -> int:
    """
    倾向 SIGSEGV 类短 native 栈：#02/#03 连续命中同一 libapp_*.so（无符号行也算）。
    """
    idx_map = _parse_pc_frame_index_map(call_stack)
    bonus = 0
    frame_count = len(idx_map)
    if frame_count <= 6:
        bonus += 20
    elif frame_count > 10:
        bonus -= 15

    f2 = idx_map.get(2)
    f3 = idx_map.get(3)
    if not f2 or not f3:
        return bonus
    mod2, mod3 = f2[1], f3[1]
    if not (
        any(h in mod2 for h in _BUNDLE_LIB_PATH_HINTS)
        and any(h in mod3 for h in _BUNDLE_LIB_PATH_HINTS)
        and "libapp_" in mod2
        and "libapp_" in mod3
    ):
        return bonus
    base2 = mod2.rsplit("/", 1)[-1]
    base3 = mod3.rsplit("/", 1)[-1]
    if base2 == base3:
        bonus += 80
    else:
        bonus += 35
    return bonus


def _score_body_stack_entry(st: Dict[str, Any], attributed: Dict[str, Any]) -> int:
    """为 body.stacks[] 中选崩溃 native 栈打分（优先含 bundle so 的 #NN pc 栈）。"""
    cs = str(st.get("call_stack") or "")
    if not _call_stack_has_pc_lines(cs):
        return -1
    score = 0
    if any(h in cs for h in _BUNDLE_LIB_PATH_HINTS):
        score += 50
    if "libapp_" in cs:
        score += 30
    score += _crash_native_stack_bonus(cs)
    attr_tid = attributed.get("thread_id")
    attr_name = attributed.get("thread_name")
    if attr_tid is not None and st.get("thread_id") == attr_tid:
        score += 100
    if attr_name and st.get("thread_name") == attr_name:
        score += 100
    return score


def _select_native_call_stack(
    doc: Dict[str, Any],
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    选取含 ``#NN pc`` 的 native 栈文本。

    优先 ``attributed_stack.call_stack``；为空时从 ``body.stacks[]`` 中选最高分
    （典型：崩溃点在 libapp_*.so，而 stack_frames 仅为另一线程的符号化栈）。
    """
    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    attributed = body.get("attributed_stack") if isinstance(body.get("attributed_stack"), dict) else {}

    cs_attr = str(attributed.get("call_stack") or "").strip()
    if _call_stack_has_pc_lines(cs_attr):
        return cs_attr, attributed

    stacks = body.get("stacks")
    if not isinstance(stacks, list):
        return None, None

    best_cs: Optional[str] = None
    best_meta: Optional[Dict[str, Any]] = None
    best_score = -1
    for st in stacks:
        if not isinstance(st, dict):
            continue
        score = _score_body_stack_entry(st, attributed)
        if score > best_score:
            best_score = score
            best_cs = str(st.get("call_stack") or "").strip()
            best_meta = st
    if best_cs and _call_stack_has_pc_lines(best_cs):
        return best_cs, best_meta
    return None, None


def _file_line_for_text_probe(content: str, probe: str) -> int:
    """在原始崩溃日志文件中定位 ``probe`` 首次出现的 1-based 行号。"""
    if not content or not probe:
        return 1
    probe = probe.strip()
    if not probe:
        return 1
    lines = content.splitlines()
    if not lines:
        return 1
    for size in (min(160, len(probe)), min(100, len(probe)), min(60, len(probe)), min(32, len(probe))):
        snippet = probe[:size]
        if len(snippet) < 12:
            continue
        for i, line in enumerate(lines, start=1):
            if snippet in line:
                return i
    pos = content.find(probe[: min(80, len(probe))])
    if pos >= 0:
        return content[:pos].count("\n") + 1
    return 1


def _file_line_for_stack_text_line(content: str, stack_line: str) -> int:
    """将 ``#NN pc ...`` 栈文本行映射到原始日志文件行号。"""
    return _file_line_for_text_probe(content, stack_line)


def _file_line_for_diagnosis_entry(content: str, entry: Dict[str, Any]) -> Optional[int]:
    """将 ``stack_frames[]`` JSON 条目映射到原始日志文件行号（按地址/符号检索）。"""
    if not content:
        return None
    probes: List[str] = []
    addr = str(entry.get("frame_addr") or "").strip()
    if addr:
        probes.append(addr)
        probes.append(_normalize_address(addr).replace("0x", "", 1))
    sym = str(entry.get("local_symbol") or "").strip()
    if sym and len(sym) >= 8:
        probes.append(sym[:80])
    image = str(entry.get("image") or "").strip()
    if image:
        probes.append(image.split("/")[-1])
    for probe in probes:
        if probe not in content:
            continue
        return _file_line_for_text_probe(content, probe)
    return None


def _parse_call_stack_pc_lines(
    call_stack: str,
    *,
    content: str = "",
) -> List[StackFrame]:
    """解析 Harmony ``#NN pc 0xADDR /path/to/lib.so (sym+off) [::buildid]`` 文本栈。"""
    normalized_lines: List[str] = []
    for line in call_stack.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = _HARMONY_BUILD_ID_SUFFIX_RE.sub("", stripped)
        if _call_stack_has_pc_lines(stripped):
            normalized_lines.append(stripped)
    if not normalized_lines:
        return []

    raw_frames = extract_stack_frames("\n".join(normalized_lines))
    out: List[StackFrame] = []
    for fr in raw_frames:
        mod = fr.module
        full_path = mod
        fn = int(fr.frame_number)
        source_line = normalized_lines[fn] if 0 <= fn < len(normalized_lines) else ""
        if mod and "/" not in mod:
            for line in normalized_lines:
                if mod in line and "/" in line:
                    m = re.search(
                        rf"(/[^\s]+{re.escape(mod)})",
                        line,
                    )
                    if m:
                        full_path = m.group(1)
                    break
        raw_log_line = _file_line_for_stack_text_line(content, source_line) if content else None
        out.append(
            replace(
                fr,
                address=_normalize_address(fr.address),
                library_type=_classify_library(mod, full_path),
                layer="native",
                language="cpp",
                raw_log_line=raw_log_line,
            )
        )
    return out


def _primary_stack_frame_entries(doc: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    body = doc.get("body")
    if not isinstance(body, dict):
        return None
    attributed = body.get("attributed_stack")
    if not isinstance(attributed, dict):
        return None
    frames = attributed.get("stack_frames")
    if not isinstance(frames, list) or not frames:
        return None
    return [f for f in frames if isinstance(f, dict)]


def _normalize_address(addr: Any) -> str:
    s = str(addr or "").strip()
    if not s:
        return ""
    if not s.lower().startswith("0x"):
        s = f"0x{s}"
    return s


def _module_name_from_image(image: Any) -> Optional[str]:
    if not image:
        return None
    path = str(image).strip()
    if not path:
        return None
    name = path.split("/")[-1] if "/" in path else path
    if "(" in name:
        name = name.split("(", 1)[0]
    if " [" in name:
        name = name.split(" [", 1)[0]
    mod_l = name.lower()
    if ".apk!" in mod_l:
        idx = mod_l.find(".apk!")
        name = name[idx + len(".apk!") :].strip()
    return name.strip() or None


def _classify_library(module: Optional[str], image: Optional[str] = None) -> str:
    img = str(image or "")
    if "/system/" in img or img.startswith("/system"):
        return "system"
    if not module:
        return "unknown"
    name = module.strip()
    base = name
    if ".so." in base:
        base = base.split(".so.")[0] + ".so"
    base_lower = base.lower()
    system_prefixes = (
        "libc.so",
        "libm.so",
        "libstdc++",
        "libdl.so",
        "libunwind.so",
        "liblog.so",
        "ld-musl-aarch64.so",
        "libsqlite.so",
    )
    system_exact = {
        "ld-musl-aarch64.so.1",
        "libffrt.so",
        "libeventhandler.z.so",
        "libhicollie.z.so",
        "libipc_core.z.so",
        "libipc_common.z.so",
        "libhdc_register.z.so",
        "libappspawn_ace.z.so",
        "appspawn",
    }
    platform_exact = {
        "libark_jsruntime.so",
        "libace_napi.z.so",
    }
    if base_lower.startswith(system_prefixes) or name in system_exact or base in system_exact:
        return "system"
    if name in platform_exact or base in platform_exact:
        return "system"
    return "app"


def _layer_language_from_frame_type(frame_type: Any) -> Tuple[str, str]:
    t = str(frame_type or "native").strip().lower()
    if t in ("js", "arkts", "ets"):
        return "arkts", "arkts"
    if t == "java":
        return "native", "java"
    return "native", "cpp"


def stack_frame_from_diagnosis_entry(
    entry: Dict[str, Any],
    frame_number: int,
    *,
    content: str = "",
    raw_log_line: Optional[int] = None,
) -> StackFrame:
    """将 ``stack_frames[]`` 单条记录映射为 StackFrame。"""
    image = entry.get("image")
    module = _module_name_from_image(image)
    layer, language = _layer_language_from_frame_type(entry.get("type"))
    offset_val = entry.get("offset")
    offset_str: Optional[str] = None
    if entry.get("has_offset") and offset_val is not None:
        offset_str = str(offset_val)
    if raw_log_line is None and content:
        raw_log_line = _file_line_for_diagnosis_entry(content, entry)
    return StackFrame(
        frame_number=frame_number,
        address=_normalize_address(entry.get("frame_addr")),
        function=(str(entry.get("local_symbol")).strip() or None)
        if entry.get("local_symbol")
        else None,
        file=None,
        line=None,
        raw_log_line=raw_log_line,
        module=module,
        offset=offset_str,
        stack_type=None,
        library_type=_classify_library(module, image),
        layer=layer,
        language=language,
        subsystem=None,
    )


def extract_crash_info_from_diagnosis(doc: Dict[str, Any]) -> CrashInfo:
    """从 crashDiagnosis JSON 的 attributes / exp_info 提取崩溃信息。"""
    attrs = doc.get("attributes") if isinstance(doc.get("attributes"), dict) else {}
    exp = attrs.get("exp_info") if isinstance(attrs.get("exp_info"), dict) else {}
    exp_name = str(exp.get("name") or "").strip()
    exp_msg = str(exp.get("message") or "").strip()

    signal: Optional[str] = None
    crash_reason = "unknown"
    exception_type: Optional[str] = None
    category: Optional[str] = "native_crash"

    sig_m = _SIG_NAME_RE.search(exp_name) if exp_name else None
    if sig_m:
        sig_name = sig_m.group(1).upper()
        crash_reason = _SIGNAL_REASON_MAP.get(sig_name, "native_crash")
        num_m = _SIG_NUM_RE.search(exp_name)
        signal = f"{num_m.group(1)} ({sig_name})" if num_m else sig_name
    elif exp_name:
        exception_type = exp_name
        crash_reason = exp_name

    if exp_msg:
        exception_type = f"{exception_type}: {exp_msg}".strip(": ").strip() if exception_type else exp_msg

    thread_type = "main"

    # 无结构化字段时回退到全文启发式（兼容旧逻辑）
    if signal is None and crash_reason == "unknown":
        return extract_crash_info(json.dumps(doc, ensure_ascii=False))

    return CrashInfo(
        thread_type=thread_type,
        crash_reason=crash_reason,
        signal=signal,
        exception_type=exception_type,
        crash_address=None,
        category=category,
        primary_language="cpp",
    )


def extract_meta_info_from_diagnosis(doc: Dict[str, Any]) -> MetaInfo:
    """从 crashDiagnosis JSON 顶层字段提取元信息。"""
    attrs = doc.get("attributes") if isinstance(doc.get("attributes"), dict) else {}
    platform = str(doc.get("platform") or "").strip() or None
    os_type = "harmonyos"
    if platform and platform.lower() not in ("harmony", "harmonyos", "openharmony"):
        os_type = detect_os_type_from_platform(platform)

    app_version = str(doc.get("app_version") or "").strip() or None
    bundle_id = str(doc.get("bundle_id") or "").strip() or None
    app_name = str(doc.get("app_name") or "").strip() or None
    process_name = bundle_id or app_name
    if not process_name:
        proc = attrs.get("process_name")
        if proc:
            process_name = str(proc).strip().strip("()")

    timestamp: Optional[str] = None
    for key in ("crashTime", "event_time", "event_time_in_ms"):
        val = doc.get(key)
        if val is not None:
            timestamp = str(val)
            break

    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    attributed = body.get("attributed_stack") if isinstance(body.get("attributed_stack"), dict) else {}
    process_id = None
    if attributed.get("thread_id") is not None:
        process_id = str(attributed.get("thread_id"))

    arch = "arm64"
    entries = _primary_stack_frame_entries(doc) or []
    for ent in entries:
        a = str(ent.get("arch") or "").strip()
        if a:
            arch = a
            break
    if not arch or arch == "":
        img0 = str((entries[0] if entries else {}).get("image") or "")
        if "aarch64" in img0 or "arm64" in img0:
            arch = "arm64"

    stacks = body.get("stacks")
    thread_count_total = len(stacks) if isinstance(stacks, list) else 1

    return MetaInfo(
        os_type=os_type,
        os_version=str(doc.get("sdk_version") or "").strip() or None,
        app_version=app_version,
        device_model=None,
        timestamp=timestamp,
        platform=platform,
        compiler=None,
        process_id=process_id,
        module_base_addresses=None,
        arch=arch,
        symbol_path=None,
        ability_name=None,
        process_name=process_name,
        anr_suspected=None,
        thread_count_total=thread_count_total,
        thread_count_extracted=1,
        log_format="harmony_crash_diagnosis_json",
    )


def detect_os_type_from_platform(platform: str) -> str:
    pl = platform.lower()
    if "harmony" in pl or "ohos" in pl:
        return "harmonyos"
    if "android" in pl:
        return "android"
    if "ios" in pl:
        return "ios"
    return "unknown"


def _is_app_library_frame(frame: StackFrame) -> bool:
    if frame.library_type == "app":
        return True
    mod = str(frame.module or "").lower()
    if mod.startswith("libapp_"):
        return True
    return any(h in mod for h in _BUNDLE_LIB_PATH_HINTS)


def _frame_dedup_key(frame: StackFrame) -> Tuple[str, str, str]:
    return (
        str(frame.address or ""),
        str(frame.module or ""),
        str(frame.function or ""),
    )


def _collect_unique_app_frames_from_doc(doc: Dict[str, Any], *, content: str = "") -> List[StackFrame]:
    """汇总文档内所有 ``#NN pc`` / stack_frames 中的去重应用库帧（跨线程）。"""
    seen: set = set()
    collected: List[StackFrame] = []

    def _add(frames: List[StackFrame]) -> None:
        for fr in frames:
            if not _is_app_library_frame(fr):
                continue
            if not str(fr.address or "").strip():
                continue
            key = _frame_dedup_key(fr)
            if key in seen:
                continue
            seen.add(key)
            collected.append(fr)

    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    attributed = body.get("attributed_stack") if isinstance(body.get("attributed_stack"), dict) else {}
    candidates: List[Dict[str, Any]] = []
    if attributed:
        candidates.append(attributed)
    stacks = body.get("stacks")
    if isinstance(stacks, list):
        candidates.extend(st for st in stacks if isinstance(st, dict))

    for st in candidates:
        cs = str(st.get("call_stack") or "").strip()
        if cs:
            _add(_parse_call_stack_pc_lines(cs, content=content))

    for ent in _primary_stack_frame_entries(doc) or []:
        _add([stack_frame_from_diagnosis_entry(ent, 0, content=content)])

    for i, fr in enumerate(collected):
        fr.frame_number = i
    return collected


def _resolve_primary_frames(
    doc: Dict[str, Any],
    entries: List[Dict[str, Any]],
    opts: CrashParseOptions,
    *,
    content: str = "",
) -> Tuple[List[StackFrame], Dict[str, Any]]:
    """
    合并 JSON ``stack_frames`` 与 ``#NN pc`` 文本栈。

    当文本栈含 bundle/libapp_ 而 stack_frames 无应用库地址时，采用文本栈（供符号化）。
    """
    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    attributed = body.get("attributed_stack") if isinstance(body.get("attributed_stack"), dict) else {}
    thread_meta: Dict[str, Any] = dict(attributed)

    json_frames = [
        stack_frame_from_diagnosis_entry(ent, i, content=content)
        for i, ent in enumerate(entries)
    ]
    call_stack, stack_meta = _select_native_call_stack(doc)
    pc_frames = _parse_call_stack_pc_lines(call_stack, content=content) if call_stack else []

    use_pc = False
    if pc_frames:
        pc_has_bundle = any(
            f.library_type == "app" or (f.module or "").startswith("libapp_")
            for f in pc_frames
        )
        json_has_bundle = _json_entries_have_bundle_lib(entries)
        if pc_has_bundle or not json_has_bundle:
            use_pc = True
            if isinstance(stack_meta, dict):
                thread_meta = stack_meta

    frames = pc_frames if use_pc else json_frames
    max_pf = max(0, int(opts.max_primary_frames))
    if max_pf > 0 and len(frames) > max_pf:
        frames = frames[:max_pf]
    for i, fr in enumerate(frames):
        fr.frame_number = i
    return frames, thread_meta


def _build_primary_thread(
    doc: Dict[str, Any],
    entries: List[Dict[str, Any]],
    opts: CrashParseOptions,
    *,
    content: str = "",
) -> ThreadStack:
    frames, thread_meta = _resolve_primary_frames(doc, entries, opts, content=content)
    tid, name = normalize_harmony_thread_fields(
        thread_meta.get("thread_id"),
        thread_meta.get("thread_name"),
    )
    return ThreadStack(
        tid=tid,
        name=name,
        thread_index=0,
        is_crash_thread=False,
        is_main_thread=None,
        frames=frames,
        **_thread_layer_summary(frames),
    )


def _library_files_for_options(opts: CrashParseOptions, os_type: str) -> Optional[List[Path]]:
    """加载 ``library_dir`` 下可用于匹配的库文件列表；无效时返回 None。"""
    lib_raw = (opts.library_dir or "").strip()
    if not lib_raw:
        return None
    lib_path = Path(lib_raw)
    if not lib_path.exists():
        return None
    from tools._library_frame_whitelist import find_library_files_in_dir

    if lib_path.is_file():
        return [lib_path]
    if lib_path.is_dir():
        files = find_library_files_in_dir(lib_raw, os_type)
        return files if files else None
    return None


def _frame_matches_library_files(frame: StackFrame, library_files: List[Path]) -> bool:
    from tools._library_frame_whitelist import match_libraries_for_module

    mod = frame.module if isinstance(frame.module, str) else None
    return bool(match_libraries_for_module(mod, library_files))


def _attributed_stack_matches_library_dir(
    doc: Dict[str, Any],
    opts: CrashParseOptions,
    os_type: str,
    *,
    content: str = "",
) -> bool:
    """``attributed_stack`` 是否至少有一帧 module 命中 ``library_dir`` 内库文件。"""
    library_files = _library_files_for_options(opts, os_type)
    if not library_files:
        return False
    entries = _primary_stack_frame_entries(doc) or []
    for i, ent in enumerate(entries):
        fr = stack_frame_from_diagnosis_entry(ent, i, content=content)
        if _frame_matches_library_files(fr, library_files):
            return True
    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    attributed = body.get("attributed_stack") if isinstance(body.get("attributed_stack"), dict) else {}
    cs = str(attributed.get("call_stack") or "").strip()
    if cs:
        for fr in _parse_call_stack_pc_lines(cs, content=content):
            if _frame_matches_library_files(fr, library_files):
                return True
    return False


def _should_full_thread_extract(
    doc: Dict[str, Any],
    opts: CrashParseOptions,
    os_type: str,
    *,
    content: str = "",
) -> bool:
    """
    当用户配置了 ``library_dir``，且平台标注崩溃线程栈内无命中库时，全量解析 ``body.stacks[]``。
    """
    if not (opts.library_dir or "").strip() or not Path(opts.library_dir).exists():
        return False
    if not _primary_stack_frame_entries(doc):
        return False
    return not _attributed_stack_matches_library_dir(doc, opts, os_type, content=content)


def _attributed_stack_meta(doc: Dict[str, Any]) -> Dict[str, Any]:
    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    attributed = body.get("attributed_stack") if isinstance(body.get("attributed_stack"), dict) else {}
    return dict(attributed)


def _build_attributed_crash_thread(
    doc: Dict[str, Any],
    opts: CrashParseOptions,
    *,
    content: str = "",
) -> Optional[ThreadStack]:
    """平台 ``attributed_stack.stack_frames`` → 归因崩溃线程。"""
    entries = _primary_stack_frame_entries(doc) or []
    if not entries:
        return None
    attributed = _attributed_stack_meta(doc)
    frames = [
        stack_frame_from_diagnosis_entry(ent, i, content=content)
        for i, ent in enumerate(entries)
    ]
    max_pf = max(0, int(opts.max_primary_frames))
    if max_pf > 0 and len(frames) > max_pf:
        frames = frames[:max_pf]
    for i, fr in enumerate(frames):
        fr.frame_number = i
    tid, name = normalize_harmony_thread_fields(
        attributed.get("thread_id"),
        attributed.get("thread_name"),
    )
    return ThreadStack(
        tid=tid,
        name=name,
        # attributed_stack 不在 body.stacks[] 中，无数组下标 → null（勿与 -1 混淆）
        thread_index=None,
        is_crash_thread=True,
        is_main_thread=True,
        frames=frames,
        **_thread_layer_summary(frames),
    )


def _build_background_thread_from_call_stack(
    stack_meta: Dict[str, Any],
    call_stack: str,
    thread_index: int,
    opts: CrashParseOptions,
    *,
    content: str = "",
    max_frames_override: Optional[int] = None,
) -> Optional[ThreadStack]:
    frames = _parse_call_stack_pc_lines(call_stack, content=content)
    if not frames:
        return None
    if max_frames_override is not None:
        max_bf = max(0, int(max_frames_override))
    else:
        max_bf = max(0, int(opts.max_background_frames))
    if max_bf > 0 and len(frames) > max_bf:
        frames = frames[:max_bf]
    for i, fr in enumerate(frames):
        fr.frame_number = i
    tid, name = normalize_harmony_thread_fields(
        stack_meta.get("thread_id"),
        stack_meta.get("thread_name"),
    )
    return ThreadStack(
        tid=tid,
        name=name,
        thread_index=thread_index,
        is_crash_thread=False,
        is_main_thread=False,
        frames=frames,
        **_thread_layer_summary(frames),
    )


def _build_threads_full_from_diagnosis(
    doc: Dict[str, Any],
    opts: CrashParseOptions,
    *,
    content: str = "",
) -> List[ThreadStack]:
    """全量：归因崩溃线程 + ``body.stacks[]`` 每条 ``#NN pc`` 工作线程栈。"""
    threads: List[ThreadStack] = []
    crash = _build_attributed_crash_thread(doc, opts, content=content)
    if crash and crash.frames:
        threads.append(crash)

    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    stacks = body.get("stacks")
    if isinstance(stacks, list):
        for idx, st in enumerate(stacks):
            if not isinstance(st, dict):
                continue
            cs = str(st.get("call_stack") or "").strip()
            if not _call_stack_has_pc_lines(cs):
                continue
            bg = _build_background_thread_from_call_stack(
                st,
                cs,
                idx,
                opts,
                content=content,
                max_frames_override=0,
            )
            if bg and bg.frames:
                threads.append(bg)
    return threads


def _build_threads_selective_from_diagnosis(
    doc: Dict[str, Any],
    entries: List[Dict[str, Any]],
    opts: CrashParseOptions,
    *,
    content: str = "",
) -> List[ThreadStack]:
    """
    精选模式：

    1. ``crash``：``attributed_stack.stack_frames``
    2. ``primary``：最高分含应用库的 ``#NN pc`` 栈（供符号化）
    3. ``background``：其它含应用库的去重 ``call_stack``（受 max_threads 限制）
    4. ``aggregated_app_libs``：跨线程去重应用库帧
    """
    threads: List[ThreadStack] = []
    crash = _build_attributed_crash_thread(doc, opts, content=content)
    if crash and crash.frames:
        threads.append(crash)

    primary = _build_primary_thread(doc, entries, opts, content=content)
    if primary.frames:
        threads.append(primary)

    primary_cs, _ = _select_native_call_stack(doc)
    primary_sig = primary_cs.strip() if primary_cs else None
    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    stacks = body.get("stacks")
    seen_cs: set = set()
    if primary_sig:
        seen_cs.add(primary_sig)

    background_candidates: List[Tuple[int, int, str, Dict[str, Any]]] = []
    if isinstance(stacks, list):
        for idx, st in enumerate(stacks):
            if not isinstance(st, dict):
                continue
            cs = str(st.get("call_stack") or "").strip()
            if not _call_stack_has_pc_lines(cs) or cs in seen_cs:
                continue
            seen_cs.add(cs)
            preview = _parse_call_stack_pc_lines(cs)
            app_count = sum(1 for f in preview if _is_app_library_frame(f))
            if app_count <= 0:
                continue
            background_candidates.append((app_count, idx, cs, st))

    background_candidates.sort(key=lambda x: (-x[0], x[1]))
    max_threads = max(0, int(opts.max_threads))
    slots = max(0, max_threads - len(threads)) if max_threads > 0 else len(background_candidates)
    for _, idx, cs, st in background_candidates[:slots]:
        bg = _build_background_thread_from_call_stack(st, cs, idx, opts, content=content)
        if bg and bg.frames:
            threads.append(bg)

    aggregated = _collect_unique_app_frames_from_doc(doc, content=content)
    if aggregated:
        primary_keys: set = set()
        for t in threads:
            if t.is_crash_thread or t.is_main_thread is not False:
                primary_keys.update(
                    _frame_dedup_key(f) for f in t.frames if _is_app_library_frame(f)
                )
        extra_count = sum(1 for f in aggregated if _frame_dedup_key(f) not in primary_keys)
        if extra_count > 0:
            for i, fr in enumerate(aggregated):
                fr.frame_number = i
            threads.append(
                ThreadStack(
                    tid=None,
                    name="aggregated_app_libs",
                    thread_index=10000,
                    is_crash_thread=False,
                    is_main_thread=False,
                    frames=aggregated,
                    **_thread_layer_summary(aggregated),
                )
            )

    return threads


def _build_threads_from_diagnosis(
    doc: Dict[str, Any],
    entries: List[Dict[str, Any]],
    opts: CrashParseOptions,
    *,
    content: str = "",
) -> Tuple[List[ThreadStack], str]:
    """
    构建多线程输出。

    返回 ``(threads, harmony_extraction_mode)``，mode 为 ``full_by_threads`` 或 ``selective``。
    """
    os_type = extract_meta_info_from_diagnosis(doc).os_type
    if _should_full_thread_extract(doc, opts, os_type, content=content):
        return _build_threads_full_from_diagnosis(doc, opts, content=content), "full_by_threads"
    return (
        _build_threads_selective_from_diagnosis(doc, entries, opts, content=content),
        "selective",
    )


def parse_harmony_crash_diagnosis(
    content: str,
    debug: bool = False,
    *,
    options: Optional[CrashParseOptions] = None,
) -> CrashAnalysisResult:
    """解析 ``crashDiagnosis:`` JSON 导出，返回 CrashAnalysisResult。"""
    opts = options if options is not None else CrashParseOptions()
    doc = try_load_crash_diagnosis_document(content)
    if not doc:
        return CrashAnalysisResult(
            threads=[],
            crash_info=extract_crash_info(content),
            meta_info=replace(extract_meta_info(content), log_format="harmony_crash_diagnosis_json"),
            raw_content=content if opts.save_raw_content else "",
            parse_status="error",
            crash_backtrace_sum_count=0,
            crash_backtrace_index_set=max(1, int(opts.crash_segment_index)),
        )

    entries = _primary_stack_frame_entries(doc) or []
    call_stack, _ = _select_native_call_stack(doc)
    pc_frames = _parse_call_stack_pc_lines(call_stack, content=content) if call_stack else []
    if not entries and not pc_frames:
        return CrashAnalysisResult(
            threads=[],
            crash_info=extract_crash_info_from_diagnosis(doc),
            meta_info=extract_meta_info_from_diagnosis(doc),
            raw_content=content if opts.save_raw_content else "",
            parse_status="error",
            crash_backtrace_sum_count=0,
            crash_backtrace_index_set=max(1, int(opts.crash_segment_index)),
        )

    if debug:
        logger.info(
            "harmony crashDiagnosis: json_frames=%s pc_frames=%s",
            len(entries),
            len(pc_frames),
        )

    threads, harmony_mode = _build_threads_from_diagnosis(doc, entries, opts, content=content)

    os_type = extract_meta_info_from_diagnosis(doc).os_type
    if harmony_mode == "full_by_threads":
        threads, removed, lib_applied = threads, 0, False
    else:
        threads, removed, lib_applied = _maybe_filter_threads_by_library_dir(threads, os_type, opts)

    crash_info = extract_crash_info_from_diagnosis(doc)
    meta_info = extract_meta_info_from_diagnosis(doc)
    attributed = _attributed_stack_meta(doc)
    crash_tid = attributed.get("thread_id")
    crash_tname = attributed.get("thread_name")
    unique_cs_count = _count_unique_pc_call_stacks(doc)
    meta_info = replace(
        meta_info,
        thread_count_extracted=len(threads),
        library_dir_frame_filter_applied=lib_applied if lib_applied else None,
        frames_removed_by_library_dir_filter=removed if lib_applied else None,
        crash_thread_id=str(crash_tid) if crash_tid is not None else None,
        crash_thread_name=str(crash_tname).strip() if crash_tname else None,
        harmony_extraction_mode=harmony_mode,
    )

    total_frames = sum(len(t.frames) for t in threads)
    parse_status = "ok" if total_frames > 0 else "error"
    seg = max(1, int(opts.crash_segment_index))

    return CrashAnalysisResult(
        threads=threads,
        crash_info=crash_info,
        meta_info=meta_info,
        raw_content=content if opts.save_raw_content else "",
        parse_status=parse_status,
        crash_backtrace_sum_count=max(1, unique_cs_count),
        crash_backtrace_index_set=seg,
    )


def _count_unique_pc_call_stacks(doc: Dict[str, Any]) -> int:
    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    seen: set = set()
    attributed = body.get("attributed_stack") if isinstance(body.get("attributed_stack"), dict) else {}
    cs = str(attributed.get("call_stack") or "").strip()
    if _call_stack_has_pc_lines(cs):
        seen.add(cs)
    stacks = body.get("stacks")
    if isinstance(stacks, list):
        for st in stacks:
            if not isinstance(st, dict):
                continue
            cs = str(st.get("call_stack") or "").strip()
            if _call_stack_has_pc_lines(cs):
                seen.add(cs)
    return len(seen)
