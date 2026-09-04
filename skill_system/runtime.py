#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill 运行时：将 Skill 包桥接到现有 Tool/Workflow 执行内核。
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import SkillBundle, SkillExport, SkillRunResult
from .manager import SkillManager


def _load_ref(ref: str) -> Any:
    if not ref:
        raise ValueError("export.ref 不能为空")
    module_name, attr_name = (ref.split(":", 1) + [None])[:2] if ":" in ref else (ref, None)
    module = importlib.import_module(module_name)
    if attr_name:
        return getattr(module, attr_name)
    return module


def _instantiate_export_object(obj: Any, params: Dict[str, Any]) -> Any:
    if inspect.isclass(obj):
        return obj(**params) if params else obj()
    if callable(obj) and not hasattr(obj, "definition"):
        return obj
    return obj


def _priority_enum(registry: Any, raw: str) -> Any:
    priority = getattr(registry, "Priority", None)
    if priority is None:
        try:
            from tool_system import Priority as _Priority

            priority = _Priority
        except Exception:
            priority = None
    if priority is None:
        return raw
    key = str(raw or "EXTENSION").strip().upper()
    return getattr(priority, key, getattr(priority, "EXTENSION"))


def register_skill_exports(bundle: SkillBundle, registry: Any) -> List[str]:
    """
    将 skill 的 exports 注册到注册表。

    支持的导出：
    - module/register_all/regis ter 函数
    - BaseTool / BaseWorkflow 类或实例
    """
    registered: List[str] = []
    for export in bundle.package.exports:
        if not export.enabled:
            continue
        obj = _load_ref(export.ref)
        if hasattr(obj, "register_all") and callable(getattr(obj, "register_all")):
            obj.register_all(registry)
            registered.append(export.name or export.ref)
            continue
        if hasattr(obj, "register") and callable(getattr(obj, "register")) and not hasattr(obj, "definition"):
            obj.register(registry)
            registered.append(export.name or export.ref)
            continue

        instance = _instantiate_export_object(obj, export.params)
        priority = _priority_enum(registry, export.priority)
        if hasattr(instance, "execute") and hasattr(instance, "definition"):
            definition_name = getattr(instance.definition, "name", None)
            target_name = export.name or definition_name or instance.__class__.__name__
            registry.register(
                target_name,
                instance,
                priority=priority,
                force_override=bool(export.force_override),
                is_tool=True,
                module=bundle.command_name,
            )
            registered.append(target_name)
            continue
        if hasattr(instance, "solve") and hasattr(instance, "definition"):
            definition_name = getattr(instance.definition, "name", None)
            target_name = export.name or definition_name or instance.__class__.__name__
            registry.register(
                target_name,
                instance,
                priority=priority,
                force_override=bool(export.force_override),
                is_tool=False,
                module=bundle.command_name,
            )
            registered.append(target_name)
            continue
        raise TypeError(f"无法注册 skill export: {export.ref}")
    return registered


def _extract_problem_from_input(input_data: Any) -> Dict[str, Any]:
    if isinstance(input_data, dict):
        return dict(input_data)
    if input_data is None:
        return {}
    raise TypeError("workflow skill 的输入必须是字典")


def _policy_from_skill_capabilities(capabilities: Dict[str, Any]) -> Any:
    from services.policy import PolicyEngine

    permissions = capabilities.get("permissions") if isinstance(capabilities.get("permissions"), dict) else {}
    allowed_roots = permissions.get("allowed_roots") or []
    if isinstance(allowed_roots, str):
        allowed_roots = [allowed_roots]
    return PolicyEngine(
        allow_network=bool(permissions.get("network")),
        allow_destructive=bool(permissions.get("destructive")),
        allowed_roots=[str(item) for item in allowed_roots if str(item).strip()],
    )


class SkillRuntime:
    """Skill 运行器。"""

    manager: SkillManager

    def render(self, skill_name: str, arguments: str = "", **context: Any) -> SkillRunResult:
        bundle = self.manager.resolve(skill_name)
        return SkillRunResult(
            mode="prompt",
            skill_name=bundle.command_name,
            prompt=bundle.render(arguments, **context),
            bundle=bundle,
            metadata={"arguments": arguments, "context": context},
        )

    def execute(
        self,
        skill_name: str,
        *,
        arguments: str = "",
        input_payload: Optional[Dict[str, Any]] = None,
        llm_adapter: Any = None,
    ) -> SkillRunResult:
        bundle = self.manager.resolve(skill_name)
        entrypoint = (bundle.entrypoint or "prompt").strip()
        if entrypoint.startswith("workflow:"):
            workflow_name = entrypoint.split(":", 1)[1].strip()
            return self._execute_workflow_skill(bundle, workflow_name, input_payload or {}, llm_adapter=llm_adapter)
        if entrypoint.startswith("tool:"):
            tool_name = entrypoint.split(":", 1)[1].strip()
            return self._execute_tool_skill(bundle, tool_name, input_payload or {})
        return self.render(skill_name, arguments=arguments)

    def _execute_workflow_skill(
        self,
        bundle: SkillBundle,
        workflow_name: str,
        input_payload: Dict[str, Any],
        *,
        llm_adapter: Any = None,
    ) -> SkillRunResult:
        from tool_system import AgentRuntime, ConfigDrivenExecutor, SystemConfig, ToolConfig, WorkflowConfig
        from tools import register_all_tools
        from workflows import register_all_workflows
        from tool_system import ToolAndWorkflowRegistry

        registry = ToolAndWorkflowRegistry()
        register_all_tools(registry)
        register_all_workflows(registry)
        register_skill_exports(bundle, registry)

        workflow = registry.get_workflow(workflow_name)
        required_tools: List[str] = []
        if workflow is not None and hasattr(workflow, "definition"):
            required_tools = list(getattr(workflow.definition, "required_tools", []) or [])
        allowed_tools = {str(name).strip() for name in (bundle.frontmatter.allowed_tools or []) if str(name).strip()}
        if allowed_tools:
            unauthorized = sorted(set(required_tools) - allowed_tools)
            if unauthorized:
                raise PermissionError(
                    f"skill '{bundle.command_name}' is not authorized for workflow tools: {', '.join(unauthorized)}"
                )

        tools = [ToolConfig(name=name, enabled=True) for name in required_tools]
        workflows = [WorkflowConfig(name=workflow_name, enabled=True)]
        config = SystemConfig(tools=tools, workflows=workflows)
        executor = ConfigDrivenExecutor(registry, config, llm_adapter=llm_adapter)
        result = AgentRuntime(executor).run(workflow_name, _extract_problem_from_input(input_payload))
        # Preserve the lightweight skill-workflow return contract for generic
        # exports; analyze-shaped runs retain their Harness metadata.
        if isinstance(result, dict) and not any(
            key in result for key in ("analysis", "crash_diagnosis", "applied_ai_fixes", "verification")
        ):
            result = {
                key: value for key, value in result.items()
                if key not in {"metadata", "judge", "decide"}
            }
        return SkillRunResult(
            mode="workflow",
            skill_name=bundle.command_name,
            result=result,
            bundle=bundle,
            metadata={"workflow_name": workflow_name, "input_keys": sorted(list(input_payload.keys()))},
        )

    def _execute_tool_skill(self, bundle: SkillBundle, tool_name: str, input_payload: Dict[str, Any]) -> SkillRunResult:
        from tool_system import ToolAndWorkflowRegistry
        from tool_system.runtime import RunTrace
        from tool_system.tool_gateway import ToolExecutionGateway
        from services.observations import ObservationStore
        from tools import register_all_tools

        registry = ToolAndWorkflowRegistry()
        register_all_tools(registry)
        register_skill_exports(bundle, registry)
        tool = registry.get_tool(tool_name)
        if tool is None:
            raise KeyError(f"未找到 tool export: {tool_name}")
        allowed_tools = set(bundle.frontmatter.allowed_tools or [])
        if allowed_tools and tool_name not in allowed_tools:
            raise PermissionError(
                f"skill '{bundle.command_name}' is not authorized to invoke tool '{tool_name}'"
            )

        import os

        run_id = str(os.environ.get("STABILITY_AGENT_RUN_ID") or "").strip()
        trace = RunTrace(run_id=run_id or f"skill_{bundle.command_name}")
        observations = ObservationStore()
        policy = _policy_from_skill_capabilities(bundle.capabilities)
        gateway = ToolExecutionGateway(policy, trace)
        payload = dict(input_payload or {})
        payload["_observation_store"] = observations
        result = gateway.execute(tool_name, tool, payload)
        return SkillRunResult(
            mode="tool",
            skill_name=bundle.command_name,
            result=result,
            bundle=bundle,
            metadata={
                "tool_name": tool_name,
                "input_keys": sorted(list(input_payload.keys())),
                "runtime_trace": trace.snapshot(),
                "observations": observations.snapshot(),
            },
        )
