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

try:
    from rag.feature_extractor import extract_features, build_pattern_query
    from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB
    RAG_AVAILABLE = True
except ImportError as e:
    logger.warning(f"RAG modules not available: {e}")
    extract_features = None  # type: ignore
    build_pattern_query = None  # type: ignore
    AIStabilityAnalyzerWithVectorDB = None  # type: ignore
    RAG_AVAILABLE = False


# ==================== Base Crash Analysis Workflow ====================

class BaseCrashAnalysisWorkflow(BaseWorkflow):
    """崩溃分析工作流基类"""

    def __init__(self):
        self.platform = "unknown"

    def solve(self, problem: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        """
        标准的崩溃分析流程：
        1. 解析 crash log
        2. 解析地址符号化
        3. 提取代码上下文
        4. 调用 LLM 分析
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
        else:
            total_steps = 4

        try:
            # Step 1: 解析崩溃日志
            print(f"[阶段 1/{total_steps}] 解析崩溃日志...")
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

            # Step 2: 符号化地址
            print(f"[阶段 2/{total_steps}] 符号化堆栈地址...")
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

            # Step 3: 提取代码上下文
            print(f"[阶段 3/{total_steps}] 提取源码上下文...")
            logger.info(f"[{self.definition.name}] Step 3: Extracting code context...")
            code_context = context.execute_tool("code_content_provider", {
                "resolved_stack": json.dumps(resolved),
                "code_roots": code_roots
            })
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
            print(f"[阶段 4/{total_steps}] 进行 AI 推理与修复建议...")
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
            prompt_cap_raw = problem.get("max_prompt_chars") if isinstance(problem, dict) else None
            if prompt_cap_raw is None:
                prompt_cap_raw = os.getenv("SA_MAX_PROMPT_CHARS", "50000")
            try:
                prompt_cap = int(prompt_cap_raw)
            except (TypeError, ValueError):
                prompt_cap = 50000
            if prompt_cap <= 0:
                prompt_cap = 50000
            if len(analysis_prompt) > prompt_cap:
                logger.warning(
                    f"[{self.definition.name}] analysis_prompt too long ({len(analysis_prompt)} chars), "
                    f"truncating to {prompt_cap} chars"
                )
                keep_head = max(1000, int(prompt_cap * 0.75))
                keep_tail = max(500, prompt_cap - keep_head)
                analysis_prompt = (
                    analysis_prompt[:keep_head]
                    + "\n\n...[PROMPT TRUNCATED DUE TO LENGTH LIMIT]...\n\n"
                    + analysis_prompt[-keep_tail:]
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

            first_try_tokens = 12000
            if configured_max_tokens > 0:
                first_try_tokens = min(first_try_tokens, configured_max_tokens)

            token_attempts: List[Optional[int]] = []
            if first_try_tokens > 0:
                token_attempts.append(first_try_tokens)
            for candidate in [4000, 2000, 1200, 800]:
                if candidate not in token_attempts and candidate < first_try_tokens:
                    token_attempts.append(candidate)
            token_attempts.append(None)  # 最后兜底走适配器默认值

            llm_response = None
            last_llm_exc: Optional[Exception] = None
            for idx, tok in enumerate(token_attempts, start=1):
                try:
                    if tok is None:
                        logger.info(f"[{self.definition.name}] LLM attempt {idx}: default max_tokens")
                        llm_response = context.call_llm(analysis_prompt)
                    else:
                        logger.info(f"[{self.definition.name}] LLM attempt {idx}: max_tokens={tok}")
                        llm_response = context.call_llm(analysis_prompt, max_tokens=tok)
                    break
                except Exception as llm_exc:
                    last_llm_exc = llm_exc
                    logger.warning(
                        f"[{self.definition.name}] LLM attempt {idx} failed "
                        f"(max_tokens={tok if tok is not None else 'default'}): {llm_exc}"
                    )

            if llm_response is None:
                raise RuntimeError(f"LLM call failed after retries: {last_llm_exc}")

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
                "crash_line_number",
                "error_type",
                "thread_id",
                "crash_location_source",
                "crash_line_note",
            ]:
                if crash_summary.get(key) is not None:
                    lines.append(f"- {key}: {crash_summary.get(key)}")
        lines.append("")

        lines.append("## 崩溃上下文")
        lines.append("崩溃函数被调用的路径（基于代码结构推断，自上而下）:")
        call_paths = graph.get("call_chain_from_code", []) if isinstance(graph, dict) else []
        primary_path_nodes: List[str] = []
        if isinstance(call_paths, list) and call_paths:
            for path in call_paths:
                if not isinstance(path, dict):
                    continue
                path_nodes = path.get("nodes", [])
                if isinstance(path_nodes, list) and path_nodes:
                    primary_path_nodes = [nid for nid in path_nodes if isinstance(nid, str)]
                    break
        if primary_path_nodes:
            lines.append("- 路径1:")
            for nid in primary_path_nodes:
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

        lines.append("## 可疑代码片段")
        lines.append("可疑崩溃代码行（基于地址解析）:")
        lines.append(f"- 文件: {((crash_node or {}).get('file') or 'N/A')}:{(crash_summary.get('crash_line_number') if isinstance(crash_summary, dict) else 'N/A')}")
        lines.append(f"- 代码行: {(crash_summary.get('crash_line_code') if isinstance(crash_summary, dict) else 'N/A')}")
        lines.append("")
        lines.append("")

        lines.append("## 以上涉及的函数源码")
        lines.append("")
        shown_ids: List[str] = []
        shown_signatures: Set[str] = set()
        source_ids: List[str] = []
        # 仅展示主调用链，避免多路径重复噪声
        source_ids.extend(primary_path_nodes)
        # 崩溃函数优先展示
        if isinstance(node_id, str):
            source_ids.insert(0, node_id)

        # 补充同类生命周期关键函数（如析构/clear/destroy/shutdown/stop），提升修复可操作性。
        crash_sig = str((crash_node or {}).get("signature") or "")
        owner = ""
        m_owner = re.search(r"\b([A-Za-z_]\w*)::[~]?[A-Za-z_]\w*\s*\(", crash_sig)
        if m_owner:
            owner = m_owner.group(1)
        if owner:
            lifecycle_re = re.compile(
                rf"\b{re.escape(owner)}::(~{re.escape(owner)}|clear(?:_[A-Za-z_]\w*)?|destroy(?:_[A-Za-z_]\w*)?|shutdown(?:_[A-Za-z_]\w*)?|stop(?:_[A-Za-z_]\w*)?)\s*\("
            )
            for nid, n in node_map.items():
                if not isinstance(nid, str) or not isinstance(n, dict):
                    continue
                sig = str(n.get("signature") or "")
                if lifecycle_re.search(sig):
                    source_ids.append(nid)

        for nid in source_ids:
            normalized = nid.rstrip().rstrip("{").rstrip() if isinstance(nid, str) else nid
            node = node_map.get(nid) or node_map.get(normalized)
            if not isinstance(node, dict):
                continue
            sid = str(node.get("id"))
            if sid in shown_ids:
                continue
            shown_ids.append(sid)
            sig = str(node.get("signature") or "")
            if sig:
                shown_signatures.add(sig)
            # 默认跳过信号处理/日志类函数，避免稀释主根因函数；崩溃函数例外。
            if sid != str((crash_node or {}).get("id") or "") and re.search(
                r"\b(signal_handler|sig_handler|crash_handler|log|logger)\b", sig
            ):
                continue
            lines.append("堆栈地址解析相关函数的代码片段:")
            lines.append(f"- 文件: {node.get('file', 'N/A')}:N/A")
            lines.append(f"- 函数: {node.get('signature', 'N/A')}")
            lines.append("- 代码片段:")
            snippet = node.get("snippet", [])
            if isinstance(snippet, list) and snippet:
                lines.extend([str(s) for s in snippet])
            else:
                continue
            lines.append("")
        # 对未提取到完整源码片段的堆栈函数不再补充 N/A 区块，避免重复/冲突信息误导模型。
        # 若未来需要展示，可考虑仅在 debug 模式输出。
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
        lines.append("1. 崩溃原因分析（直接原因和根本原因）。")
        lines.append("2. 需要修改的函数列表（仅列出需要改动的函数）。")
        lines.append("3. 修复代码（仅包含需要修改的函数的最终完整代码）。")
        lines.append("")
        lines.append("**可选提供**：")
        lines.append("- 无需修改但与根因相关的函数列表（只写“不修改原因”，不要粘贴原代码）。")
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
        lines.append("#### 无需修改但关键相关的函数（可选）")
        lines.append("- [函数名X] - [为什么不需要修改]")
        lines.append("")
        lines.append("#### 修复代码（仅包含“需要修改的函数”）")
        lines.append("（只粘贴需要修改的函数的最终完整可编译代码块；未修改函数禁止粘贴原代码）")
        lines.append("")
        lines.append("## 关键约束")
        lines.append("- 必须基于实际源代码进行修复；")
        lines.append("- 修复代码必须完整且可编译；")
        lines.append("- **修复代码只允许包含“需要修改的函数”**；对“无需修改的函数”，只允许给出不修改理由（1–2 句），禁止输出其完整代码；")
        lines.append("- 禁止使用“未知”“假设”“示例”等表述性的占位词。")
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
        max_lines = 180
        if len(snippet) > max_lines:
            snippet = snippet[:max_lines]
            snippet.append("... [truncated] ...")
        return snippet

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
        if not RAG_AVAILABLE or extract_features is None or build_pattern_query is None or AIStabilityAnalyzerWithVectorDB is None:
            return None
        try:
            vector_db_path = str(problem.get("vector_db_path") or "./vector_db")
            analyzer = AIStabilityAnalyzerWithVectorDB(vector_db_path=vector_db_path)
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
