#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内置技能实现 - 将现有的分析能力封装为 Skill
（迁移自 tool_system/skill_builtins.py，仅修改 import 路径）
"""

import logging
import json
from typing import Any, Dict, List, Optional

from tool_system import BaseSkill, SkillDefinition, SkillContext, Priority, register_skill
from tool_system.registry import ToolAndSkillRegistry

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


# ==================== Base Crash Analysis Skill ====================

class BaseCrashAnalysisSkill(BaseSkill):
    """崩溃分析技能基类"""

    def __init__(self):
        self.platform = "unknown"

    def solve(self, problem: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
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
                return {
                    "status": "success",
                    "platform": self.platform,
                    "skill": self.definition.name,
                    "parse_result": parse_result,
                    "resolved_stack": resolved,
                    "code_context": code_context,
                    "analysis": None,
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
            analysis_prompt = self._build_analysis_prompt(parse_result, resolved, code_context, memory_context)
            llm_response = context.call_llm(analysis_prompt)

            return {
                "status": "success",
                "platform": self.platform,
                "skill": self.definition.name,
                "parse_result": parse_result,
                "resolved_stack": resolved,
                "code_context": code_context,
                "analysis": llm_response.content,
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
                "skill": self.definition.name
            }

    def _build_analysis_prompt(self, parse_result: Dict, resolved: Dict, code_context: Dict, memory_context: str = "") -> str:
        """构建分析提示词"""
        # 实际结构是 crash_info，不是 crash_summary
        crash_info = parse_result.get("crash_info", {})
        crash_summary = {
            "error_type": crash_info.get("signal", "N/A"),
            "crash_reason": crash_info.get("crash_reason", "N/A"),
            "crash_address": crash_info.get("crash_address", "N/A"),
            "category": crash_info.get("category", "N/A"),
            "thread_type": crash_info.get("thread_type", "N/A"),
        }

        if PROMPTS_AVAILABLE:
            prompt = generate_crash_analysis_prompt({
                "crash_summary": crash_summary,
                "resolved_stack": resolved,
                "code_context": code_context
            })
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


# ==================== iOS Crash Analysis Skill ====================

class iOSCrashAnalyzeSkill(BaseCrashAnalysisSkill):
    """iOS 崩溃分析技能"""

    def __init__(self):
        super().__init__()
        self.platform = "ios"

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
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


# ==================== Android Crash Analysis Skill ====================

class AndroidCrashAnalyzeSkill(BaseCrashAnalysisSkill):
    """Android 崩溃分析技能"""

    def __init__(self):
        super().__init__()
        self.platform = "android"

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
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


# ==================== Generic Crash Analysis Skill ====================

class GenericCrashAnalyzeSkill(BaseCrashAnalysisSkill):
    """通用崩溃分析技能（自动检测平台）"""

    def __init__(self):
        super().__init__()
        self.platform = "auto"

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="crash_analysis",
            description="通用崩溃分析技能，自动检测平台并分析",
            problem_type="crash_analysis",
            required_tools=["crash_log_parser", "add2line_resolver", "code_content_provider"],
            version="1.0.0",
            metadata={
                "platform": "auto",
                "languages": ["auto"]
            }
        )

    def solve(self, problem: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        # 自动检测平台
        crash_log = problem.get("crash_log", "")

        # 简单的平台检测
        if "iOS" in crash_log or "Swift" in crash_log or "SIGSEGV" in crash_log:
            ios_skill = iOSCrashAnalyzeSkill()
            return ios_skill.solve(problem, context)
        elif "Android" in crash_log or "java.lang" in crash_log or "Native crash" in crash_log:
            android_skill = AndroidCrashAnalyzeSkill()
            return android_skill.solve(problem, context)
        else:
            return super().solve(problem, context)
