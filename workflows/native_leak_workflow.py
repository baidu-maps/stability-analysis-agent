#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dedicated native memory leak workflow."""

from __future__ import annotations

from typing import Any, Dict, List

from tool_system import BaseWorkflow, WorkflowContext, WorkflowDefinition
from tools.native_leak_diagnosis.core import collect_source_search_queries


def _build_prompt(diagnosis: Dict[str, Any], code_search: List[Dict[str, Any]], *, agent_loop: str = "single") -> str:
    lines = [str(diagnosis.get("prompt_section_zh") or "").rstrip(), ""]
    modes = diagnosis.get("fault_mode_matches") or []
    if modes:
        lines.extend(["### 故障模式候选", ""])
        for mode in modes:
            lines.append(
                f"- {mode.get('root_cause_l1')} / {mode.get('root_cause_l2')} / "
                f"{', '.join(mode.get('root_cause_l3_candidates') or [])} "
                f"(置信度: {mode.get('confidence')})"
            )
        lines.append("")
    if code_search:
        lines.extend(["### 源码检索结果", ""])
        for item in code_search:
            lines.append(f"- 符号/API: {item.get('query')}")
            for match in (item.get("definitions") or item.get("matches") or [])[:5]:
                if isinstance(match, dict):
                    path = match.get("file_path") or match.get("path") or ""
                    line = match.get("line") or match.get("line_number") or ""
                    lines.append(f"  - {path}:{line}")
        lines.append("")
    lines.extend([
        "### 分析与修复要求",
        "",
        "1. 严格区分已确认事实、候选根因和缺失证据。",
        "2. 引用增长曲线、PSS 分类、NMD 增量、未释放调用栈和源码位置形成闭环证据链。",
        "3. 检查申请/释放 API、所有权转移、异常返回、取消和析构路径。",
        "4. 给出可实施的代码修复；证据不足时只给验证步骤，不虚构唯一泄漏点。",
        "5. OOM 或高内存不等同于泄漏，需排除有界缓存和业务峰值。",
    ])
    agent_loop = str(agent_loop or "single")
    if agent_loop == "context_loop":
        from services.context_loop_contract import (
            build_round0_must_provide_lines,
            build_round0_output_format_lines,
        )

        lines.append("")
        lines.extend(build_round0_must_provide_lines(agent_loop=agent_loop))
        lines.extend(build_round0_output_format_lines(agent_loop=agent_loop))
    return "\n".join(lines).rstrip() + "\n"


class NativeLeakAnalysisWorkflow(BaseWorkflow):
    @property
    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="native_leak_analysis",
            description="HarmonyOS Native leak analysis using sample/smaps/NMD/native-hook/DMA evidence",
            problem_type="native_memory_leak",
            required_tools=["native_leak_analyzer"],
            version="1.0.0",
            metadata={"platform": "HarmonyOS", "artifact_primary": "04d_native_leak_diagnosis.json"},
        )

    def solve(self, problem: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        path = str(problem.get("native_leak_path") or problem.get("path") or "").strip()
        if not path:
            return {"status": "error", "workflow": self.definition.name, "error": "missing native_leak_path"}
        diagnosis = context.execute_tool("native_leak_analyzer", {
            "path": path,
            "trace_db": str(problem.get("native_leak_trace_db") or problem.get("trace_db") or ""),
            "max_callchains": int(problem.get("max_callchains") or 5),
            "min_callchain_percentage": float(problem.get("min_callchain_percentage") or 0.0),
        })

        code_search: List[Dict[str, Any]] = []
        code_roots = problem.get("code_roots") or []
        queries = collect_source_search_queries(diagnosis)
        if code_roots:
            for query in queries[:8]:
                mode = "find_symbol" if "::" in query or query[:1].isalpha() else "grep"
                try:
                    found = context.execute_tool("repo_search", {
                        "code_roots": code_roots,
                        "mode": mode,
                        "query": query,
                        "symbol_name": query,
                        "max_matches": 20,
                    })
                    if isinstance(found, dict) and found.get("success"):
                        found["query"] = query
                        code_search.append(found)
                except Exception:
                    continue

        prompt = _build_prompt(
            diagnosis,
            code_search,
            agent_loop=str(problem.get("agent_loop") or "single"),
        )
        scope = str(problem.get("scope") or "gen_prompt_only")
        if isinstance(problem, dict) and problem.get("_runtime_owned_context_loop"):
            return {
                "status": "success",
                "workflow": self.definition.name,
                "platform": "HarmonyOS",
                "native_leak_diagnosis": diagnosis,
                "code_search": code_search,
                "analysis": None,
                "final_tip": prompt,
                "metadata": {"problem_type": self.definition.problem_type},
                "_analyze_prepare": {
                    "initial_prompt": prompt,
                    "code_roots": list(code_roots or []),
                    "step": 3,
                    "total_steps": 3,
                    "skip_context_loop": context.llm is None,
                },
            }
        analysis = None
        note = "Native leak deterministic analysis completed"
        if scope == "full" and context.llm is not None:
            try:
                response = context.call_llm(prompt, temperature=0)
                analysis = str(getattr(response, "content", "") or "") or None
            except Exception as exc:
                note = f"LLM analysis failed; deterministic evidence is preserved: {exc}"
        return {
            "status": "success",
            "workflow": self.definition.name,
            "platform": "HarmonyOS",
            "native_leak_diagnosis": diagnosis,
            "code_search": code_search,
            "analysis": analysis,
            "final_tip": prompt,
            "note": note,
            "metadata": {"problem_type": self.definition.problem_type},
        }
