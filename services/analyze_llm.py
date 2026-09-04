"""Analyze-stage LLM invocation, routing, retry, and prompt-size handling."""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from cli.phase_spinner import PhaseSpinner


logger = logging.getLogger(__name__)
DEFAULT_ANALYSIS_PROMPT_CHARS = 120000


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
    prompt_cap_raw = problem.get("max_prompt_chars") if isinstance(problem, dict) else None
    if prompt_cap_raw is None:
        prompt_cap_raw = os.getenv("SA_MAX_PROMPT_CHARS")
    prompt_cap: Optional[int] = None
    if prompt_cap_raw in (None, ""):
        prompt_cap = DEFAULT_ANALYSIS_PROMPT_CHARS
    else:
        try:
            parsed = int(prompt_cap_raw)
            if parsed > 0:
                prompt_cap = parsed
            elif parsed == 0:
                prompt_cap = None
        except (TypeError, ValueError):
            prompt_cap = DEFAULT_ANALYSIS_PROMPT_CHARS
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

    _prepare_router_for_round(context, problem, round_index=round_index)
    llm_adapter = getattr(context, "llm", None)
    try:
        configured_max_tokens = int(
            (getattr(llm_adapter, "max_tokens", 0) if llm_adapter is not None else 0) or 0
        )
    except Exception:
        configured_max_tokens = 0
    first_try_tokens = min(8192, configured_max_tokens) if configured_max_tokens > 0 else 8192
    token_attempts: List[Optional[int]] = [first_try_tokens]
    for candidate in (4096, 2048):
        if candidate not in token_attempts and candidate < first_try_tokens:
            token_attempts.append(candidate)
    token_attempts.append(None)

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
                    if completion_tokens >= max_tokens * 0.95:
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
