"""Analyze-stage pipeline: prepare + context loop under AgentRuntime."""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.agent_context_loop import (
    CallableContextLoopHooks,
    ContextLoopConfig,
    ContextLoopResult,
    run_agent_context_loop,
)
from services.analyze_llm import call_analyze_llm_with_phase
from services.context_engine import (
    ContextEngine,
    ContextEngineConfig,
    extract_stack_priority_classes,
    resolve_agent_loop,
    resolve_max_agent_rounds,
)
from services.crash_repo_map import CrashRepoMap, render_repo_map
from services.code_evidence_index import CodeEvidenceIndex
from services.crash_evidence_retriever import CrashEvidenceRetriever
from services.investigation_controller import InvestigationController
from services.context_request_contract import (
    attach_return_form_metadata,
    format_context_resolution,
    parse_context_requests,
)
from services.context_observation_resolver import build_context_resolver_registry
from services.context_source_resolver import CodeContextRequestResolver
from services.stage_artifacts import save_analyze_round_artifact


def should_run_context_loop(problem: Optional[Dict[str, Any]], prepare: Dict[str, Any]) -> bool:
    if not isinstance(prepare, dict):
        return False
    if prepare.get("skip_context_loop"):
        return False
    scope = str((problem or {}).get("scope") or "full").strip()
    if scope != "full":
        return False
    return bool(prepare.get("initial_prompt"))


def build_analyze_context_loop_hooks(
    context: Any,
    problem: Optional[Dict[str, Any]],
    *,
    step: int = 5,
    total_steps: int = 5,
    runtime_state: Any = None,
    report_dir: Optional[str] = None,
    trace: Any = None,
) -> CallableContextLoopHooks:
    from cli.phase_spinner import PhaseSpinner

    @contextmanager
    def _around_resolve(round_index: int):
        labels = ("第一轮", "第二轮", "第三轮", "第四轮", "第五轮", "第六轮", "第七轮", "第八轮")
        round_label = labels[round_index] if 0 <= round_index < len(labels) else f"第{round_index + 1}轮"
        with PhaseSpinner(
            f"{round_label}：补充代码上下文",
            step=step,
            total_steps=total_steps,
        ):
            yield

    def _checkpoint_round(round_index: int, round_payload: Dict[str, Any]) -> None:
        if report_dir:
            save_analyze_round_artifact(Path(report_dir), round_index, round_payload)
        if runtime_state is not None and hasattr(runtime_state, "checkpoint"):
            artifact_path = None
            if report_dir:
                artifact_path = str(
                    Path(report_dir) / "artifacts" / f"analyze_round_{int(round_index)}.json"
                )
            runtime_state.checkpoint(
                state={
                    "schema_version": 2,
                    "round_index": round_index,
                    "agent_loop": "context_loop",
                    "request_count": len(round_payload.get("context_requests") or []),
                    "termination_reason": round_payload.get("termination_reason"),
                },
                status="running",
                idempotency_key=f"analyze:round:{round_index}",
                output_artifact=artifact_path,
            )
        if trace is not None:
            trace.emit(
                "analyze.round_checkpoint",
                kind="stage",
                name="analyze",
                status="running",
                round_index=round_index,
            )

    def _call_llm(prompt: str, *, round_index: int):
        bounded = context.select_prompt(prompt)
        return call_analyze_llm_with_phase(
            context,
            bounded,
            problem,
            step=step,
            total_steps=total_steps,
            round_index=round_index,
        )

    def _on_evidence(resolved_context, round_index: int):
        evidence_store = getattr(context, "evidence", None)
        if evidence_store is None:
            return
        for item in resolved_context:
            if isinstance(item, dict):
                evidence_store.add_dict({
                    "kind": "source_code" if item.get("success") else "context_result",
                    "content": format_context_resolution(item),
                    "source": "context_loop",
                    "file": item.get("file"),
                    "line_start": item.get("line_start") or item.get("line"),
                    "line_end": item.get("line_end") or item.get("line"),
                    "relevance": 1.0 if item.get("success") else 0.6,
                    "round": round_index,
                })

    return CallableContextLoopHooks(
        call_llm=_call_llm,
        on_evidence=_on_evidence,
        around_resolve=_around_resolve,
        on_round_complete=_checkpoint_round,
    )


def run_analyze_context_loop(
    *,
    context: Any,
    prepare: Dict[str, Any],
    problem: Optional[Dict[str, Any]],
    trace: Any = None,
    runtime_state: Any = None,
    report_dir: Optional[str] = None,
) -> ContextLoopResult:
    max_rounds = resolve_max_agent_rounds(problem)
    if trace is not None and getattr(getattr(trace, "budget", None), "max_llm_calls", 0) > 0:
        max_rounds = min(max_rounds, max(1, trace.budget.max_llm_calls))
    agent_loop = resolve_agent_loop(problem)
    max_requests = 8
    if isinstance(problem, dict):
        try:
            max_requests = int(problem.get("max_context_requests_per_round") or 8)
        except (TypeError, ValueError):
            max_requests = 8
    hooks = build_analyze_context_loop_hooks(
        context,
        problem,
        step=int(prepare.get("step") or 5),
        total_steps=int(prepare.get("total_steps") or 5),
        runtime_state=runtime_state,
        report_dir=report_dir,
        trace=trace,
    )
    initial = str(prepare.get("initial_prompt") or "")
    repo_map_payload: Dict[str, Any] = {}
    repo_map_text = ""
    evidence_retriever = None
    index_snapshot = None
    investigation_controller = InvestigationController()
    context_config = getattr(context, "config", {}) if context is not None else {}
    max_chars = int(context_config.get("evidence_max_chars", 24000) or 24000) if isinstance(context_config, dict) else 24000
    code_roots = list(prepare.get("code_roots") or []) if isinstance(prepare, dict) else []
    if code_roots and not (isinstance(problem, dict) and problem.get("disable_repo_map")):
        try:
            repo_map = CrashRepoMap(code_roots)
            snapshot = repo_map.build()
            anchors: Dict[str, Any] = {
                "stack_files": [],
                "stack_symbols": [],
                "fields": [],
                "callers": [],
            }
            resolved_stack = prepare.get("resolved_stack") if isinstance(prepare, dict) else None
            if isinstance(resolved_stack, dict):
                stack_blob = json.dumps(resolved_stack, ensure_ascii=False, default=str)
                anchors["stack_files"] = re.findall(r"(?:^|[\"'])((?:/|[A-Za-z]:[\\/])[^\"']+\.(?:c|cc|cpp|cxx|h|hpp|m|mm|swift))", stack_blob)
                anchors["stack_symbols"] = re.findall(r"\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+\b", stack_blob)
            diagnosis = prepare.get("crash_diagnosis") if isinstance(prepare, dict) else None
            if isinstance(diagnosis, dict):
                anchors["fields"] = re.findall(r"\b(?:m_|this->)([A-Za-z_]\w*)", json.dumps(diagnosis, ensure_ascii=False, default=str))
            entries = repo_map.rank(snapshot, anchors, max_files=20, max_tokens=max(256, max_chars // 20))
            repo_map_payload = {
                "schema_version": 1,
                "fingerprint": snapshot.fingerprint,
                "roots": snapshot.roots,
                "entries": [entry.to_dict() for entry in entries],
                "cache_hit": repo_map.cache_hit,
            }
            repo_map_text = render_repo_map(entries, max_chars=min(7000, max_chars // 3))
            # Keep source retrieval independent from the prompt skeleton.  The
            # ContextEngine consumes these candidates as structured evidence;
            # only the existing repo-map rendering remains in the prompt.
            evidence_index = CodeEvidenceIndex()
            revision = None
            try:
                from services.workspace_revision import workspace_revisions
                revision, _ = workspace_revisions(code_roots)
            except Exception:
                pass
            index_snapshot = evidence_index.update(code_roots, revision=revision)
            evidence_retriever = CrashEvidenceRetriever(
                evidence_index,
                repo_map=snapshot,
            )
            repo_map_payload["index_fingerprint"] = index_snapshot.fingerprint
            repo_map_payload["index_revision"] = index_snapshot.revision
        except Exception as exc:
            if trace is not None:
                trace.emit("repo_map.failed", kind="context", name="repo_map", status="degraded", error=str(exc))
    if repo_map_text:
        initial = initial.rstrip() + "\n\n" + repo_map_text
    if agent_loop == "context_loop":
        from services.context_loop_contract import build_json_format_reminder, prompt_has_json_contract

        if not prompt_has_json_contract(initial):
            initial = initial.rstrip() + "\n\n" + build_json_format_reminder(is_final_round=False)
    if hasattr(context, "select_prompt"):
        initial = context.select_prompt(initial)
    outcomes: Dict[str, str] = {}

    def _resolve_one(request: Dict[str, Any]) -> Dict[str, Any]:
        resolved = CodeContextRequestResolver.resolve_requests(
            [request],
            list(prepare.get("code_roots") or []),
            context=context,
            max_requests=1,
            request_outcomes=outcomes,
            stack_priority_classes=extract_stack_priority_classes(problem),
        )
        item = resolved[0] if resolved else {
            "request": request,
            "success": False,
            "error": "context resolver returned no result",
        }
        return attach_return_form_metadata(item) if isinstance(item, dict) else item

    registry = build_context_resolver_registry(
        prepare=prepare,
        problem=problem,
        context=context,
        trace=trace,
        code_resolver=_resolve_one,
        code_roots=list(prepare.get("code_roots") or []),
    )
    context_config = getattr(context, "config", {}) if context is not None else {}
    max_chars = int(context_config.get("evidence_max_chars", 24000) or 24000) if isinstance(context_config, dict) else 24000
    max_tokens = int(context_config.get("evidence_max_tokens", 0) or 0) if isinstance(context_config, dict) else 0
    verification_profile = None
    problem_value = problem if isinstance(problem, dict) else {}
    profile_value = problem_value.get("verification_profile") or problem_value.get("verification")
    if isinstance(profile_value, dict) and (profile_value.get("checks") or profile_value.get("command") or profile_value.get("verification")):
        try:
            from services.verification_profile import VerificationProfile
            verification_profile = VerificationProfile.from_mapping(profile_value)
        except ValueError:
            verification_profile = None
    context_engine = ContextEngine(
        ContextEngineConfig(
            max_requests=max_requests,
            max_chars=max_chars,
            max_tokens=max_tokens,
        ),
        registry,
        format_resolution=format_context_resolution,
        decision_parser=parse_context_requests,
        observation_store=getattr(context, "observations", None),
        repo_map=repo_map_payload,
        trace=trace,
        evidence_retriever=evidence_retriever,
        investigation_controller=investigation_controller,
        verification_profile=verification_profile,
    )
    # Persist retrieval metadata for replay and diagnostics.  This is additive
    # and intentionally does not alter round_0/06_ai_prompt.md.
    if index_snapshot is not None:
        context_engine.session.repo_map["index_fingerprint"] = index_snapshot.fingerprint
        context_engine.session.repo_map["index_revision"] = index_snapshot.revision
        try:
            resolved_stack = prepare.get("resolved_stack") if isinstance(prepare, dict) else {}
            anchors = {"stack_files": [], "stack_symbols": [], "fields": [], "callers": []}
            if isinstance(resolved_stack, dict):
                blob = json.dumps(resolved_stack, ensure_ascii=False, default=str)
                anchors["stack_symbols"] = re.findall(r"\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+\b", blob)
            candidates = context_engine.retrieve_evidence(anchors, limit=12)
            context_engine.session.repo_map["retrieval_candidates"] = candidates
            context_engine.session.repo_map["investigation_anchors"] = anchors
            # Seed provenance with deterministic crash anchors.  Later source,
            # hypothesis, edit and verification nodes can therefore be traced
            # back to the original stack instead of forming an orphan graph.
            graph = context_engine.evidence_graph
            frame_ids = []
            for symbol in anchors.get("stack_symbols") or []:
                frame_ids.append(graph.add_node("crash_frame", str(symbol), round=0))
            for path in anchors.get("stack_files") or []:
                frame_ids.append(graph.add_node("crash_location", str(path), round=0))
            for candidate in candidates:
                if not isinstance(candidate, dict) or not candidate.get("file"):
                    continue
                candidate_id = graph.add_node("source_candidate", {
                    "file": candidate.get("file"), "symbol": candidate.get("symbol", ""),
                    "line_start": candidate.get("line_start", 0),
                }, score=candidate.get("score"), ranking_reasons=candidate.get("ranking_reasons"))
                for frame_id in frame_ids:
                    graph.add_edge(frame_id, "candidate", candidate_id)
            context_engine.session.evidence_graph = graph.to_dict()
            planned = investigation_controller.plan(
                {**anchors, "hypotheses": [], "problem_types": [str((problem or {}).get("problem_type") or "")]},
                candidates,
                round_index=0,
            )
            context_engine.session.repo_map["investigation_plan"] = [item.to_dict() for item in planned]
            context_engine.session.investigation_actions = [
                {"round": 0, "kind": item.kind, "target": item.target,
                 "reason": item.reason, "priority": item.priority,
                 "expected_return_form": item.expected_return_form}
                for item in planned
            ]
        except Exception:
            pass
    return run_agent_context_loop(
        config=ContextLoopConfig(
            max_rounds=max_rounds,
            max_requests=max_requests,
            agent_loop_mode=agent_loop,
        ),
        hooks=hooks,
        initial_prompt=initial,
        trace=trace,
        context_engine=context_engine,
    )


def merge_analyze_prepare_result(
    base: Dict[str, Any],
    loop_out: ContextLoopResult,
) -> Dict[str, Any]:
    out = dict(base)
    out.pop("_analyze_prepare", None)
    out["analysis"] = loop_out.analysis_text
    out["final_prompt"] = loop_out.prompt_used
    out["agent_rounds"] = loop_out.rounds
    out["context_session"] = loop_out.context_session or {
        "schema_version": 2,
        "mode": "single",
        "status": "completed",
        "termination_reason": loop_out.termination_reason or "model_final",
        "rounds": loop_out.rounds,
        "request_ledger": [],
        "budget": {},
        "stats": {},
    }
    out["termination_reason"] = loop_out.termination_reason
    if loop_out.repo_map:
        out["repo_map"] = loop_out.repo_map
    if loop_out.last_response is not None:
        out["_llm_response"] = loop_out.last_response
    if loop_out.structured_analysis:
        metadata = out.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["structured_analysis"] = loop_out.structured_analysis
    return out
