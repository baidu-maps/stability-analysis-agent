#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内置工作流实现 - 将现有的分析能力封装为 Workflow
"""

import logging
import json
from typing import Any, Dict, List, Optional

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

        # 兼容 code_root
        if code_root and not code_roots:
            code_roots = [code_root]

        if not crash_log:
            return {"error": "缺少 crash_log"}

        try:
            # Step 1: 解析崩溃日志
            logger.info(f"[{self.definition.name}] Step 1: Parsing crash log...")
            parse_result = context.execute_tool("crash_log_parser", {
                "log_content": crash_log
            })

            # Step 2: 符号化地址
            logger.info(f"[{self.definition.name}] Step 2: Resolving symbols...")
            resolved = context.execute_tool("add2line_resolver", {
                "crash_json": json.dumps(parse_result),
                "library_dir": library_dir
            })

            # Step 3: 提取代码上下文
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
                if problem.get("skip_ai"):
                    # --skip-ai 模式下使用历史 ai_tip 风格，保持与既有调试产物兼容
                    assembled_prompt = self._build_skip_ai_final_tip(
                        parse_result=parse_result,
                        resolved=resolved,
                        code_context=code_context,
                        memory_context=memory_context,
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
            logger.info(f"[{self.definition.name}] Step 4: LLM analysis...")
            # 与 --skip-ai 模式对齐：统一使用同一份 final_tip 作为 LLM 输入与 05 文件落盘内容
            analysis_prompt = self._build_skip_ai_final_tip(
                parse_result=parse_result,
                resolved=resolved,
                code_context=code_context,
                memory_context=memory_context,
            )
            llm_response = context.call_llm(analysis_prompt)

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

    def _build_skip_ai_final_tip(
        self,
        parse_result: Dict[str, Any],
        resolved: Dict[str, Any],
        code_context: Dict[str, Any],
        memory_context: str = "",
    ) -> str:
        """构建 --skip-ai 的历史兼容提示词格式。"""
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
        if isinstance(call_paths, list) and call_paths:
            for idx, path in enumerate(call_paths, 1):
                if not isinstance(path, dict):
                    continue
                path_nodes = path.get("nodes", [])
                if not isinstance(path_nodes, list):
                    continue
                lines.append(f"- 路径{idx}:")
                for nid in path_nodes:
                    node = node_map.get(nid)
                    if isinstance(node, dict):
                        lines.append(f"  - {node.get('signature', nid)}")
                    else:
                        lines.append(f"  - {nid}")
        lines.append("")

        lines.append("根据堆栈顺序解析的函数/帧语义列表（按调用顺序，自下而上）：")
        resolved_frames = resolved.get("resolved_frames", []) if isinstance(resolved, dict) else []
        if isinstance(resolved_frames, list):
            for idx, frame in enumerate(resolved_frames, 1):
                if not isinstance(frame, dict):
                    continue
                func = frame.get("function") or frame.get("raw_address") or "N/A"
                file_path = frame.get("file")
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
        source_ids: List[str] = []
        if isinstance(call_paths, list) and call_paths:
            first_path = call_paths[0] if isinstance(call_paths[0], dict) else {}
            source_ids = [nid for nid in first_path.get("nodes", []) if isinstance(first_path.get("nodes", []), list)]
        if isinstance(node_id, str):
            source_ids.append(node_id)
        for nid in source_ids:
            normalized = nid.rstrip().rstrip("{").rstrip() if isinstance(nid, str) else nid
            node = node_map.get(nid) or node_map.get(normalized)
            if not isinstance(node, dict):
                continue
            sid = str(node.get("id"))
            if sid in shown_ids:
                continue
            shown_ids.append(sid)
            lines.append("堆栈地址解析相关函数的代码片段:")
            lines.append(f"- 文件: {node.get('file', 'N/A')}:N/A")
            lines.append(f"- 函数: {node.get('signature', 'N/A')}")
            lines.append("- 代码片段:")
            snippet = node.get("snippet", [])
            if isinstance(snippet, list) and snippet:
                lines.extend([str(s) for s in snippet])
            else:
                lines.append("N/A")
            lines.append("")
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
