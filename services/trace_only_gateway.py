"""Trace helpers for operations that are not routed through ToolExecutionGateway."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


def emit_traced_operation(
    trace: Any,
    name: str,
    fn: Callable[[], T],
    *,
    kind: str = "tool",
    input_hash: Optional[str] = None,
) -> T:
    """Run ``fn`` and emit harness-style tool success/failed events when trace is set."""
    started = time.perf_counter()
    if trace is not None:
        trace.budget.consume("tool")
    try:
        result = fn()
    except Exception as exc:
        if trace is not None:
            trace.emit(
                "tool.failed",
                kind=kind,
                name=name,
                status="failed",
                duration_ms=int(round((time.perf_counter() - started) * 1000)),
                error=str(exc),
                input_hash=input_hash,
            )
        raise
    if trace is not None:
        trace.emit(
            "tool.success",
            kind=kind,
            name=name,
            status="success",
            duration_ms=int(round((time.perf_counter() - started) * 1000)),
            input_hash=input_hash,
        )
    return result


def execute_tool_via_gateway(
    gateway: Any,
    name: str,
    tool: Any,
    payload: Dict[str, Any],
    *,
    trace: Any = None,
    fallback: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if gateway is not None:
        return gateway.execute(name, tool, dict(payload or {}))
    if fallback is not None:
        return emit_traced_operation(trace, name, fallback)
    if hasattr(tool, "execute_with_validation"):
        return emit_traced_operation(
            trace,
            name,
            lambda: tool.execute_with_validation(payload),
        )
    return emit_traced_operation(trace, name, lambda: tool.execute(payload))
