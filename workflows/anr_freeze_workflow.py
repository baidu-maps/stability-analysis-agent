#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANR/Freeze 专用分析工作流（MVP）。

主轨：解析 →（符号化）→ ANR 诊断（04c）→ ANR prompt / LLM
mixed_anr_crash：另附 crash 诊断（04a）作为次要 sidecar。
不修改 crash workflow 的 ``_build_prompt_final_tip`` 装配结构。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from cli.phase_spinner import PhaseSpinner
from tools.parse_crash_errors import (
    parse_result_has_usable_crash_data,
    pipeline_skip_metadata,
)
from tools.resolve_stack_errors import resolved_stack_has_usable_resolution
from tool_system import BaseWorkflow, WorkflowDefinition, WorkflowContext
from tools.crash_parser.log_kind_classifier import (
    LOG_KIND_MIXED_ANR_CRASH,
    is_anr_family_kind,
    log_kind_from_parse_result,
)

logger = logging.getLogger(__name__)


def _build_anr_prompt(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    anr_diagnosis: Dict[str, Any],
    *,
    crash_diagnosis: Optional[Dict[str, Any]] = None,
    log_kind: str = "",
    problem: Optional[Dict[str, Any]] = None,
) -> str:
    """基于 01/03/04c（及 mixed 时 04a）构建 ANR 专用提示词。"""
    meta = parse_result.get("meta_info") if isinstance(parse_result.get("meta_info"), dict) else {}
    crash_info = parse_result.get("crash_info") if isinstance(parse_result.get("crash_info"), dict) else {}
    kind = log_kind or str(meta.get("log_kind") or "unknown")

    lines: List[str] = [
        "下面是本次 ANR/Freeze（无响应）相关信息：",
        "",
        "## 事件摘要",
        f"- 日志类型 (log_kind): {kind}",
    ]
    if meta.get("log_kind_confidence") is not None:
        lines.append(f"- 分类置信度: {meta.get('log_kind_confidence')}")
    reasons = meta.get("log_kind_reasons") or []
    if isinstance(reasons, list) and reasons:
        lines.append(f"- 分类依据: {', '.join(str(r) for r in reasons)}")
    if meta.get("os_type"):
        lines.append(f"- 平台: {meta.get('os_type')}")
    if meta.get("process_name"):
        lines.append(f"- 进程: {meta.get('process_name')}")
    if crash_info.get("crash_reason"):
        lines.append(f"- 原因摘要: {crash_info.get('crash_reason')}")
    lines.append("")

    # 线程热点概览（符号化或原始栈）
    threads = []
    if isinstance(resolved_stack, dict):
        threads = resolved_stack.get("resolved_threads") or []
    if not threads and isinstance(parse_result, dict):
        threads = parse_result.get("threads") or []
    if isinstance(threads, list) and threads:
        lines.append("## 线程栈概览（前若干帧）")
        for th in threads[:8]:
            if not isinstance(th, dict):
                continue
            tid = th.get("tid") or th.get("thread_id") or "?"
            name = th.get("name") or th.get("thread_name") or ""
            role = []
            if th.get("is_crash_thread") or th.get("is_main"):
                role.append("主/归因")
            role_s = f"（{'/'.join(role)}）" if role else ""
            lines.append(f"- Tid={tid} {name}{role_s}")
            frames = th.get("frames") or []
            for fr in (frames if isinstance(frames, list) else [])[:6]:
                if not isinstance(fr, dict):
                    continue
                fn = (
                    fr.get("resolved_function")
                    or fr.get("function")
                    or fr.get("symbol")
                    or "?"
                )
                mod = fr.get("module") or ""
                lines.append(f"  - {fn}" + (f" [{mod}]" if mod else ""))
        lines.append("")

    anr_sec = ""
    if isinstance(anr_diagnosis, dict):
        anr_sec = str(anr_diagnosis.get("prompt_section_zh") or "").strip()
    if anr_sec:
        lines.append(anr_sec)
        lines.append("")

    if kind == LOG_KIND_MIXED_ANR_CRASH and isinstance(crash_diagnosis, dict):
        lines.append("## 次要：崩溃信号诊断（非主因轨道）")
        lines.append("")
        lines.append(
            "本日志同时含 ANR/Freeze 与 crash 信号。以下 crash 诊断仅作辅轨参考，"
            "根因分析仍以主线程阻塞 / 热点 / Binder / EventHandler 为主。"
        )
        lines.append("")
        crash_sec = str(crash_diagnosis.get("prompt_section_zh") or "").strip()
        if crash_sec:
            lines.append(crash_sec)
            lines.append("")
        else:
            lines.append("（辅轨 crash 诊断无额外摘要）")
            lines.append("")

    lines.extend([
        "## 分析要求",
        "",
        "请基于上述证据给出：",
        "1. 主线程（或关键线程）阻塞/卡顿的最可能根因",
        "2. 关键证据（热点函数、锁、Binder、消息队列）",
        "3. 可验证的修复或排查建议",
        "4. 若证据不足，明确需要补充的材料（完整 traces、主线程消息队列等）",
        "",
    ])
    agent_loop = str((problem or {}).get("agent_loop") or "single").strip()
    if agent_loop == "context_loop":
        from services.context_loop_contract import (
            build_round0_must_provide_lines,
            build_round0_output_format_lines,
        )

        lines.append("## 输出格式")
        lines.extend(build_round0_must_provide_lines(agent_loop=agent_loop))
        lines.append("")
        lines.extend(build_round0_output_format_lines(agent_loop=agent_loop))
    return "\n".join(lines).rstrip() + "\n"


class AnrFreezeAnalysisWorkflow(BaseWorkflow):
    """ANR/Freeze 专用工作流。"""

    def __init__(self):
        self.platform = "auto"

    @property
    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="anr_freeze_analysis",
            description="ANR/Freeze/Watchdog 无响应分析（热点栈/EventHandler/Binder）",
            problem_type="anr_freeze",
            required_tools=["crash_log_parser", "add2line_resolver"],
            version="1.0.0",
            metadata={
                "platform": "auto",
                "languages": ["auto"],
                "artifact_primary": "04c_anr_freeze_diagnosis.json",
            },
        )

    def _resolve_scope(self, problem: Dict[str, Any]) -> str:
        scope = str(problem.get("scope") or "full").strip()
        if scope not in {"full", "gen_prompt_only", "parse_stack_only", "parse_log_only"}:
            return "full"
        return scope

    @staticmethod
    def _ingest_anr_evidence(context: WorkflowContext, **kwargs: Any) -> None:
        store = getattr(context, "evidence", None)
        if store is None:
            return
        from services.evidence_ingest import ingest_diagnosis, ingest_parse, ingest_symbolize

        ingest_parse(store, kwargs.get("parse_result"))
        ingest_symbolize(store, kwargs.get("resolved"), kwargs.get("memory_maps"))
        anr = kwargs.get("anr_diagnosis")
        if anr:
            store.add_dict({
                "kind": "anr_diagnosis",
                "content": json.dumps(anr, ensure_ascii=False, sort_keys=True, default=str),
                "source": "anr_freeze_diagnosis",
                "layer": "inference",
                "relevance": 1.0,
                "round": 0,
            })
        ingest_diagnosis(store, kwargs.get("crash_diagnosis"))

    def solve(self, problem: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        crash_log = problem.get("crash_log", "")
        library_dir = problem.get("library_dir")
        scope = self._resolve_scope(problem)
        force_anr = bool(
            problem.get("force_anr_analysis") or problem.get("enable_anr_analysis")
        )

        if not crash_log:
            return {"error": "缺少 crash_log", "workflow": self.definition.name}

        if scope == "parse_log_only":
            total_steps = 1
        elif scope == "parse_stack_only":
            total_steps = 3  # parse + resolve + anr diag
        else:
            total_steps = 4  # + prompt/LLM

        try:
            with PhaseSpinner("解析 ANR/崩溃日志", step=1, total_steps=total_steps) as _parse_spinner:
                parse_result = context.execute_tool("crash_log_parser", {
                    "log_content": crash_log,
                    "options": {
                        "library_dir": os.path.abspath(library_dir),
                    } if library_dir and os.path.exists(library_dir) else {},
                })
                if not isinstance(parse_result, dict):
                    parse_result = {}
                if not parse_result_has_usable_crash_data(parse_result):
                    # ANR traces 有时帧稀疏：仍继续若 log_kind 属 ANR 族
                    kind = log_kind_from_parse_result(parse_result)
                    if not (is_anr_family_kind(kind) or force_anr):
                        _parse_spinner.set_partial_failure()
                        skip_meta = pipeline_skip_metadata(parse_result)
                        return {
                            "status": "success",
                            "platform": self.platform,
                            "workflow": self.definition.name,
                            "parse_result": parse_result,
                            "resolved_stack": {},
                            "anr_diagnosis": {},
                            "crash_diagnosis": {},
                            "code_context": {},
                            "analysis": None,
                            "final_tip": None,
                            "note": "skipped_no_usable_parse",
                            "metadata": {
                                "problem_type": self.definition.problem_type,
                                **skip_meta,
                            },
                        }

            self._ingest_anr_evidence(context, parse_result=parse_result)

            log_kind = log_kind_from_parse_result(parse_result)
            if scope == "parse_log_only":
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "memory_maps": {},
                    "resolved_stack": {},
                    "anr_diagnosis": {},
                    "crash_diagnosis": {},
                    "code_context": {},
                    "analysis": None,
                    "final_tip": None,
                    "note": "scope=parse_log_only，仅执行日志解析（含 log_kind）",
                    "metadata": {"problem_type": self.definition.problem_type, "log_kind": log_kind},
                }

            memory_maps_data: Dict[str, Any] = {}
            try:
                from tools.crash_diagnosis.maps_extractor import extract_memory_maps
                memory_maps_data = extract_memory_maps(crash_log)
            except Exception as maps_exc:
                logger.debug("memory_maps extraction skipped: %s", maps_exc)

            resolved: Dict[str, Any] = {}
            with PhaseSpinner("堆栈符号化", step=2, total_steps=total_steps):
                if library_dir:
                    resolved_raw = context.execute_tool("add2line_resolver", {
                        "crash_json": json.dumps(parse_result),
                        "library_dir": library_dir,
                    })
                    resolved = resolved_raw if isinstance(resolved_raw, dict) else {}
                if not resolved:
                    # 无 library_dir 时用 parse 线程作为 resolved 近似，供热点分析
                    resolved = {
                        "resolved_threads": [
                            {
                                **(th if isinstance(th, dict) else {}),
                                "frames": list((th.get("frames") or []) if isinstance(th, dict) else []),
                            }
                            for th in (parse_result.get("threads") or [])
                            if isinstance(th, dict)
                        ],
                        "symbolization_skipped": True,
                    }

            crash_log_content = str(
                problem.get("crash_log_content") or problem.get("crash_log") or crash_log or ""
            )

            with PhaseSpinner("ANR/Freeze 诊断", step=3, total_steps=total_steps):
                from tools.anr_diagnosis.core import run_anr_freeze_diagnosis
                anr_diagnosis = run_anr_freeze_diagnosis(
                    parse_result,
                    resolved,
                    crash_log_content,
                    force=True,  # 本 workflow 即 ANR 主轨，始终执行
                ) or {}

            crash_diagnosis: Dict[str, Any] = {}
            if log_kind == LOG_KIND_MIXED_ANR_CRASH:
                try:
                    from tools.crash_diagnosis.core import run_crash_diagnosis
                    crash_diagnosis = run_crash_diagnosis(
                        parse_result,
                        memory_maps_data,
                        resolved if resolved_stack_has_usable_resolution(resolved) else {},
                        crash_log_content=crash_log_content,
                        library_dir=str(library_dir or ""),
                        force_disassembly=bool(
                            problem.get("force_disassembly")
                            or problem.get("enable_disassembly")
                        ),
                    )
                    if isinstance(crash_diagnosis, dict):
                        crash_diagnosis["role"] = "secondary"
                        crash_diagnosis["note_zh"] = (
                            "mixed_anr_crash 辅轨：crash 信号诊断，非 ANR 主因轨道"
                        )
                except Exception as diag_exc:
                    logger.warning("[%s] mixed crash secondary failed: %s", self.definition.name, diag_exc)
                    crash_diagnosis = {
                        "role": "secondary",
                        "error": str(diag_exc),
                        "prompt_section_zh": f"## 崩溃证据诊断（辅轨）\n\n诊断异常: {diag_exc}\n",
                    }

            if scope == "parse_stack_only":
                self._ingest_anr_evidence(
                    context,
                    parse_result=parse_result,
                    resolved=resolved,
                    memory_maps=memory_maps_data,
                    anr_diagnosis=anr_diagnosis,
                    crash_diagnosis=crash_diagnosis,
                )
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "memory_maps": memory_maps_data,
                    "resolved_stack": resolved,
                    "anr_diagnosis": anr_diagnosis,
                    "crash_diagnosis": crash_diagnosis,
                    "code_context": {},
                    "analysis": None,
                    "final_tip": None,
                    "note": "scope=parse_stack_only，已执行解析+符号化+ANR 诊断",
                    "metadata": {
                        "problem_type": self.definition.problem_type,
                        "log_kind": log_kind,
                    },
                }

            final_tip = _build_anr_prompt(
                parse_result,
                resolved,
                anr_diagnosis,
                crash_diagnosis=crash_diagnosis or None,
                log_kind=log_kind,
                problem=problem,
            )

            if isinstance(problem, dict) and problem.get("_runtime_owned_context_loop"):
                self._ingest_anr_evidence(
                    context,
                    parse_result=parse_result,
                    resolved=resolved,
                    memory_maps=memory_maps_data,
                    anr_diagnosis=anr_diagnosis,
                    crash_diagnosis=crash_diagnosis,
                )
                return {
                    "status": "success",
                    "platform": self.platform,
                    "workflow": self.definition.name,
                    "parse_result": parse_result,
                    "memory_maps": memory_maps_data,
                    "resolved_stack": resolved,
                    "anr_diagnosis": anr_diagnosis,
                    "crash_diagnosis": crash_diagnosis,
                    "code_context": {},
                    "analysis": None,
                    "final_tip": final_tip,
                    "metadata": {"problem_type": self.definition.problem_type, "log_kind": log_kind},
                    "_analyze_prepare": {
                        "initial_prompt": final_tip,
                        "code_roots": list(problem.get("code_roots") or []),
                        "step": 4,
                        "total_steps": total_steps,
                        "skip_context_loop": context.llm is None,
                    },
                }

            analysis = None
            if scope == "full":
                with PhaseSpinner("AI 分析根因", step=4, total_steps=total_steps):
                    try:
                        llm_response = context.call_llm(final_tip, temperature=0)
                        analysis = str(getattr(llm_response, "content", "") or "") or None
                    except Exception as llm_exc:
                        logger.warning("[%s] LLM failed: %s", self.definition.name, llm_exc)
                        analysis = None
                        return {
                            "status": "success",
                            "platform": self.platform,
                            "workflow": self.definition.name,
                            "parse_result": parse_result,
                            "memory_maps": memory_maps_data,
                            "resolved_stack": resolved,
                            "anr_diagnosis": anr_diagnosis,
                            "crash_diagnosis": crash_diagnosis,
                            "code_context": {},
                            "analysis": None,
                            "final_tip": final_tip,
                            "note": f"LLM 调用失败，已保留 ANR prompt: {llm_exc}",
                            "metadata": {
                                "problem_type": self.definition.problem_type,
                                "log_kind": log_kind,
                                "llm_skipped": True,
                            },
                        }

            return {
                "status": "success",
                "platform": self.platform,
                "workflow": self.definition.name,
                "parse_result": parse_result,
                "memory_maps": memory_maps_data,
                "resolved_stack": resolved,
                "anr_diagnosis": anr_diagnosis,
                "crash_diagnosis": crash_diagnosis,
                "code_context": {},
                "analysis": analysis,
                "final_tip": final_tip,
                "note": (
                    "scope=gen_prompt_only，已生成 ANR prompt"
                    if scope == "gen_prompt_only"
                    else "ANR/Freeze 分析完成"
                ),
                "rule_hits": [],
                "pattern_hits": [],
                "evidence_map": {},
                "strategy_hits": [],
                "decision_trace": [],
                "vector_used": False,
                "memory_context": "",
                "metadata": {
                    "problem_type": self.definition.problem_type,
                    "log_kind": log_kind,
                },
            }

        except Exception as exc:
            logger.exception("[%s] failed: %s", self.definition.name, exc)
            return {
                "status": "error",
                "error": str(exc),
                "workflow": self.definition.name,
                "platform": self.platform,
            }
