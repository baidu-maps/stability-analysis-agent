#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一崩溃的分析缓冲：证据一致时复用定位结果；结论只在全流程参数一致时复用。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
_EVIDENCE_IDENTITY_KEYS = (
    "log_sha256",
    "base_commit",
    "source_origin",
    "library_dir",
    "enterprise_version",
    "domain_knowledge",
    "knowledge_pack_id",
)
_CONCLUSION_PARAM_KEYS = (
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
    """记下产出结论的大模型配置。不含密钥，查找时不参与匹配。"""
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


def _review_fields(review: Any) -> Dict[str, str]:
    """评审记录至少保留链接、创建者和仓库定位。"""
    payload = review if isinstance(review, dict) else {}
    return {
        "review_url": str(payload.get("review_url") or ""),
        "creator": str(payload.get("creator") or ""),
        "repo": str(payload.get("repo") or ""),
        "branch": str(payload.get("branch") or ""),
        "commit": str(payload.get("commit") or ""),
    }


def _source_patch_ready(source_cache: Optional[Dict[str, Any]]) -> bool:
    """复用来源已经记过补丁，并且有补丁哈希。"""
    if not isinstance(source_cache, dict):
        return False
    if not source_cache.get("has_patch"):
        return False
    return bool(str(source_cache.get("patch_sha256") or "").strip())


def _write_apply_artifact(report_dir: Path, record: Dict[str, Any]) -> None:
    """把改码结果写成 ``08_apply_ai_fixes.json``。"""
    root = Path(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    (root / "08_apply_ai_fixes.json").write_text(text, encoding="utf-8")


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
    source_cache: Optional[Dict[str, Any]] = None
    if reused_from:
        loaded_source = _load_json(Path(reused_from) / CACHE_NAME)
        if isinstance(loaded_source, dict):
            source_cache = loaded_source
    current_apply_ok = bool(isinstance(apply_payload, dict) and apply_payload.get("success"))
    source_sha = str((source_cache or {}).get("patch_sha256") or "").strip()
    source_ready = isinstance(source_cache, dict) and _source_patch_ready(source_cache)
    # 共享检出里补丁还在、本次不再写 08 时，不能把「已改码」记成没有补丁。
    if source_ready and not current_apply_ok:
        patch_sha = source_sha
        has_patch = True
        apply_success = bool(source_cache.get("apply_success"))
    else:
        if current_apply_ok and source_sha:
            patch_sha = source_sha
        has_patch = bool(patch_sha) and current_apply_ok
        apply_success = current_apply_ok
    current_review_url = str(review.get("review_url") or "").strip()
    # 只继承补丁。评审链接和创建人属于上次推送人，不抄到这次报告上。
    if source_ready and not current_review_url:
        review = {}
    record = {
        "schema_version": 1,
        "reusable": bool(evidence_ok),
        "evidence_key": _digest(material) if evidence_ok else "",
        "llm_key": _digest(model) if evidence_ok else "",
        "evidence": material,
        "llm": model,
        "patch_sha256": patch_sha,
        "has_patch": has_patch,
        "apply_success": apply_success,
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


def _load_ready_evidence(report_dir: Path) -> Optional[Dict[str, Any]]:
    """读齐解析、符号化和源码定位后才可复用。"""
    if not report_dir.is_dir():
        return None
    evidence = _load_evidence(report_dir)
    if "parse_result" not in evidence or "resolved_stack" not in evidence:
        return None
    if "code_context" not in evidence:
        return None
    return evidence


def _conclusion_text(report_dir: Path) -> str:
    """上次结论文本。读不到时返回空串。"""
    try:
        return (report_dir / "final_output.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _evidence_identity(material: Dict[str, Any]) -> Dict[str, Any]:
    """定位结果是否可复用。范围和模型轮数不参与。"""
    return {key: material.get(key) for key in _EVIDENCE_IDENTITY_KEYS}


def _cached_evidence_material(source: Path) -> Optional[Dict[str, Any]]:
    """读报告里记下的证据材料。旧索引只有哈希时返回 None。"""
    record = _load_json(source / CACHE_NAME)
    if not isinstance(record, dict):
        return None
    material = record.get("evidence")
    return material if isinstance(material, dict) else None


def _same_evidence(requested: Dict[str, Any], cached: Dict[str, Any]) -> bool:
    """日志、源码提交、符号目录和企业版本一致即可复用定位。"""
    return _evidence_identity(requested) == _evidence_identity(cached)


def _conclusion_compatible(requested: Dict[str, Any], cached: Dict[str, Any]) -> bool:
    """只有两次都是全流程，且提示词参数一致，才复用结论。"""
    if str(requested.get("scope") or "") != "full":
        return False
    if str(cached.get("scope") or "") != "full":
        return False
    for key in _CONCLUSION_PARAM_KEYS:
        if requested.get(key) != cached.get(key):
            return False
    return True


def lookup_crash_replay(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按证据找可复用报告。全流程可接上一次只生成提示词的定位结果。"""
    if not isinstance(request, dict):
        return None
    material = evidence_material(request)
    if not _usable_evidence(material):
        return None
    evidence_key = _digest(material)
    evidence_hit: Optional[Dict[str, Any]] = None
    conclusion_hit: Optional[Dict[str, Any]] = None
    for row in reversed(_read_index()):
        source = Path(str(row.get("report_dir") or ""))
        cached_material = _cached_evidence_material(source)
        if cached_material is None:
            if str(row.get("evidence_key") or "") != evidence_key:
                continue
            cached_material = material
        elif not _same_evidence(material, cached_material):
            continue
        evidence = _load_ready_evidence(source)
        if evidence is None:
            continue
        if evidence_hit is None:
            evidence_hit = {"row": row, "source": source, "evidence": evidence}
        conclusion_ok = bool(row.get("conclusion_reusable")) and _conclusion_compatible(
            material,
            cached_material,
        )
        if conclusion_hit is None and conclusion_ok:
            analysis = _conclusion_text(source)
            if analysis.strip():
                conclusion_hit = {
                    "row": row,
                    "source": source,
                    "evidence": evidence,
                    "analysis": analysis,
                }
        if evidence_hit is not None and conclusion_hit is not None:
            break
    if conclusion_hit is not None:
        return _conclusion_lookup(evidence_key, conclusion_hit)
    if evidence_hit is None:
        return None
    source = evidence_hit["source"]
    return {
        "level": "evidence",
        "source_report": str(source),
        "evidence_key": evidence_key,
        "llm_key": str(evidence_hit["row"].get("llm_key") or ""),
        "evidence": evidence_hit["evidence"],
    }


def _load_replay_context_session(source: Path) -> Dict[str, Any]:
    """沿 reused_from 找到带 request_ledger 的 session，避免空报告把预取路径丢掉。"""
    current = Path(source)
    seen = set()
    last: Dict[str, Any] = {}
    for _ in range(6):
        key = str(current.resolve())
        if key in seen:
            break
        seen.add(key)
        session = _load_json(current / "context_session.json")
        if isinstance(session, dict):
            last = session
            if session.get("request_ledger"):
                return session
        cache = _load_json(current / CACHE_NAME) or {}
        reused = str(cache.get("reused_from") or "").strip()
        if not reused:
            break
        current = Path(reused)
    return last


def _required_fix_names_with_fences(analysis: str) -> List[str]:
    """分析必改且已给出完整函数围栏的目标，才作为补丁完整性标准。"""
    try:
        from services.code_fixer import (
            _extract_replacement_from_analysis,
            _extract_required_function_names_from_analysis,
        )
    except Exception:
        return []
    names = _extract_required_function_names_from_analysis(analysis)
    kept: List[str] = []
    for name in names:
        replacement = _extract_replacement_from_analysis(analysis, name)
        if replacement and str(replacement).strip():
            kept.append(name)
    return kept


def _apply_covers_required_functions(
    analysis: str,
    apply_record: Optional[Dict[str, Any]],
    patch_text: str,
) -> bool:
    """上次补丁必须覆盖分析里已给出完整围栏的必改函数。"""
    names = _required_fix_names_with_fences(analysis)
    if not names:
        return bool(str(patch_text or "").strip())
    if not str(patch_text or "").strip() or not isinstance(apply_record, dict):
        return False
    try:
        from services.code_fixer import _extract_simple_function_name
    except Exception:
        return False
    applied_names = set()
    for item in apply_record.get("applied") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "applied").strip().lower()
        if status not in {"applied", "success"}:
            continue
        simple = _extract_simple_function_name(str(item.get("function_signature") or ""))
        if simple:
            applied_names.add(simple)
    blob = str(patch_text)
    for name in names:
        simple = _extract_simple_function_name(name)
        head = str(name or "").split("(", 1)[0]
        if simple and (simple in applied_names or simple in blob):
            continue
        if head and head in blob:
            continue
        return False
    return True


def _conclusion_lookup(evidence_key: str, chosen: Dict[str, Any]) -> Dict[str, Any]:
    """把最近一次成功结论装成可直接套用的命中。"""
    source = chosen["source"]
    evidence = chosen["evidence"]
    analysis = str(chosen["analysis"] or "")
    apply_record = _load_json(source / "08_apply_ai_fixes.json")
    patch_text = ""
    patch_path = source / "09_ai_fix.patch"
    if patch_path.is_file() and isinstance(apply_record, dict) and apply_record.get("success"):
        try:
            patch_text = patch_path.read_text(encoding="utf-8")
        except OSError:
            patch_text = ""
    if not _apply_covers_required_functions(analysis, apply_record, patch_text):
        patch_text = ""
        apply_record = None
    session = _load_replay_context_session(source)
    return {
        "level": "conclusion",
        "source_report": str(source),
        "evidence_key": evidence_key,
        "llm_key": str(chosen["row"].get("llm_key") or ""),
        "evidence": evidence,
        "analysis": analysis,
        "patch_text": patch_text,
        "apply_record": apply_record if patch_text else None,
        "result": {
            "status": "success",
            "parse_result": evidence.get("parse_result") or {},
            "memory_maps": evidence.get("memory_maps") or {},
            "resolved_stack": evidence.get("resolved_stack") or {},
            "crash_diagnosis": evidence.get("crash_diagnosis") or {},
            "code_context": evidence.get("code_context") or {},
            "context_session": session if isinstance(session, dict) else {},
            "analysis": analysis,
            "final_tip": analysis,
            "metadata": {
                "crash_replay": {
                    "level": "conclusion",
                    "source_report": str(source),
                },
            },
        },
    }


def replay_cached_fix(
    *,
    run_id: str,
    code_roots: List[str],
    patch_text: str,
    previous_apply: Optional[Dict[str, Any]],
    report_dir: Path,
    reuse_source_cache: bool = True,
) -> Dict[str, Any]:
    """在工作区上应用上次补丁，不再请求模型。

    源码缓冲命中且企业侧标记了就地改码时，补丁打在已检出的目录上。
    否则仍新建隔离 worktree。
    """
    import sys

    from services.git_worktree_manager import (
        _apply_in_place_requested,
        adopt_prepared_checkout,
        prepare_isolated_workspace,
        write_workspace_artifacts,
    )

    record = dict(previous_apply or {})
    record["replayed"] = True
    keep_applied = os.environ.get("BD_SA_KEEP_APPLIED_PATCH") == "1"
    try:
        if reuse_source_cache and _apply_in_place_requested():
            workspace = adopt_prepared_checkout(run_id, code_roots)
        else:
            workspace = prepare_isolated_workspace(run_id, code_roots)
            keep_applied = False
    except Exception as exc:
        record["success"] = False
        record["replay_error"] = f"无法建立隔离工作区: {exc}"
        return record
    if keep_applied:
        print("[crash-replay] 补丁已在工作区，不再重放", file=sys.stderr, flush=True)
        write_workspace_artifacts(workspace, Path(report_dir))
        record["success"] = True
        record["kept_applied_patch"] = True
        record["replay_error"] = ""
        # 不再重放时调用方可能不落盘；HTTP 的 fixes_applied 只看这份 08。
        _write_apply_artifact(Path(report_dir), record)
        return record
    print("[crash-replay] 复用上次代码修改", file=sys.stderr, flush=True)
    applied = False
    errors: List[str] = []
    for item in workspace.repositories:
        check = subprocess.run(
            ["git", "-C", str(item.worktree), "apply", "--recount", "--check", "-"],
            input=patch_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if check.returncode != 0:
            errors.append((check.stderr or check.stdout or "").strip())
            continue
        proc = subprocess.run(
            ["git", "-C", str(item.worktree), "apply", "--recount", "-"],
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
        record["error"] = record["replay_error"]
        return record
    write_workspace_artifacts(workspace, Path(report_dir))
    record["success"] = True
    record["replay_error"] = ""
    return record


def _env_json_list(name: str) -> List[str]:
    """读环境变量里的 JSON 字符串列表。坏数据当成空列表。"""
    try:
        payload = json.loads(str(os.environ.get(name) or "[]"))
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item).strip() for item in payload if str(item).strip()]


def _review_repo_selected(
    repo: str,
    code_roots: Sequence[str],
    named_repos: Sequence[str],
) -> bool:
    """评审仓库是本次检出目录，或是记下的仓库名。"""
    text = str(repo or "").strip()
    if not text:
        return False
    if text in named_repos:
        return True
    candidate = Path(text)
    if not candidate.is_absolute():
        return False
    for root in code_roots:
        root_text = str(root or "").strip()
        if not root_text:
            continue
        root_path = Path(root_text).expanduser()
        try:
            candidate.resolve().relative_to(root_path.resolve())
        except (OSError, ValueError):
            continue
        return True
    return False


def peek_reusable_review(
    evidence_key: str,
    patch_text: str,
    *,
    creator: str,
    code_roots: Sequence[str],
    repos: Sequence[str],
    branches: Sequence[str],
) -> Optional[Dict[str, Any]]:
    """同一创建人、同一补丁、同一仓库分支已有评审时返回它。不读本次报告。"""
    evidence = str(evidence_key or "").strip()
    creator_text = str(creator or "").strip()
    patch_sha = hashlib.sha256(str(patch_text or "").encode("utf-8")).hexdigest()
    branch_set = {str(item).strip() for item in branches if str(item).strip()}
    if not evidence or not creator_text or not str(patch_text or "").strip() or not branch_set:
        return None
    for row in reversed(_read_index()):
        if str(row.get("evidence_key") or "") != evidence:
            continue
        if not row.get("conclusion_reusable") or not row.get("has_patch"):
            continue
        if str(row.get("patch_sha256") or "") != patch_sha:
            continue
        review = row.get("review") if isinstance(row.get("review"), dict) else {}
        if str(review.get("creator") or "").strip() != creator_text:
            continue
        if str(review.get("branch") or "").strip() not in branch_set:
            continue
        if not _review_repo_selected(str(review.get("repo") or ""), code_roots, repos):
            continue
        if str(review.get("review_url") or "").strip():
            return dict(review)
    return None


def settle_deferred_checkout(
    replay_hit: Optional[Dict[str, Any]],
    code_roots: Sequence[str],
) -> bool:
    """能复用评审就留下已套用的补丁，否则把推迟的复位现在做掉。"""
    if os.environ.get("BD_SA_DEFER_DIRTY_RESET") != "1":
        os.environ.pop("BD_SA_KEEP_APPLIED_PATCH", None)
        return False
    review = None
    if isinstance(replay_hit, dict) and str(replay_hit.get("level") or "") == "conclusion":
        review = peek_reusable_review(
            str(replay_hit.get("evidence_key") or ""),
            str(replay_hit.get("patch_text") or ""),
            creator=str(os.environ.get("BD_SA_REVIEW_CREATOR") or ""),
            code_roots=code_roots,
            repos=_env_json_list("BD_SA_REVIEW_REPOS"),
            branches=_env_json_list("BD_SA_REVIEW_BRANCHES"),
        )
    if review is not None:
        os.environ["BD_SA_KEEP_APPLIED_PATCH"] = "1"
        return True
    os.environ.pop("BD_SA_KEEP_APPLIED_PATCH", None)
    os.environ["BD_SA_DEFER_DIRTY_RESET"] = "0"
    from services.git_worktree_manager import discard_deferred_checkout

    discard_deferred_checkout(code_roots)
    return False


def find_reusable_review(
    report_dir: Path,
    *,
    creator: str,
    repo: str,
    branch: str,
) -> Optional[Dict[str, Any]]:
    """同补丁、同仓库分支、且创建人就是本次推送人时，才返回已有评审。

    不看大模型。别的创建人留下的链接不能命中，旧评审也不在这里改写。
    """
    current = _load_json(Path(report_dir) / CACHE_NAME)
    if not isinstance(current, dict) or not current.get("reusable"):
        return None
    creator_text = str(creator or "").strip()
    patch_sha = str(current.get("patch_sha256") or "").strip()
    evidence_key = str(current.get("evidence_key") or "").strip()
    if not creator_text or not patch_sha or not evidence_key:
        return None
    current_dir = str(Path(report_dir).resolve())
    for row in reversed(_read_index()):
        if str(row.get("report_dir") or "") == current_dir:
            continue
        if str(row.get("evidence_key") or "") != evidence_key:
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
