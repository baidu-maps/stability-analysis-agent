#!/usr/bin/env python3
"""Two-stage, deterministic API fault diagnosis with compact knowledge entries."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from tools.diagnosis.models import KnowledgeEntry
from tools.diagnosis.project_context import discover_project_context


KNOWLEDGE: List[Dict[str, Any]] = [
    {"id": "multimedia.no_permission", "module": "multimedia", "api": ["AVPlayer", "AVRecorder"], "codes": ["5400101"], "patterns": ["no permission", "mserr_user_no_permission"], "root_cause": "应用缺少媒体权限或媒体服务内存不足", "guidance": ["检查 ohos.permission 媒体相关声明与运行时授权", "同时排除 No Memory 映射到同一错误码的情况"], "source": "Huawei apifault-analysis multimedia_player_framework", "api_version": "HarmonyOS"},
    {"id": "multimedia.invalid_parameter", "module": "multimedia", "api": ["AVPlayer", "AVRecorder", "AVMetadata"], "codes": ["5400102"], "patterns": ["invalid parameter", "mserr_invalid_val", "mserr_invalid_operation"], "root_cause": "媒体 API 入参不合法或当前状态不允许该操作", "guidance": ["核对 URL/fd/格式参数和播放器状态机", "不要在 prepare 完成前调用 play/seek"], "source": "Huawei apifault-analysis multimedia_player_framework", "api_version": "HarmonyOS"},
    {"id": "multimedia.io_error", "module": "multimedia", "api": ["AVPlayer"], "codes": ["5400103"], "patterns": ["io error", "open file failed", "data source io"], "root_cause": "媒体文件读写失败或数据源 IO 异常", "guidance": ["检查文件路径、URI 可达性和存储权限", "对 5400103 交叉验证媒体文件是否损坏"], "source": "Huawei apifault-analysis multimedia_player_framework", "api_version": "HarmonyOS"},
    {"id": "multimedia.network_timeout", "module": "multimedia", "api": ["AVPlayer"], "codes": ["5400104", "5411002"], "patterns": ["network timeout", "connection timeout", "mserr_network_timeout"], "root_cause": "流媒体网络超时或 TCP 连接建立失败", "guidance": ["检查网络连通性、DNS 和带宽", "为播放器设置超时并在 error 回调中释放实例"], "source": "Huawei apifault-analysis multimedia_player_framework", "api_version": "HarmonyOS"},
    {"id": "multimedia.service_died", "module": "multimedia", "api": ["AVPlayer", "AVRecorder", "AVSession"], "codes": ["5400105"], "patterns": ["service died", "media service", "ms_err_ext_api9_service_died"], "root_cause": "媒体服务进程崩溃或被系统回收", "guidance": ["销毁旧的 AVPlayer/AVRecorder 实例", "重新创建并初始化资源", "监听 stateChange/error 状态后再恢复播放或录制"], "source": "Huawei apifault-analysis multimedia knowledge", "api_version": "HarmonyOS"},
    {"id": "multimedia.unsupported_format", "module": "multimedia", "api": ["AVPlayer", "AVMetadata"], "codes": ["5400106"], "patterns": ["unsupported format", "format not support", "invalid media"], "root_cause": "媒体格式不支持或媒体文件无效", "guidance": ["检查容器格式、编码格式和文件完整性", "确认文件路径和访问权限", "在调用 API 前校验媒体资源"], "source": "Huawei apifault-analysis multimedia knowledge", "api_version": "HarmonyOS"},
    {"id": "multimedia.audio_interrupted", "module": "multimedia", "api": ["AVPlayer"], "codes": ["5400107"], "patterns": ["audio interrupted", "aud_interrupt"], "root_cause": "音频焦点被电话/闹钟等抢占", "guidance": ["监听 audioInterrupt 并在可恢复时继续播放", "这是正常业务场景，不要当成播放器缺陷"], "source": "Huawei apifault-analysis multimedia_player_framework", "api_version": "HarmonyOS"},
    {"id": "multimedia.host_not_found", "module": "multimedia", "api": ["AVPlayer"], "codes": ["5411001"], "patterns": ["cannot find host", "dns"], "root_cause": "流媒体主机 DNS 解析失败", "guidance": ["检查播放地址和 DNS 配置"], "source": "Huawei apifault-analysis multimedia_player_framework", "api_version": "HarmonyOS"},
    {"id": "common.permission_denied", "module": "permission", "api": [], "codes": ["201", "401"], "patterns": ["permission denied", "not authorized", "access denied"], "root_cause": "权限未声明、未授予或运行时权限已失效", "guidance": ["检查 module.json5 权限声明", "在调用 API 前完成运行时授权并处理拒绝分支"], "source": "Huawei apifault-analysis common issues", "api_version": "HarmonyOS"},
    {"id": "common.invalid_parameter", "module": "common", "api": [], "codes": [], "patterns": ["invalid parameter", "parameter invalid", "bad argument", "invalid value"], "root_cause": "API 入参为空、越界、类型不匹配或状态不合法", "guidance": ["根据 API 契约校验参数和对象状态", "保留错误码并在失败分支释放临时资源"], "source": "Huawei apifault-analysis common issues", "api_version": "HarmonyOS"},
    {"id": "common.repeated_reset", "module": "common", "api": ["reset", "stop", "prepare"], "codes": [], "patterns": ["reset", "already reset", "invalid state"], "root_cause": "应用侧重复 reset/stop/prepare，框架守卫只是表象", "guidance": ["根据状态机收敛调用顺序，避免在已释放实例上重复 reset", "把错误码回溯到应用调用时间线，而不是停在框架校验"], "source": "Huawei apifault-analysis state-machine rule", "api_version": "HarmonyOS"},
    {"id": "network.timeout", "module": "network", "api": ["http", "request", "fetch", "socket"], "codes": [], "patterns": ["timeout", "timed out", "connection refused", "network unavailable"], "root_cause": "网络不可达、服务端响应超时或连接状态失效", "guidance": ["设置合理超时和取消机制", "区分可重试错误与业务失败", "避免在 UI/主线程同步等待网络"], "source": "Huawei apifault-analysis network knowledge", "api_version": "HarmonyOS"},
    {"id": "database.invalid_state", "module": "database", "api": ["rdb", "database", "resultset"], "codes": ["14800014", "14800013", "14800021"], "patterns": ["database", "resultset", "cursor", "transaction", "closed", "already closed", "column out of bounds"], "root_cause": "数据库连接、事务或 ResultSet 生命周期状态不合法", "guidance": ["核对 open/begin/commit/close 生命周期", "确保 ResultSet 使用完成后释放，避免跨线程复用连接"], "source": "Huawei apifault-analysis database knowledge", "api_version": "HarmonyOS"},
]


def knowledge_entries() -> List[KnowledgeEntry]:
    return [KnowledgeEntry(id=item["id"], domain="api_fault", module=item["module"], root_cause=item["root_cause"], evidence_patterns=item["patterns"], guidance=item["guidance"], source=item["source"], api_version=item["api_version"]) for item in KNOWLEDGE]


def _first(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _normalize_code(value: Any) -> Dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {"raw_code": None, "normalized_code": None, "code_format": None}
    try:
        if raw.lower().startswith("0x"):
            return {"raw_code": raw, "normalized_code": str(int(raw, 16)), "code_format": "hex"}
        if re.fullmatch(r"[+-]?\d+", raw):
            return {"raw_code": raw, "normalized_code": str(int(raw, 10)), "code_format": "decimal"}
    except ValueError:
        pass
    return {"raw_code": raw, "normalized_code": raw, "code_format": "symbolic"}


def normalize_api_error(data: Mapping[str, Any]) -> Dict[str, Any]:
    payload = data.get("error") if isinstance(data.get("error"), Mapping) else data
    raw_log = str(data.get("raw_log") or data.get("raw_content") or payload.get("raw_log") or "")
    code = _first(payload, "error_code", "errorCode", "code", "errno", "status_code")
    name = _first(payload, "error_name", "errorName", "name", "exception")
    message = _first(payload, "error_message", "errorMessage", "message", "reason")
    api = _first(payload, "api", "api_name", "apiName", "function", "method")
    module = _first(payload, "module", "domain", "component")
    if raw_log:
        def find(label: str) -> Optional[str]:
            match = re.search(rf"(?im)^\s*{label}\s*[:=]\s*(.+?)\s*$", raw_log)
            return match.group(1).strip() if match else None
        code = code or find("Error code") or find("code")
        name = name or find("Error name") or find("name")
        message = message or find("Error message") or find("message")
        api = api or find("API")
    code_info = _normalize_code(code)
    return {**code_info, "error_name": str(name or ""), "message": str(message or ""), "api": str(api or ""), "module": str(module or ""), "source": "structured+raw_log" if raw_log else "structured"}


def _module_candidates(error: Mapping[str, Any]) -> List[Dict[str, Any]]:
    text = " ".join(str(error.get(key) or "") for key in ("message", "api", "module", "error_name")).lower()
    code = str(error.get("normalized_code") or "")
    scores: Dict[str, Dict[str, Any]] = {}
    for entry in KNOWLEDGE:
        score = 0.0
        evidence: List[str] = []
        if entry["module"].lower() in text:
            score += 0.35; evidence.append("module text")
        for api in entry["api"]:
            if api.lower() in text:
                score += 0.4; evidence.append(f"api={api}")
        for candidate in entry["codes"]:
            if candidate == code:
                score += 0.55; evidence.append(f"code={candidate}")
        for pattern in entry["patterns"]:
            if pattern.lower() in text:
                score += 0.3; evidence.append(f"message={pattern}")
        if score:
            item = scores.setdefault(entry["module"], {"module": entry["module"], "score": 0.0, "evidence": [], "knowledge_ids": []})
            item["score"] = max(item["score"], min(0.99, score))
            item["evidence"].extend(evidence)
            item["knowledge_ids"].append(entry["id"])
    if error.get("module") and not scores:
        scores[str(error["module"])] = {"module": str(error["module"]), "score": 0.5, "evidence": ["explicit module"], "knowledge_ids": []}
    return sorted(scores.values(), key=lambda item: (-item["score"], item["module"]))


def _matches(error: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    text = " ".join(str(error.get(key) or "") for key in ("message", "api", "module", "error_name")).lower()
    code = str(error.get("normalized_code") or "")
    result = []
    for entry in KNOWLEDGE:
        if candidates and entry["module"] != candidates[0]["module"] and entry["module"] not in {item["module"] for item in candidates[:2]}:
            continue
        evidence = []
        if code in entry["codes"]: evidence.append(f"code={code}")
        evidence.extend(f"api={api}" for api in entry["api"] if api.lower() in text)
        evidence.extend(f"message={pattern}" for pattern in entry["patterns"] if pattern.lower() in text)
        if evidence:
            result.append({"id": entry["id"], "module": entry["module"], "root_cause": entry["root_cause"], "guidance": entry["guidance"], "evidence": list(dict.fromkeys(evidence)), "source": entry["source"], "api_version": entry["api_version"], "confidence": min(0.98, 0.5 + 0.15 * len(set(evidence)))})
    return sorted(result, key=lambda item: (-item["confidence"], item["id"]))


def _project_evidence(error: Mapping[str, Any], project_root: Optional[str]) -> Dict[str, Any]:
    needles = [str(error.get("api") or "").strip(), str(error.get("module") or "").strip()]
    needles = [needle for needle in needles if len(needle) >= 3]
    return discover_project_context(project_root, needles, max_files=50)


def _state_timeline(raw_log: str) -> Dict[str, Any]:
    text = raw_log or ""
    actions = ("reset", "stop", "prepare", "play", "release")
    counts = {action: len(re.findall(rf"\b{action}\b", text, re.I)) for action in actions}
    notes = []
    if counts["reset"] >= 3:
        notes.append("repeated reset() — escalate past the framework guard to the app call sequence")
    if counts["prepare"] and counts["play"] and counts["prepare"] > counts["play"] * 2:
        notes.append("prepare 远多于 play，可能在未就绪状态下反复初始化")
    return {"counts": counts, "notes": notes, "escalate_to_app": bool(notes)}


def diagnose_api_fault(data: Mapping[str, Any], *, project_root: Optional[str] = None) -> Dict[str, Any]:
    error = normalize_api_error(data)
    candidates = _module_candidates(error)
    matches = _matches(error, candidates)
    raw_log = str(data.get("raw_log") or data.get("raw_content") or "")
    timeline = _state_timeline(raw_log)
    if timeline["escalate_to_app"] and not any(item["id"] == "common.repeated_reset" for item in matches):
        matches.append({"id": "common.repeated_reset", "module": "common", "root_cause": "应用侧重复 reset/stop/prepare，框架守卫只是表象", "guidance": ["根据状态机收敛调用顺序，避免在已释放实例上重复 reset"], "evidence": timeline["notes"], "source": "Huawei apifault-analysis state-machine rule", "api_version": "HarmonyOS", "confidence": 0.7})
        matches.sort(key=lambda item: (-item["confidence"], item["id"]))
    project = _project_evidence(error, project_root or data.get("project_root"))
    missing: List[Dict[str, Any]] = []
    if not error["normalized_code"]: missing.append({"id": "error_code", "description": "缺少错误码", "required": False})
    if not error["message"]: missing.append({"id": "message", "description": "缺少完整错误信息", "required": True})
    if not error["api"]: missing.append({"id": "api_name", "description": "缺少失败 API 名称", "required": True})
    if project.get("status") == "success" and not project.get("api_usage_sites"): missing.append({"id": "project_usage", "description": "项目中未找到 API 使用点，需核对源码/版本", "required": False})
    confidence = max((float(item["confidence"]) for item in matches), default=(candidates[0]["score"] if candidates else 0.2))
    status = "confirmed" if confidence >= 0.85 and matches else ("probable" if matches or candidates else "preliminary")
    guidance = {"direct_fix": [item["root_cause"] for item in matches[:3]], "defensive_fix": list(dict.fromkeys(g for item in matches[:3] for g in item["guidance"])), "verification": ["使用对应错误码和失败 API 的回归 Case 验证错误处理。", "确认失败分支释放资源、取消异步任务并向调用方返回可识别错误。"]}
    return {"status": "success", "diagnosis_status": status, "error": error, "module_classification": {"candidates": candidates, "selected": candidates[0] if candidates else None}, "knowledge_matches": matches, "state_timeline": timeline, "project_context": project, "diagnosis": {"root_cause": matches[0]["root_cause"] if matches else "需要补充错误码、API 和项目上下文后再确定根因", "confidence": round(confidence, 3), "evidence": (matches[0]["evidence"] if matches else [])}, "missing_evidence": missing, "next_questions": ["调用的是哪个具体 API？", "错误码和完整错误消息是什么？", "是否有对应版本的设备日志？"] if missing else [], "repair_guidance": guidance}
