#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一崩溃的分析缓冲：证据层可跨模型复用，结论层只在大模型配置相同时复用。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

CACHE_NAME = "12_crash_replay_cache.json"
EVIDENCE_PARAM_KEYS = (
    "scope",
    "prompt_mode",
    "agent_loop",
    "max_agent_rounds",
    "max_context_requests_per_round",
    "domain_knowledge",
    "knowledge_pack_id",
)


def crash_replay_index_path() -> Path:
    """缓冲索引路径。测试可用 STABILITY_AGENT_CRASH_REPLAY_INDEX 覆盖。"""
    override = str(os.environ.get("STABILITY_AGENT_CRASH_REPLAY_INDEX") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "stability-analysis-agent" / "crash-replay" / "index.jsonl"


def _canonical(payload: Dict[str, Any]) -> str:
    """稳定序列化，用来做缓冲键。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _digest(payload: Dict[str, Any]) -> str:
    """对身份材料做 sha256。"""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _log_sha256(request: Dict[str, Any]) -> str:
    """日志内容哈希。目录输入把各文件哈希再卷一次。"""
    crash = request.get("crash_input") if isinstance(request.get("crash_input"), dict) else {}
    direct = str(crash.get("sha256") or "").strip()
    if direct:
        return direct
    files = crash.get("files") if isinstance(crash.get("files"), list) else []
    parts = [
        str(item.get("sha256") or "").strip()
        for item in files
        if isinstance(item, dict) and str(item.get("sha256") or "").strip()
    ]
    if not parts:
        return ""
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def evidence_material(request: Dict[str, Any]) -> Dict[str, Any]:
    """代码配置、日志和闭源 Agent 版本。不含开源版本、开源仓库提交，也不含大模型。"""
    runtime = request.get("runtime") if isinstance(request.get("runtime"), dict) else {}
    effective = (
        request.get("effective_parameters")
        if isinstance(request.get("effective_parameters"), dict)
        else {}
    )
    source = request.get("source_identity") if isinstance(request.get("source_identity"), dict) else {}
    paths = request.get("paths") if isinstance(request.get("paths"), dict) else {}
    material: Dict[str, Any] = {
        "log_sha256": _log_sha256(request),
        "base_commit": str(source.get("base_commit") or effective.get("base_commit") or "").lower(),
        "source_origin": str(source.get("source_origin") or effective.get("source_origin") or ""),
        "library_dir": str(paths.get("library_dir") or ""),
        "enterprise_version": str(runtime.get("enterprise_version") or ""),
    }
    for key in EVIDENCE_PARAM_KEYS:
        material[key] = effective.get(key)
    return material


def llm_material(request: Dict[str, Any]) -> Dict[str, Any]:
    """决定结论能否复用的大模型配置。不含密钥。"""
    llm = request.get("llm") if isinstance(request.get("llm"), dict) else {}
    return {
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "request_format": llm.get("request_format"),
        "temperature": llm.get("temperature"),
        "max_tokens": llm.get("max_tokens"),
        "mode": llm.get("mode"),
        "llm_profile": llm.get("llm_profile"),
        "endpoint": llm.get("endpoint"),
    }


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    """读取对象 JSON；失败或不是对象时返回 None。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_index() -> List[Dict[str, Any]]:
    """读索引。坏行跳过。"""
    path = crash_replay_index_path()
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _append_index(row: Dict[str, Any]) -> None:
    """追加一条索引。"""
    path = crash_replay_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _usable_evidence(material: Dict[str, Any]) -> bool:
    """缺日志或缺闭源版本时不能复用。"""
    if not str(material.get("log_sha256") or "").strip():
        return False
    if not str(material.get("enterprise_version") or "").strip():
        return False
    return True


def _file_sha256(path: Path) -> str:
    """文件字节哈希。不存在时返回空串。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    return hashlib.sha256(raw).hexdigest()


def _conclusion_ok(report_dir: Path, apply_payload: Optional[Dict[str, Any]]) -> bool:
    """上次有可读结论，且改码记录不是失败。"""
    final_output = report_dir / "final_output.md"
    try:
        if not final_output.is_file() or not final_output.read_text(encoding="utf-8").strip():
            return False
    except OSError:
        return False
    summary = _load_json(report_dir / "00_run_summary.json") or {}
    if summary and str(summary.get("status") or "") not in {"", "success"}:
        return False
    if isinstance(apply_payload, dict) and apply_payload.get("success") is False:
        return False
    return True


def remember_crash_replay(
    report_dir: Path,
    request: Dict[str, Any],
    applied_fix: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    """把这次运行写入报告目录和索引，供以后匹配。"""
    root = Path(report_dir)
    if not root.is_dir() or not isinstance(request, dict):
        return None
    material = evidence_material(request)
    model = llm_material(request)
    apply_payload = applied_fix if isinstance(applied_fix, dict) else _load_json(root / "08_apply_ai_fixes.json")
    patch_sha = _file_sha256(root / "09_ai_fix.patch")
    evidence_ok = _usable_evidence(material) and (root / "01_crash_log_parser.json").is_file()
    evidence_ok = evidence_ok and (root / "03_add2line_resolver.json").is_file()
    evidence_ok = evidence_ok and (root / "04b_code_content_provider.json").is_file()
    conclusion_ok = evidence_ok and _conclusion_ok(root, apply_payload if isinstance(apply_payload, dict) else None)
    existing = _load_json(root / CACHE_NAME) or {}
    review = existing.get("review") if isinstance(existing.get("review"), dict) else {}
    noted = request.get("crash_replay") if isinstance(request.get("crash_replay"), dict) else {}
    reused_from = str(noted.get("source_report") or existing.get("reused_from") or "")
    if reused_from and isinstance(apply_payload, dict) and apply_payload.get("success"):
        source_cache = _load_json(Path(reused_from) / CACHE_NAME)
        if isinstance(source_cache, dict) and str(source_cache.get("patch_sha256") or "").strip():
            patch_sha = str(source_cache.get("patch_sha256") or "")
    record = {
        "schema_version": 1,
        "reusable": bool(evidence_ok),
        "evidence_key": _digest(material) if evidence_ok else "",
        "llm_key": _digest(model) if evidence_ok else "",
        "evidence": material,
        "llm": model,
        "patch_sha256": patch_sha,
        "has_patch": bool(patch_sha) and bool(isinstance(apply_payload, dict) and apply_payload.get("success")),
        "apply_success": bool(isinstance(apply_payload, dict) and apply_payload.get("success")),
        "conclusion_reusable": bool(conclusion_ok),
        "reused_from": reused_from,
        "review": review,
    }
    path = root / CACHE_NAME
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if evidence_ok:
        _append_index({
            "report_dir": str(root.resolve()),
            "evidence_key": record["evidence_key"],
            "llm_key": record["llm_key"],
            "patch_sha256": patch_sha,
            "has_patch": record["has_patch"],
            "conclusion_reusable": record["conclusion_reusable"],
            "review": review,
        })
    return path


def _load_evidence(report_dir: Path) -> Dict[str, Any]:
    """读模型之前的产物。缺文件的键不放入。"""
    mapping = {
        "parse_result": "01_crash_log_parser.json",
        "memory_maps": "02_memory_maps.json",
        "resolved_stack": "03_add2line_resolver.json",
        "crash_diagnosis": "04a_crash_diagnosis.json",
        "code_context": "04b_code_content_provider.json",
    }
    payload: Dict[str, Any] = {}
    for key, name in mapping.items():
        item = _load_json(report_dir / name)
        if isinstance(item, dict):
            payload[key] = item
    return payload


def lookup_crash_replay(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按当前请求找最近一次可复用的报告。"""
    if not isinstance(request, dict):
        return None
    material = evidence_material(request)
    if not _usable_evidence(material):
        return None
    evidence_key = _digest(material)
    model_key = _digest(llm_material(request))
    chosen: Optional[Dict[str, Any]] = None
    for row in reversed(_read_index()):
        if str(row.get("evidence_key") or "") != evidence_key:
            continue
        source = Path(str(row.get("report_dir") or ""))
        if not source.is_dir():
            continue
        if not (source / "01_crash_log_parser.json").is_file():
            continue
        chosen = row
        break
    if chosen is None:
        return None
    source = Path(str(chosen.get("report_dir") or ""))
    evidence = _load_evidence(source)
    if "parse_result" not in evidence or "resolved_stack" not in evidence:
        return None
    if "code_context" not in evidence:
        return None
    same_model = str(chosen.get("llm_key") or "") == model_key and bool(chosen.get("conclusion_reusable"))
    hit: Dict[str, Any] = {
        "level": "conclusion" if same_model else "evidence",
        "source_report": str(source),
        "evidence_key": evidence_key,
        "llm_key": model_key,
        "evidence": evidence,
    }
    if not same_model:
        return hit
    try:
        analysis = (source / "final_output.md").read_text(encoding="utf-8")
    except OSError:
        analysis = ""
    if not analysis.strip():
        hit["level"] = "evidence"
        return hit
    apply_record = _load_json(source / "08_apply_ai_fixes.json")
    patch_text = ""
    patch_path = source / "09_ai_fix.patch"
    if patch_path.is_file() and isinstance(apply_record, dict) and apply_record.get("success"):
        try:
            patch_text = patch_path.read_text(encoding="utf-8")
        except OSError:
            patch_text = ""
    hit["analysis"] = analysis
    hit["patch_text"] = patch_text
    hit["apply_record"] = apply_record if patch_text else None
    hit["result"] = {
        "status": "success",
        "parse_result": evidence.get("parse_result") or {},
        "memory_maps": evidence.get("memory_maps") or {},
        "resolved_stack": evidence.get("resolved_stack") or {},
        "crash_diagnosis": evidence.get("crash_diagnosis") or {},
        "code_context": evidence.get("code_context") or {},
        "analysis": analysis,
        "final_tip": analysis,
        "metadata": {
            "crash_replay": {
                "level": "conclusion",
                "source_report": str(source),
            },
        },
    }
    return hit


def replay_cached_fix(
    *,
    run_id: str,
    code_roots: List[str],
    patch_text: str,
    previous_apply: Optional[Dict[str, Any]],
    report_dir: Path,
) -> Dict[str, Any]:
    """在新的隔离工作区上应用上次补丁，不再请求模型。"""
    import sys

    from services.git_worktree_manager import prepare_isolated_workspace, write_workspace_artifacts

    print("[crash-replay] 复用上次代码修改", file=sys.stderr, flush=True)
    record = dict(previous_apply or {})
    record["replayed"] = True
    try:
        workspace = prepare_isolated_workspace(run_id, code_roots)
    except Exception as exc:
        record["success"] = False
        record["replay_error"] = f"无法建立隔离工作区: {exc}"
        return record
    applied = False
    errors: List[str] = []
    for item in workspace.repositories:
        check = subprocess.run(
            ["git", "-C", str(item.worktree), "apply", "--check", "-"],
            input=patch_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if check.returncode != 0:
            errors.append((check.stderr or check.stdout or "").strip())
            continue
        proc = subprocess.run(
            ["git", "-C", str(item.worktree), "apply", "-"],
            input=patch_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            applied = True
        else:
            errors.append((proc.stderr or proc.stdout or "").strip())
    if not applied:
        record["success"] = False
        record["replay_error"] = "上次补丁未能应用到当前工作区"
        if errors:
            record["replay_error"] += ": " + errors[0][:400]
        return record
    write_workspace_artifacts(workspace, Path(report_dir))
    record["success"] = True
    record["replay_error"] = ""
    return record


def find_reusable_review(
    report_dir: Path,
    *,
    creator: str,
    repo: str,
    branch: str,
) -> Optional[Dict[str, Any]]:
    """同分析缓冲、同补丁、同仓库分支、同创建者时返回已有评审。"""
    current = _load_json(Path(report_dir) / CACHE_NAME)
    if not isinstance(current, dict) or not current.get("reusable"):
        return None
    creator_text = str(creator or "").strip()
    patch_sha = str(current.get("patch_sha256") or "").strip()
    evidence_key = str(current.get("evidence_key") or "").strip()
    llm_key = str(current.get("llm_key") or "").strip()
    if not creator_text or not patch_sha or not evidence_key or not llm_key:
        return None
    current_dir = str(Path(report_dir).resolve())
    for row in reversed(_read_index()):
        if str(row.get("report_dir") or "") == current_dir:
            continue
        if str(row.get("evidence_key") or "") != evidence_key:
            continue
        if str(row.get("llm_key") or "") != llm_key:
            continue
        if str(row.get("patch_sha256") or "") != patch_sha:
            continue
        review = row.get("review") if isinstance(row.get("review"), dict) else {}
        if str(review.get("creator") or "").strip() != creator_text:
            continue
        if repo and str(review.get("repo") or "").strip() != str(repo).strip():
            continue
        if branch and str(review.get("branch") or "").strip() != str(branch).strip():
            continue
        url = str(review.get("review_url") or "").strip()
        if url:
            return dict(review)
    return None


def attach_review(report_dir: Path, review: Dict[str, Any]) -> None:
    """把评审链接和创建者写回这次报告，并追加到索引。"""
    root = Path(report_dir)
    current = _load_json(root / CACHE_NAME) or {}
    current["review"] = {
        "review_url": str(review.get("review_url") or ""),
        "creator": str(review.get("creator") or ""),
        "repo": str(review.get("repo") or ""),
        "branch": str(review.get("branch") or ""),
        "commit": str(review.get("commit") or ""),
    }
    (root / CACHE_NAME).write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if current.get("evidence_key"):
        _append_index({
            "report_dir": str(root.resolve()),
            "evidence_key": current.get("evidence_key") or "",
            "llm_key": current.get("llm_key") or "",
            "patch_sha256": current.get("patch_sha256") or "",
            "has_patch": bool(current.get("has_patch")),
            "conclusion_reusable": bool(current.get("conclusion_reusable")),
            "review": current["review"],
        })
