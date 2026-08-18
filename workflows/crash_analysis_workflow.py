#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内置工作流实现 - 将现有的分析能力封装为 Workflow
"""

import logging
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from cli.phase_spinner import PhaseSpinner
from tools.analysis_entry_display import (
    CONFIDENCE_DIRECT_CRASH_THREAD,
    CONFIDENCE_INVESTIGATION_HINT,
    is_investigation_hint_attribution,
    resolve_analysis_entry_confidence_source,
)
from tools.crash_location_display import (
    CRASH_POSITION_PROMPT_ZH_WEAK,
    LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS,
    format_crash_position_summary_line,
    resolve_crash_location_status_source,
)
from tools.thread_display import (
    format_prompt_thread_identity,
    format_prompt_thread_role_flags,
)
from tools.code_context_errors import (
    code_context_has_failure,
    code_context_has_usable_code,
    pipeline_skip_metadata_code,
)
from tools.parse_crash_errors import (
    parse_result_has_usable_crash_data,
    pipeline_skip_metadata,
)
from tools.resolve_stack_errors import (
    pipeline_skip_metadata_resolve,
    resolved_stack_has_usable_resolution,
)
from services.code_locator import CodeLocatorService, LocatorConfig
from tools.snippet_extractor_tool import SnippetExtractorTool
from tool_system import BaseWorkflow, WorkflowDefinition, WorkflowContext, Priority, register_workflow
from tool_system.registry import ToolAndWorkflowRegistry

logger = logging.getLogger(__name__)

# 尝试导入现有的 prompts
try:
    from prompts.crash_analysis_prompt_templates import generate_crash_analysis_prompt
    PROMPTS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"crash_analysis_prompt_templates not available: {e}")
    PROMPTS_AVAILABLE = False
    generate_crash_analysis_prompt = None

def _truncate_analysis_prompt(prompt: str, prompt_cap: int) -> str:
    """截断分析 prompt：保留头部上下文与尾部输出约束。"""
    if len(prompt) <= prompt_cap:
        return prompt
    keep_head = max(1000, int(prompt_cap * 0.72))
    keep_tail = max(500, prompt_cap - keep_head - 80)
    merged = (
        prompt[:keep_head]
        + "\n\n...[PROMPT TRUNCATED — 中间省略]...\n\n"
        + prompt[-keep_tail:]
    )
    if len(merged) > prompt_cap:
        merged = merged[:prompt_cap]
    return merged


# ==================== Base Crash Analysis Workflow ====================

class BaseCrashAnalysisWorkflow(BaseWorkflow):
    """崩溃分析工作流基类"""

    def __init__(self):
        self.platform = "unknown"

    def _return_skipped_no_usable_code(
        self,
        parse_result: Dict[str, Any],
        resolved_stack: Dict[str, Any],
        code_context: Dict[str, Any],
        scope: str,
        problem: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Step 3 无可用源码上下文时提前结束（含 gen_prompt_only，不生成 05）。"""
        skip_meta = pipeline_skip_metadata_code(code_context, scope=scope)
        if scope == "gen_prompt_only":
            note = "skipped_no_usable_code，未生成 05 提示词"
        else:
            note = "skipped_no_usable_code，未执行 AI 分析/改码"
        return {
            "status": "success",
            "platform": self.platform,
            "workflow": self.definition.name,
            "parse_result": parse_result,
            "resolved_stack": resolved_stack,
            "code_context": code_context,
            "analysis": None,
            "final_tip": None,
            "note": note,
            "rule_hits": [],
            "pattern_hits": [],
            "evidence_map": {},
            "strategy_hits": [],
            "decision_trace": [],
            "vector_used": False,
            "memory_context": "",
            "metadata": {
                "problem_type": self.definition.problem_type,
                **skip_meta,
            },
        }

    def _return_skipped_no_usable_resolve(
        self,
        parse_result: Dict[str, Any],
        resolved_stack: Dict[str, Any],
        scope: str,
        problem: Dict[str, Any],
        memory_maps: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Step 2 无可用符号化结果时提前结束（parse_stack_only 仍保留 maps/符号化/诊断产物）。"""
        skip_meta = pipeline_skip_metadata_resolve(resolved_stack)
        memory_maps_data = memory_maps if isinstance(memory_maps, dict) else {}
        crash_diagnosis: Dict[str, Any] = {}
        if scope in {"parse_stack_only", "full", "gen_prompt_only"}:
            try:
                from tools.crash_diagnosis.core import run_crash_diagnosis
                crash_log_content = ""
                if isinstance(problem, dict):
                    crash_log_content = str(
                        problem.get("crash_log_content")
                        or problem.get("crash_log")
                        or ""
                    )
                crash_diagnosis = run_crash_diagnosis(
                    parse_result,
                    memory_maps_data,
                    resolved_stack,
                    crash_log_content=crash_log_content,
                    library_dir=str(problem.get("library_dir") or "") if isinstance(problem, dict) else "",
                    force_disassembly=bool(
                        (problem or {}).get("force_disassembly")
                        or (problem or {}).get("enable_disassembly")
                    ) if isinstance(problem, dict) else False,
                )
            except Exception as diag_exc:
                logger.warning(
                    "[%s] crash_diagnosis on resolve-skip: %s",
                    self.definition.name,
                    diag_exc,
                )
                crash_diagnosis = {
                    "crash_classification": {
                        "primary_pattern": "diagnosis_error",
                        "confidence": 0.0,
                        "summary_zh": f"诊断模块异常，已降级：{diag_exc}",
                    },
                    "error": str(diag_exc),
                    "prompt_section_zh": (
                        "## 崩溃证据诊断\n\n"
                        f"诊断模块异常（已降级保留占位）: {diag_exc}\n"
                    ),
                }
        if scope == "parse_stack_only":
            note = "skipped_no_usable_resolve，已写入符号化/诊断产物但无可用符号"
        else:
            note = "skipped_no_usable_resolve，未执行源码定位/AI"
        return {
            "status": "success",
            "platform": self.platform,
            "workflow": self.definition.name,
            "parse_result": parse_result,
            "memory_maps": memory_maps_data,
            "resolved_stack": resolved_stack,
            "crash_diagnosis": crash_diagnosis,
            "anr_diagnosis": {},
            "memory_diagnosis": {},
            "timeline_diagnosis": {},
            "code_context": {},
            "analysis": None,
            "final_tip": None,
            "note": note,
            "rule_hits": [],
            "pattern_hits": [],
            "evidence_map": {},
            "strategy_hits": [],
            "decision_trace": [],
            "vector_used": False,
            "memory_context": "",
            "metadata": {
                "problem_type": self.definition.problem_type,
                **skip_meta,
            },
        }

    def _return_skipped_no_usable_parse(
        self,
        parse_result: Dict[str, Any],
        scope: str,
        problem: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Step 1 无可用堆栈信息时提前结束（所有 scope）。"""
        skip_meta = pipeline_skip_metadata(parse_result)
        note = "skipped_no_usable_parse"
        if scope == "parse_log_only":
            note = "skipped_no_usable_parse，仅写入 01"
        elif scope == "parse_stack_only":
            note = "skipped_no_usable_parse，未执行符号化及后续步骤"
        else:
            note = "skipped_no_usable_parse，未执行符号化/源码定位/AI"
        return {
            "status": "success",
            "platform": self.platform,
            "workflow": self.definition.name,
            "parse_result": parse_result,
            "resolved_stack": {},
            "code_context": {},
            "analysis": None,
            "final_tip": None,
            "note": note,
            "rule_hits": [],
            "pattern_hits": [],
            "evidence_map": {},
            "strategy_hits": [],
            "decision_trace": [],
            "vector_used": False,
            "memory_context": "",
            "metadata": {
                "problem_type": self.definition.problem_type,
                **skip_meta,
            },
        }

    def solve(self, problem: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        """
        标准的崩溃分析流程：
        1. 解析崩溃日志
        2. 堆栈符号化
        3. 定位崩溃源码
        4. AI 分析根因
        """
        crash_log = problem.get("crash_log", "")
        library_dir = problem.get("library_dir")
        code_roots = problem.get("code_roots", [])
        code_root = problem.get("code_root")
        scope = self._resolve_scope(problem)

        # 兼容 code_root
        if code_root and not code_roots:
            code_roots = [code_root]

        if not crash_log:
            return {"error": "缺少 crash_log"}

        if scope == "parse_log_only":
            total_steps = 1
        elif scope == "parse_stack_only":
            total_steps = 3
        elif scope == "gen_prompt_only":
            # parse → symbolize → diagnosis → code → (prompt 无单独 spinner)
            total_steps = 4
        else:
            # full: parse → symbolize → diagnosis → code → LLM；apply 由 CLI 作为最后一步展示
            apply_ai_fixes = problem.get("apply_ai_fixes", True)
            total_steps = 6 if apply_ai_fixes else 5

        try:
            # Step 1: 解析崩溃日志
            with PhaseSpinner("解析崩溃日志", step=1, total_steps=total_steps) as _parse_spinner:
                logger.info(f"[{self.definition.name}] Step 1: Parsing crash log...")
                parse_result = context.execute_tool("crash_log_parser", {
                    "log_content": crash_log,
                    "options": {
                        "library_dir": os.path.abspath(library_dir),
                    } if library_dir and os.path.exists(library_dir) else {},
                })
                if not isinstance(parse_result, dict):
                    parse_result = {}
                if not parse_result_has_usable_crash_data(parse_result):
                    logger.info(
                        "[%s] parse_result has no usable stack frames; stopping pipeline.",
                        self.definition.name,
                    )
                    _parse_spinner.set_partial_failure()
                    return self._return_skipped_no_usable_parse(parse_result, scope, problem)

            if scope == "parse_log_only":
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "memory_maps": {},
                    "resolved_stack": {},
                    "crash_diagnosis": {},
                    "code_context": {},
                    "analysis": None,
                    "final_tip": None,
                    "note": "scope=parse_log_only，仅执行日志解析",
                    "rule_hits": [],
                    "pattern_hits": [],
                    "evidence_map": {},
                    "strategy_hits": [],
                    "decision_trace": [],
                    "vector_used": False,
                    "memory_context": "",
                    "metadata": {
                        "problem_type": self.definition.problem_type
                    }
                }

            # Step 2: 提取内存映射（轻量，紧跟 01 之后）
            memory_maps_data: Dict[str, Any] = {}
            try:
                from tools.crash_diagnosis.maps_extractor import extract_memory_maps
                memory_maps_data = extract_memory_maps(crash_log)
            except Exception as maps_exc:
                logger.debug("memory_maps extraction skipped: %s", maps_exc)

            # Step 3: 堆栈符号化
            with PhaseSpinner("堆栈符号化", step=2, total_steps=total_steps) as _resolve_spinner:
                logger.info(f"[{self.definition.name}] Step 2: Resolving symbols...")
                resolved = context.execute_tool("add2line_resolver", {
                    "crash_json": json.dumps(parse_result),
                    "library_dir": library_dir
                })
                if not isinstance(resolved, dict):
                    resolved = {}
                if not resolved_stack_has_usable_resolution(resolved):
                    logger.info(
                        "[%s] resolved_stack has no usable symbols; stopping pipeline.",
                        self.definition.name,
                    )
                    _resolve_spinner.set_partial_failure()
                    return self._return_skipped_no_usable_resolve(
                        parse_result,
                        resolved,
                        scope,
                        problem,
                        memory_maps=memory_maps_data,
                    )

            # Step 4a: 崩溃诊断（依赖 01+02 maps+03 符号化；含 DeterministicAnalyzer）
            crash_diagnosis: Dict[str, Any] = {}
            crash_log_content = ""
            if isinstance(problem, dict):
                crash_log_content = str(
                    problem.get("crash_log_content")
                    or problem.get("crash_log")
                    or ""
                )
            try:
                with PhaseSpinner("崩溃证据诊断", step=3, total_steps=total_steps):
                    from tools.crash_diagnosis.core import run_crash_diagnosis
                    crash_diagnosis = run_crash_diagnosis(
                        parse_result,
                        memory_maps_data,
                        resolved,
                        crash_log_content=crash_log_content,
                        library_dir=str(library_dir or ""),
                        force_disassembly=bool(
                            problem.get("force_disassembly")
                            or problem.get("enable_disassembly")
                        ),
                    )
            except Exception as diag_exc:
                logger.warning("[%s] crash_diagnosis failed: %s", self.definition.name, diag_exc)
                crash_diagnosis = {
                    "crash_classification": {
                        "primary_pattern": "diagnosis_error",
                        "confidence": 0.0,
                        "summary_zh": f"诊断模块异常，已降级：{diag_exc}",
                    },
                    "register_diagnosis": None,
                    "evidence_chain": [
                        {
                            "type": "error",
                            "finding": str(diag_exc),
                            "implication": "04a 诊断未完整执行，请结合 01/03 手工分析",
                        }
                    ],
                    "prompt_section_zh": (
                        "## 崩溃证据诊断\n\n"
                        f"诊断模块异常（已降级保留占位）: {diag_exc}\n"
                    ),
                    "error": str(diag_exc),
                }

            # Step 4a+: ANR/Freeze（仅 anr 族兜底或 force；专用 workflow 主路径不双跑）
            anr_diagnosis = self._maybe_run_anr_diagnosis(
                parse_result=parse_result,
                resolved_stack=resolved,
                problem=problem if isinstance(problem, dict) else {},
                crash_log_content=crash_log_content,
            )

            # Step 4a++: 内存压力/OOM（oom 族或 force；阶段 A 旁路 → 04d）
            memory_diagnosis = self._maybe_run_memory_diagnosis(
                parse_result=parse_result,
                resolved_stack=resolved,
                problem=problem if isinstance(problem, dict) else {},
                crash_log_content=crash_log_content,
            )

            # Step 4a+++: 崩溃前时序/业务路径（有 logcat/HiLog/ASI 信号或 force → 04e）
            timeline_diagnosis = self._maybe_run_timeline_diagnosis(
                parse_result=parse_result,
                problem=problem if isinstance(problem, dict) else {},
                crash_log_content=crash_log_content,
            )

            if scope == "parse_stack_only":
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "memory_maps": memory_maps_data,
                    "resolved_stack": resolved,
                    "crash_diagnosis": crash_diagnosis,
                    "anr_diagnosis": anr_diagnosis or {},
                    "memory_diagnosis": memory_diagnosis or {},
                    "timeline_diagnosis": timeline_diagnosis or {},
                    "code_context": {},
                    "analysis": None,
                    "final_tip": None,
                    "note": "scope=parse_stack_only，已执行日志解析+符号化+崩溃诊断",
                    "rule_hits": [],
                    "pattern_hits": [],
                    "evidence_map": {},
                    "strategy_hits": [],
                    "decision_trace": [],
                    "vector_used": False,
                    "memory_context": "",
                    "metadata": {
                        "problem_type": self.definition.problem_type
                    }
                }

            # Step 4b: 定位崩溃源码
            with PhaseSpinner("定位崩溃源码", step=4, total_steps=total_steps) as _ccp_spinner:
                logger.info(f"[{self.definition.name}] Step 3: Extracting code context...")
                ccp_input: Dict[str, Any] = {
                    "resolved_stack": json.dumps(resolved),
                    "code_roots": code_roots,
                }
                # Step 3a: 先做轻量调用点召回，给 code_content_provider 提供优先文件 seed
                try:
                    callsite_seed = context.execute_tool(
                        "symbol_callsite_finder",
                        {
                            "resolved_stack": json.dumps(resolved),
                            "code_roots": code_roots,
                            "max_results": 500,
                        },
                    )
                    if isinstance(callsite_seed, dict):
                        candidate_files = callsite_seed.get("candidate_files") or []
                        if isinstance(candidate_files, list) and candidate_files:
                            ccp_input["_candidate_callsite_files"] = candidate_files
                except Exception as seed_exc:
                    logger.info(f"[{self.definition.name}] symbol_callsite_finder skipped: {seed_exc}")
                for _k in (
                    "max_sibling_member_functions",
                    "max_direct_callers",
                    "max_shared_var_related_functions",
                    "min_key_read_related_functions",
                    "max_static_call_chain_depth",
                    "max_symbol_only_rescues",
                    "find_source_timeout_sec",
                    "code_context_timeout_sec",
                    "max_nearby_module_scan_files",
                    "max_prompt_stack_frame_functions",
                    "max_crash_caller_search_files",
                    "_code_index_service",
                    "use_ctags_index",
                ):
                    if isinstance(problem, dict) and _k in problem and problem[_k] is not None:
                        ccp_input[_k] = problem[_k]
                code_context = context.execute_tool("code_content_provider", ccp_input)
                if code_context_has_failure(code_context):
                    _ccp_spinner.set_partial_failure()

                if not code_context_has_usable_code(code_context):
                    logger.info(
                        "[%s] code_context has no usable source snippets; "
                        "stopping pipeline (scope=%s, no 05/LLM).",
                        self.definition.name,
                        scope,
                    )
                    return self._return_skipped_no_usable_code(
                        parse_result, resolved, code_context, scope, problem
                    )

            memory_context = ""
            memory_retrieval: Dict[str, Any] = {}
            rule_hits: List[Dict[str, Any]] = []
            pattern_hits: List[Dict[str, Any]] = []
            evidence_map: Dict[str, Any] = {}
            strategy_hits: List[Dict[str, Any]] = []
            decision_trace: List[Dict[str, Any]] = []
            vector_used = False

            rag_input: Dict[str, Any] = {
                "parse_result": parse_result,
                "resolved_stack": resolved,
                "code_context": code_context,
            }
            if isinstance(problem, dict):
                if problem.get("vector_db_path") is not None:
                    rag_input["vector_db_path"] = problem.get("vector_db_path")
                if problem.get("rule_confidence_threshold") is not None:
                    rag_input["rule_confidence_threshold"] = problem.get("rule_confidence_threshold")
                if problem.get("vector_db_max_results") is not None:
                    rag_input["vector_db_max_results"] = problem.get("vector_db_max_results")
                if problem.get("vector_db_readonly") is not None:
                    rag_input["vector_db_readonly"] = problem.get("vector_db_readonly")
                else:
                    rag_input["vector_db_readonly"] = True
            try:
                rag_result = context.execute_tool("vector_memory_retriever", rag_input)
            except Exception as rag_exc:
                logger.info(
                    "[%s] vector_memory_retriever skipped: %s",
                    self.definition.name,
                    rag_exc,
                )
                rag_result = {}
            if isinstance(rag_result, dict):
                memory_retrieval = rag_result
                memory_context = str(rag_result.get("memory_context") or "")
                rule_hits = rag_result.get("rule_hits", []) or []
                pattern_hits = rag_result.get("pattern_hits", []) or []
                evidence_map = rag_result.get("evidence_map", {}) or {}
                strategy_hits = rag_result.get("strategy_hits", []) or []
                decision_trace = rag_result.get("decision_trace", []) or []
                vector_used = bool(rag_result.get("vector_used", False))

            # === 确定性结论已并入 04a.prompt_section_zh，此处不再旁路重复注入 ===

            # 检查是否有 LLM
            if context.llm is None:
                logger.warning(f"[{self.definition.name}] No LLM configured, skipping AI analysis")
                assembled_prompt = ""
                if scope == "gen_prompt_only":
                    # gen_prompt_only 模式：完整工具链已就绪，仅生成可复用提示词，不调用 LLM
                    assembled_prompt = self._build_prompt_final_tip(
                        parse_result=parse_result,
                        resolved=resolved,
                        code_context=code_context,
                        memory_context=memory_context,
                        problem=problem,
                        crash_diagnosis=crash_diagnosis,
                        anr_diagnosis=anr_diagnosis,
                        memory_diagnosis=memory_diagnosis,
                        timeline_diagnosis=timeline_diagnosis,
                    )
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "memory_maps": memory_maps_data,
                    "resolved_stack": resolved,
                    "crash_diagnosis": crash_diagnosis,
                    "anr_diagnosis": anr_diagnosis or {},
                    "memory_diagnosis": memory_diagnosis or {},
                    "timeline_diagnosis": timeline_diagnosis or {},
                    "code_context": code_context,
                    "analysis": None,
                    "final_tip": assembled_prompt or None,
                    "note": "LLM not configured, AI analysis skipped",
                    "rule_hits": rule_hits,
                    "pattern_hits": pattern_hits,
                    "evidence_map": evidence_map,
                    "strategy_hits": strategy_hits,
                    "decision_trace": decision_trace,
                    "vector_used": vector_used,
                    "memory_context": memory_context,
                    "memory_retrieval": memory_retrieval,
                    "metadata": {
                        "problem_type": self.definition.problem_type
                    }
                }

            # Step 5: LLM 分析（按轮次展示子阶段）
            logger.info(f"[{self.definition.name}] Step 5: LLM analysis...")
            # 与 gen_prompt_only 模式对齐：首轮 05 以首轮 prompt 为基准
            analysis_prompt = self._build_prompt_final_tip(
                parse_result=parse_result,
                resolved=resolved,
                code_context=code_context,
                memory_context=memory_context,
                problem=problem,
                crash_diagnosis=crash_diagnosis,
                anr_diagnosis=anr_diagnosis,
                memory_diagnosis=memory_diagnosis,
                timeline_diagnosis=timeline_diagnosis,
            )
            # === 追加结构化报告格式要求（确定性事实已在 04a 诊断段）===
            try:
                from prompts.report_schema import get_report_instruction
                report_instruction = get_report_instruction(mode="full")
                if report_instruction:
                    analysis_prompt += "\n\n" + report_instruction
            except Exception as ri_exc:
                logger.debug("Report instruction injection skipped: %s", ri_exc)
            if isinstance(problem, dict):
                problem["_crash_diagnosis"] = crash_diagnosis
            analysis_text, final_prompt, agent_rounds, llm_response = self._run_llm_context_loop(
                context=context,
                initial_prompt=analysis_prompt,
                problem=problem,
                code_roots=code_roots,
                step=5,
                total_steps=total_steps,
            )

            return {
                "status": "success",
                "platform": self.platform,
                "workflow": self.definition.name,
                "parse_result": parse_result,
                "memory_maps": memory_maps_data,
                "resolved_stack": resolved,
                "crash_diagnosis": crash_diagnosis,
                "anr_diagnosis": anr_diagnosis or {},
                "memory_diagnosis": memory_diagnosis or {},
                "timeline_diagnosis": timeline_diagnosis or {},
                "code_context": code_context,
                "analysis": analysis_text,
                "final_tip": analysis_prompt,
                "agent_rounds": agent_rounds,
                "final_prompt": final_prompt,
                "rule_hits": rule_hits,
                "pattern_hits": pattern_hits,
                "evidence_map": evidence_map,
                "strategy_hits": strategy_hits,
                "decision_trace": decision_trace,
                "vector_used": vector_used,
                "memory_context": memory_context,
                "memory_retrieval": memory_retrieval,
                "metadata": self._metadata_with_llm_routing(
                    problem, problem_type=self.definition.problem_type
                ),
            }

        except Exception as e:
            logger.error(f"[{self.definition.name}] Error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "workflow": self.definition.name,
                "metadata": self._metadata_with_llm_routing(
                    problem if isinstance(problem, dict) else None,
                    problem_type=self.definition.problem_type,
                ),
            }

    @staticmethod
    def _metadata_with_llm_routing(
        problem: Optional[Dict[str, Any]],
        *,
        problem_type: str = "crash_analysis",
    ) -> Dict[str, Any]:
        meta: Dict[str, Any] = {"problem_type": problem_type}
        if not isinstance(problem, dict):
            return meta
        state = problem.get("_llm_router_state")
        if state is not None and hasattr(state, "to_summary_dict"):
            try:
                meta["llm_routing"] = state.to_summary_dict()
            except Exception:
                pass
        return meta

    def _build_analysis_prompt(self, parse_result: Dict, resolved: Dict, code_context: Dict, memory_context: str = "") -> str:
        """构建分析提示词"""
        crash_info = parse_result.get("crash_info", {}) if isinstance(parse_result, dict) else {}
        # 解耦重构：从 01+02+03 独立构建 crash_summary
        from tools.merge_utils import build_crash_summary_view
        cc_summary = build_crash_summary_view(parse_result, resolved, code_context)
        graph = code_context.get("graph", {}) if isinstance(code_context, dict) else {}
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        node_by_id = {
            n.get("id"): n
            for n in nodes
            if isinstance(n, dict) and isinstance(n.get("id"), str)
        }
        crash_node = node_by_id.get(cc_summary.get("node_id")) if isinstance(cc_summary, dict) else None
        if crash_node is None and isinstance(cc_summary, dict):
            raw_node_id = cc_summary.get("node_id")
            if isinstance(raw_node_id, str):
                # code_context 的 node_id 可能携带函数体起始符号 "{"
                normalized_id = raw_node_id.rstrip().rstrip("{").rstrip()
                crash_node = node_by_id.get(normalized_id)

        crash_summary = {
            "file": (crash_node or {}).get("file", "unknown"),
            "function": (crash_node or {}).get("signature", "unknown"),
            "line": cc_summary.get("crash_line_number", "unknown") if isinstance(cc_summary, dict) else "unknown",
            "stack_address": (
                cc_summary.get("stack_address")
                if isinstance(cc_summary, dict) and cc_summary.get("stack_address")
                else crash_info.get("crash_address", "unknown")
            ),
            "error_type": crash_info.get("signal", "unknown"),
            "thread_id": (
                cc_summary.get("thread_id")
                if isinstance(cc_summary, dict) and cc_summary.get("thread_id")
                else crash_info.get("thread_type", "unknown")
            ),
        }

        crash_func = None
        if isinstance(crash_node, dict):
            crash_func = {
                "name": str(crash_node.get("signature") or "unknown"),
                "signature": str(crash_node.get("signature") or "unknown"),
                "snippet": crash_node.get("snippet") if isinstance(crash_node.get("snippet"), list) else [],
                "crash_line": cc_summary.get("crash_line_code", "unknown") if isinstance(cc_summary, dict) else "unknown",
            }

        call_chain_fun: List[Dict[str, Any]] = []
        call_chain = graph.get("call_chain_from_code", []) if isinstance(graph, dict) else []
        if isinstance(call_chain, list) and call_chain:
            chain_nodes = call_chain[0].get("nodes", []) if isinstance(call_chain[0], dict) else []
            for node_id in chain_nodes:
                node = node_by_id.get(node_id)
                if not isinstance(node, dict):
                    continue
                call_chain_fun.append(
                    {
                        "name": str(node.get("signature") or "unknown"),
                        "file": str(node.get("file") or "unknown"),
                        "snippet": node.get("snippet") if isinstance(node.get("snippet"), list) else [],
                    }
                )

        thread_context: List[Dict[str, Any]] = []
        add2line_chains = graph.get("call_chain_from_add2line", []) if isinstance(graph, dict) else []
        if isinstance(add2line_chains, list):
            for item in add2line_chains:
                if not isinstance(item, dict):
                    continue
                symbols = []
                for node_id in item.get("nodes", []) if isinstance(item.get("nodes"), list) else []:
                    node = node_by_id.get(node_id)
                    if isinstance(node, dict):
                        symbols.append(str(node.get("signature") or node_id))
                    else:
                        symbols.append(str(node_id))
                thread_context.append(
                    {
                        "thread_id": str(item.get("thread_id") or "unknown"),
                        "thread_name": item.get("thread_name"),
                        "is_crash_thread": item.get("is_crash_thread"),
                        "is_main_thread": item.get("is_main_thread"),
                        "has_business_frames": item.get("has_business_frames"),
                        "call_chain": symbols,
                    }
                )

        prompt_payload: Dict[str, Any] = {"crash_summary": crash_summary}
        if crash_func:
            prompt_payload["crash_func"] = crash_func
        if call_chain_fun:
            prompt_payload["call_chain_fun"] = call_chain_fun
        if thread_context:
            prompt_payload["thread_context"] = thread_context

        if PROMPTS_AVAILABLE:
            prompt = generate_crash_analysis_prompt(prompt_payload)
            if memory_context:
                prompt += f"\n\n## 规则与经验模式参考\n{memory_context}"
            return prompt
        else:
            # 简单的 fallback 提示词
            prompt = f"""请分析以下崩溃：

崩溃信息: {json.dumps(parse_result, ensure_ascii=False)}

解析后的堆栈: {json.dumps(resolved, ensure_ascii=False)}

代码上下文: {json.dumps(code_context, ensure_ascii=False)}

请提供修复建议。"""
            if memory_context:
                prompt += f"\n\n规则与经验模式参考:\n{memory_context}"
            return prompt

    @staticmethod
    def _resolve_scope(problem: Dict[str, Any]) -> str:
        """从 problem 解析 scope；兼容旧字段（skip_ai + run_scope）。"""
        valid = {"full", "gen_prompt_only", "parse_stack_only", "parse_log_only"}
        raw = str(problem.get("scope") or "").strip()
        if raw in valid:
            return raw
        legacy_scope = str(problem.get("run_scope") or "").strip()
        if legacy_scope in {"parse_stack_only", "parse_log_only"}:
            return legacy_scope
        if bool(problem.get("skip_ai", False)):
            return "gen_prompt_only"
        return "full"

    @staticmethod
    def _resolve_prompt_mode(problem: Optional[Dict[str, Any]] = None) -> str:
        """解析 05 / LLM 提示词模式；仅控制输出契约，不控制是否自动应用修复。"""
        valid = {"analysis", "fix"}
        if isinstance(problem, dict):
            raw = str(problem.get("prompt_mode") or "").strip().lower()
            if raw in valid:
                return raw
        return "analysis"

    @staticmethod
    def _resolve_agent_loop(problem: Optional[Dict[str, Any]] = None) -> str:
        """解析 Agent 编排模式；独立于 LLM engine。"""
        valid = {"single", "context_loop"}
        if isinstance(problem, dict):
            raw = str(problem.get("agent_loop") or "").strip().lower()
            if raw in valid:
                return raw
        # 未显式指定时：analysis 模式默认 context_loop，其它模式默认 single
        mode = BaseCrashAnalysisWorkflow._resolve_prompt_mode(problem)
        return "context_loop" if mode == "analysis" else "single"

    @staticmethod
    def _resolve_max_agent_rounds(problem: Optional[Dict[str, Any]] = None) -> int:
        if not isinstance(problem, dict):
            return 1
        try:
            val = int(problem.get("max_agent_rounds") or 0)
        except (TypeError, ValueError):
            val = 0
        if val <= 0:
            # 未显式指定时：analysis 模式默认 3 轮，其它模式默认 1 轮
            mode = BaseCrashAnalysisWorkflow._resolve_prompt_mode(problem)
            return 3 if mode == "analysis" else 1
        return max(1, min(val, 8))

    @staticmethod
    def _include_memory_context_in_final_tip(problem: Optional[Dict[str, Any]] = None) -> bool:
        """
        05 / LLM 提示词是否并入向量库检索的 memory_context。

        默认关闭（避免 RAG 规则/经验误导分析）。CLI 使用 --include-memory-in-05 开启。
        """
        if isinstance(problem, dict):
            return bool(problem.get("include_memory_context_in_final_tip", False))
        return False

    @staticmethod
    def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
        """从 LLM 输出中提取一个 JSON object，支持 fenced json。"""
        raw = str(text or "").strip()
        if not raw:
            return None
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S | re.I)
        candidates = [fenced.group(1)] if fenced else []
        if raw.startswith("{") and raw.endswith("}"):
            candidates.append(raw)
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            candidates.append(raw[first : last + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return None

    CONTEXT_REQUEST_RETURN_FORMS: Dict[str, str] = {
        "function": "function_source",
        "field": "member_declaration",
        "references": "read_write_references",
        "callers": "caller_snippets",
    }
    CONTEXT_REQUEST_RETURN_FORM_LABELS: Dict[str, str] = {
        "function_source": "函数定义处完整源码（含签名与函数体）",
        "member_declaration": "所属类成员声明（优先头文件）",
        "member_initialization": "成员初始化语句",
        "class_declaration": "类/结构体声明块（头文件）",
        "read_write_references": "读写/引用位置（文件:行 + 片段）",
        "caller_snippets": "调用方函数名 + 调用点片段",
    }

    @classmethod
    def _default_expected_return_form(cls, req_type: str) -> str:
        return cls.CONTEXT_REQUEST_RETURN_FORMS.get(
            str(req_type or "").strip().lower(), "function_source"
        )

    @classmethod
    def _return_form_label(cls, form: str) -> str:
        return cls.CONTEXT_REQUEST_RETURN_FORM_LABELS.get(
            str(form or "").strip(), str(form or "")
        )

    @classmethod
    def _normalize_expected_return_form(cls, req_type: str, raw_form: Any) -> str:
        req_type = str(req_type or "").strip().lower()
        default_form = cls._default_expected_return_form(req_type)
        token = str(raw_form or "").strip().lower()
        if not token:
            return default_form
        aliases = {
            "function": "function_source",
            "function_source": "function_source",
            "source": "function_source",
            "field": "member_declaration",
            "member_declaration": "member_declaration",
            "declaration": "member_declaration",
            "references": "read_write_references",
            "read_write_references": "read_write_references",
            "reference": "read_write_references",
            "callers": "caller_snippets",
            "caller_snippets": "caller_snippets",
        }
        normalized = aliases.get(token, token)
        allowed_by_type = {
            "function": {"function_source"},
            "field": {"member_declaration", "member_initialization"},
            "references": {"read_write_references"},
            "callers": {"caller_snippets"},
        }
        if normalized in allowed_by_type.get(req_type, {default_form}):
            return normalized
        return default_form

    @classmethod
    def _infer_actual_return_form(cls, item: Dict[str, Any]) -> Optional[str]:
        if not item.get("success"):
            return None
        context_type = str(item.get("context_type") or "function").strip().lower()
        if context_type == "function":
            return "function_source"
        if context_type == "field":
            matches = item.get("matches")
            if isinstance(matches, list) and matches:
                kind = str((matches[0] or {}).get("match_kind") or "").strip().lower()
                if kind == "class_declaration":
                    return "class_declaration"
                if kind == "initialization":
                    return "member_initialization"
            return "member_declaration"
        if context_type == "references":
            return "read_write_references"
        if context_type == "callers":
            return "caller_snippets"
        return None

    @classmethod
    def _fulfillment_matched(cls, expected_form: str, actual_form: Optional[str]) -> Optional[bool]:
        if not actual_form:
            return None
        expected = str(expected_form or "").strip().lower()
        actual = str(actual_form or "").strip().lower()
        if expected == actual:
            return True
        if expected == "member_declaration" and actual in {
            "member_initialization",
            "class_declaration",
        }:
            return True
        return False

    @classmethod
    def _attach_return_form_metadata(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return item
        req = item.get("request") if isinstance(item.get("request"), dict) else {}
        req_type = str(req.get("type") or "function").strip().lower()
        expected = cls._normalize_expected_return_form(
            req_type,
            req.get("expected_return_form")
            or req.get("return_form")
            or req.get("expected_form"),
        )
        item["expected_return_form"] = expected
        item["expected_return_form_label"] = cls._return_form_label(expected)
        actual = cls._infer_actual_return_form(item)
        if actual:
            item["actual_return_form"] = actual
            item["actual_return_form_label"] = cls._return_form_label(actual)
            matched = cls._fulfillment_matched(expected, actual)
            if matched is not None:
                item["fulfillment_matched"] = matched
        return item

    @classmethod
    def _parse_context_requests(cls, analysis_text: str) -> Dict[str, Any]:
        payload = cls._extract_json_object(analysis_text) or {}
        requests = payload.get("context_requests")
        if not isinstance(requests, list):
            requests = payload.get("need_context")
        if not isinstance(requests, list):
            requests = []
        normalized: List[Dict[str, Any]] = []
        for item in requests:
            if not isinstance(item, dict):
                continue
            symbol = (
                item.get("symbol")
                or item.get("function")
                or item.get("function_name")
                or item.get("name")
            )
            file_path = item.get("file") or item.get("file_path")
            req_type = str(item.get("type") or "function").strip().lower()
            reason = str(item.get("reason") or "").strip()
            priority = str(item.get("priority") or "").strip().lower()
            try:
                line_number = int(item.get("line") or item.get("line_number") or 0)
            except (TypeError, ValueError):
                line_number = 0
            expected_return_form = cls._normalize_expected_return_form(
                req_type,
                item.get("expected_return_form")
                or item.get("return_form")
                or item.get("expected_form"),
            )
            fulfillment_note = str(
                item.get("fulfillment_note")
                or item.get("request_type_note")
                or item.get("return_form_note")
                or ""
            ).strip()
            normalized.append(
                {
                    "type": req_type,
                    "symbol": str(symbol or "").strip(),
                    "file": str(file_path or "").strip(),
                    "line_number": line_number,
                    "reason": reason,
                    "priority": priority,
                    "expected_return_form": expected_return_form,
                    "expected_return_form_label": cls._return_form_label(
                        expected_return_form
                    ),
                    "fulfillment_note": fulfillment_note,
                }
            )
        agent_can_fetch_more = cls._parse_agent_can_fetch_more(payload, normalized)
        return {
            "agent_can_fetch_more": agent_can_fetch_more,
            # 兼容旧版 LLM 输出字段
            "need_more_context": agent_can_fetch_more,
            "context_requests": normalized,
            "raw_payload": payload,
        }

    @staticmethod
    def _parse_agent_can_fetch_more(
        payload: Dict[str, Any], normalized_requests: List[Dict[str, Any]]
    ) -> bool:
        """解析是否继续由 Agent 自动拉取上下文（兼容 need_more_context）。"""
        raw = payload.get("agent_can_fetch_more")
        if raw is None:
            legacy = payload.get("need_more_context")
            if legacy is not None:
                raw = legacy
        if raw is None:
            return bool(normalized_requests)
        return bool(raw) or bool(normalized_requests)

    @staticmethod
    def _read_source_lines(file_path: str) -> List[str]:
        try:
            return Path(file_path).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return []

    @staticmethod
    def _is_destructor_symbol(symbol: str) -> bool:
        raw = str(symbol or "").strip()
        if not raw:
            return False
        head = raw.split("(", 1)[0]
        return bool(re.search(r"::~\w+\s*$", head) or re.search(r"::~\w+\s*$", raw))

    @classmethod
    def _normalize_context_request_symbol(cls, symbol: str, req_type: str = "") -> str:
        """规范化 context request 符号，减少等价写法导致的定位/去重失败。"""
        raw = str(symbol or "").strip()
        if not raw:
            return raw
        req_type = str(req_type or "").strip().lower()
        head = raw.split("(", 1)[0].strip()
        if req_type == "function" and re.search(r"::\s*~\w+\s*$", head):
            return head + "()"
        if req_type == "references":
            return raw.rstrip().rstrip(";")
        return raw

    @classmethod
    def _canonical_context_request_symbol(cls, symbol: str, req_type: str = "") -> str:
        """将等价函数符号规范为同一去重键（如 CVList::RemoveAll 与 CVList<T>::RemoveAll）。"""
        norm = cls._normalize_context_request_symbol(symbol, req_type)
        if str(req_type or "").strip().lower() != "function":
            return norm
        head = norm.split("(", 1)[0].strip()
        from services.code_locator import SymbolLocator

        tpl = SymbolLocator.parse_template_qualified_symbol(head)
        if tpl:
            return f"{tpl['template_class']}<>::{tpl['member']}"
        parsed = SymbolLocator.parse_qualified_member_symbol(head)
        if parsed:
            scope = str(parsed.get("short_scope") or "")
            method = str(parsed.get("method") or "")
            if (
                scope
                and method
                and scope[0].isupper()
                and not scope.startswith("m_")
                and "<" not in head
            ):
                return f"{scope}<>::{method}"
        return norm

    @classmethod
    def _context_request_outcome_key(
        cls, req_type: str, symbol: str, file_path: str, line_number: int
    ) -> str:
        norm = cls._normalize_context_request_symbol(symbol, req_type)
        req_type = str(req_type or "").strip().lower()
        if req_type == "function" and not file_path and line_number <= 0:
            return f"{req_type}::{norm}"
        return f"{req_type}:{file_path}:{line_number}:{norm}".strip(":")

    @classmethod
    def _context_request_success_dedupe_keys(
        cls, req_type: str, symbol: str, file_path: str, line_number: int
    ) -> List[str]:
        """成功去重键：等价模板函数写法共享；失败/拒绝仍用精确 outcome key。"""
        exact = cls._context_request_outcome_key(req_type, symbol, file_path, line_number)
        keys = [exact]
        if str(req_type or "").strip().lower() == "function":
            canon = cls._canonical_context_request_symbol(symbol, req_type)
            canon_key = cls._context_request_outcome_key(req_type, canon, file_path, line_number)
            if canon_key not in keys:
                keys.append(canon_key)
        return keys

    @classmethod
    def _record_context_request_outcome(
        cls,
        outcomes: Dict[str, str],
        req_type: str,
        symbol: str,
        file_path: str,
        line_number: int,
        status: str,
    ) -> None:
        key = cls._context_request_outcome_key(req_type, symbol, file_path, line_number)
        if not key:
            return
        outcomes[key] = status
        if status == "success":
            for alias in cls._context_request_success_dedupe_keys(
                req_type, symbol, file_path, line_number
            ):
                outcomes[alias] = "success"

    @staticmethod
    def _looks_like_package_or_thread_label(symbol: str) -> bool:
        """判断是否为 Android 包名/线程标签（而非 C++ 成员访问）。"""
        symbol = str(symbol or "").strip()
        if not symbol or "::" in symbol:
            return False
        if re.match(r"^m_[A-Za-z_]\w*\.[A-Za-z_]\w+", symbol):
            return False
        if not re.match(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", symbol):
            return False
        return True

    @staticmethod
    def _parse_member_method_reference(symbol: str) -> Optional[Dict[str, str]]:
        """解析 obj.method / obj.method() 形式的引用符号。"""
        raw = str(symbol or "").strip().rstrip(";")
        if raw.endswith("()"):
            raw = raw[:-2]
        raw = raw.rstrip(")").strip()
        match = re.match(r"^(?:([\w:<>*,&\s]+)::)?(m_\w+)\.(\w+)$", raw)
        if match:
            return {
                "owner_prefix": str(match.group(1) or "").strip(),
                "member": match.group(2),
                "method": match.group(3),
            }
        match = re.match(r"^(\w+)\.(\w+)$", raw)
        if match and match.group(1).startswith("m_"):
            return {
                "owner_prefix": "",
                "member": match.group(1),
                "method": match.group(2),
            }
        return None

    @classmethod
    def _extract_brace_block_lines(
        cls, lines: List[str], start_index: int, *, max_lines: int = 120
    ) -> List[str]:
        if start_index < 0 or start_index >= len(lines):
            return []
        collected: List[str] = []
        depth = 0
        started = False
        for idx in range(start_index, min(len(lines), start_index + max_lines)):
            line = lines[idx]
            collected.append(line)
            depth += line.count("{") - line.count("}")
            if "{" in line:
                started = True
            if started and depth <= 0:
                break
        if not collected:
            collected = lines[start_index : min(len(lines), start_index + 20)]
        return collected

    @classmethod
    def _repair_template_function_snippet(
        cls,
        *,
        symbol: str,
        target_file: str,
        target_line: int,
        out: Dict[str, Any],
    ) -> Dict[str, Any]:
        from services.code_locator import SymbolLocator

        lines = cls._read_source_lines(target_file)
        if not lines or not (0 < target_line <= len(lines)):
            return out
        def_idx = target_line - 1
        def_line = lines[def_idx]
        is_template_member = bool(
            re.search(r"\b\w+\s*<[^>]*>\s*::", def_line)
            or (
                def_idx > 0
                and re.match(r"\s*template\s*<", lines[def_idx - 1])
                and "::" in def_line
            )
        )
        if not is_template_member:
            return out
        head = str(symbol or "").split("(", 1)[0].strip()
        if not (
            SymbolLocator.parse_template_qualified_symbol(symbol)
            or SymbolLocator.parse_qualified_member_symbol(head)
        ):
            return out
        start_idx = def_idx
        if def_idx > 0 and re.match(r"\s*template\s*<", lines[def_idx - 1]):
            start_idx = def_idx - 1
        body_lines = cls._extract_brace_block_lines(lines, def_idx)
        if not body_lines:
            return out
        snippet_lines: List[str] = []
        if start_idx < def_idx:
            snippet_lines.append(lines[start_idx])
        snippet_lines.extend(body_lines)
        out = dict(out)
        out["snippet"] = snippet_lines
        out["function_name"] = def_line.strip()
        out["snippet_start_line"] = start_idx + 1
        out["snippet_end_line"] = start_idx + len(snippet_lines)
        return out

    @classmethod
    def _template_symbol_matches_text(cls, symbol: str, *texts: str) -> bool:
        from services.code_locator import SymbolLocator

        parsed = SymbolLocator.parse_template_qualified_symbol(symbol)
        if not parsed:
            return False
        template_class = str(parsed.get("template_class") or "")
        member = str(parsed.get("member") or "")
        if not template_class or not member:
            return False
        pattern = rf"\b{re.escape(template_class)}\s*<[^>]*>\s*::\s*{re.escape(member)}\s*\("
        for candidate in texts:
            text = str(candidate or "").strip()
            if text and re.search(pattern, text):
                return True
        return False

    @classmethod
    def _bare_template_class_method_matches_text(cls, symbol: str, *texts: str) -> bool:
        """CVList::RemoveAll 等未写模板参数的限定符号，匹配 CVList<T>::RemoveAll 实现。"""
        from services.code_locator import SymbolLocator

        head = str(symbol or "").split("(", 1)[0].strip()
        if "<" in head:
            return False
        parsed = SymbolLocator.parse_qualified_member_symbol(head)
        if not parsed:
            return False
        scope = str(parsed.get("short_scope") or "")
        method = str(parsed.get("method") or "")
        if not scope or not method or not scope[0].isupper() or scope.startswith("m_"):
            return False
        pattern = rf"\b{re.escape(scope)}\s*<[^>]*>\s*::\s*{re.escape(method)}\s*\("
        for candidate in texts:
            text = str(candidate or "").strip()
            if text and re.search(pattern, text):
                return True
        return False

    @classmethod
    def _resolved_function_matches_symbol(
        cls,
        symbol: str,
        file_path: str,
        line_number: int,
        function_signature: str = "",
        snippet_text: str = "",
    ) -> bool:
        """校验定位结果是否与请求的符号类型一致（限定名、ctor/dtor）。"""
        from services.code_locator import SymbolLocator

        lines = cls._read_source_lines(file_path)
        line = ""
        if 0 < line_number <= len(lines):
            line = lines[line_number - 1]
        sig = str(function_signature or line)
        norm_symbol = cls._normalize_context_request_symbol(symbol, "function")
        if SymbolLocator.parse_template_qualified_symbol(norm_symbol):
            return cls._template_symbol_matches_text(norm_symbol, line, snippet_text)
        if cls._template_symbol_matches_text(norm_symbol, sig, line):
            return True
        if cls._bare_template_class_method_matches_text(
            norm_symbol, sig, line, snippet_text
        ):
            return True
        head = str(norm_symbol or "").split("(", 1)[0]
        if "::" in head:
            if not SymbolLocator.qualified_symbol_matches_line(norm_symbol, line, sig):
                if cls._is_destructor_symbol(norm_symbol):
                    m = re.search(r"::~(\w+)", norm_symbol)
                    if m:
                        class_name = m.group(1)
                        return bool(re.search(rf"::~\s*{re.escape(class_name)}\s*\(", sig))
                return False
        if cls._is_destructor_symbol(norm_symbol):
            m = re.search(r"::~(\w+)", norm_symbol)
            if m:
                class_name = m.group(1)
                return bool(re.search(rf"::~\s*{re.escape(class_name)}\s*\(", sig))
        return True

    @classmethod
    def _resolved_snippet_matches_symbol(
        cls, symbol: str, snippet_text: str, function_signature: str = ""
    ) -> bool:
        """限定成员符号时，要求片段或签名中出现 Class::method。"""
        from services.code_locator import SymbolLocator

        if cls._template_symbol_matches_text(symbol, snippet_text, function_signature):
            return True
        if cls._bare_template_class_method_matches_text(
            symbol, function_signature, snippet_text
        ):
            return True
        head = str(symbol or "").split("(", 1)[0]
        if "::" not in head:
            return True
        for candidate in (function_signature, snippet_text):
            text = str(candidate or "").strip()
            if text and SymbolLocator.qualified_symbol_matches_line(symbol, text):
                return True
        return False

    @staticmethod
    def _context_request_symbol_leaf(symbol: str) -> str:
        raw = str(symbol or "").strip()
        if "::" in raw:
            raw = raw.rsplit("::", 1)[-1]
        return raw.strip()

    @classmethod
    def _looks_like_member_field_request(cls, symbol: str) -> bool:
        leaf = cls._context_request_symbol_leaf(symbol)
        # 当前工程中成员变量/函数指针大多以 m_ 命名；作为 function 请求时应要求改用 field/references。
        return bool(re.match(r"^m_[A-Za-z_]\w*$", leaf))

    @staticmethod
    def _reject_unavailable_context_request(
        req: Dict[str, Any],
        *,
        symbol: str,
        file_path: str,
        line_number: int,
    ) -> Optional[str]:
        """拒绝明显不可机器定位的 context_requests，避免误命中无关源码。"""
        if file_path and line_number > 0:
            return None
        symbol = str(symbol or "").strip()
        if not symbol:
            return None
        # 典型误用：把线程名/进程名/包名（如 com.anjuke.home）臆造成 C++ 符号。
        if BaseCrashAnalysisWorkflow._looks_like_package_or_thread_label(symbol):
            return (
                f"拒绝请求: `{symbol}` 看起来是线程名/进程名/包名标签，"
                "不是可解析的源码符号；请作为缺失证据/人工补充项处理。"
            )
        if re.match(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+::", symbol):
            prefix = symbol.split("::", 1)[0]
            return (
                f"拒绝请求: `{symbol}` 看起来由线程名/进程名/包名 `{prefix}` "
                "臆造成函数符号；当前无法机器定位该上下文，请作为缺失证据/人工补充项处理。"
            )
        # 裸 main 在大型仓库中高度歧义；没有 file+line 时容易命中无关工具程序入口。
        if symbol in {"main", "::main"}:
            return (
                "拒绝请求: `main` 未提供文件或行号，仓库内可能存在多个无关入口函数；"
                "请作为缺失证据/人工补充项处理，或提供明确 file+line。"
            )
        if symbol in {"Activity::onCreate", "Application::onCreate"}:
            return (
                f"拒绝请求: `{symbol}` 是无日志/栈帧证据支持的入口函数猜测；"
                "当前无法机器定位该上下文，请作为缺失证据/人工补充项处理。"
            )
        return None

    @staticmethod
    def _iter_context_search_files(
        code_roots: List[str],
        *,
        max_files: int = 5000,
    ) -> List[str]:
        config = LocatorConfig(max_code_length=0)
        files: List[str] = []
        seen: Set[str] = set()
        for root in code_roots:
            root_path = Path(str(root or "")).expanduser()
            if not root_path.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in config.exclude_dirs and not d.startswith(".")
                ]
                for name in filenames:
                    path = Path(dirpath) / name
                    if path.suffix not in config.supported_extensions:
                        continue
                    try:
                        if path.stat().st_size > config.max_file_size:
                            continue
                        resolved = str(path.resolve())
                    except Exception:
                        continue
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    files.append(resolved)
                    if len(files) >= max_files:
                        return files
        return files

    @staticmethod
    def _parse_qualified_field_symbol(symbol: str) -> Optional[Dict[str, str]]:
        """解析 Class::field / Ns::Class::field 形式的字段符号。"""
        head = str(symbol or "").strip()
        if not head:
            return None
        paren_idx = head.find("(")
        if paren_idx > 0:
            head = head[:paren_idx].strip()
        if "::" not in head:
            return None
        parts = [p.strip() for p in head.split("::") if p.strip()]
        if len(parts) < 2:
            return None
        return {
            "class_name": parts[-2],
            "field": parts[-1],
            "scope": "::".join(parts[:-1]),
        }

    @staticmethod
    def _field_context_path_score(
        file_path: str,
        class_name: str = "",
        *,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> int:
        path = str(file_path or "").replace("\\", "/").lower()
        score = 0
        if any(
            token in path
            for token in ("/demo/", "/test/", "/apptest/", "/unittest/", "/systemtest/", "/enginetest/")
        ):
            score -= 120
        if any(token in path for token in ("/support/", "/huiwei")):
            score -= 150
        if any(token in path for token in ("/engine-dev/", "/src/app/", "/src/", "/inc/")):
            score += 80
        if "/basemap/vmap/" in path:
            score += 50
        elif "/basemap/gmap/" in path:
            score -= 30
        if path.endswith(".h") or path.endswith(".hpp") or path.endswith(".hh"):
            score += 60
        elif path.endswith(".cpp") or path.endswith(".cc") or path.endswith(".cxx"):
            score += 20
        if path.endswith("vtempl.h"):
            score += 40
        if class_name:
            stem = class_name.lower().lstrip("cv").lstrip("c")
            base = class_name.lower()
            file_name = path.rsplit("/", 1)[-1]
            if base in file_name or (stem and stem in file_name):
                score += 100
        for cls_name in stack_priority_classes or []:
            token = str(cls_name or "").strip().lower()
            if token and token in path:
                score += 70
        return score

    @classmethod
    def _classify_field_match_kind(cls, line: str, field: str) -> Optional[str]:
        stripped = str(line or "").strip()
        if not stripped or not re.search(rf"\b{re.escape(field)}\b", stripped):
            return None
        if re.search(rf"\b{re.escape(field)}\s*;", stripped) and not re.search(
            rf"\b{re.escape(field)}\s*=", stripped
        ):
            return "declaration"
        if re.search(rf"\b{re.escape(field)}\s*\(", stripped) and (
            ":" in stripped or stripped.endswith(")") or stripped.endswith("),")
        ):
            return "initialization"
        if re.search(rf"\b{re.escape(field)}\s*=", stripped) or re.search(
            rf"\b{re.escape(field)}\s*[+\-]{2}", stripped
        ):
            return "usage"
        if re.search(rf"\bif\s*\([^)]*\b{re.escape(field)}\b", stripped):
            return "usage"
        if re.search(rf"\b{re.escape(field)}\b\s*[,;=)]", stripped):
            return "usage"
        return "usage"

    @staticmethod
    def _field_match_kind_label(kind: str) -> str:
        return {
            "declaration": "成员声明",
            "initialization": "初始化",
            "usage": "读写/使用",
            "class_declaration": "类声明",
        }.get(str(kind or ""), str(kind or ""))

    @classmethod
    def _looks_like_type_name_field_request(cls, symbol: str) -> bool:
        raw = str(symbol or "").strip()
        if not raw or "::" in raw or raw.startswith("m_"):
            return False
        return bool(re.match(r"^[A-Z][A-Za-z0-9_]*$", raw))

    @classmethod
    def _extract_class_declaration_span(
        cls, lines: List[str], line_number: int
    ) -> Tuple[int, int]:
        idx = max(0, line_number - 1)
        start_idx = idx
        if idx > 0 and re.match(r"\s*template\s*<", lines[idx - 1]):
            start_idx = idx - 1
        depth = 0
        started = False
        end_idx = idx
        for i in range(start_idx, min(len(lines), start_idx + 100)):
            line = lines[i]
            end_idx = i
            if "{" in line:
                started = True
            depth += line.count("{") - line.count("}")
            if started and depth <= 0:
                break
            if not started and line.strip().endswith(";") and i > idx:
                break
        return start_idx + 1, end_idx + 1

    @classmethod
    def _find_class_declaration_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        max_matches: int = 2,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        class_name = str(symbol or "").strip()
        if not class_name:
            return []
        class_re = re.compile(rf"\b(?:class|struct)\s+{re.escape(class_name)}\b")
        ctor_re = re.compile(
            rf"\b{re.escape(class_name)}\s*::\s*{re.escape(class_name)}\s*\("
        )
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        files = cls._iter_context_search_files(code_roots)
        files.sort(
            key=lambda fp: -cls._field_context_path_score(
                fp, class_name, stack_priority_classes=stack_priority_classes
            )
        )
        for file_path in files:
            lines = cls._read_source_lines(file_path)
            if not lines:
                continue
            path_score = cls._field_context_path_score(
                file_path, class_name, stack_priority_classes=stack_priority_classes
            )
            for idx, line in enumerate(lines, start=1):
                if ctor_re.search(line) or not class_re.search(line):
                    continue
                start, end = cls._extract_class_declaration_span(lines, idx)
                ranked.append(
                    (
                        path_score,
                        {
                            "file": file_path,
                            "line_number": idx,
                            "line_text": lines[idx - 1].strip(),
                            "match_kind": "class_declaration",
                            "match_kind_label": cls._field_match_kind_label(
                                "class_declaration"
                            ),
                            "context_start_line": start,
                            "context_end_line": end,
                            "context": lines[start - 1 : end],
                        },
                    )
                )
        ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
        return [entry for _, entry in ranked[:max_matches]]

    @classmethod
    def _file_declares_class(cls, lines: List[str], class_name: str) -> bool:
        if not class_name or not lines:
            return False
        pattern = re.compile(rf"\b(?:class|struct)\s+{re.escape(class_name)}\b")
        return any(pattern.search(line) for line in lines)

    @classmethod
    def _build_field_match_entry(
        cls,
        *,
        file_path: str,
        lines: List[str],
        line_number: int,
        match_kind: str,
        context_radius: int = 2,
    ) -> Dict[str, Any]:
        stripped = lines[line_number - 1].strip()
        if match_kind == "class_declaration":
            start, end = cls._extract_class_declaration_span(lines, line_number)
            return {
                "file": file_path,
                "line_number": line_number,
                "line_text": stripped,
                "match_kind": match_kind,
                "match_kind_label": cls._field_match_kind_label(match_kind),
                "context_start_line": start,
                "context_end_line": end,
                "context": lines[start - 1 : end],
            }
        if match_kind == "declaration":
            context_radius = 4
        start = max(1, line_number - context_radius)
        end = min(len(lines), line_number + context_radius)
        return {
            "file": file_path,
            "line_number": line_number,
            "line_text": stripped,
            "match_kind": match_kind,
            "match_kind_label": cls._field_match_kind_label(match_kind),
            "context_start_line": start,
            "context_end_line": end,
            "context": lines[start - 1:end],
        }

    @classmethod
    def _find_template_field_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        max_matches: int = 2,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        from services.code_locator import SymbolLocator

        parsed = SymbolLocator.parse_template_qualified_symbol(symbol)
        if not parsed:
            return []
        template_class = str(parsed.get("template_class") or "")
        field = str(parsed.get("member") or "")
        if not template_class or not field:
            return []
        class_re = re.compile(rf"\b(?:class|struct)\s+{re.escape(template_class)}\b")
        token_re = re.compile(rf"\b{re.escape(field)}\b")
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        files = cls._iter_context_search_files(code_roots)
        files.sort(
            key=lambda fp: -cls._field_context_path_score(
                fp, template_class, stack_priority_classes=stack_priority_classes
            )
        )
        for file_path in files:
            lines = cls._read_source_lines(file_path)
            if not lines or not class_re.search("\n".join(lines)):
                continue
            path_score = cls._field_context_path_score(
                file_path, template_class, stack_priority_classes=stack_priority_classes
            )
            for idx, line in enumerate(lines, start=1):
                if not token_re.search(line):
                    continue
                match_kind = cls._classify_field_match_kind(line, field)
                if match_kind != "declaration":
                    continue
                entry = cls._build_field_match_entry(
                    file_path=file_path,
                    lines=lines,
                    line_number=idx,
                    match_kind="declaration",
                )
                ranked.append((path_score, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
        return [entry for _, entry in ranked[:max_matches]]

    @classmethod
    def _collect_field_match_candidates(
        cls,
        *,
        field: str,
        class_name: str,
        code_roots: List[str],
        allowed_kinds: Optional[Set[str]] = None,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Tuple[int, Dict[str, Any]]]:
        if not field:
            return []
        token_re = re.compile(rf"\b{re.escape(field)}\b")
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        kind_weight = {"declaration": 200, "initialization": 80, "usage": 20}
        files = cls._iter_context_search_files(code_roots)
        files.sort(
            key=lambda fp: -cls._field_context_path_score(
                fp, class_name, stack_priority_classes=stack_priority_classes
            )
        )
        for file_path in files:
            lines = cls._read_source_lines(file_path)
            if not lines:
                continue
            if class_name and not cls._file_declares_class(lines, class_name):
                continue
            path_score = cls._field_context_path_score(
                file_path, class_name, stack_priority_classes=stack_priority_classes
            )
            for idx, line in enumerate(lines, start=1):
                if not token_re.search(line):
                    continue
                match_kind = cls._classify_field_match_kind(line, field)
                if not match_kind:
                    continue
                if allowed_kinds is not None and match_kind not in allowed_kinds:
                    continue
                entry = cls._build_field_match_entry(
                    file_path=file_path,
                    lines=lines,
                    line_number=idx,
                    match_kind=match_kind,
                )
                score = path_score + kind_weight.get(match_kind, 0) - idx // 10000
                ranked.append((score, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
        return ranked

    @classmethod
    def _find_field_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        max_matches: int = 4,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        from services.code_locator import SymbolLocator

        if cls._looks_like_type_name_field_request(symbol):
            class_matches = cls._find_class_declaration_context_matches(
                symbol,
                code_roots,
                max_matches=max_matches,
                stack_priority_classes=stack_priority_classes,
            )
            if class_matches:
                return class_matches
        template_matches = cls._find_template_field_context_matches(
            symbol,
            code_roots,
            max_matches=max_matches,
            stack_priority_classes=stack_priority_classes,
        )
        if template_matches:
            return template_matches
        parsed = cls._parse_qualified_field_symbol(symbol)
        field = parsed["field"] if parsed else cls._context_request_symbol_leaf(symbol)
        class_name = str(parsed.get("class_name") or "") if parsed else ""
        if not field:
            return []
        if not class_name:
            max_matches = min(max_matches, 1)

        if class_name:
            ranked = cls._collect_field_match_candidates(
                field=field,
                class_name=class_name,
                code_roots=code_roots,
                allowed_kinds={"declaration"},
                stack_priority_classes=stack_priority_classes,
            )
            if not ranked:
                ranked = cls._collect_field_match_candidates(
                    field=field,
                    class_name=class_name,
                    code_roots=code_roots,
                    allowed_kinds={"initialization"},
                    stack_priority_classes=stack_priority_classes,
                )
        else:
            ranked = cls._collect_field_match_candidates(
                field=field,
                class_name="",
                code_roots=code_roots,
                allowed_kinds={"declaration", "initialization"},
                stack_priority_classes=stack_priority_classes,
            )

        matches: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, int, str]] = set()
        for _, entry in ranked:
            key = (entry["file"], int(entry["line_number"]), str(entry.get("match_kind")))
            if key in seen:
                continue
            seen.add(key)
            matches.append(entry)
            if len(matches) >= max_matches:
                break
        return matches

    @staticmethod
    def _extract_stack_priority_classes(problem: Optional[Dict[str, Any]]) -> List[str]:
        """从 problem / 解析结果中提取栈上业务类名，用于字段/引用消歧。"""
        classes: List[str] = []
        seen: Set[str] = set()
        if not isinstance(problem, dict):
            return classes

        def _add(name: str) -> None:
            token = str(name or "").strip()
            if not token or token in seen:
                return
            seen.add(token)
            classes.append(token)

        for key in ("stack_priority_classes", "crash_priority_classes"):
            raw = problem.get(key)
            if isinstance(raw, list):
                for item in raw:
                    _add(str(item))

        for blob_key in ("parsed_stack", "symbolized_stack", "resolve_stack"):
            blob = problem.get(blob_key)
            if not isinstance(blob, dict):
                continue
            for thread in blob.get("threads", []) or []:
                if not isinstance(thread, dict):
                    continue
                for frame in thread.get("frames", []) or []:
                    if not isinstance(frame, dict):
                        continue
                    for token_key in ("function", "symbol", "raw_symbol"):
                        fn = str(frame.get(token_key) or "")
                        for m in re.finditer(r"\b([A-Z][A-Za-z0-9_]*(?:::[A-Za-z0-9_~]+)*)\b", fn):
                            head = m.group(1).split("::")[0]
                            if head.endswith("Layer") or head.endswith("Control") or head.startswith("C"):
                                _add(head)
        return classes[:12]

    @classmethod
    def _find_reference_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        max_matches: int = 12,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        member_method = cls._parse_member_method_reference(symbol)
        if member_method:
            member = member_method["member"]
            method = member_method["method"]
            literal_re = re.compile(
                rf"\b{re.escape(member)}\s*\.\s*{re.escape(method)}\s*\("
            )
            method_re = re.compile(
                rf"\.{re.escape(method)}\s*\(|\b{re.escape(method)}\s*\("
            )
            ranked: List[Tuple[int, Dict[str, Any]]] = []
            files = cls._iter_context_search_files(code_roots)
            files.sort(
                key=lambda fp: -cls._field_context_path_score(
                    fp, "", stack_priority_classes=stack_priority_classes
                )
            )
            for file_path in files:
                lines = cls._read_source_lines(file_path)
                if not lines:
                    continue
                path_score = cls._field_context_path_score(
                    file_path, "", stack_priority_classes=stack_priority_classes
                )
                for idx, line in enumerate(lines, start=1):
                    stripped = line.strip()
                    if literal_re.search(stripped):
                        boost = 120
                    elif method_re.search(stripped) and member in stripped:
                        boost = 80
                    elif method_re.search(stripped):
                        boost = 20
                    else:
                        continue
                    entry = cls._build_field_match_entry(
                        file_path=file_path,
                        lines=lines,
                        line_number=idx,
                        match_kind="usage",
                    )
                    ranked.append((path_score + boost - idx // 10000, entry))
            ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
            matches: List[Dict[str, Any]] = []
            seen: Set[Tuple[str, int]] = set()
            for _, entry in ranked:
                key = (entry["file"], int(entry["line_number"]))
                if key in seen:
                    continue
                seen.add(key)
                matches.append(entry)
                if len(matches) >= max_matches:
                    break
            if matches:
                return matches

        return cls._find_text_context_matches(
            symbol,
            code_roots,
            mode="references",
            max_matches=max_matches,
            stack_priority_classes=stack_priority_classes,
            parse_member_method=False,
        )

    @classmethod
    def _find_text_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        mode: str,
        max_matches: int = 8,
        stack_priority_classes: Optional[List[str]] = None,
        parse_member_method: bool = True,
    ) -> List[Dict[str, Any]]:
        if mode == "field":
            return cls._find_field_context_matches(
                symbol,
                code_roots,
                max_matches=max_matches,
                stack_priority_classes=stack_priority_classes,
            )
        leaf = cls._context_request_symbol_leaf(symbol)
        if not leaf:
            return []
        parsed = cls._parse_qualified_field_symbol(symbol)
        class_name = str(parsed.get("class_name") or "") if parsed else ""
        token_re = re.compile(rf"\b{re.escape(leaf)}\b")
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        files = cls._iter_context_search_files(code_roots)
        files.sort(
            key=lambda fp: -cls._field_context_path_score(
                fp, class_name, stack_priority_classes=stack_priority_classes
            )
        )
        for file_path in files:
            lines = cls._read_source_lines(file_path)
            if not lines:
                continue
            if class_name and not cls._file_declares_class(lines, class_name):
                continue
            path_score = cls._field_context_path_score(
                file_path, class_name, stack_priority_classes=stack_priority_classes
            )
            for idx, line in enumerate(lines, start=1):
                if not token_re.search(line):
                    continue
                match_kind = cls._classify_field_match_kind(line, leaf) or "usage"
                entry = cls._build_field_match_entry(
                    file_path=file_path,
                    lines=lines,
                    line_number=idx,
                    match_kind=match_kind,
                )
                ranked.append((path_score - idx // 10000, entry))
                if len(ranked) >= max_matches * 4:
                    break
        ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
        matches: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, int]] = set()
        for _, entry in ranked:
            key = (entry["file"], int(entry["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            matches.append(entry)
            if len(matches) >= max_matches:
                break
        return matches

    @classmethod
    def _resolve_non_function_context_request(
        cls,
        req: Dict[str, Any],
        req_type: str,
        symbol: str,
        code_roots: List[str],
        locator: CodeLocatorService,
        *,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if req_type == "field":
            matches = cls._find_text_context_matches(
                symbol,
                code_roots,
                mode="field",
                max_matches=4,
                stack_priority_classes=stack_priority_classes,
            )
            if matches:
                return {
                    "request": req,
                    "success": True,
                    "context_type": "field",
                    "symbol": symbol,
                    "matches": matches,
                }
            return {
                "request": req,
                "success": False,
                "context_type": "field",
                "error": f"未定位到字段声明/初始化: {symbol}",
            }
        if req_type == "references":
            matches = cls._find_reference_context_matches(
                symbol,
                code_roots,
                max_matches=12,
                stack_priority_classes=stack_priority_classes,
            )
            if matches:
                return {
                    "request": req,
                    "success": True,
                    "context_type": "references",
                    "symbol": symbol,
                    "matches": matches,
                }
            return {
                "request": req,
                "success": False,
                "context_type": "references",
                "error": f"未定位到引用: {symbol}",
            }
        if req_type == "callers":
            if cls._is_destructor_symbol(symbol):
                norm = cls._normalize_context_request_symbol(symbol, "function")
                found = locator.find_function_definition_for_symbol(norm, code_roots)
                if found:
                    out = SnippetExtractorTool().execute(
                        {
                            "file_path": found[0],
                            "line_number": int(found[1]),
                            "function_name": norm,
                            "max_code_length": 0,
                        }
                    )
                    if not out.get("error"):
                        return {
                            "request": req,
                            "success": True,
                            "context_type": "callers",
                            "symbol": symbol,
                            "matches": [
                                {
                                    "name": out.get("function_name") or norm,
                                    "file": out.get("file_path") or found[0],
                                    "parent_fun": "",
                                    "snippet": out.get("snippet")
                                    if isinstance(out.get("snippet"), list)
                                    else [],
                                    "note": "callers 请求命中析构函数，已回填析构实现源码。",
                                }
                            ],
                        }
            simple = locator.symbol_locator.extract_simple_function_name(symbol) or symbol
            callers = locator.find_callers(simple, code_roots, max_search_files=600)[:8]
            if callers:
                return {
                    "request": req,
                    "success": True,
                    "context_type": "callers",
                    "symbol": symbol,
                    "matches": [
                        {
                            "name": c.name,
                            "file": c.file,
                            "parent_fun": c.parent_fun,
                            "snippet": c.snippet,
                        }
                        for c in callers
                    ],
                }
            return {
                "request": req,
                "success": False,
                "context_type": "callers",
                "error": f"未定位到调用方: {symbol}",
            }
        return {
            "request": req,
            "success": False,
            "error": (
                f"不支持的 context request type: {req_type}; "
                "支持 function/field/references/callers"
            ),
        }

    @classmethod
    def _resolve_context_requests(
        cls,
        requests: List[Dict[str, Any]],
        code_roots: List[str],
        *,
        max_requests: int = 5,
        seen_keys: Optional[Set[str]] = None,
        request_outcomes: Optional[Dict[str, str]] = None,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """按模型请求补充函数源码。当前仅支持 file+line 或 symbol/function 定位。"""
        # seen_keys 保留兼容；新逻辑使用 request_outcomes 记录 success/failed/rejected。
        outcomes = request_outcomes if isinstance(request_outcomes, dict) else {}
        if isinstance(seen_keys, set) and seen_keys and not outcomes:
            for legacy_key in seen_keys:
                outcomes.setdefault(str(legacy_key), "success")
        locator = CodeLocatorService(LocatorConfig(max_code_length=0))
        extractor = SnippetExtractorTool()
        resolved: List[Dict[str, Any]] = []
        for req in requests[: max(0, max_requests)]:
            if not isinstance(req, dict):
                continue
            req_type = str(req.get("type") or "function").strip().lower()
            symbol = cls._normalize_context_request_symbol(
                str(req.get("symbol") or "").strip(), req_type
            )
            file_path = str(req.get("file") or "").strip()
            line_number = int(req.get("line_number") or 0)
            key = cls._context_request_outcome_key(req_type, symbol, file_path, line_number)
            success_keys = cls._context_request_success_dedupe_keys(
                req_type, symbol, file_path, line_number
            )
            if any(outcomes.get(alias) == "success" for alias in success_keys):
                resolved.append(
                    {
                        "request": req,
                        "success": False,
                        "skipped": True,
                        "skip_reason": "duplicate_request",
                        "error": f"重复请求，已在前序轮次成功补充: {symbol or file_path}",
                    }
                )
                continue
            if key and key in outcomes:
                prior = outcomes[key]
                if prior == "success":
                    resolved.append(
                        {
                            "request": req,
                            "success": False,
                            "skipped": True,
                            "skip_reason": "duplicate_request",
                            "error": f"重复请求，已在前序轮次成功补充: {symbol or file_path}",
                        }
                    )
                elif prior == "rejected":
                    resolved.append(
                        {
                            "request": req,
                            "success": False,
                            "rejected": True,
                            "reject_reason": "duplicate_rejected",
                            "error": f"重复请求，此前已判定不可用: {symbol or file_path}",
                        }
                    )
                else:
                    resolved.append(
                        {
                            "request": req,
                            "success": False,
                            "lookup_exhausted": True,
                            "error": (
                                f"此前已尝试但未定位，Agent 无法自动补充: "
                                f"{symbol or file_path}"
                            ),
                        }
                    )
                continue

            target_file = ""
            target_line = 0
            function_name = symbol
            error = ""
            reject_reason = cls._reject_unavailable_context_request(
                req,
                symbol=symbol,
                file_path=file_path,
                line_number=line_number,
            )
            if reject_reason:
                resolved.append(
                    {
                        "request": req,
                        "success": False,
                        "error": reject_reason,
                        "rejected": True,
                        "reject_reason": "unavailable_context",
                    }
                )
                cls._record_context_request_outcome(
                    outcomes, req_type, symbol, file_path, line_number, "rejected"
                )
                continue
            if req_type != "function":
                item = cls._resolve_non_function_context_request(
                    req,
                    req_type,
                    symbol,
                    code_roots,
                    locator,
                    stack_priority_classes=stack_priority_classes,
                )
                resolved.append(item)
                if item.get("success"):
                    cls._record_context_request_outcome(
                        outcomes, req_type, symbol, file_path, line_number, "success"
                    )
                elif item.get("rejected"):
                    cls._record_context_request_outcome(
                        outcomes, req_type, symbol, file_path, line_number, "rejected"
                    )
                else:
                    cls._record_context_request_outcome(
                        outcomes, req_type, symbol, file_path, line_number, "failed"
                    )
                continue
            if file_path and line_number > 0 and Path(file_path).is_file():
                target_file = str(Path(file_path).expanduser().resolve())
                target_line = line_number
            elif symbol:
                if cls._looks_like_member_field_request(symbol):
                    resolved.append(
                        {
                            "request": req,
                            "success": False,
                            "rejected": True,
                            "reject_reason": "type_mismatch",
                            "error": (
                                f"拒绝请求: `{symbol}` 看起来是成员变量/字段，不是函数；"
                                "请改用 type=field 获取声明/初始化，或 type=references 获取读写引用。"
                            ),
                        }
                    )
                    cls._record_context_request_outcome(
                        outcomes, req_type, symbol, file_path, line_number, "rejected"
                    )
                    continue
                found = locator.find_function_definition_for_symbol(symbol, code_roots)
                if found:
                    target_file, target_line = found[0], int(found[1])
                else:
                    error = f"未定位到函数定义: {symbol}"
            else:
                error = "缺少 symbol/function 或 file+line"

            if not target_file or target_line <= 0:
                item = {"request": req, "success": False, "error": error or "定位失败"}
                resolved.append(item)
                cls._record_context_request_outcome(
                    outcomes, req_type, symbol, file_path, line_number, "failed"
                )
                continue

            out = extractor.execute(
                {
                    "file_path": target_file,
                    "line_number": target_line,
                    "function_name": function_name,
                    "max_code_length": 0,
                }
            )
            if out.get("error"):
                item = {"request": req, "success": False, "error": str(out.get("error"))}
                resolved.append(item)
                cls._record_context_request_outcome(
                    outcomes, req_type, symbol, file_path, line_number, "failed"
                )
                continue
            out = cls._repair_template_function_snippet(
                symbol=symbol,
                target_file=target_file,
                target_line=target_line,
                out=out,
            )
            resolved_sig = str(out.get("function_name") or function_name or "")
            snippet_lines = out.get("snippet") if isinstance(out.get("snippet"), list) else []
            snippet_text = "\n".join(str(line) for line in snippet_lines)
            if not cls._resolved_function_matches_symbol(
                symbol, target_file, target_line, resolved_sig, snippet_text
            ) or not cls._resolved_snippet_matches_symbol(symbol, snippet_text, resolved_sig):
                item = {
                    "request": req,
                    "success": False,
                    "error": (
                        f"定位结果与请求符号不一致（疑似误命中构造函数/其它同名函数）: "
                        f"{symbol}"
                    ),
                }
                resolved.append(item)
                cls._record_context_request_outcome(
                    outcomes, req_type, symbol, file_path, line_number, "failed"
                )
                continue
            resolved.append(
                {
                    "request": req,
                    "success": True,
                    "file": out.get("file_path") or target_file,
                    "function_signature": resolved_sig,
                    "snippet_start_line": out.get("snippet_start_line"),
                    "snippet_end_line": out.get("snippet_end_line"),
                    "snippet": out.get("snippet") if isinstance(out.get("snippet"), list) else [],
                    "strategy": out.get("strategy"),
                    "is_complete_function": out.get("is_complete_function"),
                }
            )
            cls._record_context_request_outcome(
                outcomes, req_type, symbol, file_path, line_number, "success"
            )
        return resolved

    @staticmethod
    def _strip_context_request_section(analysis_text: str) -> str:
        """续轮 prompt 中复用上一轮分析时，去掉末尾重复的 context_requests 展示。"""
        text = str(analysis_text or "").strip()
        if not text:
            return ""
        pattern = (
            r"\n+#{2,4}\s*需要\s*Agent\s*补充的上下文[^\n]*"
            r"\n+```(?:json)?\s*\n\{.*?\"context_requests\".*?\}\s*\n```"
            r"\s*$"
        )
        cleaned = re.sub(pattern, "", text, flags=re.DOTALL)
        return cleaned.strip()

    @staticmethod
    def _handled_context_item_label(item: Dict[str, Any]) -> str:
        req = item.get("request") if isinstance(item, dict) else {}
        req = req if isinstance(req, dict) else {}
        symbol = str(req.get("symbol") or req.get("file") or "未知符号").strip()
        req_type = str(req.get("type") or item.get("context_type") or "function").strip()
        return f"`{symbol}`（{req_type}）"

    @classmethod
    def _append_handled_context_summary(
        cls,
        lines: List[str],
        handled_context: List[Dict[str, Any]],
    ) -> None:
        if not handled_context:
            return
        buckets: Dict[str, List[str]] = {
            "success": [],
            "rejected": [],
            "skipped": [],
            "failed": [],
        }
        seen_labels: Set[Tuple[str, str]] = set()
        for item in handled_context:
            if not isinstance(item, dict):
                continue
            label = cls._handled_context_item_label(item)
            if item.get("success"):
                context_type = str(item.get("context_type") or "function")
                desc = {
                    "function": "已提供函数源码",
                    "field": "已提供字段声明/初始化相关上下文",
                    "references": "已提供引用/读写位置上下文",
                    "callers": "已提供调用方上下文",
                }.get(context_type, "已提供相关上下文")
                bucket = "success"
            elif item.get("lookup_exhausted"):
                desc = str(item.get("error") or "此前已尝试但未定位")
                bucket = "failed"
            elif item.get("rejected"):
                desc = str(item.get("error") or "请求已拒绝")
                bucket = "rejected"
            elif item.get("skipped"):
                desc = str(item.get("error") or "请求已跳过")
                bucket = "skipped"
            else:
                desc = str(item.get("error") or "已尝试但未定位")
                bucket = "failed"
            dedupe_key = (bucket, label)
            if dedupe_key in seen_labels:
                continue
            seen_labels.add(dedupe_key)
            buckets[bucket].append(f"- {label}：{desc}")

        if not any(buckets.values()):
            return

        lines.append("")
        lines.append("## 已处理的上下文请求")
        lines.append("以下上下文已经由 Agent 处理过，本轮不得再次放入 `context_requests`：")
        sections = [
            ("success", "### 已成功补充"),
            ("rejected", "### 已拒绝"),
            ("skipped", "### 已跳过"),
            ("failed", "### 已尝试但未定位"),
        ]
        for key, title in sections:
            if not buckets[key]:
                continue
            lines.append("")
            lines.append(title)
            lines.extend(buckets[key])

    @staticmethod
    def _context_request_exhausted(item: Dict[str, Any]) -> bool:
        if not isinstance(item, dict) or item.get("success"):
            return False
        if item.get("lookup_exhausted"):
            return True
        if item.get("rejected"):
            return True
        if item.get("skipped") and item.get("skip_reason") == "duplicate_request":
            return True
        return False

    @classmethod
    def _all_context_requests_blocked(cls, resolved_context: List[Dict[str, Any]]) -> bool:
        items = [x for x in resolved_context if isinstance(x, dict)]
        if not items:
            return False
        if any(bool(x.get("success")) for x in items):
            return False
        return all(cls._context_request_exhausted(x) for x in items)

    @classmethod
    def _context_request_dedupe_key(cls, item: Dict[str, Any]) -> str:
        req = item.get("request") if isinstance(item, dict) else {}
        req = req if isinstance(req, dict) else {}
        req_type = str(req.get("type") or item.get("context_type") or "function").strip().lower()
        symbol = str(req.get("symbol") or req.get("file") or "").strip()
        file_path = str(req.get("file") or "").strip()
        line_number = int(req.get("line_number") or 0)
        return cls._context_request_outcome_key(req_type, symbol, file_path, line_number)

    @classmethod
    def _merge_accumulated_context_items(
        cls,
        accumulated: List[Dict[str, Any]],
        new_items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for item in list(accumulated) + list(new_items):
            if not isinstance(item, dict):
                continue
            key = cls._context_request_dedupe_key(item)
            if not key:
                continue
            if key not in merged:
                order.append(key)
            merged[key] = item
        return [merged[k] for k in order if k in merged]

    @staticmethod
    def _resolved_context_status(item: Dict[str, Any]) -> str:
        if item.get("success"):
            return "located"
        if item.get("lookup_exhausted"):
            return "lookup_exhausted"
        if item.get("rejected"):
            return "rejected"
        if item.get("skipped"):
            return "duplicate_success"
        return "not_located"

    @classmethod
    def _build_pre_round_add_res(
        cls,
        *,
        source_round: int,
        target_round: int,
        resolved_context: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        requests_out: List[Dict[str, Any]] = []
        for item in resolved_context:
            if not isinstance(item, dict):
                continue
            req = item.get("request") if isinstance(item.get("request"), dict) else {}
            status = cls._resolved_context_status(item)
            req_type = str(req.get("type") or "function")
            expected_return_form = cls._normalize_expected_return_form(
                req_type,
                req.get("expected_return_form")
                or item.get("expected_return_form"),
            )
            entry: Dict[str, Any] = {
                "type": req_type,
                "symbol": str(req.get("symbol") or ""),
                "reason": str(req.get("reason") or ""),
                "priority": str(req.get("priority") or ""),
                "expected_return_form": expected_return_form,
                "expected_return_form_label": cls._return_form_label(expected_return_form),
                "fulfillment_note": str(req.get("fulfillment_note") or "") or None,
                "status": status,
                "located": bool(item.get("success")),
                "error": str(item.get("error") or "") or None,
            }
            actual_return_form = cls._infer_actual_return_form(item)
            if actual_return_form:
                entry["actual_return_form"] = actual_return_form
                entry["actual_return_form_label"] = cls._return_form_label(actual_return_form)
                matched = cls._fulfillment_matched(expected_return_form, actual_return_form)
                if matched is not None:
                    entry["fulfillment_matched"] = matched
            if item.get("success"):
                context_type = str(item.get("context_type") or "function")
                entry["context_type"] = context_type
                if context_type == "function":
                    entry["file"] = item.get("file")
                    entry["line_start"] = item.get("snippet_start_line")
                    entry["line_end"] = item.get("snippet_end_line")
                    entry["function_signature"] = item.get("function_signature")
                else:
                    entry["matches"] = item.get("matches")
            requests_out.append(entry)
        return {
            "source_round": int(source_round),
            "target_round": int(target_round),
            "requests": requests_out,
        }

    @classmethod
    def _format_extra_code_context_item_markdown(cls, item: Dict[str, Any]) -> str:
        req = item.get("request") if isinstance(item.get("request"), dict) else {}
        symbol = str(req.get("symbol") or req.get("file") or "未知符号").strip()
        req_type = str(req.get("type") or item.get("context_type") or "function").strip()
        title = f"{symbol}（{req_type}）"
        lines: List[str] = [f"#### 其它代码上下文: {title}"]
        expected_form = cls._normalize_expected_return_form(
            req_type,
            req.get("expected_return_form") or item.get("expected_return_form"),
        )
        lines.append(
            f"- 期望回填形式: {cls._return_form_label(expected_form)} (`{expected_form}`)"
        )
        if req.get("fulfillment_note"):
            lines.append(f"- 请求说明: {req.get('fulfillment_note')}")

        if not item.get("success"):
            status = cls._resolved_context_status(item)
            status_label = {
                "located": "已定位",
                "not_located": "未定位",
                "lookup_exhausted": "此前已尝试未定位",
                "rejected": "已拒绝",
                "duplicate_success": "已成功补充（重复请求）",
            }.get(status, "未定位")
            lines.append(f"- 状态: {status_label}")
            if item.get("error"):
                lines.append(f"- 说明: {item.get('error')}")
            return "\n".join(lines)

        context_type = str(item.get("context_type") or "function")
        lines.append("- 状态: 已定位")
        if context_type in {"field", "references"}:
            if context_type == "field":
                lines.append("- 回填说明: 优先返回所属类的成员声明；若无声明则返回同类初始化；不包含调用链。")
            else:
                lines.append("- 回填说明: 返回符号在所属类相关文件中的读写/引用位置。")
            for m_idx, match in enumerate(item.get("matches", [])[:8], start=1):
                if not isinstance(match, dict):
                    continue
                if m_idx > 1:
                    lines.append("")
                if match.get("match_kind_label") or match.get("match_kind"):
                    label = match.get("match_kind_label") or match.get("match_kind")
                    lines.append(f"- 命中类型: {label}")
                lines.append(f"- 文件: {match.get('file')}")
                lines.append(f"- 行号: {match.get('line_number')}")
                if match.get("line_text"):
                    lines.append(f"- 命中行: {match.get('line_text')}")
                ctx = match.get("context")
                if isinstance(ctx, list) and ctx:
                    lines.append("- 代码片段:")
                    lines.append("```cpp")
                    lines.extend([str(x) for x in ctx])
                    lines.append("```")
            return "\n".join(lines)

        if context_type == "callers":
            for m_idx, match in enumerate(item.get("matches", [])[:8], start=1):
                if not isinstance(match, dict):
                    continue
                if m_idx > 1:
                    lines.append("")
                if match.get("name"):
                    lines.append(f"- 函数: {match.get('name')}")
                lines.append(f"- 文件: {match.get('file')}")
                snippet = match.get("snippet")
                if isinstance(snippet, list) and snippet:
                    lines.append("- 代码片段:")
                    lines.append("```cpp")
                    lines.extend([str(x) for x in snippet[:80]])
                    lines.append("```")
            return "\n".join(lines)

        lines.append(f"- 文件: {item.get('file')}")
        start_line = item.get("snippet_start_line")
        end_line = item.get("snippet_end_line")
        if start_line and end_line:
            lines.append(f"- 行号范围: {start_line}～{end_line}")
        sig = item.get("function_signature")
        if sig:
            lines.append(f"- 函数: {sig}")
        snippet = item.get("snippet")
        if isinstance(snippet, list) and snippet:
            lines.append("- 代码片段:")
            lines.append("```cpp")
            lines.extend([str(x) for x in snippet])
            lines.append("```")
        return "\n".join(lines)

    @classmethod
    def _format_extra_code_context_section(cls, accumulated_context: List[Dict[str, Any]]) -> str:
        blocks: List[str] = []
        for item in accumulated_context:
            if not isinstance(item, dict):
                continue
            block = cls._format_extra_code_context_item_markdown(item).strip()
            if block:
                blocks.append(block)
        if not blocks:
            return ""
        return "\n\n".join(blocks)

    @staticmethod
    def _inject_extra_code_context_into_base_prompt(
        base_prompt: str, extra_context_markdown: str
    ) -> str:
        text = str(base_prompt or "")
        extra = str(extra_context_markdown or "").strip()
        if not extra:
            return text
        section = f"## 其它代码上下文\n\n{extra}\n"
        for marker in ("### 崩溃所属类骨架", "# 崩溃分析任务"):
            pos = text.find(marker)
            if pos >= 0:
                return text[:pos].rstrip() + "\n\n" + section + "\n" + text[pos:].lstrip()
        return text.rstrip() + "\n\n" + section

    @staticmethod
    def _append_round_task_to_prompt(
        prompt: str,
        *,
        is_final_round: bool,
        early_final_reason: Optional[str] = None,
    ) -> str:
        lines: List[str] = ["## 本轮任务"]
        if is_final_round:
            if early_final_reason == "all_requests_blocked":
                lines.append(
                    "- 上一轮 `context_requests` 中的请求均已由 Agent 处理过（重复、拒绝或不可用），"
                    "无法继续补充新上下文。"
                )
            else:
                lines.append("- 当前已经达到允许的最大多轮次数，本轮必须输出最终分析。")
            lines.append("- 不得再请求 Agent 补充上下文；请将仍缺失的信息列为人工补充证据或排查建议。")
            lines.append(
                "- 末尾必须输出 Agent 上下文获取结束 JSON："
                '`{"agent_can_fetch_more": false, "context_requests": []}`。'
            )
            lines.append(
                "- 说明：`agent_can_fetch_more=false` 表示 Agent 自动化不再拉取上下文，"
                "不等于所有证据已齐全；仍缺的信息应写入「人工补充证据」。"
            )
        else:
            lines.append("- 先判断是否仍需要 Agent 继续补充上下文。")
            lines.append(
                "- 若仍需补充：只输出需要 Agent 补充什么及原因；不要输出最终分析报告、修复方案或修复代码。"
            )
            lines.append(
                "- 若证据足够或剩余缺口只能人工补充：输出完整最终分析，并将 `agent_can_fetch_more` 置为 false。"
            )
            lines.append(
                "- 不得重复请求「其它代码上下文」中状态为已定位 / 未定位 / 此前已尝试未定位 / 已拒绝的 symbol。"
            )
            lines.append(
                "- 只有当「其它代码上下文」中的新增源码直接引出新的高价值函数、字段或引用时，"
                "才继续提出下一轮 `context_requests`。"
            )
        task_block = "\n".join(lines)
        closing = "请基于以上信息"
        pos = prompt.rfind(closing)
        if pos >= 0:
            return prompt[:pos].rstrip() + "\n\n" + task_block + "\n\n" + prompt[pos:].lstrip()
        return prompt.rstrip() + "\n\n" + task_block

    @classmethod
    def _build_context_loop_prompt(
        cls,
        base_prompt: str,
        round_index: int,
        resolved_context: List[Dict[str, Any]],
        accumulated_context: Optional[List[Dict[str, Any]]] = None,
        *,
        is_final_round: bool = False,
        early_final_reason: Optional[str] = None,
    ) -> str:
        merged = (
            accumulated_context
            if isinstance(accumulated_context, list)
            else resolved_context
        )
        extra_md = cls._format_extra_code_context_section(merged)
        body = cls._inject_extra_code_context_into_base_prompt(base_prompt, extra_md)
        return cls._append_round_task_to_prompt(
            body,
            is_final_round=is_final_round,
            early_final_reason=early_final_reason,
        )

    @staticmethod
    def _apply_router_endpoint_to_context(context: WorkflowContext, endpoint: Any) -> None:
        """Swap context.llm to the given router endpoint."""
        if endpoint is None:
            return
        try:
            from tool_system.llm.llm_adapter import LLMAdapterFactory
            from tool_system.llm.llm_router import build_llm_config_for_endpoint

            cfg = build_llm_config_for_endpoint(endpoint, engine="direct")
            context.llm = LLMAdapterFactory.create(cfg.to_dict())
        except Exception as exc:
            logger.warning("Failed to switch LLM adapter for failover: %s", exc)

    @staticmethod
    def _prepare_router_for_round(
        context: WorkflowContext,
        problem: Optional[Dict[str, Any]],
        *,
        round_index: int,
    ) -> None:
        """Re-select tier/endpoint for this round when mode=auto."""
        if not isinstance(problem, dict):
            return
        state = problem.get("_llm_router_state")
        if state is None or getattr(state, "mode", "fixed") == "fixed":
            return
        try:
            from tool_system.llm.llm_router import re_resolve_tier
            from tool_system.llm.routing_policy import RoutingContext

            diag = problem.get("_crash_diagnosis")
            if not isinstance(diag, dict):
                diag = None
            ctx = RoutingContext(
                mode=str(getattr(state, "mode", "auto")),
                force_profile=getattr(state, "force_profile", None),
                prompt_mode=str(problem.get("prompt_mode") or "analysis"),
                apply_ai_fixes=bool(problem.get("apply_ai_fixes")),
                agent_loop=str(problem.get("agent_loop") or "single"),
                round_index=int(round_index),
                crash_diagnosis=diag,
            )
            selected = re_resolve_tier(state, ctx)
            if selected is not None:
                BaseCrashAnalysisWorkflow._apply_router_endpoint_to_context(context, selected)
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

    @staticmethod
    def _llm_call_with_retries(
        context: WorkflowContext,
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
        if prompt_cap_raw not in (None, ""):
            try:
                parsed = int(prompt_cap_raw)
                if parsed > 0:
                    prompt_cap = parsed
            except (TypeError, ValueError):
                prompt_cap = None
        prompt_used = prompt
        if prompt_cap and len(prompt_used) > prompt_cap:
            orig_len = len(prompt_used)
            prompt_used = _truncate_analysis_prompt(prompt_used, prompt_cap)
            logger.warning(
                "analysis_prompt too long (%s chars), smart-truncated to %s chars (max_prompt_chars=%s)",
                orig_len,
                len(prompt_used),
                prompt_cap,
            )

        BaseCrashAnalysisWorkflow._prepare_router_for_round(
            context, problem, round_index=round_index
        )

        llm_adapter = getattr(context, "llm", None)
        configured_max_tokens = 0
        try:
            configured_max_tokens = int(
                (getattr(llm_adapter, "max_tokens", 0) if llm_adapter is not None else 0) or 0
            )
        except Exception:
            configured_max_tokens = 0

        first_try_tokens = 8192
        if configured_max_tokens > 0:
            first_try_tokens = min(first_try_tokens, configured_max_tokens)

        token_attempts: List[Optional[int]] = []
        if first_try_tokens > 0:
            token_attempts.append(first_try_tokens)
        for candidate in [4096, 2048]:
            if candidate not in token_attempts and candidate < first_try_tokens:
                token_attempts.append(candidate)
        token_attempts.append(None)

        router_state = problem.get("_llm_router_state") if isinstance(problem, dict) else None
        max_endpoint_attempts = 1
        if router_state is not None and getattr(router_state, "failover_enabled", False):
            max_endpoint_attempts = max(1, len(getattr(router_state, "pool", []) or []) or 1)

        llm_response = None
        last_llm_exc: Optional[Exception] = None
        call_started = None
        for endpoint_attempt in range(max_endpoint_attempts):
            llm_response = None
            last_llm_exc = None
            import time as _time

            call_started = _time.perf_counter()
            for idx, tok in enumerate(token_attempts, start=1):
                try:
                    if tok is None:
                        logger.info("LLM attempt %s: default max_tokens", idx)
                        llm_response = context.call_llm(prompt_used, temperature=0)
                    else:
                        logger.info("LLM attempt %s: max_tokens=%s", idx, tok)
                        llm_response = context.call_llm(prompt_used, max_tokens=tok, temperature=0)
                    if tok is not None and llm_response is not None:
                        usage = getattr(llm_response, "usage", None) or {}
                        completion_tokens = usage.get("completion_tokens", 0) or 0
                        if completion_tokens >= tok * 0.95:
                            logger.warning(
                                "LLM output likely truncated (completion_tokens=%s, max_tokens=%s), retrying",
                                completion_tokens,
                                tok,
                            )
                            continue
                    break
                except Exception as llm_exc:
                    last_llm_exc = llm_exc
                    logger.warning(
                        "LLM attempt %s failed (max_tokens=%s): %s",
                        idx,
                        tok if tok is not None else "default",
                        llm_exc,
                    )
            duration_ms = int(round((_time.perf_counter() - call_started) * 1000))
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

            # Endpoint-level failure → optional failover
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
                    next_ep = failover_next(
                        router_state, cause=str(last_llm_exc or "llm_call_failed")
                    )
                    if next_ep is None:
                        break
                    BaseCrashAnalysisWorkflow._apply_router_endpoint_to_context(context, next_ep)
                    logger.warning(
                        "LLM failover to provider=%s model=%s",
                        next_ep.provider_key,
                        next_ep.model,
                    )
                    continue
                except Exception as fo_exc:
                    logger.debug("LLM failover skipped: %s", fo_exc)
                    break
            break

        if llm_response is None:
            raise RuntimeError(f"LLM call failed after retries: {last_llm_exc}")
        return llm_response, prompt_used

    @staticmethod
    def _round_label_cn(round_index: int) -> str:
        labels = ("第一轮", "第二轮", "第三轮", "第四轮", "第五轮", "第六轮", "第七轮", "第八轮")
        if 0 <= round_index < len(labels):
            return labels[round_index]
        return f"第{round_index + 1}轮"

    @classmethod
    def _llm_call_with_phase(
        cls,
        context: WorkflowContext,
        prompt: str,
        problem: Optional[Dict[str, Any]],
        *,
        step: int,
        total_steps: int,
        round_index: int,
    ) -> Tuple[Any, str]:
        label = f"{cls._round_label_cn(round_index)}：AI推理分析"
        with PhaseSpinner(label, step=step, total_steps=total_steps) as sp:
            response, prompt_used = cls._llm_call_with_retries(
                context,
                prompt,
                problem,
                round_index=round_index,
                stage="analysis" if round_index == 0 else "context_followup",
            )
            usage = getattr(response, "usage", None) or {}
            if isinstance(usage, dict):
                sp.set_tokens(
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                )
        return response, prompt_used

    @classmethod
    def _run_llm_context_loop(
        cls,
        context: WorkflowContext,
        initial_prompt: str,
        problem: Optional[Dict[str, Any]],
        code_roots: List[str],
        *,
        step: int = 4,
        total_steps: int = 4,
    ) -> Tuple[str, str, List[Dict[str, Any]], Any]:
        max_rounds = cls._resolve_max_agent_rounds(problem)
        agent_loop = cls._resolve_agent_loop(problem)
        max_requests = 5
        if isinstance(problem, dict):
            try:
                max_requests = int(problem.get("max_context_requests_per_round") or 5)
            except (TypeError, ValueError):
                max_requests = 5
        max_requests = max(1, min(max_requests, 16))

        response, prompt_used = cls._llm_call_with_phase(
            context,
            initial_prompt,
            problem,
            step=step,
            total_steps=total_steps,
            round_index=0,
        )
        analysis_text = str(getattr(response, "content", "") or "")
        rounds: List[Dict[str, Any]] = [
            {
                "round": 0,
                "prompt": prompt_used,
                "analysis": analysis_text,
                "context_requests": [],
                "resolved_context": [],
            }
        ]
        if agent_loop != "context_loop" or max_rounds <= 1:
            return analysis_text, prompt_used, rounds, response

        request_outcomes: Dict[str, str] = {}
        base_prompt = prompt_used
        last_prompt_used = base_prompt
        accumulated_context: List[Dict[str, Any]] = []
        previous_analysis = analysis_text
        last_response = response
        for round_index in range(1, max_rounds):
            parsed = cls._parse_context_requests(previous_analysis)
            requests = parsed.get("context_requests", [])
            rounds[-1]["context_requests"] = requests
            agent_can_fetch_more = parsed.get(
                "agent_can_fetch_more", parsed.get("need_more_context")
            )
            if not agent_can_fetch_more or not requests:
                break
            round_label = cls._round_label_cn(round_index)
            with PhaseSpinner(
                f"{round_label}：补充函数源码",
                step=step,
                total_steps=total_steps,
            ):
                resolved_context = cls._resolve_context_requests(
                    requests,
                    code_roots,
                    max_requests=max_requests,
                    request_outcomes=request_outcomes,
                    stack_priority_classes=cls._extract_stack_priority_classes(problem),
                )
                resolved_context = [
                    cls._attach_return_form_metadata(x)
                    if isinstance(x, dict)
                    else x
                    for x in resolved_context
                ]
            accumulated_context = cls._merge_accumulated_context_items(
                accumulated_context,
                [x for x in resolved_context if isinstance(x, dict)],
            )
            pre_round_add_res = cls._build_pre_round_add_res(
                source_round=round_index - 1,
                target_round=round_index,
                resolved_context=resolved_context,
            )
            has_success = any(
                bool(x.get("success")) for x in resolved_context if isinstance(x, dict)
            )
            if not has_success:
                rounds[-1]["resolved_context"] = resolved_context
                all_blocked = cls._all_context_requests_blocked(resolved_context)
                at_max_round = round_index >= max_rounds - 1
                if all_blocked or at_max_round:
                    early_reason = (
                        "all_requests_blocked" if all_blocked else "max_rounds"
                    )
                    final_prompt = cls._build_context_loop_prompt(
                        base_prompt,
                        round_index,
                        resolved_context,
                        accumulated_context=accumulated_context,
                        is_final_round=True,
                        early_final_reason=early_reason,
                    )
                    final_response, final_prompt_used = cls._llm_call_with_phase(
                        context,
                        final_prompt,
                        problem,
                        step=step,
                        total_steps=total_steps,
                        round_index=round_index,
                    )
                    previous_analysis = str(getattr(final_response, "content", "") or "")
                    last_prompt_used = final_prompt_used
                    last_response = final_response
                    rounds.append(
                        {
                            "round": round_index,
                            "prompt": final_prompt_used,
                            "analysis": previous_analysis,
                            "context_requests": [],
                            "resolved_context": resolved_context,
                            "pre_round_add_res": pre_round_add_res,
                            "early_final_reason": early_reason,
                        }
                    )
                    break
                feedback_prompt = cls._build_context_loop_prompt(
                    base_prompt,
                    round_index,
                    resolved_context,
                    accumulated_context=accumulated_context,
                    is_final_round=False,
                )
                feedback_response, feedback_prompt_used = cls._llm_call_with_phase(
                    context,
                    feedback_prompt,
                    problem,
                    step=step,
                    total_steps=total_steps,
                    round_index=round_index,
                )
                previous_analysis = str(getattr(feedback_response, "content", "") or "")
                last_prompt_used = feedback_prompt_used
                last_response = feedback_response
                rounds.append(
                    {
                        "round": round_index,
                        "prompt": feedback_prompt_used,
                        "analysis": previous_analysis,
                        "context_requests": [],
                        "resolved_context": resolved_context,
                        "pre_round_add_res": pre_round_add_res,
                    }
                )
                continue
            next_prompt = cls._build_context_loop_prompt(
                base_prompt,
                round_index,
                resolved_context,
                accumulated_context=accumulated_context,
                is_final_round=round_index >= max_rounds - 1,
            )
            next_response, next_prompt_used = cls._llm_call_with_phase(
                context,
                next_prompt,
                problem,
                step=step,
                total_steps=total_steps,
                round_index=round_index,
            )
            previous_analysis = str(getattr(next_response, "content", "") or "")
            last_prompt_used = next_prompt_used
            last_response = next_response
            rounds.append(
                {
                    "round": round_index,
                    "prompt": next_prompt_used,
                    "analysis": previous_analysis,
                    "context_requests": [],
                    "resolved_context": resolved_context,
                    "pre_round_add_res": pre_round_add_res,
                }
            )
        return previous_analysis, last_prompt_used, rounds, last_response

    @staticmethod
    def _norm_graph_nid(raw: Optional[str]) -> str:
        if not isinstance(raw, str):
            return ""
        return raw.rstrip().rstrip("{").rstrip()

    @classmethod
    def _function_node_has_snippet(cls, node: Any) -> bool:
        return (
            isinstance(node, dict)
            and node.get("type") == "function"
            and isinstance(node.get("snippet"), list)
            and bool(node.get("snippet"))
        )

    @classmethod
    def _resolve_max_prompt_stack_frame_functions(
        cls,
        graph: Dict[str, Any],
        code_context: Dict[str, Any],
        problem: Optional[Dict[str, Any]],
    ) -> int:
        for src in (
            cls._code_context_options(code_context),
            problem or {},
        ):
            if not isinstance(src, dict):
                continue
            raw = src.get("max_prompt_stack_frame_functions")
            if raw is not None:
                try:
                    return max(1, min(int(raw), 32))
                except (TypeError, ValueError):
                    pass
        if isinstance(graph, dict):
            kept = graph.get("stack_kept_original_indices")
            if isinstance(kept, list) and kept:
                return max(3, min(len(kept) + 2, 16))
        return 8

    @classmethod
    def _collect_add2line_stack_node_ids(
        cls,
        graph: Dict[str, Any],
        node_map: Dict[str, Any],
        max_frames: int,
    ) -> List[str]:
        """从 call_chain_from_add2line 收集带 snippet 的工程栈帧节点（保持栈序、去重）。"""
        out: List[str] = []
        seen: Set[str] = set()
        chains = graph.get("call_chain_from_add2line") if isinstance(graph, dict) else []
        if not isinstance(chains, list):
            return out
        for item in chains:
            if not isinstance(item, dict):
                continue
            for nid in item.get("nodes") or []:
                if not isinstance(nid, str):
                    continue
                norm = cls._norm_graph_nid(nid)
                if norm in seen:
                    continue
                node = node_map.get(nid) or node_map.get(norm)
                if not cls._function_node_has_snippet(node):
                    continue
                seen.add(norm)
                out.append(nid)
                if len(out) >= max_frames:
                    return out
        return out

    @classmethod
    def _collect_prompt_source_node_ids(
        cls,
        graph: Dict[str, Any],
        crash_summary: Dict[str, Any],
        primary_path_nodes: List[str],
        node_map: Dict[str, Any],
        code_context: Dict[str, Any],
        problem: Optional[Dict[str, Any]],
    ) -> List[str]:
        """
        汇总应送入 LLM 的函数节点 id：崩溃函数优先；
        静态调用链不足时以 add2line 栈帧补全（避免仅有崩溃点一行源码）。
        """
        ordered: List[str] = []
        seen: Set[str] = set()

        def _append(nid: Optional[str]) -> None:
            if not isinstance(nid, str) or not nid.strip():
                return
            norm = cls._norm_graph_nid(nid)
            if norm in seen:
                return
            node = node_map.get(nid) or node_map.get(norm)
            if not cls._function_node_has_snippet(node):
                return
            seen.add(norm)
            ordered.append(nid)

        if not is_investigation_hint_attribution(crash_summary):
            node_id = (
                crash_summary.get("node_id") if isinstance(crash_summary, dict) else None
            )
            if isinstance(node_id, str):
                _append(node_id)

        path_nodes = (
            []
            if is_investigation_hint_attribution(crash_summary)
            else [n for n in primary_path_nodes if isinstance(n, str)]
        )
        if len(path_nodes) < 2:
            max_sf = cls._resolve_max_prompt_stack_frame_functions(graph, code_context, problem)
            for nid in cls._collect_add2line_stack_node_ids(graph, node_map, max_sf):
                _append(nid)
        else:
            for nid in path_nodes:
                _append(nid)
            # 即使静态调用链充足，也补充 add2line 栈顶帧（实际崩溃路径），
            # 确保 LLM 能看到运行时调用方的源码（避免幻觉）。
            for nid in cls._collect_add2line_stack_node_ids(graph, node_map, 3):
                _append(nid)

        if len(ordered) <= 1:
            max_sf = cls._resolve_max_prompt_stack_frame_functions(graph, code_context, problem)
            for nid in cls._collect_add2line_stack_node_ids(graph, node_map, max_sf):
                _append(nid)

        return ordered

    @staticmethod
    def _top_resolved_frame_has_file_line(resolved: Any) -> bool:
        """栈顶帧是否带有 add2line 解析出的 file:line。"""
        if not isinstance(resolved, dict):
            return False
        from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

        frames = flatten_resolved_frames_from_stack(resolved)
        if not frames or not isinstance(frames[0], dict):
            return False
        top = frames[0]
        if not str(top.get("resolved_file") or "").strip():
            return False
        try:
            return int(top.get("resolved_line") or 0) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _prompt_has_confident_crash_line(crash_summary: Any, resolved: Any) -> bool:
        """
        是否向 LLM 展示「可疑崩溃代码行」。
        symbol-only / from_log_deduce 等无明确 addr2line 行号时不展示。
        """
        crash_summary = BaseCrashAnalysisWorkflow._compat_crash_summary(crash_summary)
        if not isinstance(crash_summary, dict):
            return False
        source = str(crash_summary.get("crash_location_source") or "").strip()
        if source == "from_log_deduce":
            return False
        if crash_summary.get("selected_analysis_is_crash_thread") is False:
            return False
        if source not in ("", "from_add2line"):
            return False
        try:
            line_no = int(crash_summary.get("crash_line_number") or 0)
        except (TypeError, ValueError):
            return False
        if line_no <= 0:
            return False
        if not str(crash_summary.get("crash_line_code") or "").strip():
            return False
        if not BaseCrashAnalysisWorkflow._top_resolved_frame_has_file_line(resolved):
            return False
        return True

    @staticmethod
    def _compat_crash_summary(crash_summary: Any) -> Dict[str, Any]:
        """兼容 03 v2 嵌套结构与旧版平铺字段，供 05 拼装逻辑统一读取。"""
        if not isinstance(crash_summary, dict):
            return {}
        out: Dict[str, Any] = dict(crash_summary)

        crash_thread = crash_summary.get("crash_thread")
        if isinstance(crash_thread, dict):
            out.setdefault("crash_thread_id", crash_thread.get("id"))
            out.setdefault("crash_thread_name", crash_thread.get("name"))
            out.setdefault("is_main_thread_crash", crash_thread.get("is_main_thread"))
            out.setdefault(
                "crash_thread_has_business_frames",
                crash_thread.get("has_library_dir_business_frames"),
            )
            out.setdefault("crash_attribution_source", crash_thread.get("attribution_source"))

        crash_location = crash_summary.get("crash_location")
        if isinstance(crash_location, dict):
            status_norm, source_norm = resolve_crash_location_status_source(
                crash_location
            )
            if source_norm:
                out.setdefault("crash_location_source", source_norm)
            if status_norm:
                out.setdefault("attributed_crash_location_status", status_norm)
            location_type = crash_location.get("location_type")
            out.setdefault(
                "crash_line_note",
                crash_location.get("reason") or location_type,
            )
            out.setdefault("crash_line_number", crash_location.get("line"))
            out.setdefault("crash_line_code", crash_location.get("code"))
            out.setdefault("stack_address", crash_location.get("stack_address"))
            out.setdefault("node_id", crash_location.get("node_id"))
            out.setdefault("owner_class_node_id", crash_location.get("owner_class_node_id"))
            out.setdefault("analysis_entry_file", crash_location.get("file"))
            out.setdefault("analysis_entry_function", crash_location.get("function"))
            loc_src = crash_location.get("source")
            if loc_src:
                out.setdefault("analysis_entry_location_source", loc_src)
            loc_type = crash_location.get("location_type")
            if loc_type == LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS:
                out.setdefault("selected_analysis_confidence", CONFIDENCE_INVESTIGATION_HINT)
                out.setdefault("selected_analysis_is_crash_thread", False)
            elif crash_location.get("line") or crash_location.get("node_id"):
                out.setdefault("selected_analysis_confidence", CONFIDENCE_DIRECT_CRASH_THREAD)
                out.setdefault("selected_analysis_is_crash_thread", True)

        analysis_entry = crash_summary.get("analysis_entry")
        if isinstance(analysis_entry, dict):
            thread = analysis_entry.get("thread")
            if isinstance(thread, dict):
                out.setdefault("selected_analysis_thread_id", thread.get("id"))
                out.setdefault("selected_analysis_thread_name", thread.get("name"))
                out.setdefault("selected_analysis_is_crash_thread", thread.get("is_crash_thread"))
                out.setdefault("selected_analysis_is_main_thread", thread.get("is_main_thread"))
            location = analysis_entry.get("location")
            if isinstance(location, dict):
                out.setdefault("analysis_entry_file", location.get("file"))
                out.setdefault("analysis_entry_function", location.get("function"))
                out.setdefault("analysis_entry_line_number", location.get("line"))
                out.setdefault("analysis_entry_line_code", location.get("code"))
                out.setdefault("analysis_entry_location_source", location.get("source"))
                out.setdefault("analysis_entry_stack_address", location.get("stack_address"))
            conf_norm, src_norm = resolve_analysis_entry_confidence_source(
                analysis_entry
            )
            if src_norm:
                out.setdefault("selected_analysis_source", src_norm)
            if conf_norm:
                out.setdefault("selected_analysis_confidence", conf_norm)
            entry_type = analysis_entry.get("entry_type")
            out.setdefault(
                "selected_analysis_note",
                analysis_entry.get("note") or entry_type,
            )
            out.setdefault("analysis_entry_node_id", analysis_entry.get("node_id"))
            if not is_investigation_hint_attribution(out):
                if not out.get("node_id"):
                    out["node_id"] = analysis_entry.get("node_id")

        if is_investigation_hint_attribution(out):
            out.pop("node_id", None)
            out.setdefault("selected_analysis_is_crash_thread", False)
        elif out.get("selected_analysis_is_crash_thread") is None:
            crash_location = out.get("crash_location")
            if isinstance(crash_location, dict) and (
                crash_location.get("line") or crash_location.get("node_id")
            ):
                out["selected_analysis_is_crash_thread"] = True

        return {k: v for k, v in out.items() if v is not None}

    @staticmethod
    def _code_context_options(code_context: Any) -> Dict[str, Any]:
        """读取 03 v2 diagnostics 下的选项；兼容旧顶层 code_context_options。"""
        if not isinstance(code_context, dict):
            return {}
        diagnostics = code_context.get("diagnostics")
        if isinstance(diagnostics, dict) and isinstance(diagnostics.get("code_context_options"), dict):
            return diagnostics["code_context_options"]
        opts = code_context.get("code_context_options")
        return opts if isinstance(opts, dict) else {}

    @staticmethod
    def _crash_location_prompt_conclusion(
        crash_summary: Any,
        resolved: Any,
    ) -> str:
        """供 LLM 提示词使用的崩溃点定位结论（一句，不含过程说明）。"""
        crash_summary = BaseCrashAnalysisWorkflow._compat_crash_summary(crash_summary)
        source = str(crash_summary.get("crash_location_source") or "").strip()
        if BaseCrashAnalysisWorkflow._prompt_has_confident_crash_line(crash_summary, resolved):
            return "结论：崩溃点已通过符号化堆栈关联到具体源码行（置信度有限，需结合函数上下文验证）。"
        if is_investigation_hint_attribution(crash_summary):
            return CRASH_POSITION_PROMPT_ZH_WEAK
        if source == "from_log_deduce":
            return "结论：未精确定位到 file:line 级崩溃行；已关联到工程内崩溃函数，请以下文该函数完整源码为准。"
        if source == "from_add2line":
            return "结论：已定位到崩溃函数及参考行号，请以该函数完整源码为主要分析依据。"
        return "结论：崩溃点定位信息有限，请以下文函数源码与调用链为准。"

    @staticmethod
    def _append_crash_location_prompt_section(
        lines: List[str],
        crash_summary: Any,
        crash_node: Any,
        resolved: Any,
    ) -> None:
        """
        崩溃点定位：向 LLM 输出一句结论；仅有明确 addr2line 行号时再附带可疑单行。
        详细过程说明保留在 03 crash_line_note，不写入提示词。
        """
        crash_summary = BaseCrashAnalysisWorkflow._compat_crash_summary(crash_summary)
        if not isinstance(crash_summary, dict):
            return
        if is_investigation_hint_attribution(crash_summary):
            return

        source = str(crash_summary.get("crash_location_source") or "").strip()
        has_confident_line = BaseCrashAnalysisWorkflow._prompt_has_confident_crash_line(
            crash_summary, resolved
        )

        if not source and not has_confident_line:
            return

        lines.append("## 崩溃点定位")
        lines.append("")
        lines.append(
            BaseCrashAnalysisWorkflow._crash_location_prompt_conclusion(
                crash_summary, resolved
            )
        )

        if has_confident_line:
            crash_file = str((crash_node or {}).get("file") or "").strip() or "N/A"
            try:
                line_no = int(crash_summary.get("crash_line_number") or 0)
            except (TypeError, ValueError):
                line_no = 0
            code_line = str(crash_summary.get("crash_line_code") or "").strip()
            lines.append(
                f"可疑单行：`{crash_file}:{line_no}` — `{code_line}`（低置信度，勿单独作为改码依据）。"
            )

        lines.append("")
        lines.append("")

    @staticmethod
    def _maybe_run_anr_diagnosis(
        *,
        parse_result: Dict[str, Any],
        resolved_stack: Dict[str, Any],
        problem: Dict[str, Any],
        crash_log_content: str = "",
    ) -> Dict[str, Any]:
        """ANR/Freeze 诊断兜底（crash workflow 内）。

        正常 ANR 流量由 ``anr_freeze_analysis`` 专用 workflow 产出 04c。
        此处仅在预分类未路由到专用 workflow、但 parse 后 ``log_kind`` 属 ANR 族，
        或显式 ``force_anr_analysis`` 时执行，避免与专用 workflow 双跑。
        """
        if problem.get("skip_anr_sidepath"):
            return {}
        force = bool(
            problem.get("force_anr_analysis")
            or problem.get("enable_anr_analysis")
        )
        meta = parse_result.get("meta_info") if isinstance(parse_result, dict) else None
        log_kind = None
        if isinstance(meta, dict):
            log_kind = meta.get("log_kind")
        try:
            from tools.crash_parser.log_kind_classifier import is_anr_family_kind
            anr_family = is_anr_family_kind(log_kind)
        except Exception:
            anr_family = False
        # 无 ANR 族 log_kind 且非 force：不跑（普通 native crash 不再误触）
        if not force and not anr_family:
            # 兼容旧字段：仅当 log_kind 缺失时回退 anr_suspected
            if log_kind or not (isinstance(meta, dict) and meta.get("anr_suspected")):
                return {}

        # --- 改进：从原始文件路径读取日志 ---
        import os
        log_content = crash_log_content
        if not log_content:
            crash_log_path = (
                problem.get("crash_log_path")
                or problem.get("crash_log")
                or ""
            )
            if isinstance(crash_log_path, str) and crash_log_path and os.path.isfile(crash_log_path):
                try:
                    with open(crash_log_path, "r", encoding="utf-8", errors="replace") as f:
                        log_content = f.read()
                except Exception:
                    pass

        try:
            from tools.anr_diagnosis.core import run_anr_freeze_diagnosis
            out = run_anr_freeze_diagnosis(
                parse_result,
                resolved_stack,
                log_content or "",
                force=force or anr_family,
            )
            return out if isinstance(out, dict) else {}
        except Exception as exc:
            logger.warning("anr_freeze_diagnosis skipped: %s", exc)
            return {
                "analyzed": False,
                "error": str(exc),
                "prompt_section_zh": "",
            }

    @staticmethod
    def _maybe_run_memory_diagnosis(
        *,
        parse_result: Dict[str, Any],
        resolved_stack: Dict[str, Any],
        problem: Dict[str, Any],
        crash_log_content: str = "",
    ) -> Dict[str, Any]:
        """内存压力/OOM 旁路（阶段 A）：oom 族 log_kind 或 force 时产出 04d。"""
        if problem.get("skip_memory_sidepath"):
            return {}
        native_leak_dir = str(problem.get("native_leak_dir") or "").strip()
        native_leak_trace_db = str(problem.get("native_leak_trace_db") or "").strip()
        force = bool(
            problem.get("force_memory_analysis")
            or problem.get("enable_memory_analysis")
            or native_leak_dir
            or native_leak_trace_db
        )
        try:
            from tools.memory_diagnosis.core import run_memory_pressure_diagnosis
            out = run_memory_pressure_diagnosis(
                parse_result,
                resolved_stack,
                crash_log_content or str(problem.get("crash_log") or ""),
                force=force,
                native_leak_path=native_leak_dir,
                native_leak_trace_db=native_leak_trace_db,
            )
            return out if isinstance(out, dict) else {}
        except Exception as exc:
            logger.warning("memory_pressure_diagnosis skipped: %s", exc)
            return {
                "analyzed": False,
                "error": str(exc),
                "prompt_section_zh": "",
            }

    @staticmethod
    def _maybe_run_timeline_diagnosis(
        *,
        parse_result: Dict[str, Any],
        problem: Dict[str, Any],
        crash_log_content: str = "",
    ) -> Dict[str, Any]:
        """崩溃前时序/业务路径旁路 → 04e。

        优先从原始崩溃日志文件路径读取完整内容（而非依赖 parse_result.raw_content），
        因为 parser 可能跳过业务日志部分、且 raw_content 默认不落盘。
        """
        if problem.get("skip_timeline_sidepath"):
            return {}
        force = bool(
            problem.get("force_timeline_analysis")
            or problem.get("enable_timeline_analysis")
        )

        # --- 改进：从原始文件路径读取日志（不依赖 raw_content） ---
        import os
        log_content = crash_log_content
        if not log_content:
            crash_log_path = (
                problem.get("crash_log_path")
                or problem.get("crash_log")
                or ""
            )
            if isinstance(crash_log_path, str) and crash_log_path and os.path.isfile(crash_log_path):
                try:
                    with open(crash_log_path, "r", encoding="utf-8", errors="replace") as f:
                        log_content = f.read()
                except Exception:
                    pass
            if not log_content:
                log_content = str((parse_result or {}).get("raw_content") or "")

        # --- 改进：放宽触发条件——只要文件有足够行数就尝试 ---
        if not force and log_content:
            line_count = log_content.count("\n")
            if line_count >= 20:
                force = True  # 日志超过 20 行时默认尝试，由 core 内部判断是否有效

        try:
            from tools.timeline_diagnosis.core import run_log_timeline_diagnosis
            out = run_log_timeline_diagnosis(
                parse_result,
                log_content,
                force=force,
            )
            return out if isinstance(out, dict) else {}
        except Exception as exc:
            logger.warning("log_timeline_diagnosis skipped: %s", exc)
            return {
                "analyzed": False,
                "error": str(exc),
                "prompt_section_zh": "",
            }

    def _build_prompt_final_tip(
        self,
        parse_result: Dict[str, Any],
        resolved: Dict[str, Any],
        code_context: Dict[str, Any],
        memory_context: str = "",
        problem: Optional[Dict[str, Any]] = None,
        crash_diagnosis: Optional[Dict[str, Any]] = None,
        anr_diagnosis: Optional[Dict[str, Any]] = None,
        memory_diagnosis: Optional[Dict[str, Any]] = None,
        timeline_diagnosis: Optional[Dict[str, Any]] = None,
    ) -> str:
        """构建供 gen_prompt_only 模式或作为 LLM 输入的统一提示词。"""
        # 解耦重构：从 01+02+03 独立构建 crash_summary，不再依赖 03.crash_summary
        from tools.merge_utils import build_crash_summary_view
        crash_summary = build_crash_summary_view(parse_result, resolved, code_context)
        # 兼容回退：如果 merge_utils 构建结果缺少关键字段，尝试旧路径
        if not crash_summary.get("analysis_entry_function") and isinstance(code_context, dict):
            old_cs = code_context.get("crash_summary")
            if isinstance(old_cs, dict):
                crash_summary = self._compat_crash_summary(old_cs)
        weak_attribution = is_investigation_hint_attribution(crash_summary)
        graph = code_context.get("graph", {}) if isinstance(code_context, dict) else {}
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        node_map = {
            n.get("id"): n
            for n in nodes
            if isinstance(n, dict) and isinstance(n.get("id"), str)
        }
        crash_node = None
        node_id = crash_summary.get("node_id") if isinstance(crash_summary, dict) else None
        if isinstance(node_id, str):
            crash_node = node_map.get(node_id)
            if crash_node is None:
                crash_node = node_map.get(node_id.rstrip().rstrip("{").rstrip())

        lines: List[str] = []
        summary_emitted_structured_crash_point = False
        lines.append("下面是本次崩溃的相关信息：")
        lines.append("")
        lines.append("## 崩溃摘要")
        if isinstance(crash_summary, dict):
            if crash_summary.get("error_type") is not None:
                lines.append(f"- 错误类型: {crash_summary.get('error_type')}")
            crash_tid = crash_summary.get("crash_thread_id")
            crash_tname = crash_summary.get("crash_thread_name")
            if crash_tid or crash_tname:
                label = format_prompt_thread_identity(crash_tid, crash_tname)
                main_flag = crash_summary.get("is_main_thread_crash")
                if main_flag is True:
                    label += "（主线程）"
                elif main_flag is False:
                    label += "（非主线程）"
                lines.append(f"- 日志标记的崩溃线程: {label}")
            entry_func = str(crash_summary.get("analysis_entry_function") or "").strip()
            entry_file = str(crash_summary.get("analysis_entry_file") or "").strip()
            entry_line = crash_summary.get("crash_line_number")
            entry_code = str(crash_summary.get("crash_line_code") or "").strip()
            try:
                entry_line_int = int(entry_line or 0)
            except (TypeError, ValueError):
                entry_line_int = 0
            if entry_func and entry_file and entry_line_int > 0 and entry_code:
                summary_emitted_structured_crash_point = True
                lines.append("- 崩溃点信息：")
                lines.append(f"  - 崩溃函数: {entry_func}")
                lines.append(f"  - 崩溃位置: {entry_file}:{entry_line_int}")
                lines.append(f"  - 崩溃位置对应代码: `{entry_code}`")
                source = str(crash_summary.get("crash_location_source") or "").strip()
                if source or self._prompt_has_confident_crash_line(
                    crash_summary, resolved
                ):
                    conclusion = self._crash_location_prompt_conclusion(
                        crash_summary, resolved
                    )
                    if conclusion.startswith("结论："):
                        conclusion = conclusion[len("结论：") :]
                    lines.append(f"  - 定位说明: {conclusion}")
                    if self._prompt_has_confident_crash_line(crash_summary, resolved):
                        lines.append(
                            "  - 定位置信度: 低（勿单独作为改码依据）"
                        )
            else:
                pos_summary = format_crash_position_summary_line(
                    crash_summary, crash_node
                )
                if pos_summary:
                    lines.append(f"- {pos_summary}")
        lines.append("")

        if weak_attribution:
            lines.append("## 代码结构线索")
            lines.append(
                "说明：请优先阅读下文「根据日志不同线程的代码调用关系」"
                "与函数源码，自行选定分析起点。"
            )
            lines.append("")
        else:
            lines.append("## 崩溃上下文")
            lines.append("崩溃函数被调用的路径（基于代码结构推断，自上而下）:")
        edges_list_early = (
            [] if weak_attribution else (graph.get("edges", []) if isinstance(graph, dict) else [])
        )
        evidence_summary = (
            graph.get("evidence_summary") if isinstance(graph, dict) else None
        )
        has_calls_direct = False
        has_calls_to_crash_site = False
        has_calls_stack_order = False
        has_stack_adjacent_verified_chain = False
        has_shared_var_write_upstream = False
        if isinstance(evidence_summary, dict):
            has_calls_direct = bool(evidence_summary.get("has_calls_direct"))
            has_calls_to_crash_site = bool(evidence_summary.get("has_calls_to_crash_site"))
            has_calls_stack_order = bool(evidence_summary.get("has_calls_stack_order"))
            has_stack_adjacent_verified_chain = bool(
                evidence_summary.get("has_stack_adjacent_verified_chain")
            )
            has_shared_var_write_upstream = bool(
                evidence_summary.get("has_shared_var_write_upstream")
            )
        if isinstance(edges_list_early, list):
            for e in edges_list_early:
                if not isinstance(e, dict):
                    continue
                et = str(e.get("type") or "")
                if et == "calls_direct":
                    has_calls_direct = True
                elif et == "calls_to_crash_site":
                    has_calls_to_crash_site = True
                elif et == "calls_stack_order":
                    has_calls_stack_order = True
                elif et == "calls_stack_verified":
                    has_stack_adjacent_verified_chain = True
                elif et == "use_shared_var":
                    rel = str(e.get("relation") or "").strip().lower()
                    if rel in ("write", "assign", "delete"):
                        has_shared_var_write_upstream = True
        call_paths = (
            []
            if weak_attribution
            else (graph.get("call_chain_from_code", []) if isinstance(graph, dict) else [])
        )
        primary_path_nodes: List[str] = []
        all_path_nodes_list: List[List[str]] = []
        if not weak_attribution and isinstance(call_paths, list) and call_paths:
            for path in call_paths:
                if not isinstance(path, dict):
                    continue
                path_nodes = path.get("nodes", [])
                if isinstance(path_nodes, list) and path_nodes:
                    extracted = [nid for nid in path_nodes if isinstance(nid, str)]
                    if extracted:
                        all_path_nodes_list.append(extracted)
                        if not primary_path_nodes:
                            primary_path_nodes = extracted
        if not weak_attribution:
            path_label = "路径1"
            primary_path_desc = ""
            if isinstance(call_paths, list) and call_paths:
                primary_path_desc = str(
                    (call_paths[0] or {}).get("inference")
                    or (call_paths[0] or {}).get("description")
                    or ""
                ).strip()
            if isinstance(call_paths, list):
                for path in call_paths:
                    if not isinstance(path, dict):
                        continue
                    desc = str(
                        path.get("inference") or path.get("description") or ""
                    ).strip()
                    if desc == "stack_adjacent_verified":
                        path_label = "路径1（栈相邻帧 + 源码间接调用链已校验）"
                        break
                    if desc == "inferred_from_add2line_stack_order":
                        path_label = "路径1（addr2line 栈序；静态未校验相邻帧调用关系）"
                        break
                    if desc == "static_repo_inferred":
                        path_label = "路径1（全仓静态推断；未与栈相邻帧校验一致）"
                        break
            if primary_path_nodes:
                if (
                    primary_path_desc == "stack_adjacent_verified"
                    or has_stack_adjacent_verified_chain
                ):
                    lines.append(
                        "- 证据强度说明：高置信度（栈相邻帧 + 源码调用链已校验），"
                        "可单独作为改码依据。"
                    )
                elif primary_path_desc == "static_repo_inferred" or (
                    (has_calls_direct or has_calls_to_crash_site)
                    and not has_stack_adjacent_verified_chain
                ):
                    lines.append(
                        "- 证据强度说明：本节来自全仓静态推断，未与栈相邻帧校验一致，"
                        "只能作为排查线索，不能单独作为改码依据。"
                    )
                elif "addr2line 栈序" in path_label or has_calls_stack_order:
                    lines.append(
                        "- 证据强度说明：本节主要来自栈序关联，属于线索证据，"
                        "不能单独作为改码依据。"
                    )
                elif has_calls_direct or has_calls_to_crash_site:
                    lines.append("- 证据强度说明：高置信度，可单独作为改码依据。")
            if all_path_nodes_list:
                for path_idx, path_nodes in enumerate(all_path_nodes_list, 1):
                    if path_idx > 1:
                        lines.append(
                            f"- 路径{path_idx}: 共 {len(path_nodes)} 个节点（未展开；"
                            "不得据此定位根因或改码，仅作线索）"
                        )
                        continue
                    label = path_label if path_idx == 1 else f"路径{path_idx}"
                    lines.append(f"- {label}:")
                    for nid in path_nodes:
                        node = node_map.get(nid)
                        if isinstance(node, dict):
                            lines.append(f"  - {node.get('signature', nid)}")
                        else:
                            lines.append(f"  - {nid}")
            else:
                lines.append("- 路径1: N/A")
            lines.append("")

        resolved_threads = (
            resolved.get("resolved_threads", []) if isinstance(resolved, dict) else []
        )
        add2line_chains = (
            graph.get("call_chain_from_add2line", [])
            if isinstance(graph, dict)
            else []
        )
        has_add2line_chains = (
            isinstance(add2line_chains, list)
            and any(
                isinstance(item, dict) and (item.get("nodes") or [])
                for item in add2line_chains
            )
        )
        if isinstance(resolved_threads, list) and resolved_threads and not has_add2line_chains:
            lines.append("## 按线程符号化结果（02）")
            for rt in resolved_threads:
                if not isinstance(rt, dict):
                    continue
                is_crash = bool(rt.get("is_crash_thread"))
                is_main = rt.get("is_main_thread")
                tid = rt.get("tid")
                tname = rt.get("name")
                label = format_prompt_thread_identity(tid, tname)
                flags = format_prompt_thread_role_flags(is_crash, is_main)
                lines.append(f"### 线程（{flags}）{label}")
                frames_rt = rt.get("frames") or []
                cap = len(frames_rt) if is_crash else min(8, len(frames_rt))
                for fr in frames_rt[:cap]:
                    if not isinstance(fr, dict):
                        continue
                    func = (
                        fr.get("resolved_function")
                        or fr.get("function")
                        or fr.get("address")
                        or "N/A"
                    )
                    fp = fr.get("resolved_file") or fr.get("file")
                    ln = fr.get("resolved_line")
                    if ln in (None, "", "None"):
                        ln = fr.get("line")
                    mod = fr.get("module")
                    if fp not in (None, "", "None") and ln not in (None, "", "None"):
                        lines.append(f"  - {func} ({fp}:{ln}) [{mod or ''}]")
                    else:
                        lines.append(f"  - {func} [{mod or ''}]")
                if not is_crash and len(frames_rt) > cap:
                    lines.append(f"  - … 省略 {len(frames_rt) - cap} 帧")
                lines.append("")

        if has_add2line_chains:
            lines.append("## 根据日志不同线程的代码调用关系")
            for item in add2line_chains:
                if not isinstance(item, dict):
                    continue
                nodes_in_chain = item.get("nodes") or []
                if not isinstance(nodes_in_chain, list) or not nodes_in_chain:
                    continue
                tid = item.get("thread_id")
                tname = item.get("thread_name")
                is_crash = bool(item.get("is_crash_thread"))
                is_main = item.get("is_main_thread")
                label = format_prompt_thread_identity(tid, tname)
                flags = format_prompt_thread_role_flags(is_crash, is_main)
                # 统一先给出线程身份说明，再引出按栈序排列的帧语义列表。
                lines.append("### 线程调用链")
                lines.append(
                    f"{label}（{flags}）按堆栈顺序解析的函数/帧语义列表"
                    "（按调用顺序，自下而上）："
                )
                if is_crash:
                    lines.append(
                        "- 证据强度说明：低置信度，只能作为排查线索，"
                        "不能单独作为改码依据。"
                    )
                else:
                    lines.append(
                        "- 证据强度说明：该链路来自非日志标记的崩溃线程，"
                        "仅作跨线程/异步排查线索。"
                    )
                for pos, nid in enumerate(nodes_in_chain, 1):
                    node = node_map.get(nid) if isinstance(nid, str) else None
                    if isinstance(node, dict):
                        lines.append(f"  - [{pos}] {node.get('signature') or node.get('name') or nid}")
                    else:
                        lines.append(f"  - [{pos}] {nid}")
                lines.append("")

        multi_thread_resolved = (
            isinstance(resolved_threads, list) and len(resolved_threads) > 1
        )
        # 已有按线程的 add2line 栈帧列表时，不再重复输出扁平堆栈帧段落。
        if (
            not has_add2line_chains
            and not (weak_attribution and multi_thread_resolved)
        ):
            lines.append("根据日志中堆栈顺序解析的函数/帧语义列表（按调用顺序，自下而上）：")
            from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

            resolved_frames = (
                flatten_resolved_frames_from_stack(resolved)
                if isinstance(resolved, dict)
                else []
            )
            if isinstance(resolved_frames, list) and resolved_frames:
                lines.append(
                    "- 证据强度说明：低置信度，只能作为排查线索，"
                    "不能单独作为改码依据。"
                )
            if isinstance(resolved_frames, list):
                for idx, frame in enumerate(resolved_frames, 1):
                    if not isinstance(frame, dict):
                        continue
                    func = (
                        frame.get("resolved_function")
                        or frame.get("function")
                        or frame.get("raw_address")
                        or frame.get("address")
                        or "N/A"
                    )
                    file_path = frame.get("resolved_file") or frame.get("file")
                    line_no = frame.get("resolved_line")
                    if line_no in (None, "", "None"):
                        line_no = frame.get("line")
                    if file_path in (None, "", "None") or line_no in (
                        None,
                        "",
                        "None",
                    ):
                        lines.append(f"- [第{idx}帧][源码函数] {func}")
                    else:
                        lines.append(
                            f"- [第{idx}帧][源码函数] {func} ({file_path}:{line_no})"
                        )
            lines.append("")
            lines.append("")

        # ---------- 共享成员与跨函数写路径（来自 graph use_shared_var，列表展示） ----------
        def _norm_graph_nid(raw: Optional[str]) -> str:
            if not isinstance(raw, str):
                return ""
            return raw.rstrip().rstrip("{").rstrip()

        def _relation_write_like(rel: Optional[str]) -> bool:
            """图中 relation 的写类语义（含 assign/delete），用于优先突出竞态写路径。"""
            r = (rel or "").strip().lower()
            return r in ("write", "assign", "delete")

        crash_fn_norm_ids: Set[str] = set()
        if isinstance(crash_node, dict) and crash_node.get("id"):
            crash_fn_norm_ids.add(_norm_graph_nid(str(crash_node.get("id"))))
        if isinstance(node_id, str):
            crash_fn_norm_ids.add(_norm_graph_nid(node_id))
        crash_fn_norm_ids.discard("")

        edges_list = graph.get("edges", []) if isinstance(graph, dict) else []
        if not isinstance(edges_list, list):
            edges_list = []

        var_rel_by_tid: Dict[str, Set[str]] = {}
        shared_var_tids: Set[str] = set()
        if crash_fn_norm_ids:
            for e in edges_list:
                if not isinstance(e, dict):
                    continue
                if e.get("type") != "use_shared_var":
                    continue
                fid = _norm_graph_nid(str(e.get("from_id") or ""))
                if fid not in crash_fn_norm_ids:
                    continue
                tid = str(e.get("to_id") or "")
                if not tid.startswith("var|"):
                    continue
                rel = str(e.get("relation") or "unknown")
                var_rel_by_tid.setdefault(tid, set()).add(rel)
                shared_var_tids.add(tid)

        if var_rel_by_tid:
            lines.append("### 共享成员与写路径交叉（崩溃点关联）")
            if has_shared_var_write_upstream:
                lines.append("- 证据强度说明：高置信度，可单独作为改码依据。")
            else:
                lines.append("- 证据强度说明：本节仅提供共享变量关联线索；若无“其它函数写/删同一变量”，不能单独作为改码依据。")
            lines.append("崩溃函数关联的成员/共享变量：")
            lines.append(
                "说明：以下“声明摘录”来自成员变量定义行（通常位于头文件），用于识别变量类型与存储形态，不代表运行时值。"
            )
            for tid in sorted(var_rel_by_tid.keys()):
                vn = node_map.get(tid) if isinstance(node_map, dict) else None
                vname = tid
                decl = ""
                if isinstance(vn, dict):
                    vname = str(vn.get("name") or tid)
                    decl = str(vn.get("signature") or "").strip()
                rels = "/".join(sorted(var_rel_by_tid[tid]))
                if decl and len(decl) > 160:
                    decl = decl[:157] + "..."
                if decl:
                    lines.append(f"- 变量: {vname}；访问: {rels}；声明摘录: {decl}")
                else:
                    lines.append(f"- 变量: {vname}；访问: {rels}")

            def _looks_like_key_read_name(sig_or_name: str) -> bool:
                n = str(sig_or_name or "").lower()
                return any(
                    kw in n
                    for kw in (
                        "get",
                        "read",
                        "fetch",
                        "query",
                        "find",
                        "lookup",
                        "scan",
                        "walk",
                        "traverse",
                        "visit",
                        "modify",
                        "update",
                        "access",
                    )
                )

            other_fid_write: Set[str] = set()
            other_fid_key_read: Set[str] = set()
            if shared_var_tids:
                for e in edges_list:
                    if not isinstance(e, dict):
                        continue
                    if e.get("type") != "use_shared_var":
                        continue
                    tid = str(e.get("to_id") or "")
                    if tid not in shared_var_tids:
                        continue
                    raw_fid = str(e.get("from_id") or "")
                    if _norm_graph_nid(raw_fid) in crash_fn_norm_ids:
                        continue
                    fn = node_map.get(raw_fid)
                    if not isinstance(fn, dict) or fn.get("type") != "function":
                        continue
                    rel = str(e.get("relation") or "")
                    if _relation_write_like(rel):
                        other_fid_write.add(raw_fid)
                    elif rel.strip().lower() == "read":
                        sig = str(fn.get("signature") or raw_fid)
                        if _looks_like_key_read_name(sig):
                            other_fid_key_read.add(raw_fid)

            def _fid_to_sig(fid: str) -> str:
                fn = node_map.get(fid)
                if isinstance(fn, dict):
                    return str(fn.get("signature") or fid).strip() or fid
                return fid

            _OTHER_SIG_CAP = 15
            if other_fid_write:
                lines.append("")
                lines.append("同一批变量的其它写路径函数（仅列签名，源码见后文专门小节）：")
                sig_list = sorted({_fid_to_sig(x) for x in other_fid_write})
                shown = sig_list[:_OTHER_SIG_CAP]
                for s in shown:
                    lines.append(f"- {s}")
                rest = len(sig_list) - len(shown)
                if rest > 0:
                    lines.append(f"- … 另有 {rest} 个函数未列出")
            if other_fid_key_read:
                lines.append("")
                lines.append("同一批变量的关键读路径函数（仅列签名，源码见后文专门小节）：")
                sig_list = sorted({_fid_to_sig(x) for x in other_fid_key_read})
                shown = sig_list[:_OTHER_SIG_CAP]
                for s in shown:
                    lines.append(f"- {s}")
                rest = len(sig_list) - len(shown)
                if rest > 0:
                    lines.append(f"- … 另有 {rest} 个函数未列出")
            lines.append("")
        lines.append("")

        if not summary_emitted_structured_crash_point:
            self._append_crash_location_prompt_section(
                lines, crash_summary, crash_node, resolved
            )

        # === 注入寄存器与内存状态诊断（来自 04a_crash_diagnosis）===
        if isinstance(crash_diagnosis, dict):
            diag_prompt = crash_diagnosis.get("prompt_section_zh", "")
            if diag_prompt and isinstance(diag_prompt, str) and diag_prompt.strip():
                lines.append("")
                lines.append(diag_prompt)
                lines.append("")

        # === 注入 ANR/Freeze 诊断（来自 04c，仅 anr 族/force 时有内容）===
        if isinstance(anr_diagnosis, dict):
            anr_prompt = anr_diagnosis.get("prompt_section_zh", "")
            if anr_prompt and isinstance(anr_prompt, str) and anr_prompt.strip():
                lines.append("")
                lines.append(anr_prompt)
                lines.append("")

        # === 注入内存压力/OOM 诊断（来自 04d，阶段 A 旁路）===
        if isinstance(memory_diagnosis, dict):
            mem_prompt = memory_diagnosis.get("prompt_section_zh", "")
            if mem_prompt and isinstance(mem_prompt, str) and mem_prompt.strip():
                lines.append("")
                lines.append(mem_prompt)
                lines.append("")

        # === 注入崩溃前时序/业务路径（来自 04e）===
        if isinstance(timeline_diagnosis, dict):
            tl_prompt = timeline_diagnosis.get("prompt_section_zh", "")
            if tl_prompt and isinstance(tl_prompt, str) and tl_prompt.strip():
                lines.append("")
                lines.append(tl_prompt)
                lines.append("")

        lines.append("## 函数源码")
        lines.append("")

        source_ids = self._collect_prompt_source_node_ids(
            graph if isinstance(graph, dict) else {},
            crash_summary if isinstance(crash_summary, dict) else {},
            primary_path_nodes,
            node_map,
            code_context if isinstance(code_context, dict) else {},
            problem,
        )

        from tools._prompt_context_filter import (
            build_stack_anchor_paths,
            filter_prompt_function_records,
            match_resolved_frames_to_node_ids,
            resolve_prompt_filter_options,
        )

        prompt_filter_opts = resolve_prompt_filter_options(
            code_context if isinstance(code_context, dict) else None,
            problem,
        )
        stack_anchors = build_stack_anchor_paths(
            code_context if isinstance(code_context, dict) else None,
            resolved if isinstance(resolved, dict) else None,
        )

        stack_frame_ids = match_resolved_frames_to_node_ids(
            node_map,
            resolved if isinstance(resolved, dict) else None,
            max_frames=prompt_filter_opts.max_stack_frames_in_prompt,
        )
        stack_frame_norm_ids: Set[str] = {_norm_graph_nid(nid) for nid in stack_frame_ids}
        for nid in stack_frame_ids:
            if isinstance(nid, str) and nid not in source_ids:
                source_ids.append(nid)

        root_cause_norm_ids: Set[str] = set()

        # ---------- 共享变量相关函数预算（写优先 + 关键读保底） ----------
        opts_cc = self._code_context_options(code_context)
        k_raw: Any = None
        key_read_floor_raw: Any = None
        if isinstance(opts_cc, dict):
            k_raw = opts_cc.get("max_shared_var_related_functions")
            key_read_floor_raw = opts_cc.get("min_key_read_related_functions")
        if k_raw is None and isinstance(problem, dict):
            k_raw = problem.get("max_shared_var_related_functions")
        if key_read_floor_raw is None and isinstance(problem, dict):
            key_read_floor_raw = problem.get("min_key_read_related_functions")
        try:
            k_tip = int(k_raw) if k_raw is not None else 12
        except (TypeError, ValueError):
            k_tip = 12
        k_tip = max(1, min(k_tip, 12))
        try:
            key_read_floor = int(key_read_floor_raw) if key_read_floor_raw is not None else 2
        except (TypeError, ValueError):
            key_read_floor = 2
        key_read_floor = max(0, min(key_read_floor, 20))

        def _looks_like_key_read_function_name(name: str) -> bool:
            n = str(name or "").lower()
            return any(
                kw in n
                for kw in (
                    "get",
                    "read",
                    "fetch",
                    "query",
                    "find",
                    "lookup",
                    "scan",
                    "walk",
                    "traverse",
                    "visit",
                    "modify",
                    "update",
                    "access",
                )
            )

        shared_extra_ids: List[str] = []
        shared_extra_from_write = False
        if shared_var_tids and crash_fn_norm_ids:
            cand_write: Set[str] = set()
            cand_key_read: Set[str] = set()
            cand_any: Set[str] = set()
            for e in edges_list:
                if not isinstance(e, dict):
                    continue
                if e.get("type") != "use_shared_var":
                    continue
                tid = str(e.get("to_id") or "")
                if tid not in shared_var_tids:
                    continue
                raw_fid = str(e.get("from_id") or "")
                if _norm_graph_nid(raw_fid) in crash_fn_norm_ids:
                    continue
                fn = node_map.get(raw_fid)
                if not isinstance(fn, dict) or fn.get("type") != "function":
                    continue
                cand_any.add(raw_fid)
                rel = str(e.get("relation") or "")
                if _relation_write_like(rel):
                    cand_write.add(raw_fid)
                elif rel.strip().lower() == "read":
                    sig_name = str(fn.get("signature") or raw_fid)
                    if _looks_like_key_read_function_name(sig_name):
                        cand_key_read.add(raw_fid)

            shared_extra_from_write = bool(cand_write)
            crash_sid = str((crash_node or {}).get("id") or "")
            if shared_extra_from_write:
                ordered_write = sorted(
                    cand_write, key=lambda x: str((node_map.get(x) or {}).get("signature") or x)
                )
                ordered_key_read = sorted(
                    cand_key_read, key=lambda x: str((node_map.get(x) or {}).get("signature") or x)
                )
                chosen: List[str] = []
                key_quota = min(key_read_floor, len(ordered_key_read), k_tip)
                write_quota = max(0, k_tip - key_quota)

                for fid in ordered_write:
                    if len(chosen) >= write_quota:
                        break
                    if fid == crash_sid or _norm_graph_nid(fid) in crash_fn_norm_ids:
                        continue
                    snippet = (node_map.get(fid) or {}).get("snippet", [])
                    if not (isinstance(snippet, list) and snippet):
                        continue
                    chosen.append(fid)

                for fid in ordered_key_read:
                    if len(chosen) >= k_tip:
                        break
                    if fid in chosen:
                        continue
                    if fid == crash_sid or _norm_graph_nid(fid) in crash_fn_norm_ids:
                        continue
                    snippet = (node_map.get(fid) or {}).get("snippet", [])
                    if not (isinstance(snippet, list) and snippet):
                        continue
                    chosen.append(fid)

                # 余量用其它候选补齐（按签名稳定排序）
                if len(chosen) < k_tip:
                    ordered_any = sorted(
                        cand_any, key=lambda x: str((node_map.get(x) or {}).get("signature") or x)
                    )
                    for fid in ordered_any:
                        if len(chosen) >= k_tip:
                            break
                        if fid in chosen:
                            continue
                        if fid == crash_sid or _norm_graph_nid(fid) in crash_fn_norm_ids:
                            continue
                        snippet = (node_map.get(fid) or {}).get("snippet", [])
                        if not (isinstance(snippet, list) and snippet):
                            continue
                        chosen.append(fid)
                shared_extra_ids = chosen
            else:
                ordered_any = sorted(
                    cand_any, key=lambda x: str((node_map.get(x) or {}).get("signature") or x)
                )
                for fid in ordered_any:
                    if fid == crash_sid or _norm_graph_nid(fid) in crash_fn_norm_ids:
                        continue
                    snippet = (node_map.get(fid) or {}).get("snippet", [])
                    if not (isinstance(snippet, list) and snippet):
                        continue
                    shared_extra_ids.append(fid)
                    if len(shared_extra_ids) >= k_tip:
                        break

        # ---------- 按函数聚合：一个函数只展示一次，并打来源标签 ----------
        function_index: Dict[str, Dict[str, Any]] = {}

        add2line_norm_ids: Set[str] = set()
        call_chain_from_add2line = graph.get("call_chain_from_add2line", []) if isinstance(graph, dict) else []
        if isinstance(call_chain_from_add2line, list):
            for item in call_chain_from_add2line:
                if not isinstance(item, dict):
                    continue
                for nid in item.get("nodes", []) or []:
                    if isinstance(nid, str):
                        add2line_norm_ids.add(_norm_graph_nid(nid))

        primary_path_norm_ids: Set[str] = {_norm_graph_nid(nid) for nid in primary_path_nodes if isinstance(nid, str)}

        write_shared_func_norm: Set[str] = set()
        key_read_shared_func_norm: Set[str] = set()
        any_shared_func_norm: Set[str] = set()
        shared_rels_by_func_var: Dict[Tuple[str, str], Set[str]] = {}
        for e in edges_list:
            if not isinstance(e, dict) or e.get("type") != "use_shared_var":
                continue
            fid = _norm_graph_nid(str(e.get("from_id") or ""))
            tid = str(e.get("to_id") or "")
            if not fid or not tid.startswith("var|"):
                continue
            any_shared_func_norm.add(fid)
            rel = str(e.get("relation") or "unknown")
            if _relation_write_like(rel):
                write_shared_func_norm.add(fid)
            elif rel.strip().lower() == "read":
                fn2 = node_map.get(str(e.get("from_id") or ""))
                sig2 = str((fn2 or {}).get("signature") or e.get("from_id") or "")
                if _looks_like_key_read_function_name(sig2):
                    key_read_shared_func_norm.add(fid)
            shared_rels_by_func_var.setdefault((fid, tid), set()).add(rel)

        def _register_func(fid_raw: str) -> None:
            node = node_map.get(fid_raw) or node_map.get(_norm_graph_nid(fid_raw))
            if not isinstance(node, dict):
                return
            if str(node.get("type") or "") != "function":
                return
            sid = str(node.get("id") or "")
            if not sid:
                return
            nfid = _norm_graph_nid(sid)
            rec = function_index.setdefault(
                sid,
                {
                    "node": node,
                    "norm_id": nfid,
                    "tags": set(),
                    "shared_vars": {},
                    "priority": 99,
                },
            )
            tags: Set[str] = rec["tags"]
            if nfid in crash_fn_norm_ids:
                tags.add("崩溃函数")
            if nfid in primary_path_norm_ids:
                tags.add("调用链")
            if nfid in caller_to_crash_norm:
                tags.add("调用崩溃点")
            if nfid in add2line_norm_ids:
                tags.add("堆栈帧")
            if nfid in stack_frame_norm_ids:
                tags.add("栈序保留")
            if nfid in write_shared_func_norm:
                tags.add("共享变量写")
            elif nfid in key_read_shared_func_norm:
                tags.add("共享变量关键读")
            elif nfid in any_shared_func_norm:
                tags.add("共享变量读/访问")
            # 聚合共享变量命中明细
            shared_vars: Dict[str, Set[str]] = rec["shared_vars"]
            for (fid_n, tid), rels in shared_rels_by_func_var.items():
                if fid_n != nfid:
                    continue
                vn = node_map.get(tid) if isinstance(node_map, dict) else None
                vname = str((vn or {}).get("name") or tid).strip()
                if not vname:
                    continue
                shared_vars.setdefault(vname, set()).update(rels)

            if "栈序保留" in tags:
                rec["priority"] = 0
            elif "崩溃函数" in tags:
                rec["priority"] = 1
            elif "共享变量写" in tags:
                rec["priority"] = 2
            elif "调用崩溃点" in tags:
                rec["priority"] = 3
            elif "共享变量关键读" in tags:
                rec["priority"] = 4
            elif "调用链" in tags:
                rec["priority"] = 5
            elif "堆栈帧" in tags or "堆栈列表" in tags:
                rec["priority"] = 6
            elif "共享变量读/访问" in tags:
                rec["priority"] = 7
            else:
                rec["priority"] = 8

        crash_site_norm_ids: Set[str] = set(crash_fn_norm_ids)
        caller_to_crash_norm: Set[str] = set()
        if isinstance(edges_list_early, list):
            for e in edges_list_early:
                if not isinstance(e, dict):
                    continue
                if str(e.get("type") or "") not in ("calls_direct", "calls_to_crash_site"):
                    continue
                tid = _norm_graph_nid(str(e.get("to_id") or ""))
                if tid in crash_site_norm_ids:
                    caller_to_crash_norm.add(_norm_graph_nid(str(e.get("from_id") or "")))

        for nid in source_ids:
            if isinstance(nid, str):
                _register_func(nid)
        for fid in sorted(caller_to_crash_norm):
            if isinstance(fid, str) and fid:
                raw = fid
                for k, n in node_map.items():
                    if _norm_graph_nid(k) == fid:
                        raw = k
                        break
                _register_func(raw)
        for fid in shared_extra_ids:
            if isinstance(fid, str):
                _register_func(fid)

        if isinstance(graph, dict):
            for node in graph.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                if str(node.get("type") or "") != "function":
                    continue
                nid = node.get("id")
                if isinstance(nid, str) and nid.strip():
                    _register_func(nid)

        ordered_records = sorted(
            function_index.values(),
            key=lambda r: (
                int(r.get("priority", 99)),
                str((r.get("node") or {}).get("signature") or ""),
            ),
        )

        included_records, excluded_index_lines = filter_prompt_function_records(
            ordered_records,
            root_cause_norm_ids=root_cause_norm_ids,
            stack_frame_norm_ids=stack_frame_norm_ids,
            anchor_paths=stack_anchors,
            max_functions=prompt_filter_opts.max_functions_in_prompt,
        )
        # 将 prompt 裁剪/未纳入信息写入 code_context（03_code_content_provider.json），
        # 避免把元信息噪音塞进 05_ai_final_tip 文本。
        if isinstance(code_context, dict):
            diagnostics = code_context.get("diagnostics")
            if not isinstance(diagnostics, dict):
                diagnostics = {}
                code_context["diagnostics"] = diagnostics
            meta = diagnostics.get("prompt_context_meta")
            if not isinstance(meta, dict):
                meta = {}
            meta["max_functions_in_prompt"] = int(prompt_filter_opts.max_functions_in_prompt)
            meta["max_stack_frames_in_prompt"] = int(prompt_filter_opts.max_stack_frames_in_prompt)
            meta["excluded_function_index"] = list(excluded_index_lines or [])
            diagnostics["prompt_context_meta"] = meta

        if not included_records:
            lines.append("- N/A")
            lines.append("")
        else:
            from tools.function_snippet_utils import is_plausible_function_signature

            for rec in included_records:
                node = rec["node"]
                snippet = node.get("snippet", [])
                if not (isinstance(snippet, list) and snippet):
                    continue
                sig = str(node.get("signature", "N/A"))
                if not is_plausible_function_signature(sig):
                    continue
                snippet, is_complete_snippet, incomplete_reason = self._prepare_prompt_function_snippet(node, snippet)
                if not snippet:
                    continue
                lines.append(f"#### 函数源码: {sig}")
                lines.append(f"- 文件: {node.get('file', 'N/A')}")
                if not is_complete_snippet:
                    lines.append(f"- 片段说明: {incomplete_reason}")
                lines.append("- 代码片段:")
                lines.extend([str(s) for s in snippet])
                lines.append("")

        # 输出类骨架（强归因且已写入 graph 时才有）：完整类结构供结构性修复参考
        emit_class_skeleton = not (
            isinstance(crash_summary, dict)
            and crash_summary.get("selected_analysis_is_crash_thread") is False
        )
        if emit_class_skeleton:
            for nid, n in node_map.items():
                if not isinstance(n, dict) or n.get("type") != "class_skeleton":
                    continue
                skel_text = n.get("skeleton", "")
                if not skel_text.strip():
                    continue
                cls_name = n.get("class_name", "Unknown")
                skel_file = n.get("file", "N/A")
                lines.append(f"### 崩溃所属类骨架（{cls_name}）")
                lines.append(f"- 文件: {skel_file}")
                lines.append("- 说明: 以下为类的精简骨架（成员变量 + 函数签名），函数体已省略。")
                lines.append("  若修复方案需要修改成员变量声明（如 volatile → std::atomic）或其他未列出函数体的函数，")
                lines.append("  请在修复代码中明确给出对应修改。")
                lines.append("```cpp")
                lines.append(skel_text)
                lines.append("```")
                lines.append("")

        code_roots: List[str] = []
        if isinstance(problem, dict):
            prob_roots = problem.get("code_roots")
            if isinstance(prob_roots, list):
                code_roots.extend([str(p) for p in prob_roots if isinstance(p, str) and p.strip()])
            prob_root = problem.get("code_root")
            if isinstance(prob_root, str) and prob_root.strip():
                code_roots.append(prob_root)
        lines.append("")

        prompt_mode = self._resolve_prompt_mode(problem)
        unavailable_context_rule_lines: List[str] = []
        if prompt_mode == "analysis" and self._resolve_agent_loop(problem) == "context_loop":
            crash_tname = ""
            crash_has_business_frames = None
            if isinstance(crash_summary, dict):
                crash_tname = str(crash_summary.get("crash_thread_name") or "").strip()
                crash_has_business_frames = crash_summary.get("crash_thread_has_business_frames")
            if weak_attribution or crash_has_business_frames is False:
                unavailable_context_rule_lines.append("### 不可用上下文边界（context_requests 约束）")
                if crash_tname:
                    unavailable_context_rule_lines.append(
                        f"- 日志标记的崩溃线程 `{crash_tname}` 未提供可符号化的业务源码栈帧，"
                        "当前无法定位该线程的入口函数或崩溃点源码。"
                    )
                    unavailable_context_rule_lines.append(
                        f"- `{crash_tname}` 是线程名/进程名标签，不是可解析的 C/C++ 函数符号；"
                        f"禁止在 `context_requests` 中请求 `{crash_tname}::main`、`main` "
                        "或其它由线程名臆造出的入口函数源码。"
                    )
                else:
                    unavailable_context_rule_lines.append(
                        "- 日志标记的崩溃线程未提供可符号化的业务源码栈帧，"
                        "当前无法定位该线程的入口函数或崩溃点源码。"
                    )
                    unavailable_context_rule_lines.append(
                        "- 线程名/进程名标签不是可解析的 C/C++ 函数符号；"
                        "禁止在 `context_requests` 中请求 `main` 或其它由线程/进程标签臆造出的入口函数源码。"
                    )
                unavailable_context_rule_lines.append(
                    "- 如需主线程崩溃点或入口调用链，只能在分析结论中列为缺失证据/人工补充项，"
                    "不得放入 `context_requests`。"
                )
                unavailable_context_rule_lines.append(
                    "- 不要断言主线程业务代码已经被证明有问题；也不要把主线程入口作为下一轮 Agent 补充目标。"
                    "当前应将其视为崩溃承载线程/现象线程，优先分析其它有业务帧线程中的跨线程影响、"
                    "异步任务、共享对象、资源释放和生命周期问题。"
                )

        lines.append("# 崩溃分析任务")
        if prompt_mode == "fix":
            lines.append("基于提供的实际源代码，分析本次崩溃的直接原因和根本原因，并给出可以直接应用到工程中的修复代码。")
        else:
            lines.append(
                "基于提供的崩溃摘要、线程调用关系与函数源码，分析本次崩溃的直接原因、可能根因和证据充分性；"
                "仅在证据与上下文足够时给出可执行修复代码。"
            )
        lines.append("")
        lines.append("## 分析指导")
        lines.append("**关键提示**：")
        if weak_attribution:
            lines.append(
                "- 结合上文崩溃摘要、各线程调用关系与函数源码，自行选定分析起点，"
                "区分**已由证据支持的结论**与**需要进一步验证的推断**；禁止无依据猜测未给出源码的函数。"
            )
        else:
            lines.append(
                "- 先基于「崩溃摘要」中的崩溃点信息与上文函数源码论证**直接原因**；"
                "讨论**根本原因**时须有可引用的源码或栈证据，禁止无依据猜测其它文件/函数。"
            )
        lines.append(
            "- 关键结论应尽量引用上文可见证据，例如日志字段、线程栈帧、file:line 或代码片段中的可见语句；"
            "无法直接证明的内容必须标注为推断，不得编造未给出源码的函数实现。"
        )
        lines.append("- 若涉及共享数据/多线程入口（如 `*_thread` / 回调 / handler），重点检查：锁保护是否覆盖所有访问路径、加锁/解锁是否成对、是否存在数据竞争。")
        lines.append("- 若涉及对象销毁/资源释放，重点检查：是否只释放一次、释放后是否仍会被访问、是否避免“持锁删除对象本身”导致后续访问悬空对象或引入死锁。")
        lines.append("- 若修改继承链相关函数（含 `Class::Method` / override），需结合上下文判断是否保留或调整 `Base::method(...)` 等调用，并在说明中给出理由。")
        lines.append(
            "- 非静态成员函数内不得用 `if (this == nullptr)` 等形式作为 use-after-free/悬空对象 的修复；"
            "应在释放路径、任务/线程同步或所有权转移处消除非法访问。"
        )
        lines.append("")
        lines.append("**通用分析步骤**：")
        lines.append("1. 明确崩溃点的直接原因（例如空指针、越界访问、使用已释放内存等）。")
        lines.append("2. 结合调用链和共享数据流，分析导致该直接原因的上游逻辑。")
        lines.append("3. 找出与崩溃点高度相关的函数或模块，判断是否存在设计或实现层面的缺陷。")
        if prompt_mode == "fix":
            lines.append("4. 给出同时解决“症状”和“根因”的修复方案，而不仅仅是在崩溃点周围简单包裹保护代码。")
            lines.append("5. 思考修复改动是否会影响其它调用方或线程，并在方案中说明。")
        else:
            lines.append("4. 判断当前证据是否足够支持确定根因；不足时说明缺失信息与下一步排查方向。")
            lines.append("5. 若提出修复方向，应说明它解决的证据链问题；若证据不足，不要为了改而改。")
        lines.append("")
        lines.append("## 输出要求")
        if prompt_mode == "fix":
            lines.append("**必须提供**：")
            lines.append(
                "1. **证据清单（使用分项序号）**：每条须引用上文函数源码中的 file:line 或片段内可见语句。"
            )
            lines.append("2. 崩溃原因分析（直接原因和根本原因）。")
            lines.append("3. 需要修改的函数列表（仅列出需要改动的函数）。")
            lines.append(
                "4. 修复代码（仅包含「需要修改的函数」列表中的函数；每个函数给出最终完整可编译代码，"
                "不得包含未列入该列表的函数，也不得重复粘贴无需改动的原函数）。"
            )
            lines.append("以上输出内容须满足下文「必须遵守的规则」。")
            if isinstance(evidence_summary, dict) and evidence_summary.get("auto_fix_allowed") is False:
                lines.append("")
                lines.append(
                    "**重要**：当前图证据不满足自动改码条件（auto_fix_allowed=false）。"
                    "请勿在「需要修改的函数」中列出仅因栈序关联（calls_stack_order）而怀疑的函数；"
                    "不得对崩溃点函数做 this==nullptr 类防护；应给出需人工验证的调查步骤。"
                )
            lines.append("")
            lines.append("## 输出格式")
            lines.append("### 结论（崩溃定位与根因）")
            lines.append("- 直接原因：[具体原因]")
            lines.append("- 根本原因：[根本原因]")
            lines.append("- 位置：[文件:行号]")
            lines.append("")
            lines.append("#### 关键证据（引用堆栈/代码）")
            lines.append("- 证据1：[栈帧/文件:行号/代码语句]")
            lines.append("- 证据2：[栈帧/文件:行号/代码语句]")
            lines.append("")
            lines.append("### 修复方案")
            lines.append("#### 需要修改的函数（仅列出需要改动的函数）")
            lines.append("- [函数名1] - [修改原因]")
            lines.append("- [函数名2] - [修改原因]")
            lines.append("")
            lines.append("#### 无法生成完整修复代码/需人工处理（可选）")
            lines.append("- [函数名Y] - [缺少哪些上下文，为什么不能输出完整可替换函数]")
            lines.append("")
            lines.append("#### 修复代码（仅包含“需要修改的函数”）")
            lines.append(
                "（修复代码块中的函数必须与上一节「需要修改的函数」列表完全一致；"
                "只粘贴列表中每一项的最终完整可编译代码，禁止粘贴未列入列表的函数或原样复制的无关函数）"
            )
            lines.append("")
            lines.append("## 必须遵守的规则")
            lines.append("- 必须基于实际源代码进行修复；")
            lines.append("- 修复代码必须完整且可编译；")
            lines.append("- 修复代码必须相对提供的原源码包含实质逻辑变化；禁止仅调整格式、缩进、空行或只添加解释性注释作为修复。")
            lines.append(
                "- **「需要修改的函数」与「修复代码」必须一一对应**：列表有几项，修复代码块中就只包含几项完整函数；"
                "禁止多写、少写，或同一函数在列表与代码块中含义矛盾。"
            )
            lines.append(
                "- **禁止**单独列出「无需修改的函数」章节；不需要改的函数不得出现在「需要修改的函数」或修复代码中；"
                "若需在分析中说明某相关函数为何不改，仅在根因分析文字中简要带过，不要粘贴其源码。"
            )
            lines.append(
                "- 对共享状态的其它读写函数，逐个判断其与崩溃路径的关系；"
                "仅当确需修改时才纳入「需要修改的函数」与修复代码。"
            )
            lines.append("- 允许标注“不确定/需要补充信息”，但必须说明缺失的具体证据；禁止把未经证实的猜测写成结论。")
            lines.append("- 若删除或改写 `Base::method(...)`/继承链关键调用，必须在结论中明确给出语义等价性或替代路径依据。")
            lines.append(
                "- 禁止调用源码片段中未出现的成员方法；不要编造 Cancel/Abort 等异步控制 API。"
            )
            lines.append("- **修复代码格式**：每个修复函数必须使用独立的 ```cpp 围栏包裹；函数体必须完整，禁止用 `...`、`// ...`、`/* ... */`、`[其他代码]`、`其他逻辑保持不变`、`省略` 等任何占位内容代替代码行。")
            lines.append("- “需要修改的函数”列表中的每一项，都必须在“修复代码”中给出对应的完整函数定义（包含函数签名、完整函数体和全部原有必要逻辑），不能只给函数体片段、局部代码片段、diff 片段或伪代码。")
            lines.append("- 修复代码必须保持原函数签名不变：不得修改返回类型、函数名、类作用域、参数列表、const/static/virtual 等限定；构造函数和析构函数绝对不能添加返回类型（例如禁止写成 `VBool Class::~Class()`）。")
            lines.append("- 如果确实需要调整函数签名或类接口，不能输出自动替换代码；应放到“无法生成完整修复代码/需人工处理”并说明需要人工同步修改声明、定义和所有调用点。")
            lines.append("- 如果当前上下文不足以输出某个函数的完整可替换代码，必须不要把该函数列入“需要修改的函数”；应放到“无法生成完整修复代码/需人工处理”并说明缺少哪些上下文。")
            lines.append("- 禁止在修复函数中用注释表示保留原逻辑；所有未改动但仍需要保留的原代码也必须原样写出。")
        else:
            lines.append("**必须提供**：")
            if self._resolve_agent_loop(problem) == "context_loop":
                lines.append("1. 先判断是否需要 Agent 继续补充上下文。")
                lines.append(
                    "2. 如果还需要 Agent 自动补充上下文（`agent_can_fetch_more=true`）："
                    "不要输出最终分析报告、修复方案或修复代码；"
                    "只输出需要 Agent 补充什么内容，以及为什么需要这些内容。"
                )
                lines.append(
                    "   每个请求必须说明：请求类型、请求符号、为什么当前证据不足、"
                    "补充该内容后希望验证什么假设、优先级。"
                )
                lines.append(
                    "3. 如果 Agent 不应再继续自动拉取上下文（`agent_can_fetch_more=false`），"
                    "再输出完整崩溃分析报告：结论置信度、关键证据、相关代码分析、修复或排查建议。"
                )
                lines.append(
                    "   `agent_can_fetch_more=false` 仅表示停止 Agent 多轮补上下文，"
                    "不代表证据已齐全；仍缺的信息应写入「人工补充证据」。"
                )
                lines.append("4. 首轮应尽量一次性列出所有高价值补充请求（按优先级排序），避免逐轮试探式追加。")
            else:
                lines.append("1. **结论置信度**：高 / 中 / 低，并说明判断依据。")
                lines.append(
                    "2. **关键证据清单（使用分项序号）**：引用上文可见日志字段、线程栈帧、file:line 或代码语句。"
                )
                lines.append("3. 崩溃直接原因与可能根因；不确定处必须标注为推断，并说明缺失证据。")
                lines.append("4. 相关函数/模块分析：说明为什么相关、是否需要修改、还需要验证什么。")
                lines.append("5. 修复建议：若证据充分，可给出最小修复方案；若证据不足，只给排查建议或人工确认点。")
                lines.append(
                    "6. 如需给出代码，只输出有充分上下文且确需修改的函数；"
                    "不要补写未给出的实现或编造源码片段中没有的 API。"
                )
            lines.append("以上输出内容须满足下文「必须遵守的规则」。")
            if isinstance(evidence_summary, dict) and evidence_summary.get("auto_fix_allowed") is False:
                lines.append("")
                lines.append(
                    "**重要**：当前图证据不足以支持直接产出补丁。"
                    "请勿把仅因栈序关联而出现的函数写成确定修改点；"
                    "不得对崩溃点函数做 this==nullptr 类防护；应给出需人工验证的调查步骤。"
                )
            lines.append("")
            lines.append("## 输出格式")
            if self._resolve_agent_loop(problem) == "context_loop":
                lines.append("### 情况 A：还需要 Agent 补充上下文时")
                lines.append("只输出以下内容，不要输出最终结论、修复方案或代码：")
                lines.append("")
                lines.append("### Agent 将回填的内容（按 type 选择请求）")
                lines.append(
                    "- `function`：返回**函数定义处完整源码**（含签名与函数体）。"
                    "模板类方法优先写全 `CVList<TArgs>::Method`（如 `CVList<CBaseLayer*, CBaseLayer*>::RemoveAll`）；"
                    "也可写 `CVList::RemoveAll`，Agent 会自动搜索模板实现。"
                )
                lines.append(
                    "- `field`：返回**所属类的成员声明**（优先头文件），请写 `ClassName::member`；"
                    "若需查看**类型/容器本身的类结构**，可写 `field: TypeName`（如 `CVList`），"
                    "将返回 `class/struct` 声明块。"
                    "若无声明则返回同类中的初始化语句；**不包含**调用链或并发读写路径。"
                )
                lines.append(
                    "- `references`：返回符号在所属类相关文件中的**读写/引用位置**（文件:行 + 片段）。"
                )
                lines.append("- `callers`：返回**调用方函数名 + 调用点片段**。")
                lines.append(
                    "- 若希望验证调用链、`RemoveAll()` 后是否仍被访问、原子读写细节，"
                    "应使用 `references` 或 `function`，不要全部塞进 `field`。"
                )
                lines.append("")
                lines.append("### 需要 Agent 补充的上下文")
                lines.append("- 当前还不能形成最终结论的原因：[简要说明缺少哪些关键证据]")
                lines.append("- 下一轮需要 Agent 补充的内容：")
                lines.append("  1. `[符号]`（function/field/references/callers）：")
                lines.append("     - 原因：[为什么当前证据不足]")
                lines.append("     - 希望验证：[补充后要验证的根因假设]")
                lines.append("     - 优先级：[high/medium/low]")
                lines.append(
                    "     - 请求类型说明：[该 type 将回填什么；若需多种证据请拆成多个请求]"
                )
                lines.append("")
                lines.append("```json")
                lines.append("{")
                lines.append('  "agent_can_fetch_more": true,')
                lines.append('  "context_requests": [')
                lines.append("    {")
                lines.append('      "type": "function",')
                lines.append('      "symbol": "ClassName::FunctionName",')
                lines.append('      "expected_return_form": "function_source",')
                lines.append('      "reason": "说明为什么需要该上下文，以及补充后要验证什么假设",')
                lines.append(
                    '      "fulfillment_note": "可选：补充说明期望拿到什么形态的证据",'
                )
                lines.append('      "priority": "high"')
                lines.append("    }")
                lines.append("  ]")
                lines.append("}")
                lines.append("")
                lines.append("`expected_return_form` 必须与 `type` 一致：")
                lines.append(
                    "`function`→`function_source`；`field`→`member_declaration`；"
                    "`references`→`read_write_references`；`callers`→`caller_snippets`。"
                )
                lines.append("```")
                lines.append("")
                lines.append("### 情况 B：不再需要 Agent 补充上下文时")
                lines.append(
                    "输出完整崩溃分析报告，并在末尾输出 `agent_can_fetch_more=false` JSON："
                )
                lines.append("")
            lines.append("### 结论（崩溃定位与置信度）")
            lines.append("- 置信度：[高/中/低]")
            lines.append("- 直接原因：[已证实的直接原因；若不足请说明]")
            lines.append("- 可能根因：[根因或推断路径；不确定处必须标注]")
            lines.append("- 位置：[文件:行号/栈帧/日志字段]")
            lines.append("")
            lines.append("#### 关键证据（引用日志/堆栈/代码）")
            lines.append("- 证据1：[栈帧/文件:行号/代码语句/日志字段]")
            lines.append("- 证据2：[栈帧/文件:行号/代码语句/日志字段]")
            lines.append("")
            lines.append("### 相关代码分析")
            lines.append("- [函数或模块]：[为什么相关、支持或反驳什么结论]")
            lines.append("")
            lines.append("### 修复或排查建议")
            lines.append(
                "- 若证据足够：[最小修复方向、风险点和验证方式；"
                "若上下文完整且确需修改，可给出完整可替换的 C/C++ 修复代码块]"
            )
            lines.append(
                "- 若证据不足：[缺失信息、建议补充的日志/断点/复现路径/人工确认点；"
                "不要强行输出修复代码]"
            )
            if self._resolve_agent_loop(problem) == "context_loop":
                lines.append("")
                lines.append("### Agent 上下文获取结束")
                lines.append("如果已经给出最终分析，末尾必须输出：")
                lines.append("```json")
                lines.append('{"agent_can_fetch_more": false, "context_requests": []}')
                lines.append("```")
                lines.append("")
                lines.append("补充说明：支持的 `context_requests[].type` 与 Agent 回填形式：")
                lines.append(
                    "`function`→函数完整源码；`field`→成员声明（优先 .h）；"
                    "`references`→读写/引用位置；`callers`→调用方片段。"
                )
            lines.append("")
            lines.append("## 必须遵守的规则")
            if unavailable_context_rule_lines:
                lines.extend(unavailable_context_rule_lines)
                lines.append("")
            if self._resolve_agent_loop(problem) == "context_loop":
                lines.append("### context_requests 约束")
                lines.append("- `type=function` 只能请求函数源码，禁止把成员变量、字段或函数指针伪装成函数请求。")
                lines.append(
                    "- `type=field` 仅用于成员**类型/声明**；Agent 回填成员声明，不回答调用链或并发读写。"
                )
                lines.append(
                    "- 如需字段读写、`RemoveAll()` 后访问、原子操作路径，请使用 `type=references` 或补充相关 `function`。"
                )
                lines.append("- 如需某函数的调用方，请使用 `type=callers`。")
                lines.append("- 单个请求只对应一种回填形式；复合问题应拆成多个 `context_requests`。")
                lines.append(
                    "- 每个 `context_requests[]` 必须包含 `expected_return_form`；"
                    "可与 `fulfillment_note` 补充说明。"
                )
                lines.append("- 首轮应尽量一次性列出所有高价值补充请求；后续轮不得重复请求已补充、已拒绝或已跳过的 symbol。")
                lines.append(
                    "- 后续轮只允许请求由新增上下文直接引出的新函数、字段或引用；"
                    "若没有明确新增线索，或剩余缺口只能人工补充，应输出 `agent_can_fetch_more=false`。"
                )
                lines.append("")
            lines.append("- 必须基于实际日志、堆栈和源码片段分析；不得编造未给出源码的函数实现。")
            lines.append("- 允许说明“不确定/需要补充信息”，但必须指出缺失的具体证据；禁止把未经证实的猜测写成结论。")
            if self._resolve_agent_loop(problem) == "context_loop":
                lines.append(
                    "- 当输出 `agent_can_fetch_more=true` 时，禁止输出最终分析报告、"
                    "修复方案、修复代码或“需要修改的函数”。"
                )
                lines.append(
                    "- 只有输出 `agent_can_fetch_more=false` 时，才允许输出完整分析报告和修复或排查建议。"
                )
            lines.append(
                "- 当前是偏分析模式：不强制输出修复代码；"
                "证据不足时只给分析方法、排查路径和人工确认点；"
                "证据充分且上下文完整时，可给出直接可用的 C/C++ 修复代码块，并明确对应证据链。"
            )
            lines.append("- 对共享状态的其它读写函数，应逐个判断其与崩溃路径的关系；不要因为函数被列出就默认需要修改。")
            lines.append("- 禁止调用源码片段中未出现的成员方法；不要编造 Cancel/Abort 等异步控制 API。")
            lines.append("- 非静态成员函数内不得用 `if (this == nullptr)` 等形式作为 use-after-free/悬空对象 的修复。")
            lines.append("- 若输出修复代码，必须仅针对证据充分且上下文完整的函数，并保持原函数签名不变。")
        lines.append("")
        if memory_context and BaseCrashAnalysisWorkflow._include_memory_context_in_final_tip(
            problem
        ):
            lines.append("## 规则与经验模式参考")
            lines.append(memory_context)
            lines.append("")
        if prompt_mode == "fix":
            lines.append(
                "请基于以上信息，并严格遵循前文「崩溃分析任务」「输出格式」与「必须遵守的规则」中的要求，"
                "给出专业的崩溃分析，并提供可直接应用的修复代码。"
            )
        else:
            lines.append(
                "请基于以上信息，并严格遵循前文「崩溃分析任务」「输出格式」与「必须遵守的规则」中的要求，"
                "给出专业的崩溃分析；仅在证据和上下文足够时提供修复代码。"
            )

        return "\n".join(lines)

    def _extract_function_snippet_from_resolved_frame(
        self,
        frame: Dict[str, Any],
        code_roots: List[str],
    ) -> List[str]:
        """基于 resolved_file + resolved_line 兜底回源提取函数片段。"""
        resolved_file = frame.get("resolved_file")
        resolved_line = frame.get("resolved_line")
        if not isinstance(resolved_file, str) or not resolved_file.strip():
            return []
        try:
            line_no = int(resolved_line)
        except (TypeError, ValueError):
            return []
        if line_no <= 0:
            return []

        local_file = self._resolve_source_file_path(resolved_file, code_roots)
        if not local_file or not os.path.exists(local_file):
            return []

        try:
            with open(local_file, "r", encoding="utf-8", errors="ignore") as f:
                file_lines = f.read().splitlines()
        except Exception:
            return []
        if not file_lines or line_no > len(file_lines):
            return []

        start, end = self._find_enclosing_function_block(file_lines, line_no)
        if start is None or end is None:
            return []
        snippet = file_lines[start:end + 1]
        if not snippet:
            return []
        return snippet

    @staticmethod
    def _extract_simple_name_from_signature(signature: str) -> str:
        s = str(signature or "").strip()
        if not s:
            return ""
        paren_idx = s.find("(")
        head = s[:paren_idx].strip() if paren_idx > 0 else s
        head = BaseCrashAnalysisWorkflow._strip_template_args(head)
        if "::" in head:
            head = head.split("::")[-1].strip()
        parts = head.split()
        if parts:
            head = parts[-1].strip()
        head = head.lstrip("*&")
        m = re.search(r"([~]?[A-Za-z_]\w*)$", head)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _strip_template_args(text: str) -> str:
        out: List[str] = []
        depth = 0
        for ch in str(text or ""):
            if ch == "<":
                depth += 1
                continue
            if ch == ">" and depth > 0:
                depth -= 1
                continue
            if depth == 0:
                out.append(ch)
        return "".join(out)

    @classmethod
    def _sanitize_prompt_function_snippet(cls, signature: str, snippet: List[str]) -> List[str]:
        """净化函数片段：去除前置污染，并尽量裁剪到目标函数闭合括号。"""
        if not (isinstance(snippet, list) and snippet):
            return snippet
        token = cls._extract_simple_name_from_signature(signature)
        if not token:
            return snippet

        start_idx = cls._find_signature_index_in_snippet(signature, snippet)
        if start_idx is None:
            return snippet

        trimmed = [str(x) for x in snippet[start_idx:]]
        end_idx = cls._find_function_end_index(trimmed)
        if end_idx is not None:
            trimmed = trimmed[: end_idx + 1]
        return trimmed if trimmed else snippet

    @classmethod
    def _find_signature_index_in_snippet(
        cls,
        signature: str,
        snippet: List[str],
    ) -> Optional[int]:
        token = cls._extract_simple_name_from_signature(signature)
        if not token:
            return None
        pat = re.compile(rf"(?:\b\w+\s*::\s*)?\b~?{re.escape(token)}\s*(?:\[[^\]]+\])?\s*\(")
        for i, ln in enumerate(snippet):
            s = str(ln or "").strip()
            if not s:
                continue
            if ";" in s and "{" not in s:
                continue
            if pat.search(s):
                return i
        return None

    @staticmethod
    def _snippet_has_omission_marker(snippet: List[str]) -> bool:
        markers = (
            "lines omitted",
            "more lines",
            "[truncated]",
            "PROMPT TRUNCATED",
            "代码省略",
            "省略",
            "其他代码",
        )
        for ln in snippet or []:
            s = str(ln or "")
            if any(m in s for m in markers):
                return True
            if re.search(r"\.\.\.\s*\[\d+\s+", s):
                return True
        return False

    @classmethod
    def _find_definition_line_in_file(cls, file_path: str, signature: str) -> Optional[int]:
        token = cls._extract_simple_name_from_signature(signature)
        if not file_path or not token or not os.path.isfile(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception:
            return None

        token_pat = re.compile(rf"(?:\b\w+\s*::\s*)?{re.escape(token)}\s*\(")
        for idx, line in enumerate(lines):
            stripped = str(line or "").strip()
            if not stripped or stripped.startswith("//"):
                continue
            if not token_pat.search(stripped):
                continue
            # 函数定义允许左括号在后续几行，但在遇到分号前必须出现 "{"。
            head = ""
            for j in range(idx, min(len(lines), idx + 8)):
                head += "\n" + str(lines[j] or "")
                if "{" in lines[j]:
                    return idx + 1
                if ";" in lines[j]:
                    break
        return None

    @staticmethod
    def _find_function_end_index(snippet: List[str]) -> Optional[int]:
        depth = 0
        seen_open = False
        for i, ln in enumerate(snippet):
            for ch in ln:
                if ch == "{":
                    depth += 1
                    seen_open = True
                elif ch == "}":
                    depth -= 1
                    if seen_open and depth == 0:
                        return i
        return None

    @classmethod
    def _is_complete_function_snippet(cls, snippet: List[str]) -> bool:
        return cls._find_function_end_index(snippet) is not None

    def _prepare_prompt_function_snippet(
        self,
        node: Dict[str, Any],
        raw_snippet: List[str],
    ) -> Tuple[List[str], bool, str]:
        """为 05 提示词准备函数片段：优先回源重提完整函数，并做完整性校验。"""
        sig = str(node.get("signature") or "")
        snippet = self._sanitize_prompt_function_snippet(sig, raw_snippet)
        has_signature = self._find_signature_index_in_snippet(sig, snippet) is not None
        has_omission = self._snippet_has_omission_marker(snippet)
        if has_signature and not has_omission and self._is_complete_function_snippet(snippet):
            return snippet, True, ""

        file_path = str(node.get("file") or "").strip()
        token = self._extract_simple_name_from_signature(sig)
        if file_path and os.path.isfile(file_path) and token:
            line_no: Optional[int] = None
            start_line = node.get("snippet_start_line")
            sig_idx = self._find_signature_index_in_snippet(sig, raw_snippet)
            if sig_idx is not None and not has_omission:
                try:
                    if start_line is not None:
                        line_no = int(start_line) + int(sig_idx)
                except (TypeError, ValueError):
                    line_no = None

            # 截断片段、缺少签名或行号不可信时，回源按函数定义全局查找。
            if line_no is None or line_no <= 0 or has_omission or not has_signature:
                line_no = self._find_definition_line_in_file(file_path, sig)

            if line_no is None or line_no <= 0:
                line_no = 1

            try:
                from tools.snippet_extractor_tool import SnippetExtractorTool

                out = SnippetExtractorTool().execute(
                    {
                        "file_path": file_path,
                        "line_number": line_no,
                        "function_name": token,
                        "max_code_length": 0,
                    }
                )
                extracted = out.get("snippet")
                if isinstance(extracted, list) and extracted:
                    extracted_lines = [str(x) for x in extracted]
                    if bool(out.get("is_complete_function")) and self._is_complete_function_snippet(extracted_lines):
                        return extracted_lines, True, ""
                    snippet = self._sanitize_prompt_function_snippet(sig, extracted_lines)
            except Exception:
                pass

        reason = "函数源码片段不完整：未能从源文件重提取到闭合函数体。"
        return snippet, False, reason

    def _resolve_source_file_path(self, resolved_file: str, code_roots: List[str]) -> Optional[str]:
        if os.path.exists(resolved_file):
            return resolved_file
        if not code_roots:
            return None
        normalized = resolved_file.replace("\\", "/")
        markers = ["engine-dev/src/", "src/"]
        suffixes: List[str] = []
        for marker in markers:
            idx = normalized.find(marker)
            if idx >= 0:
                suffixes.append(normalized[idx + len(marker):])
        # 回退：取尾部路径，避免映射失败
        parts = [p for p in normalized.split("/") if p]
        if len(parts) >= 4:
            suffixes.append("/".join(parts[-4:]))
        if len(parts) >= 3:
            suffixes.append("/".join(parts[-3:]))

        for root in code_roots:
            if not isinstance(root, str) or not root.strip():
                continue
            base = root.rstrip("/")
            for suffix in suffixes:
                candidate = os.path.join(base, suffix)
                if os.path.exists(candidate):
                    return candidate
        return None

    def _find_enclosing_function_block(self, file_lines: List[str], line_no: int) -> Tuple[Optional[int], Optional[int]]:
        """按行号向上回溯函数起点，再按大括号配对提取函数体。"""
        idx = max(0, min(len(file_lines) - 1, line_no - 1))
        control_keywords = ("if", "for", "while", "switch", "catch")

        for probe in range(idx, max(-1, idx - 240), -1):
            line = file_lines[probe].strip()
            if not line or "{" not in line:
                continue

            header_idx: Optional[int] = None
            for h in range(probe, max(-1, probe - 8), -1):
                h_line = file_lines[h].strip()
                if "(" in h_line and ")" in h_line and ";" not in h_line:
                    header_idx = h
                    break
            if header_idx is None:
                continue

            header_line = file_lines[header_idx].strip()
            if not header_line or header_line.startswith("//"):
                continue
            # 防止将 if/for/while 等控制流误识别为函数头
            header_token = header_line.split("(", 1)[0].strip().split()[-1].lower() if "(" in header_line else ""
            if header_token in control_keywords:
                continue

            sig_parts = [file_lines[h].strip() for h in range(max(0, header_idx - 2), header_idx + 1)]
            sig_text = " ".join([s for s in sig_parts if s]).strip()
            lowered = sig_text.lower()
            if any(lowered.startswith(k + " ") or lowered.startswith(k + "(") for k in control_keywords):
                continue
            if not re.search(r"[A-Za-z_~][\w:<>~\s\*&]*\([^;]*\)", sig_text):
                continue

            brace_balance = 0
            saw_open = False
            for end in range(probe, len(file_lines)):
                text = file_lines[end]
                brace_balance += text.count("{")
                if text.count("{") > 0:
                    saw_open = True
                brace_balance -= text.count("}")
                if saw_open and brace_balance == 0 and end >= probe:
                    return header_idx, end
        return None, None


# ==================== iOS Crash Analysis Workflow ====================

class iOSCrashAnalyzeWorkflow(BaseCrashAnalysisWorkflow):
    """iOS 崩溃分析工作流"""

    def __init__(self):
        super().__init__()
        self.platform = "ios"

    @property
    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="ios_crash_analyze",
            description="分析 iOS 平台崩溃日志，定位 Objective-C/Swift 代码问题",
            problem_type="ios_crash",
            required_tools=["crash_log_parser", "add2line_resolver", "code_content_provider"],
            version="1.0.0",
            metadata={
                "platform": "iOS",
                "languages": ["Objective-C", "Swift"]
            }
        )


# ==================== Android Crash Analysis Workflow ====================

class AndroidCrashAnalyzeWorkflow(BaseCrashAnalysisWorkflow):
    """Android 崩溃分析工作流"""

    def __init__(self):
        super().__init__()
        self.platform = "android"

    @property
    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="android_crash_analyze",
            description="分析 Android 平台崩溃日志，定位 Java/Kotlin/C++ 代码问题",
            problem_type="android_crash",
            required_tools=["crash_log_parser", "add2line_resolver", "code_content_provider"],
            version="1.0.0",
            metadata={
                "platform": "Android",
                "languages": ["Java", "Kotlin", "C++", "NDK"]
            }
        )


# ==================== Generic Crash Analysis Workflow ====================

class GenericCrashAnalyzeWorkflow(BaseCrashAnalysisWorkflow):
    """通用崩溃分析工作流（自动检测平台）"""

    def __init__(self):
        super().__init__()
        self.platform = "auto"

    @property
    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="crash_analysis",
            description="通用崩溃分析工作流，自动检测平台并分析",
            problem_type="crash_analysis",
            required_tools=["crash_log_parser", "add2line_resolver", "code_content_provider"],
            version="1.0.0",
            metadata={
                "platform": "auto",
                "languages": ["auto"]
            }
        )

    def solve(self, problem: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        # 自动检测平台
        crash_log = problem.get("crash_log", "")

        # 简单的平台检测
        if "iOS" in crash_log or "Swift" in crash_log or "SIGSEGV" in crash_log:
            ios_workflow = iOSCrashAnalyzeWorkflow()
            return ios_workflow.solve(problem, context)
        elif "Android" in crash_log or "java.lang" in crash_log or "Native crash" in crash_log:
            android_workflow = AndroidCrashAnalyzeWorkflow()
            return android_workflow.solve(problem, context)
        else:
            return super().solve(problem, context)
