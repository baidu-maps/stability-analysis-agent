"""Agent context-loop orchestration shared by crash analysis workflows."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from services.context_engine import ContextEngine, ContextTurn
from services.toctou_write_completeness import (
    build_write_path_revision_feedback,
    should_revise_for_incomplete_write_fixes,
)


@dataclass
class ContextLoopConfig:
    max_rounds: int = 1
    max_requests: int = 5
    agent_loop_mode: str = "single"


@dataclass
class ContextLoopResult:
    analysis_text: str
    prompt_used: str
    rounds: List[Dict[str, Any]] = field(default_factory=list)
    last_response: Any = None
    structured_analysis: Optional[Dict[str, Any]] = None
    context_session: Optional[Dict[str, Any]] = None
    termination_reason: Optional[str] = None
    repo_map: Optional[Dict[str, Any]] = None


class ContextLoopHooks(Protocol):
    def call_llm(self, prompt: str, *, round_index: int) -> tuple[Any, str]: ...

    def on_evidence(self, resolved_context: List[Dict[str, Any]], *, round_index: int) -> None: ...

    def around_resolve(self, round_index: int) -> Any: ...

    def on_round_complete(self, round_index: int, round_payload: Dict[str, Any]) -> None: ...


def _noop_round_complete(_round_index: int, _payload: Dict[str, Any]) -> None:
    return None


@dataclass
class CallableContextLoopHooks:
    call_llm: Callable[..., tuple[Any, str]]
    on_evidence: Callable[[List[Dict[str, Any]], int], None]
    around_resolve: Callable[[int], Any]
    on_round_complete: Callable[[int, Dict[str, Any]], None] = field(default=_noop_round_complete)

    def __call__(self) -> ContextLoopHooks:
        return self


def run_agent_context_loop(
    *,
    config: ContextLoopConfig,
    hooks: ContextLoopHooks,
    initial_prompt: str,
    trace: Any = None,
    context_engine: ContextEngine,
) -> ContextLoopResult:
    """Run the bounded analyze loop; ContextEngine owns all per-turn context state."""
    context_engine.session.mode = config.agent_loop_mode

    max_rounds = max(1, int(config.max_rounds or 1))
    stable_prompt = str(initial_prompt or "")
    next_prompt = (
        context_engine.build_prompt(stable_prompt)
        if config.agent_loop_mode == "context_loop"
        else stable_prompt
    )
    force_final_reason: Optional[str] = None
    previous_analysis = ""
    last_prompt_used = stable_prompt
    last_response: Any = None
    write_path_revision_used = False

    def _try_write_path_revision(round_index: int, analysis_text: str, turn_obj: ContextTurn) -> bool:
        """写路径保活已在 prompt 且分析认定竞态，但缺写侧完整补丁时，强制续一轮。"""
        nonlocal next_prompt, write_path_revision_used, force_final_reason
        if write_path_revision_used or config.agent_loop_mode != "context_loop":
            return False
        if round_index >= max_rounds - 1:
            return False
        missing = should_revise_for_incomplete_write_fixes(
            prompt=str(last_prompt_used or stable_prompt),
            analysis=analysis_text,
        )
        if not missing:
            return False
        write_path_revision_used = True
        context_engine.add_turn(turn_obj)
        hooks.on_round_complete(round_index, turn_obj.to_dict())
        feedback = build_write_path_revision_feedback(missing)
        evidence_delta = [
            {
                "kind": "write_path_revision",
                "content": feedback,
                "source": "toctou_write_completeness",
                "round": round_index + 1,
            }
        ]
        # 下一轮仍允许补上下文，但优先要求补齐写路径修复代码
        force_final_reason = None
        next_prompt = context_engine.build_prompt(
            stable_prompt,
            evidence_delta=evidence_delta,
            is_final_round=(round_index + 1 >= max_rounds - 1),
            early_final_reason=("max_rounds" if round_index + 1 >= max_rounds - 1 else None),
        )
        if trace is not None:
            trace.emit(
                "agent.write_path_revision",
                kind="agent",
                name="context_loop",
                status="continue",
                round_index=round_index,
                missing_count=len(missing),
                missing_signatures=list(missing)[:8],
            )
        return True

    for round_index in range(max_rounds):
        if trace is not None:
            trace.stage = "analyze"
            trace.emit(
                "analyze.round",
                kind="stage",
                name="analyze",
                status="running",
                round_index=round_index,
            )

        is_last_slot = round_index >= max_rounds - 1
        if config.agent_loop_mode == "context_loop" and is_last_slot and force_final_reason is None:
            force_final_reason = "max_rounds"
            next_prompt = context_engine.build_prompt(
                stable_prompt,
                is_final_round=True,
                early_final_reason="max_rounds",
            )
        try:
            response, prompt_used = hooks.call_llm(next_prompt, round_index=round_index)
        except Exception as exc:
            reason = "llm_budget_exhausted" if "budget exceeded" in str(exc).lower() else "llm_error"
            turn = ContextTurn(
                round_index=round_index,
                kind="error",
                prompt=next_prompt,
                decision={"error": str(exc)},
                termination_reason=reason,
            )
            context_engine.add_turn(turn)
            context_engine.finish(reason, degraded=True)
            hooks.on_round_complete(round_index, turn.to_dict())
            if trace is not None:
                trace.emit(
                    "agent.context_loop_terminated",
                    kind="agent",
                    name="context_loop",
                    status="failed",
                    round_index=round_index,
                    termination_reason=reason,
                    error=str(exc),
                )
            raise

        previous_analysis = str(getattr(response, "content", "") or "")
        last_response = response
        last_prompt_used = prompt_used
        parsed = context_engine.parse_decision(previous_analysis)
        requests = list(parsed.get("context_requests") or [])
        invalid_requests = list(parsed.get("invalid_context_requests") or [])
        context_engine.record_invalid_requests(invalid_requests, round_index=round_index)
        turn = ContextTurn(
            round_index=round_index,
            kind="final" if force_final_reason else ("initial" if round_index == 0 else "followup"),
            prompt=prompt_used,
            analysis=previous_analysis,
            decision={
                "agent_can_fetch_more": bool(parsed.get("agent_can_fetch_more")),
                "requested_more": bool(parsed.get("requested_more")),
                "has_control_contract": bool(parsed.get("has_control_contract")),
            },
            context_requests=requests,
            invalid_context_requests=invalid_requests,
            hypotheses=list(parsed.get("hypotheses") or []),
            next_action=dict(parsed.get("next_action") or {}),
        )
        context_engine.update_investigation(parsed, round_index=round_index)

        if trace is not None:
            trace.emit(
                "agent.context_requests_parsed",
                kind="agent",
                name="context_loop",
                round_index=round_index,
                request_count=len(requests),
                invalid_count=len(invalid_requests),
            )
            if parsed.get("degraded") or invalid_requests:
                trace.emit(
                    "agent.schema_degraded",
                    kind="agent",
                    name="context_loop",
                    status="degraded",
                    round_index=round_index,
                    invalid_count=len(invalid_requests),
                    termination_reason="invalid_schema",
                )

        if force_final_reason is not None:
            if _try_write_path_revision(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            finish_reason = force_final_reason
            degraded = force_final_reason == "invalid_schema"
            # After a schema miss, a coherent final analysis should still be treated as
            # model_final so repair_pipeline is not blocked solely by invalid_schema.
            if (
                force_final_reason == "invalid_schema"
                and bool(parsed.get("has_control_contract"))
                and not bool(parsed.get("requested_more"))
                and str(previous_analysis or "").strip()
            ):
                finish_reason = "model_final"
                degraded = True
            context_engine.finish(finish_reason, degraded=degraded)
            hooks.on_round_complete(round_index, turn.to_dict())
            break

        next_action = dict(parsed.get("next_action") or {})
        next_kind = str(next_action.get("kind") or "").strip().lower()
        if config.agent_loop_mode == "context_loop" and next_kind == "propose_fix" and not is_last_slot:
            if _try_write_path_revision(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            context_engine.finish("ready_to_fix")
            hooks.on_round_complete(round_index, turn.to_dict())
            break
        if config.agent_loop_mode == "context_loop" and next_kind in {"final", "insufficient_evidence"}:
            if next_kind == "final" and _try_write_path_revision(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            termination = "insufficient_evidence" if next_kind == "insufficient_evidence" else "model_final"
            context_engine.finish(termination, degraded=bool(parsed.get("degraded")))
            hooks.on_round_complete(round_index, turn.to_dict())
            break

        if config.agent_loop_mode != "context_loop":
            context_engine.add_turn(turn)
            context_engine.finish("model_final")
            hooks.on_round_complete(round_index, turn.to_dict())
            break

        requested_more = bool(parsed.get("requested_more"))
        if parsed.get("degraded") and not parsed.get("has_control_contract") and not requests:
            # 控制契约解析失败时仍优先补齐写侧，避免 TOCTOU 只交读侧就强制终局。
            if _try_write_path_revision(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            hooks.on_round_complete(round_index, turn.to_dict())
            if round_index + 1 >= max_rounds:
                context_engine.finish("invalid_schema", degraded=True)
                break
            force_final_reason = "invalid_schema"
            next_prompt = context_engine.build_prompt(
                stable_prompt,
                is_final_round=True,
                early_final_reason="invalid_schema",
            )
            continue
        if not requested_more:
            if _try_write_path_revision(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            context_engine.finish("model_final", degraded=bool(parsed.get("degraded")))
            hooks.on_round_complete(round_index, turn.to_dict())
            break

        if not requests:
            if _try_write_path_revision(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            hooks.on_round_complete(round_index, turn.to_dict())
            if round_index + 1 >= max_rounds:
                context_engine.finish("invalid_schema", degraded=True)
                break
            force_final_reason = "invalid_schema"
            next_prompt = context_engine.build_prompt(
                stable_prompt,
                is_final_round=True,
                early_final_reason="invalid_schema",
            )
            continue

        with hooks.around_resolve(round_index + 1):
            resolved_context = context_engine.resolve_requests(requests, round_index=round_index)
        turn.resolved_context = resolved_context
        context_engine.update_investigation(parsed, round_index=round_index, resolved=resolved_context)

        if trace is not None:
            trace.emit(
                "agent.context_resolved",
                kind="agent",
                name="context_loop",
                round_index=round_index,
                requested_count=len(requests),
                success_count=sum(1 for x in resolved_context if isinstance(x, dict) and x.get("success")),
                failed_count=sum(1 for x in resolved_context if isinstance(x, dict) and not x.get("success")),
            )
        hooks.on_evidence(resolved_context, round_index + 1)
        evidence_delta = context_engine.evidence_delta(resolved_context, round_index=round_index + 1)
        turn.evidence_delta = evidence_delta
        pre_round_add_res = context_engine.build_pre_round_add_res(
            source_round=round_index,
            target_round=round_index + 1,
            resolved_context=resolved_context,
        )
        turn.pre_round_add_res = pre_round_add_res
        has_success = any(bool(x.get("success")) for x in resolved_context if isinstance(x, dict))
        all_blocked = not has_success and context_engine.all_requests_blocked(resolved_context)
        context_engine.add_turn(turn)
        hooks.on_round_complete(round_index, turn.to_dict())
        if all_blocked:
            force_final_reason = "all_requests_blocked"
        elif round_index + 1 >= max_rounds - 1:
            force_final_reason = "max_rounds"
        next_prompt = context_engine.build_prompt(
            stable_prompt,
            evidence_delta=evidence_delta,
            is_final_round=force_final_reason is not None,
            early_final_reason=force_final_reason,
        )

    if context_engine.session.status == "running":
        context_engine.finish("max_rounds")

    rounds = [turn.to_dict() for turn in context_engine.session.turns]
    from services.agent_output_parser import parse_analysis_report

    structured: Optional[Dict[str, Any]] = None
    report, report_error = parse_analysis_report(previous_analysis)
    if report is not None:
        structured = report.to_dict()
    elif report_error and trace is not None:
        trace.emit(
            "schema_violation",
            kind="agent",
            name="analysis_report",
            status="degraded",
            error=report_error,
        )
    return ContextLoopResult(
        previous_analysis,
        last_prompt_used,
        rounds,
        last_response,
        structured,
        context_engine.session.to_dict(),
        context_engine.session.termination_reason,
        context_engine.session.repo_map,
    )
