"""Bounded analyze continuation driven by executable feedback observations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from services.agent_output_parser import parse_agent_decision
from services.analyze_llm import call_analyze_llm_with_phase
from services.context_loop_contract import build_json_format_reminder
from services.observations import ObservationStore
from services.stage_artifacts import save_feedback_analyze_artifact


def _actionable_observations(observations: Any) -> bool:
    if observations is None:
        return False
    if hasattr(observations, "snapshot"):
        snapshot = observations.snapshot()
        return int(snapshot.get("actionable_count") or 0) > 0
    if isinstance(observations, dict):
        items = observations.get("items")
        if isinstance(items, list):
            return any(bool(item.get("actionable")) for item in items if isinstance(item, dict))
    return False


def should_run_feedback_analyze(
    result: Dict[str, Any],
    *,
    verification_status: Optional[str] = None,
    judge: Optional[Dict[str, Any]] = None,
    problem: Optional[Dict[str, Any]] = None,
    trace: Any = None,
) -> bool:
    if not isinstance(result, dict):
        return False
    if isinstance(problem, dict) and problem.get("enable_feedback_analyze") is False:
        return False
    try:
        limit = int((problem or {}).get("max_feedback_analyze_rounds") or 1)
    except (TypeError, ValueError):
        limit = 1
    limit = max(0, min(limit, 2))
    if limit <= 0:
        return False
    if int(result.get("_feedback_analyze_count") or 0) >= limit:
        return False
    if trace is not None and getattr(getattr(trace, "budget", None), "max_llm_calls", 0) > 0:
        if trace.budget.llm_calls >= trace.budget.max_llm_calls:
            return False
    status = str(
        verification_status
        or (result.get("verification") or {}).get("status")
        or ""
    ).strip().lower()
    if status in {"failed", "timeout", "error"}:
        return True
    judge_payload = judge if isinstance(judge, dict) else result.get("judge")
    if isinstance(judge_payload, dict) and judge_payload.get("verdict") == "reject":
        if judge_payload.get("questions"):
            return True
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        if _actionable_observations(metadata.get("observations")):
            return True
    return False


def resolve_feedback_mode(
    result: Dict[str, Any],
    *,
    problem: Optional[Dict[str, Any]] = None,
) -> str:
    explicit = str((result or {}).get("_feedback_mode") or "").strip().lower()
    if explicit in {"diagnosis_feedback", "edit_feedback"}:
        return explicit
    if isinstance(problem, dict):
        configured = str(problem.get("feedback_mode") or "").strip().lower()
        if configured in {"diagnosis_feedback", "edit_feedback"}:
            return configured
    verification = (result or {}).get("verification") if isinstance((result or {}).get("verification"), dict) else {}
    failure_class = str(verification.get("failure_class") or "").strip().lower()
    if failure_class in {"test_failure", "reproduce_failure"}:
        return "diagnosis_feedback"
    if failure_class == "compile_error":
        return "edit_feedback"
    return "diagnosis_feedback"


def build_feedback_prompt_overlay(
    result: Dict[str, Any],
    *,
    observations: Any = None,
    judge: Optional[Dict[str, Any]] = None,
    feedback_mode: str = "diagnosis_feedback",
) -> str:
    lines: List[str] = ["## 可执行反馈续分析"]
    if feedback_mode == "diagnosis_feedback":
        lines.append(
            "验证或 Judge 表明当前结论证据不足。请基于下列运行观察修订分析；"
            "如需继续探索，可输出 `agent_can_fetch_more=true` 与 `context_requests[]`。"
        )
    else:
        lines.append(
            "验证失败源于 patch/编译问题。请基于下列运行观察修订分析；"
            "本轮必须输出 `agent_can_fetch_more=false`，且不得再请求新的 context_requests。"
        )
    obs_text = ""
    if observations is not None and hasattr(observations, "markdown"):
        obs_text = observations.markdown(max_chars=4000)
    elif isinstance(observations, dict):
        store = ObservationStore()
        for item in observations.get("items") or []:
            if isinstance(item, dict):
                store.record(
                    kind=str(item.get("kind") or "runtime_event"),
                    source=str(item.get("source") or "runtime"),
                    status=str(item.get("status") or "unknown"),
                    summary=str(item.get("summary") or ""),
                    actionable=bool(item.get("actionable")),
                )
        obs_text = store.markdown(max_chars=4000)
    if obs_text:
        lines.append(obs_text)
    judge_payload = judge if isinstance(judge, dict) else {}
    questions = judge_payload.get("questions") if isinstance(judge_payload.get("questions"), list) else []
    if questions:
        lines.append("## Judge 追问")
        for question in questions:
            lines.append(f"- {question}")
    prior = str(result.get("analysis") or "").strip()
    if prior:
        lines.append("## 上一轮分析摘要")
        lines.append(prior[:4000])
    return "\n\n".join(lines)


def run_feedback_analyze(
    *,
    context: Any,
    result: Dict[str, Any],
    problem: Optional[Dict[str, Any]],
    trace: Any = None,
    runtime_state: Any = None,
    report_dir: Optional[str] = None,
    judge: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Single bounded LLM turn that revises analysis using runtime observations."""
    base_prompt = str(result.get("final_prompt") or result.get("final_tip") or "").strip()
    if not base_prompt:
        base_prompt = str(result.get("analysis") or "")
    if not base_prompt:
        return None
    observations = getattr(context, "observations", None)
    feedback_mode = resolve_feedback_mode(result, problem=problem)
    overlay = build_feedback_prompt_overlay(
        result,
        observations=observations,
        judge=judge,
        feedback_mode=feedback_mode,
    )
    prompt = overlay + "\n\n" + base_prompt.rstrip()
    prompt = prompt.rstrip() + "\n\n" + build_json_format_reminder(is_final_round=True)
    response, prompt_used = call_analyze_llm_with_phase(
        context,
        prompt,
        problem,
        step=5,
        total_steps=5,
        round_index=0,
    )
    analysis_text = str(getattr(response, "content", response) or "")
    parsed = parse_agent_decision(analysis_text)
    structured = parsed.get("raw_payload") if isinstance(parsed.get("raw_payload"), dict) else None
    payload = {
        "analysis": analysis_text,
        "prompt_used": prompt_used,
        "structured_analysis": structured,
        "termination_reason": "feedback_analyze_final",
        "feedback_mode": feedback_mode,
        "overlay_chars": len(overlay),
    }
    if report_dir:
        save_feedback_analyze_artifact(Path(report_dir), payload)
    if runtime_state is not None and hasattr(runtime_state, "checkpoint"):
        artifact_path = str(Path(report_dir) / "artifacts" / "feedback_analyze_0.json") if report_dir else None
        runtime_state.checkpoint(
            state={"schema_version": 1, "kind": "feedback_analyze", "termination_reason": "feedback_analyze_final"},
            status="running",
            idempotency_key="analyze:feedback:0",
            output_artifact=artifact_path,
        )
    if trace is not None:
        trace.emit("analyze.feedback", kind="stage", name="feedback_analyze", status="completed")
    metadata = result.setdefault("metadata", {})
    if isinstance(metadata, dict) and structured:
        metadata["structured_analysis"] = structured
    result["analysis"] = analysis_text
    result["final_prompt"] = prompt_used
    result["_feedback_analyze_count"] = int(result.get("_feedback_analyze_count") or 0) + 1
    return payload
