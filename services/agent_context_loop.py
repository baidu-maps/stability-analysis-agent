"""Agent context-loop orchestration shared by crash analysis workflows."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from services.context_engine import ContextEngine, ContextTurn
from services.fix_format_completeness import should_revise_for_fix_format
from services.toctou_write_completeness import (
    build_write_path_revision_feedback,
    should_revise_for_incomplete_write_fixes,
)

_SOURCE_FENCE_RE = re.compile(
    r"```(?:cpp|c\+\+|objc|objcpp|objective-c|cc|mm|c|h|hpp|java|kt|kotlin|swift|js|ts)\b",
    re.IGNORECASE,
)
_JSON_FIX_EDITS_RE = re.compile(
    r"```json[\s\S]*?\"fix_edits\"[\s\S]*?```", re.IGNORECASE
)


def _analysis_has_applyable_fix(analysis_text: str) -> bool:
    """判断分析里是否已给出可落盘补丁（权威 JSON 或源码围栏）。"""
    text = str(analysis_text or "")
    if _JSON_FIX_EDITS_RE.search(text):
        return True
    return bool(_SOURCE_FENCE_RE.search(text))


def _fix_recovery_needed(analysis_text: str, turn_obj: ContextTurn) -> bool:
    """模型声称要改码（propose_fix 或 修复代码）却没有可落盘补丁时为真。"""
    next_kind = str((getattr(turn_obj, "next_action", None) or {}).get("kind") or "").strip().lower()
    proposed = next_kind == "propose_fix" or ("修复代码" in str(analysis_text or ""))
    if not proposed:
        return False
    return not _analysis_has_applyable_fix(analysis_text)


def _build_fix_recovery_feedback() -> str:
    """要求模型把修复重发为权威 JSON fix_edits 加每函数围栏。"""
    return (
        "本轮必须给出可落盘补丁，`next_action.kind` 设为 `propose_fix`，并同时输出：\n"
        "(a) 一个独立的 ```json 块，形如 "
        "{\"fix_edits\": [{\"file\": \"源文件名\", \"function_signature\": \"完整函数签名\", "
        "\"replacement_code\": \"完整函数定义\"}]}，作为自动改码的权威来源；\n"
        "(b) 每个要改的函数各用一个独立 ```cpp 围栏，围栏前单独一行写 \"### `文件名`\"。\n"
        "函数体必须完整，签名保持与源码一致，禁止 `...`、`// ...`、`省略`、`其他逻辑保持不变` 等占位。"
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
    if config.agent_loop_mode == "context_loop":
        try:
            from services.knowledge_required_context import prefetch_missing_knowledge_functions

            next_prompt = prefetch_missing_knowledge_functions(
                context_engine,
                stable_prompt=stable_prompt,
                assembled_prompt=next_prompt,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).info("knowledge prefetch skipped: %s", exc)
    force_final_reason: Optional[str] = None
    previous_analysis = ""
    last_nonempty_analysis = ""
    last_prompt_used = stable_prompt
    last_response: Any = None
    write_path_revision_used = False
    fix_format_revision_used = False
    knowledge_revision_used = False
    # TASK 3/5/6 共用的「一次额外修复回合」预算：改码模式下分析没给出可落盘补丁时，
    # 至多再逼一轮要权威 JSON fix_edits 与每函数围栏，保证循环终止。
    fix_recovery_revision_used = False

    def _schedule_revision(
        round_index: int,
        turn_obj: ContextTurn,
        *,
        kind: str,
        source: str,
        feedback: str,
        trace_event: str,
        extra_trace: Optional[Dict[str, Any]] = None,
    ) -> None:
        """将反馈写入 evidence_delta 并续一轮。"""
        nonlocal next_prompt, force_final_reason
        context_engine.add_turn(turn_obj)
        hooks.on_round_complete(round_index, turn_obj.to_dict())
        evidence_delta = [
            {
                "kind": kind,
                "content": feedback,
                "source": source,
                "round": round_index + 1,
            }
        ]
        force_final_reason = None
        next_prompt = context_engine.build_prompt(
            stable_prompt,
            evidence_delta=evidence_delta,
            is_final_round=(round_index + 1 >= max_rounds - 1),
            early_final_reason=("max_rounds" if round_index + 1 >= max_rounds - 1 else None),
            prior_analysis=previous_analysis,
        )
        if trace is not None:
            payload = {
                "kind": "agent",
                "name": "context_loop",
                "status": "continue",
                "round_index": round_index,
            }
            if extra_trace:
                payload.update(extra_trace)
            trace.emit(trace_event, **payload)

    def _try_knowledge_function_revision(
        round_index: int,
        analysis_text: str,
        turn_obj: ContextTurn,
    ) -> bool:
        """首轮仍缺领域知识点名的栈函数体时，强制补齐后再终局。"""
        nonlocal knowledge_revision_used, next_prompt, force_final_reason
        del analysis_text
        if knowledge_revision_used or config.agent_loop_mode != "context_loop":
            return False
        if round_index >= max_rounds - 1:
            return False
        from services.knowledge_required_context import (
            build_prefetch_requests,
            inject_resolved_function_sources,
            missing_knowledge_stack_functions,
        )

        missing = missing_knowledge_stack_functions(str(last_prompt_used or next_prompt))
        if not missing:
            return False
        resolved = context_engine.resolve_requests(
            build_prefetch_requests(missing),
            round_index=round_index + 1,
        )
        injected = inject_resolved_function_sources(str(last_prompt_used or next_prompt), resolved)
        if injected == str(last_prompt_used or next_prompt):
            return False
        knowledge_revision_used = True
        context_engine.add_turn(turn_obj)
        hooks.on_round_complete(round_index, turn_obj.to_dict())
        force_final_reason = None
        names = "、".join(missing)
        next_prompt = (
            injected.rstrip()
            + "\n\n## 系统续轮\n"
            + f"已补齐领域知识点名的栈函数：{names}。"
            + "请把这些函数一并写入必改，不要只写进需人工处理。\n"
        )
        if trace is not None:
            trace.emit(
                "agent.knowledge_function_revision",
                kind="agent",
                name="context_loop",
                status="continue",
                round_index=round_index,
                missing_signatures=list(missing)[:8],
            )
        return True

    def _try_write_path_revision(round_index: int, analysis_text: str, turn_obj: ContextTurn) -> bool:
        """写路径保活已在 prompt 且分析认定竞态，但缺写侧完整补丁时，强制续一轮。"""
        nonlocal write_path_revision_used
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
        _schedule_revision(
            round_index,
            turn_obj,
            kind="write_path_revision",
            source="toctou_write_completeness",
            feedback=build_write_path_revision_feedback(missing),
            trace_event="agent.write_path_revision",
            extra_trace={
                "missing_count": len(missing),
                "missing_signatures": list(missing)[:8],
            },
        )
        return True

    def _try_fix_format_revision(round_index: int, analysis_text: str, turn_obj: ContextTurn) -> bool:
        """修复代码含 class 骨架或臆造锁时，强制续一轮纠正格式。"""
        nonlocal fix_format_revision_used
        if fix_format_revision_used or config.agent_loop_mode != "context_loop":
            return False
        if round_index >= max_rounds - 1:
            return False
        feedback = should_revise_for_fix_format(
            prompt=str(last_prompt_used or stable_prompt),
            analysis=analysis_text,
        )
        if not feedback:
            return False
        fix_format_revision_used = True
        _schedule_revision(
            round_index,
            turn_obj,
            kind="fix_format_revision",
            source="fix_format_completeness",
            feedback=feedback,
            trace_event="agent.fix_format_revision",
        )
        return True

    def _try_fix_recovery_revision(round_index: int, analysis_text: str, turn_obj: ContextTurn) -> bool:
        """改码模式终局却无可落盘补丁时，逼一轮要权威 JSON 与每函数围栏。"""
        nonlocal fix_recovery_revision_used
        if fix_recovery_revision_used or config.agent_loop_mode != "context_loop":
            return False
        if not getattr(context_engine, "apply_ai_fixes", False):
            return False
        if round_index >= max_rounds - 1:
            return False
        if not _fix_recovery_needed(analysis_text, turn_obj):
            return False
        fix_recovery_revision_used = True
        _schedule_revision(
            round_index,
            turn_obj,
            kind="fix_recovery_revision",
            source="fix_recovery",
            feedback=_build_fix_recovery_feedback(),
            trace_event="agent.fix_recovery_revision",
        )
        return True

    def _try_post_final_revisions(round_index: int, analysis_text: str, turn_obj: ContextTurn) -> bool:
        """终局前依次尝试知识栈函数、写路径完整性与修复格式续轮。"""
        if _try_knowledge_function_revision(round_index, analysis_text, turn_obj):
            return True
        if _try_write_path_revision(round_index, analysis_text, turn_obj):
            return True
        if _try_fix_format_revision(round_index, analysis_text, turn_obj):
            return True
        return _try_fix_recovery_revision(round_index, analysis_text, turn_obj)

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
                prior_analysis=previous_analysis,
            )
        if getattr(context_engine, "prompt_capacity_exceeded", False):
            turn = ContextTurn(
                round_index=round_index,
                kind="error",
                prompt=next_prompt,
                termination_reason="prompt_capacity_exceeded",
            )
            context_engine.add_turn(turn)
            context_engine.finish("prompt_capacity_exceeded", degraded=True)
            hooks.on_round_complete(round_index, turn.to_dict())
            break
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
        if previous_analysis.strip():
            last_nonempty_analysis = previous_analysis
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
            if _try_post_final_revisions(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            finish_reason = force_final_reason
            degraded = force_final_reason == "invalid_schema"
            if not previous_analysis.strip() and last_nonempty_analysis.strip():
                # 终局轮正文为空（思考耗尽输出预算）时沿用最近一轮分析，修复阶段再补全代码。
                previous_analysis = last_nonempty_analysis
                finish_reason = "model_final"
                degraded = True
                if trace is not None:
                    trace.emit(
                        "agent.empty_final_fallback",
                        kind="agent",
                        name="context_loop",
                        status="degraded",
                        round_index=round_index,
                        fallback_chars=len(previous_analysis),
                    )
            elif (
                force_final_reason == "invalid_schema"
                and not bool(parsed.get("requested_more"))
                and previous_analysis.strip()
            ):
                # 终局正文已写出、只是控制 JSON 缺失或被截断时，不因 invalid_schema 拦住修复。
                finish_reason = "model_final"
                degraded = True
            context_engine.finish(finish_reason, degraded=degraded)
            hooks.on_round_complete(round_index, turn.to_dict())
            break

        next_action = dict(parsed.get("next_action") or {})
        next_kind = str(next_action.get("kind") or "").strip().lower()
        if config.agent_loop_mode == "context_loop" and next_kind == "propose_fix" and not is_last_slot:
            if _try_post_final_revisions(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            context_engine.finish("ready_to_fix")
            hooks.on_round_complete(round_index, turn.to_dict())
            break
        if config.agent_loop_mode == "context_loop" and next_kind in {"final", "insufficient_evidence"}:
            if next_kind == "final" and _try_post_final_revisions(round_index, previous_analysis, turn):
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
            if _try_post_final_revisions(round_index, previous_analysis, turn):
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
                prior_analysis=previous_analysis or last_nonempty_analysis,
            )
            continue
        if not requested_more:
            if _try_post_final_revisions(round_index, previous_analysis, turn):
                continue
            context_engine.add_turn(turn)
            context_engine.finish("model_final", degraded=bool(parsed.get("degraded")))
            hooks.on_round_complete(round_index, turn.to_dict())
            break

        if not requests:
            if _try_post_final_revisions(round_index, previous_analysis, turn):
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
                prior_analysis=previous_analysis or last_nonempty_analysis,
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
        context_engine.add_turn(turn)
        hooks.on_round_complete(round_index, turn.to_dict())
        # 本轮请求的源码一个都没定位到时，下一轮没有新上下文，不再调用模型。
        if not has_success:
            context_engine.finish("all_requests_blocked", degraded=True)
            break
        if round_index + 1 >= max_rounds - 1:
            force_final_reason = "max_rounds"
        next_prompt = context_engine.build_prompt(
            stable_prompt,
            evidence_delta=evidence_delta,
            is_final_round=force_final_reason is not None,
            early_final_reason=force_final_reason,
            prior_analysis=previous_analysis,
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
