"""Bounded repair edit loop after verification failures."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.action_failures import normalize_action_result
from services.agent_output_parser import extract_json_object
from services.agent_schema import RepairPlan
from services.stage_artifacts import save_repair_edit_round_artifact


EDIT_RETRYABLE_FAILURES = frozenset({"compile_error", "test_failure", "reproduce_failure"})


def resolve_max_repair_edit_rounds(problem: Optional[Dict[str, Any]]) -> int:
    value = problem if isinstance(problem, dict) else {}
    if value.get("enable_repair_edit_loop") is False:
        return 0
    try:
        configured = int(value.get("max_repair_edit_rounds") or 2)
    except (TypeError, ValueError):
        configured = 2
    return max(0, min(configured, 3))


def classify_verification_failure(
    verification_result: Dict[str, Any],
    *,
    action: str = "verify",
) -> str:
    tool = str(
        verification_result.get("tool")
        or verification_result.get("provider")
        or action
    ).strip()
    normalized = normalize_action_result(verification_result, action=tool)
    return str(normalized.get("failure_class") or "schema_error")


def should_run_repair_edit_loop(
    failure_class: str,
    round_index: int,
    max_rounds: int,
) -> bool:
    return failure_class in EDIT_RETRYABLE_FAILURES and round_index < max_rounds


def build_edit_feedback_overlay(
    *,
    verification_result: Dict[str, Any],
    applied_fix_result: Optional[Dict[str, Any]] = None,
    failure_class: str = "",
) -> str:
    lines: List[str] = ["## 修复修订任务（验证失败反馈）"]
    lines.append(
        "上一版 patch 未通过验证。请仅输出修订后的 repair plan JSON（`summary` + `edits[]`），"
        "不要重复完整分析报告。"
    )
    if failure_class:
        lines.append(f"- 失败类型: `{failure_class}`")
    error = str(verification_result.get("error") or "").strip()
    if error:
        lines.append(f"- 错误摘要: {error[:2000]}")
    for key in ("stderr", "stdout", "output"):
        blob = str(verification_result.get(key) or "").strip()
        if blob:
            lines.append(f"- {key}:")
            lines.append("```text")
            lines.append(blob[:6000])
            lines.append("```")
    recovery = verification_result.get("recovery")
    if isinstance(recovery, dict) and recovery.get("reason"):
        lines.append(f"- 建议: {recovery.get('reason')}")
    fix_plan = (applied_fix_result or {}).get("fix_plan")
    if isinstance(fix_plan, dict):
        lines.append("- 上一版 repair plan:")
        lines.append("```json")
        lines.append(json.dumps(fix_plan, ensure_ascii=False, indent=2)[:8000])
        lines.append("```")
    edit_feedback = (applied_fix_result or {}).get("edit_feedback") or []
    if isinstance(edit_feedback, list) and edit_feedback:
        lines.append("- apply 匹配反馈:")
        lines.append("```json")
        lines.append(json.dumps(edit_feedback[:8], ensure_ascii=False, indent=2)[:4000])
        lines.append("```")
    lines.append("")
    lines.append("输出格式:")
    lines.append("```json")
    lines.append('{ "summary": "...", "edits": [ { "file": "...", "edit_type": "function_replacement", "function_signature": "...", "replacement_code": "..." } ] }')
    lines.append("```")
    return "\n".join(lines)


def revise_fix_plan_with_llm(
    *,
    llm_adapter: Any,
    overlay: str,
    analysis_text: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if llm_adapter is None:
        return None, "llm adapter unavailable"
    prompt = overlay.rstrip()
    if analysis_text.strip():
        prompt += "\n\n## 原始分析摘要\n" + analysis_text.strip()[:4000]
    try:
        response = llm_adapter.invoke(prompt) if hasattr(llm_adapter, "invoke") else llm_adapter(prompt)
        content = str(getattr(response, "content", response) or "")
    except Exception as exc:
        return None, f"llm revision failed: {exc}"
    payload = extract_json_object(content)
    if not isinstance(payload, dict):
        nested = extract_json_object(analysis_text)
        if isinstance(nested, dict) and isinstance(nested.get("edits"), list):
            payload = nested
        else:
            return None, "llm revision did not return repair plan JSON"
    repair_plan, violations = RepairPlan.from_mapping(payload)
    if repair_plan is None:
        return None, f"repair plan schema invalid: {violations[:2]}"
    return repair_plan.to_dict(), None


@dataclass
class RepairEditRoundResult:
    success: bool = False
    applied_fix_result: Optional[Dict[str, Any]] = None
    fix_plan: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    round_index: int = 0
    failure_class: str = ""
    artifacts: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "success": self.success,
            "round_index": self.round_index,
            "failure_class": self.failure_class,
            "error": self.error,
            "artifacts": dict(self.artifacts),
        }
        if self.fix_plan is not None:
            payload["fix_plan"] = dict(self.fix_plan)
        if self.applied_fix_result is not None:
            payload["applied_fix_result"] = dict(self.applied_fix_result)
        return payload


def run_repair_edit_round(
    *,
    deps: Any,
    round_index: int,
    verification_result: Dict[str, Any],
    failure_class: str,
    report_dir: Optional[Path] = None,
    trace: Any = None,
) -> RepairEditRoundResult:
    from services.code_fixer import CodeFixer, extract_candidate_nodes

    out = RepairEditRoundResult(round_index=round_index, failure_class=failure_class)
    applied = deps.applied_fix_result if isinstance(deps.applied_fix_result, dict) else {}
    result = deps.result if isinstance(deps.result, dict) else {}
    overlay = build_edit_feedback_overlay(
        verification_result=verification_result,
        applied_fix_result=applied,
        failure_class=failure_class,
    )
    revised_plan, plan_error = revise_fix_plan_with_llm(
        llm_adapter=getattr(deps, "llm_adapter", None),
        overlay=overlay,
        analysis_text=str(result.get("analysis") or ""),
    )
    if revised_plan is None:
        out.error = plan_error or "failed to revise repair plan"
        if report_dir is not None:
            out.artifacts["artifact_path"] = str(
                save_repair_edit_round_artifact(report_dir, round_index, out.to_dict())
            )
        return out
    out.fix_plan = revised_plan
    code_context = result.get("code_context") if isinstance(result.get("code_context"), dict) else {}
    candidate_nodes = extract_candidate_nodes(code_context)
    fixer = CodeFixer(llm=getattr(deps, "llm_adapter", None))
    fix_result = fixer.apply_fix_plan(
        revised_plan,
        candidate_nodes,
        list(deps.code_roots or []),
        report_dir=Path(report_dir) if report_dir is not None else deps.report_dir,
        backup_original_sources=bool(getattr(deps, "backup_original_sources", True)),
        code_context=code_context,
        tool_executor=getattr(deps, "tool_executor", None),
    )
    applied_dict = fix_result.to_dict()
    out.applied_fix_result = applied_dict
    out.success = bool(fix_result.success)
    if not out.success:
        out.error = fix_result.error or "revised patch apply failed"
    if report_dir is not None:
        payload = out.to_dict()
        out.artifacts["artifact_path"] = str(
            save_repair_edit_round_artifact(report_dir, round_index, payload)
        )
    if trace is not None:
        trace.emit(
            "repair.edit_round",
            kind="stage",
            name="repair_edit_loop",
            status="completed" if out.success else "failed",
            round_index=round_index,
            failure_class=failure_class,
            error=out.error,
        )
    return out
