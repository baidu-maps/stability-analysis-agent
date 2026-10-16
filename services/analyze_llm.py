"""Analyze-stage LLM invocation, routing, retry, and prompt-size handling."""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from cli.phase_spinner import PhaseSpinner


logger = logging.getLogger(__name__)


# 七段报告加修复代码大约 6k～8k token。配置里的 4096 会判成截断，
# 旧逻辑再降到 2048 后第三次才放行，同一份提示被完整生成三遍。
ANALYSIS_OUTPUT_TOKEN_FLOOR = 12288
ANALYSIS_OUTPUT_TOKEN_RAISE = 16384
_previous_prompt_chars: Optional[int] = None


def format_llm_send_line(
    round_index: int,
    char_count: int,
    previous_chars: Optional[int],
) -> str:
    """发送前的容量行。第一轮不带环比，之后带相对上一轮的百分比。"""
    chars = max(0, int(char_count or 0))
    approx_tokens = chars // 4
    line = (
        f"[llm] 第{int(round_index) + 1}轮发送 {chars:,} 字"
        f"（约 {approx_tokens:,} token）"
    )
    if previous_chars is not None and int(previous_chars) > 0:
        delta = int(round((chars - int(previous_chars)) * 100 / int(previous_chars)))
        sign = "+" if delta > 0 else ""
        line += f"，较上一轮 {sign}{delta}%"
    return line


def announce_llm_send(round_index: int, prompt: str) -> str:
    """在真正请求模型之前打一行容量。同一份提示的重试不再打。"""
    global _previous_prompt_chars
    line = format_llm_send_line(round_index, len(prompt or ""), _previous_prompt_chars)
    _previous_prompt_chars = len(prompt or "")
    print(line, file=sys.stderr, flush=True)
    return line


def reset_llm_send_baseline() -> None:
    """清空上一轮字数，供新任务和单测隔离。"""
    global _previous_prompt_chars
    _previous_prompt_chars = None


def analysis_token_attempts(configured_max_tokens: int) -> List[Optional[int]]:
    """分析阶段的输出预算只升不降。"""
    configured = max(0, int(configured_max_tokens or 0))
    first = max(ANALYSIS_OUTPUT_TOKEN_FLOOR, configured) if configured else ANALYSIS_OUTPUT_TOKEN_FLOOR
    attempts: List[Optional[int]] = [first]
    if ANALYSIS_OUTPUT_TOKEN_RAISE > first:
        attempts.append(ANALYSIS_OUTPUT_TOKEN_RAISE)
    attempts.append(None)
    return attempts


def output_looks_truncated(completion_tokens: int, max_tokens: Optional[int]) -> bool:
    """实际输出超过请求上限时，说明网关没有执行上限，这次已经是完整回答。"""
    if max_tokens is None or int(max_tokens) <= 0:
        return False
    produced = int(completion_tokens or 0)
    requested = int(max_tokens)
    if produced > requested:
        return False
    return produced >= requested * 0.95


def resolve_analysis_prompt_cap(problem: Optional[Dict[str, Any]]) -> Optional[int]:
    """只在显式配置为正数时裁剪提示。未配置时不再做二次裁剪。"""
    raw = problem.get("max_prompt_chars") if isinstance(problem, dict) else None
    if raw is None:
        raw = os.getenv("SA_MAX_PROMPT_CHARS")
    if raw in (None, ""):
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    if parsed > 0:
        return parsed
    return None


def _prompt_section_priority(title: str) -> int:
    if any(key in title for key in ("输出要求", "必须遵守", "崩溃分析任务")):
        return 100
    if any(key in title for key in ("崩溃证据", "已确认事实", "Abort message", "崩溃摘要")):
        return 95
    if any(key in title for key in ("崩溃函数", "函数源码")):
        return 90
    if "调用链" in title:
        return 45
    if any(key in title for key in ("变量", "兄弟", "共享")):
        return 15
    return 35


def _pack_prompt_sections_by_priority(prompt: str, prompt_cap: int) -> str:
    parts = [part for part in re.split(r"(?=^## )", prompt, flags=re.M) if part]
    if len(parts) < 3:
        return ""
    last_idx = len(parts) - 1
    required: List[Tuple[int, str]] = []
    optional: List[Tuple[int, int, str]] = []
    for index, part in enumerate(parts):
        first_line = part.splitlines()[0] if part.splitlines() else ""
        priority = _prompt_section_priority(first_line)
        if index in (0, last_idx) or priority >= 90:
            required.append((index, part))
        else:
            optional.append((index, priority, part))
    chosen = list(required)
    used = sum(len(part) for _index, part in chosen)
    optional.sort(key=lambda item: (-item[1], item[0]))
    dropped = 0
    for index, _priority, part in optional:
        if used + len(part) <= prompt_cap:
            chosen.append((index, part))
            used += len(part)
        else:
            dropped += 1
    if dropped:
        chosen.append((last_idx + 1, "\n\n...[PROMPT TRUNCATED]...\n\n"))
    chosen.sort(key=lambda item: item[0])
    return "".join(part for _index, part in chosen)


def truncate_analysis_prompt(prompt: str, prompt_cap: int) -> str:
    if len(prompt) <= prompt_cap:
        return prompt
    packed = _pack_prompt_sections_by_priority(prompt, prompt_cap)
    if packed:
        return packed
    logger.warning(
        "prompt section packing failed; keep original prompt (%s chars, cap=%s)",
        len(prompt),
        prompt_cap,
    )
    return prompt


def _apply_router_endpoint(context: Any, endpoint: Any) -> None:
    if endpoint is None:
        return
    try:
        from tool_system.llm.llm_adapter import LLMAdapterFactory
        from tool_system.llm.llm_router import build_llm_config_for_endpoint

        engine = str(getattr(getattr(context, "trace", None), "engine", "") or "direct")
        config = build_llm_config_for_endpoint(endpoint, engine=engine)
        context.llm = LLMAdapterFactory.create(config.to_dict())
    except Exception as exc:
        logger.warning("Failed to switch LLM adapter for failover: %s", exc)


def _prepare_router_for_round(
    context: Any,
    problem: Optional[Dict[str, Any]],
    *,
    round_index: int,
) -> None:
    if not isinstance(problem, dict):
        return
    state = problem.get("_llm_router_state")
    if state is None or getattr(state, "mode", "fixed") == "fixed":
        return
    try:
        from tool_system.llm.llm_router import re_resolve_tier
        from tool_system.llm.routing_policy import RoutingContext

        diagnosis = problem.get("_crash_diagnosis")
        if not isinstance(diagnosis, dict):
            diagnosis = None
        routing_context = RoutingContext(
            mode=str(getattr(state, "mode", "auto")),
            force_profile=getattr(state, "force_profile", None),
            prompt_mode=str(problem.get("prompt_mode") or "fix"),
            apply_ai_fixes=bool(problem.get("apply_ai_fixes")),
            agent_loop=str(problem.get("agent_loop") or "single"),
            round_index=int(round_index),
            crash_diagnosis=diagnosis,
        )
        selected = re_resolve_tier(state, routing_context)
        if selected is not None:
            _apply_router_endpoint(context, selected)
            logger.info(
                "LLM router round=%s tier=%s provider=%s model=%s reason=%s",
                round_index,
                getattr(state, "requested_tier", None),
                selected.provider_key,
                selected.model,
                getattr(state, "reason", None),
            )
    except Exception as exc:
        logger.debug("LLM re_resolve skipped: %s", exc)


def call_analyze_llm_with_retries(
    context: Any,
    prompt: str,
    problem: Optional[Dict[str, Any]],
    *,
    round_index: int = 0,
    stage: str = "analysis",
) -> Tuple[Any, str]:
    prompt_cap = resolve_analysis_prompt_cap(problem)
    prompt_used = prompt
    if prompt_cap and len(prompt_used) > prompt_cap:
        original_length = len(prompt_used)
        prompt_used = truncate_analysis_prompt(prompt_used, prompt_cap)
        logger.warning(
            "analysis_prompt too long (%s chars), smart-truncated to %s chars (max_prompt_chars=%s)",
            original_length,
            len(prompt_used),
            prompt_cap,
        )
    announce_llm_send(round_index, prompt_used)

    _prepare_router_for_round(context, problem, round_index=round_index)
    llm_adapter = getattr(context, "llm", None)
    try:
        configured_max_tokens = int(
            (getattr(llm_adapter, "max_tokens", 0) if llm_adapter is not None else 0) or 0
        )
    except Exception:
        configured_max_tokens = 0
    token_attempts = analysis_token_attempts(configured_max_tokens)

    router_state = problem.get("_llm_router_state") if isinstance(problem, dict) else None
    max_endpoint_attempts = 1
    if router_state is not None and getattr(router_state, "failover_enabled", False):
        max_endpoint_attempts = max(1, len(getattr(router_state, "pool", []) or []) or 1)

    llm_response = None
    last_llm_exc: Optional[Exception] = None
    for _endpoint_attempt in range(max_endpoint_attempts):
        llm_response = None
        last_llm_exc = None
        call_started = time.perf_counter()
        for attempt_index, max_tokens in enumerate(token_attempts, start=1):
            try:
                if max_tokens is None:
                    logger.info("LLM attempt %s: default max_tokens", attempt_index)
                    llm_response = context.call_llm(prompt_used, temperature=0)
                else:
                    logger.info("LLM attempt %s: max_tokens=%s", attempt_index, max_tokens)
                    llm_response = context.call_llm(
                        prompt_used,
                        max_tokens=max_tokens,
                        temperature=0,
                    )
                if max_tokens is not None and llm_response is not None:
                    usage = getattr(llm_response, "usage", None) or {}
                    completion_tokens = usage.get("completion_tokens", 0) or 0
                    if output_looks_truncated(completion_tokens, max_tokens):
                        logger.warning(
                            "LLM output likely truncated (completion_tokens=%s, max_tokens=%s), retrying",
                            completion_tokens,
                            max_tokens,
                        )
                        continue
                break
            except Exception as exc:
                last_llm_exc = exc
                logger.warning(
                    "LLM attempt %s failed (max_tokens=%s): %s",
                    attempt_index,
                    max_tokens if max_tokens is not None else "default",
                    exc,
                )
        duration_ms = int(round((time.perf_counter() - call_started) * 1000))
        if llm_response is not None:
            if router_state is not None:
                try:
                    from tool_system.llm.llm_router import record_call

                    record_call(
                        router_state,
                        stage=stage,
                        round_index=round_index,
                        status="success",
                        duration_ms=duration_ms,
                    )
                except Exception:
                    pass
            break
        if router_state is not None:
            try:
                from tool_system.llm.llm_router import failover_next, record_call

                record_call(
                    router_state,
                    stage=stage,
                    round_index=round_index,
                    status="error",
                    duration_ms=duration_ms,
                    error=str(last_llm_exc or "unknown"),
                )
                next_endpoint = failover_next(
                    router_state,
                    cause=str(last_llm_exc or "llm_call_failed"),
                )
                if next_endpoint is None:
                    break
                _apply_router_endpoint(context, next_endpoint)
                logger.warning(
                    "LLM failover to provider=%s model=%s",
                    next_endpoint.provider_key,
                    next_endpoint.model,
                )
                continue
            except Exception as exc:
                logger.debug("LLM failover skipped: %s", exc)
                break
        break
    if llm_response is None:
        raise RuntimeError(f"LLM call failed after retries: {last_llm_exc}")
    return llm_response, prompt_used


def call_analyze_llm_with_phase(
    context: Any,
    prompt: str,
    problem: Optional[Dict[str, Any]],
    *,
    step: int,
    total_steps: int,
    round_index: int,
) -> Tuple[Any, str]:
    labels = ("第一轮", "第二轮", "第三轮", "第四轮", "第五轮", "第六轮", "第七轮", "第八轮")
    round_label = labels[round_index] if 0 <= round_index < len(labels) else f"第{round_index + 1}轮"
    prompt = context.select_prompt(prompt)
    with PhaseSpinner(f"{round_label}：AI推理分析", step=step, total_steps=total_steps) as spinner:
        response, prompt_used = call_analyze_llm_with_retries(
            context,
            prompt,
            problem,
            round_index=round_index,
            stage="analysis" if round_index == 0 else "context_followup",
        )
        usage = getattr(response, "usage", None) or {}
        if isinstance(usage, dict):
            spinner.set_tokens(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
    return response, prompt_used
