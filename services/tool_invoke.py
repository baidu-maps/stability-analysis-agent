"""Unified tool invocation: gateway-first, trace-only fallback."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from services.trace_only_gateway import execute_tool_via_gateway

ToolExecutor = Callable[[str, Dict[str, Any]], Dict[str, Any]]


def _resolve_tool(name: str) -> Any:
    from tool_system.registry import ToolAndWorkflowRegistry

    tool = ToolAndWorkflowRegistry().get_tool(name)
    if tool is None:
        raise ValueError(f"tool not found: {name}")
    if isinstance(tool, type):
        tool = tool()
    return tool


def invoke_tool(
    name: str,
    payload: Dict[str, Any],
    *,
    gateway: Any = None,
    trace: Any = None,
    tool_executor: Optional[ToolExecutor] = None,
    fallback_tool: Any = None,
) -> Dict[str, Any]:
    """Invoke a registered tool through the preferred execution path.

    Priority: ``tool_executor`` → ``gateway.execute`` → ``execute_tool_via_gateway``.
    """
    data = dict(payload or {})
    if tool_executor is not None:
        return tool_executor(name, data)
    tool = fallback_tool if fallback_tool is not None else _resolve_tool(name)
    if gateway is not None:
        return gateway.execute(name, tool, data)
    return execute_tool_via_gateway(
        gateway,
        name,
        tool,
        data,
        trace=trace,
    )


def snippet_extractor_executor(
    *,
    context: Any = None,
    gateway: Any = None,
    trace: Any = None,
    tool_executor: Optional[ToolExecutor] = None,
) -> ToolExecutor:
    """Build an executor for ``snippet_extractor`` calls."""

    def _run(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if name != "snippet_extractor":
            return invoke_tool(
                name,
                payload,
                gateway=gateway,
                trace=trace,
                tool_executor=tool_executor,
            )
        if context is not None and hasattr(context, "execute_tool"):
            return context.execute_tool("snippet_extractor", payload)
        gw = gateway
        if gw is None and context is not None:
            gw = getattr(context, "gateway", None)
        tr = trace
        if tr is None and context is not None:
            tr = getattr(context, "trace", None)
        return invoke_tool(
            "snippet_extractor",
            payload,
            gateway=gw,
            trace=tr,
            tool_executor=tool_executor,
        )

    return _run
