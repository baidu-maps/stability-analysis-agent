#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内置工作流实现 - 将现有的分析能力封装为 Workflow
"""

import logging
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from cli.phase_spinner import PhaseSpinner

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

from rag.feature_extractor import extract_features, build_pattern_query
from rag.runtime import get_ai_stability_analyzer_class, rag_stack_available


def _get_rag_analyzer_class():
    return get_ai_stability_analyzer_class()


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


def _rag_available() -> bool:
    return rag_stack_available() and extract_features is not None and build_pattern_query is not None


# ==================== Base Crash Analysis Workflow ====================

class BaseCrashAnalysisWorkflow(BaseWorkflow):
    """崩溃分析工作流基类"""

    def __init__(self):
        self.platform = "unknown"

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
        elif scope == "parse_only":
            total_steps = 2
        elif scope == "prompt_only":
            total_steps = 4
        else:
            # full scope: 4 steps in workflow + step 5 (apply fix) handled by CLI
            apply_ai_fixes = problem.get("apply_ai_fixes", True)
            total_steps = 5 if apply_ai_fixes else 4

        try:
            # Step 1: 解析崩溃日志
            with PhaseSpinner("解析崩溃日志", step=1, total_steps=total_steps):
                logger.info(f"[{self.definition.name}] Step 1: Parsing crash log...")
                parse_result = context.execute_tool("crash_log_parser", {
                    "log_content": crash_log
                })

            if scope == "parse_log_only":
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "resolved_stack": {},
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

            # Step 2: 堆栈符号化
            with PhaseSpinner("堆栈符号化", step=2, total_steps=total_steps):
                logger.info(f"[{self.definition.name}] Step 2: Resolving symbols...")
                resolved = context.execute_tool("add2line_resolver", {
                    "crash_json": json.dumps(parse_result),
                    "library_dir": library_dir
                })

            if scope == "parse_only":
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "resolved_stack": resolved,
                    "code_context": {},
                    "analysis": None,
                    "final_tip": None,
                    "note": "scope=parse_only，仅执行日志解析+符号化",
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

            # Step 3: 定位崩溃源码
            with PhaseSpinner("定位崩溃源码", step=3, total_steps=total_steps):
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
                memory_context = ""
                rule_hits: List[Dict[str, Any]] = []
                pattern_hits: List[Dict[str, Any]] = []
                evidence_map: Dict[str, Any] = {}
                strategy_hits: List[Dict[str, Any]] = []
                decision_trace: List[Dict[str, Any]] = []
                vector_used = False

                rag_result = self._collect_memory_context(problem, parse_result, resolved, code_context)
                if rag_result:
                    memory_context = rag_result.get("memory_context", "")
                    rule_hits = rag_result.get("rule_hits", []) or []
                    pattern_hits = rag_result.get("pattern_hits", []) or []
                    evidence_map = rag_result.get("evidence_map", {}) or {}
                    strategy_hits = rag_result.get("strategy_hits", []) or []
                    decision_trace = rag_result.get("decision_trace", []) or []
                vector_used = bool(rag_result.get("vector_used", False))

            # 检查是否有 LLM
            if context.llm is None:
                logger.warning(f"[{self.definition.name}] No LLM configured, skipping AI analysis")
                assembled_prompt = ""
                if scope == "prompt_only":
                    # prompt_only 模式：完整工具链已就绪，仅生成可复用提示词，不调用 LLM
                    with PhaseSpinner("生成分析提示词", step=4, total_steps=total_steps):
                        assembled_prompt = self._build_prompt_final_tip(
                            parse_result=parse_result,
                            resolved=resolved,
                            code_context=code_context,
                            memory_context=memory_context,
                            problem=problem,
                        )
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "resolved_stack": resolved,
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
                    "metadata": {
                        "problem_type": self.definition.problem_type
                    }
                }

            # Step 4: LLM 分析
            _phase4_spinner = PhaseSpinner("AI 分析根因", step=4, total_steps=total_steps)
            _phase4_spinner.__enter__()
            logger.info(f"[{self.definition.name}] Step 4: LLM analysis...")
            # 与 prompt_only 模式对齐：统一使用同一份 final_tip 作为 LLM 输入与 05 文件落盘内容
            analysis_prompt = self._build_prompt_final_tip(
                parse_result=parse_result,
                resolved=resolved,
                code_context=code_context,
                memory_context=memory_context,
                problem=problem,
            )
            # 超长 prompt 会在部分网关返回空错误体（如 {'type':'','message':''}），
            # 这里做硬性上限保护，优先保证请求可被受理。
            # 默认不限制 prompt 字符长度：优先保证信息完整性。
            # 仅当显式配置 max_prompt_chars / SA_MAX_PROMPT_CHARS 且 > 0 时才启用截断。
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
            if prompt_cap and len(analysis_prompt) > prompt_cap:
                orig_len = len(analysis_prompt)
                analysis_prompt = _truncate_analysis_prompt(analysis_prompt, prompt_cap)
                logger.warning(
                    f"[{self.definition.name}] analysis_prompt too long ({orig_len} chars), "
                    f"smart-truncated to {len(analysis_prompt)} chars (max_prompt_chars={prompt_cap})"
                )
            # 动态 max_tokens 策略：
            # - 首轮不再盲打固定 12000，而是受当前 provider 配置上限约束；
            # - 失败后自动降档重试，降低超时/网关拒绝概率。
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
            token_attempts.append(None)  # 最后兜底走适配器默认值

            llm_response = None
            last_llm_exc: Optional[Exception] = None
            for idx, tok in enumerate(token_attempts, start=1):
                try:
                    if tok is None:
                        logger.info(f"[{self.definition.name}] LLM attempt {idx}: default max_tokens")
                        llm_response = context.call_llm(analysis_prompt, temperature=0)
                    else:
                        logger.info(f"[{self.definition.name}] LLM attempt {idx}: max_tokens={tok}")
                        llm_response = context.call_llm(analysis_prompt, max_tokens=tok, temperature=0)
                    # 检测输出是否被 max_tokens 截断：completion_tokens >= max_tokens * 0.95
                    if tok is not None and llm_response is not None:
                        usage = getattr(llm_response, "usage", None) or {}
                        completion_tokens = usage.get("completion_tokens", 0) or 0
                        if completion_tokens >= tok * 0.95:
                            logger.warning(
                                f"[{self.definition.name}] LLM output likely truncated "
                                f"(completion_tokens={completion_tokens}, max_tokens={tok}), retrying with more tokens"
                            )
                            continue
                    break
                except Exception as llm_exc:
                    last_llm_exc = llm_exc
                    logger.warning(
                        f"[{self.definition.name}] LLM attempt {idx} failed "
                        f"(max_tokens={tok if tok is not None else 'default'}): {llm_exc}"
                    )

            if llm_response is None:
                _phase4_spinner.__exit__(RuntimeError, None, None)
                raise RuntimeError(f"LLM call failed after retries: {last_llm_exc}")

            # 提取 token 使用统计
            if hasattr(llm_response, "usage") and isinstance(llm_response.usage, dict):
                _phase4_spinner.set_tokens(
                    input_tokens=llm_response.usage.get("prompt_tokens"),
                    output_tokens=llm_response.usage.get("completion_tokens"),
                )
            _phase4_spinner.__exit__(None, None, None)

            return {
                "status": "success",
                "platform": self.platform,
                "workflow": self.definition.name,
                "parse_result": parse_result,
                "resolved_stack": resolved,
                "code_context": code_context,
                "analysis": llm_response.content,
                "final_tip": analysis_prompt,
                "rule_hits": rule_hits,
                "pattern_hits": pattern_hits,
                "evidence_map": evidence_map,
                "strategy_hits": strategy_hits,
                "decision_trace": decision_trace,
                "vector_used": vector_used,
                "memory_context": memory_context,
                "metadata": {
                    "problem_type": self.definition.problem_type
                }
            }

        except Exception as e:
            logger.error(f"[{self.definition.name}] Error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "workflow": self.definition.name
            }

    def _build_analysis_prompt(self, parse_result: Dict, resolved: Dict, code_context: Dict, memory_context: str = "") -> str:
        """构建分析提示词"""
        crash_info = parse_result.get("crash_info", {}) if isinstance(parse_result, dict) else {}
        cc_summary = code_context.get("crash_summary", {}) if isinstance(code_context, dict) else {}
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
        valid = {"full", "prompt_only", "parse_only", "parse_log_only"}
        raw = str(problem.get("scope") or "").strip()
        if raw in valid:
            return raw
        legacy_scope = str(problem.get("run_scope") or "").strip()
        if legacy_scope in {"parse_only", "parse_log_only"}:
            return legacy_scope
        if bool(problem.get("skip_ai", False)):
            return "prompt_only"
        return "full"

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
            (code_context.get("code_context_options") or {})
            if isinstance(code_context, dict)
            else {},
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

        node_id = crash_summary.get("node_id") if isinstance(crash_summary, dict) else None
        if isinstance(node_id, str):
            _append(node_id)

        path_nodes = [n for n in primary_path_nodes if isinstance(n, str)]
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
        frames = resolved.get("resolved_frames") or []
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
        if not isinstance(crash_summary, dict):
            return False
        source = str(crash_summary.get("crash_location_source") or "").strip()
        if source == "from_log_deduce":
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
    def _crash_location_prompt_conclusion(
        crash_summary: Any,
        resolved: Any,
    ) -> str:
        """供 LLM 提示词使用的崩溃点定位结论（一句，不含过程说明）。"""
        source = str(crash_summary.get("crash_location_source") or "").strip()
        if BaseCrashAnalysisWorkflow._prompt_has_confident_crash_line(crash_summary, resolved):
            return "结论：崩溃点已通过符号化堆栈关联到具体源码行（置信度有限，需结合函数上下文验证）。"
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
        if not isinstance(crash_summary, dict):
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

    def _build_prompt_final_tip(
        self,
        parse_result: Dict[str, Any],
        resolved: Dict[str, Any],
        code_context: Dict[str, Any],
        memory_context: str = "",
        problem: Optional[Dict[str, Any]] = None,
    ) -> str:
        """构建供 prompt_only 模式或作为 LLM 输入的统一提示词。"""
        crash_summary = code_context.get("crash_summary", {}) if isinstance(code_context, dict) else {}
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
        lines.append("下面是本次崩溃的相关信息：")
        lines.append("")
        lines.append("## 崩溃摘要")
        if isinstance(crash_summary, dict):
            for key in [
                "error_type",
                "thread_id",
            ]:
                if crash_summary.get(key) is not None:
                    lines.append(f"- {key}: {crash_summary.get(key)}")
        lines.append("")

        lines.append("## 崩溃上下文")
        lines.append("崩溃函数被调用的路径（基于代码结构推断，自上而下）:")
        edges_list_early = graph.get("edges", []) if isinstance(graph, dict) else []
        evidence_summary = (
            graph.get("evidence_summary") if isinstance(graph, dict) else None
        )
        has_calls_direct = False
        has_calls_to_crash_site = False
        has_calls_stack_order = False
        has_shared_var_write_upstream = False
        if isinstance(evidence_summary, dict):
            has_calls_direct = bool(evidence_summary.get("has_calls_direct"))
            has_calls_to_crash_site = bool(evidence_summary.get("has_calls_to_crash_site"))
            has_calls_stack_order = bool(evidence_summary.get("has_calls_stack_order"))
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
                elif et == "use_shared_var":
                    rel = str(e.get("relation") or "").strip().lower()
                    if rel in ("write", "assign", "delete"):
                        has_shared_var_write_upstream = True
        call_paths = graph.get("call_chain_from_code", []) if isinstance(graph, dict) else []
        primary_path_nodes: List[str] = []
        all_path_nodes_list: List[List[str]] = []
        if isinstance(call_paths, list) and call_paths:
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
        path_label = "路径1"
        if isinstance(call_paths, list):
            for path in call_paths:
                if not isinstance(path, dict):
                    continue
                desc = str(
                    path.get("inference") or path.get("description") or ""
                ).strip()
                if desc == "inferred_from_add2line_stack_order":
                    path_label = "路径1（addr2line 栈序；静态未校验相邻帧调用关系）"
                    break
        if primary_path_nodes:
            if has_calls_direct or has_calls_to_crash_site:
                lines.append("- 证据强度说明：高置信度，可单独作为改码依据。")
            elif "addr2line 栈序" in path_label or has_calls_stack_order:
                lines.append("- 证据强度说明：本节主要来自栈序关联，属于线索证据，不能单独作为改码依据。")
        if all_path_nodes_list:
            for path_idx, path_nodes in enumerate(all_path_nodes_list, 1):
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

        lines.append("根据堆栈顺序解析的函数/帧语义列表（按调用顺序，自下而上）：")
        resolved_frames = resolved.get("resolved_frames", []) if isinstance(resolved, dict) else []
        if isinstance(resolved_frames, list) and resolved_frames:
            lines.append("- 证据强度说明：低置信度，只能作为排查线索，不能单独作为改码依据。")
        if isinstance(resolved_frames, list):
            for idx, frame in enumerate(resolved_frames, 1):
                if not isinstance(frame, dict):
                    continue
                # add2line 通常将结果写入 resolved_* 字段，这里需要优先读取，避免误显示为 N/A
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
                if file_path in (None, "", "None") or line_no in (None, "", "None"):
                    lines.append(f"- [第{idx}帧][源码函数] {func}")
                else:
                    lines.append(f"- [第{idx}帧][源码函数] {func} ({file_path}:{line_no})")
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

        self._append_crash_location_prompt_section(
            lines, crash_summary, crash_node, resolved
        )

        lines.append("## 以上涉及的函数源码")
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
        opts_cc = code_context.get("code_context_options") if isinstance(code_context, dict) else None
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
            k_tip = int(k_raw) if k_raw is not None else 20
        except (TypeError, ValueError):
            k_tip = 20
        k_tip = max(1, min(k_tip, 20))
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
            sig = str(node.get("signature") or "")
            # 默认跳过信号处理/日志类函数，避免稀释主根因函数；崩溃函数例外。
            if sid != str((crash_node or {}).get("id") or "") and re.search(
                r"\b(signal_handler|sig_handler|crash_handler|log|logger)\b", sig
            ):
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
            meta = code_context.get("prompt_context_meta")
            if not isinstance(meta, dict):
                meta = {}
            meta["max_functions_in_prompt"] = int(prompt_filter_opts.max_functions_in_prompt)
            meta["max_stack_frames_in_prompt"] = int(prompt_filter_opts.max_stack_frames_in_prompt)
            meta["excluded_function_index"] = list(excluded_index_lines or [])
            code_context["prompt_context_meta"] = meta

        lines.append("### 函数源码（按置信度筛选，高价值函数优先）")
        lines.append("")
        if not included_records:
            lines.append("- N/A")
            lines.append("")
        else:
            for rec in included_records:
                node = rec["node"]
                snippet = node.get("snippet", [])
                if not (isinstance(snippet, list) and snippet):
                    continue
                sig = str(node.get("signature", "N/A"))
                snippet, is_complete_snippet, incomplete_reason = self._prepare_prompt_function_snippet(node, snippet)
                if not snippet:
                    continue
                tags = sorted(list(rec["tags"]))
                tag_txt = "、".join(tags) if tags else "上下文候选"
                lines.append(f"#### 函数源码: {sig}")
                lines.append(f"- 来源: {tag_txt}")
                shared_vars = rec.get("shared_vars") or {}
                if isinstance(shared_vars, dict) and shared_vars:
                    detail = []
                    for vn in sorted(shared_vars.keys()):
                        rel_txt = "/".join(sorted(shared_vars[vn]))
                        detail.append(f"{vn}({rel_txt})")
                    lines.append(f"- 命中说明: 共享变量命中 {', '.join(detail)}")
                lines.append(f"- 文件: {node.get('file', 'N/A')}:N/A")
                if not is_complete_snippet:
                    lines.append(f"- 片段状态: {incomplete_reason}")
                lines.append("- 代码片段:")
                lines.extend([str(s) for s in snippet])
                lines.append("")

        # 输出类骨架（若存在）：让 AI 了解完整类结构以做出结构性修复决策
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

        lines.append("# 崩溃修复任务")
        lines.append("基于提供的实际源代码，分析本次崩溃的直接原因和根本原因，并给出可以直接应用到工程中的修复代码。")
        lines.append("")
        lines.append("## 分析指导")
        lines.append("**关键提示**：")
        lines.append("- 崩溃栈中显示的位置不一定就是根本原因，真正的问题可能出现在调用链的上游或其他相关函数中。")
        lines.append("- 建议从“为什么会出现当前崩溃现象”倒推，结合代码上下文和数据流寻找根因。")
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
        lines.append("4. 给出同时解决“症状”和“根因”的修复方案，而不仅仅是在崩溃点周围简单包裹保护代码。")
        lines.append("5. 思考修复改动是否会影响其它调用方或线程，并在方案中说明。")
        lines.append("")
        lines.append("## 输出要求")
        lines.append("**必须提供**：")
        lines.append("1. **证据清单（使用分项序号）**：每条结论标注置信度标签（按各章节中的“证据强度说明”）；")
        lines.append("   每条证据都要引用具体的 file:line，或明确指出对应的调用关系/共享变量关系。")
        lines.append("2. 崩溃原因分析（直接原因和根本原因）。")
        lines.append("3. 需要修改的函数列表（仅列出需要改动的函数）。")
        lines.append(
            "4. 修复代码（仅包含「需要修改的函数」列表中的函数；每个函数给出最终完整可编译代码，"
            "不得包含未列入该列表的函数，也不得重复粘贴无需改动的原函数）。"
        )
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
        lines.append("## 关键约束")
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
            "- 对来源标签含「共享变量关键读」的同类函数，逐个判断是否需改；仅当确需修改时才纳入「需要修改的函数」与修复代码。"
        )
        lines.append("- 禁止使用“未知”“假设”“示例”等表述性的占位词。")
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
        lines.append("")
        if memory_context:
            lines.append("## 规则与经验模式参考")
            lines.append(memory_context)
            lines.append("")
        lines.append("请基于以上信息，并严格遵循前文「崩溃修复任务」与「输出格式」中的要求，给出专业的崩溃分析，并提供可直接应用的修复代码。")

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

    def _collect_memory_context(
        self,
        problem: Dict[str, Any],
        parsed_data: Dict[str, Any],
        resolved_data: Dict[str, Any],
        prompt_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """规则优先 + 向量兜底（可选）。"""
        analyzer_cls = _get_rag_analyzer_class()
        if not _rag_available() or analyzer_cls is None:
            return None
        try:
            vector_db_path = str(problem.get("vector_db_path") or "./vector_db")
            analyzer = analyzer_cls(vector_db_path=vector_db_path)
            features = extract_features(parsed_data, resolved_data, prompt_data)
            rule_threshold = float(problem.get("rule_confidence_threshold", 0.85))
            max_results = int(problem.get("vector_db_max_results", 3))
            rule_hits = analyzer.match_rules(features, min_confidence=rule_threshold)
            high_conf_hits = [h for h in rule_hits if h.get("is_high_confidence")]
            decision_trace: List[Dict[str, Any]] = []
            pattern_hits: List[Dict[str, Any]] = []
            evidence_map: Dict[str, Any] = {}
            strategy_hits: List[Dict[str, Any]] = []
            vector_used = False

            if high_conf_hits:
                decision_trace.append(
                    {
                        "stage": "rule",
                        "result": "hit",
                        "rule_ids": [h.get("rule_id") for h in high_conf_hits if h.get("rule_id")],
                    }
                )
            else:
                query_text, _signature = build_pattern_query(parsed_data, resolved_data, prompt_data)
                pattern_hits = analyzer.retrieve_patterns(query_text, n_results=max_results)
                vector_used = bool(pattern_hits)
                pattern_ids = [p.get("pattern_id") for p in pattern_hits if p.get("pattern_id")]
                for pid in pattern_ids:
                    evidence_map[pid] = analyzer.get_evidence(pid)
                strategy_hits = analyzer.get_fix_strategies(pattern_ids)
                decision_trace.append(
                    {
                        "stage": "vector",
                        "result": "hit" if pattern_hits else "miss",
                        "pattern_ids": pattern_ids,
                    }
                )

            memory_context = self._render_memory_context(rule_hits, pattern_hits, evidence_map, strategy_hits)
            return {
                "rule_hits": rule_hits,
                "pattern_hits": pattern_hits,
                "evidence_map": evidence_map,
                "strategy_hits": strategy_hits,
                "decision_trace": decision_trace,
                "vector_used": vector_used,
                "memory_context": memory_context,
            }
        except Exception as e:
            logger.warning(f"[{self.definition.name}] RAG retrieval skipped: {e}")
            return None

    def _render_memory_context(
        self,
        rule_hits: List[Dict[str, Any]],
        pattern_hits: List[Dict[str, Any]],
        evidence_map: Dict[str, Any],
        strategy_hits: List[Dict[str, Any]],
    ) -> str:
        parts: List[str] = []
        if rule_hits:
            lines = []
            for item in rule_hits[:3]:
                payload = item.get("conclusion_payload") or {}
                hint = payload.get("hint") or payload.get("pattern") or json.dumps(payload, ensure_ascii=False)
                lines.append(f"- {item.get('rule_name') or item.get('rule_id')}: {hint}")
            if lines:
                parts.append("规则命中:\n" + "\n".join(lines))
        if pattern_hits:
            lines = []
            for item in pattern_hits[:3]:
                lines.append(f"- {item.get('pattern_summary') or item.get('pattern_id')}")
            if lines:
                parts.append("经验模式召回:\n" + "\n".join(lines))
        if strategy_hits:
            lines = []
            for item in strategy_hits[:3]:
                lines.append(f"- {item.get('fix_intent') or item.get('strategy_id')}")
            if lines:
                parts.append("可参考修复策略:\n" + "\n".join(lines))
        if evidence_map:
            evidence_count = sum(len(v or []) for v in evidence_map.values())
            if evidence_count > 0:
                parts.append(f"证据片段: 共 {evidence_count} 条")
        return "\n\n".join(parts).strip()


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
