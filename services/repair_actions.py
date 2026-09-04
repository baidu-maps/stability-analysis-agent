"""RuntimeActionExecutor handlers for the repair/verify pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import difflib
import fnmatch
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.code_fixer import CodeFixer, extract_candidate_nodes, rollback_applied_edits
from services.diff_review import review_changed_files
from services.agent_schema import VerificationDecision
from services.git_worktree_manager import (
    IsolatedCodeWorkspace,
    collect_workspace_diff,
    map_original_path,
    sync_verified_files_back,
)
from services.post_fix_diagnosis import run_post_fix_diagnosis
from services.runtime_actions import RuntimeAction, RuntimeActionExecutor, VERIFICATION_ACTION_TOOLS
from services.action_security import ActionSecurityAnalyzer
from services.verification import (
    VerificationRequest,
    consume_approval,
    create_verification_provider,
    make_approval,
    validate_approval,
)
from tool_system.runtime import RunTrace, RuntimeState


@dataclass
class RepairActionDeps:
    """Shared mutable context passed between repair runtime actions."""

    result: Dict[str, Any]
    code_roots: List[str]
    report_dir: Path
    run_id: str
    verification_config: Optional[Dict[str, Any]] = None
    llm_adapter: Any = None
    backup_original_sources: bool = True
    uaf_nullptr_guard_policy: Optional[Callable[..., Any]] = None
    request_record: Optional[Dict[str, Any]] = None
    isolated_workspace: Optional[IsolatedCodeWorkspace] = None
    tool_executor: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None
    tool_authorization: Any = None
    applied_fix_result: Optional[Dict[str, Any]] = None
    diff_review: Optional[Dict[str, Any]] = None
    verification_result: Optional[Dict[str, Any]] = None
    changed_files: List[str] = field(default_factory=list)
    approval: Optional[Dict[str, Any]] = None
    policy: Any = None
    revision_provider: Optional[Callable[[], Tuple[Optional[str], Optional[str]]]] = None
    apply_fix_fn: Optional[Callable[..., Dict[str, Any]]] = None
    trace: Any = None


def _changed_files_in_check_scope(check: Any, changed_files: List[str], workspace: str) -> bool:
    patterns = list(getattr(check, "allowed_changed_files", None) or [])
    if not patterns:
        return True
    root = Path(workspace).expanduser().resolve()
    for item in changed_files:
        path = Path(str(item)).expanduser()
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            return False
        if not any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            return False
    return True


def _run_tool(deps: RepairActionDeps, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    if deps.tool_authorization is not None:
        data["_runtime_authorization"] = deps.tool_authorization
    if deps.approval and isinstance(deps.approval, dict):
        data["approval"] = dict(deps.approval)
    if deps.tool_executor is not None:
        return deps.tool_executor(name, data)
    from tool_system import get_registry

    registry = get_registry()
    tool = registry.get_tool(name)
    if tool is None:
        raise ValueError(f"tool not found: {name}")
    if isinstance(tool, type):
        tool = tool()
    from tool_system.tool_gateway import ToolExecutionGateway

    gateway = ToolExecutionGateway(policy=deps.policy, trace=deps.trace)
    return gateway.execute(name, tool, data)


def _apply_patch_handler(deps: RepairActionDeps, payload: Dict[str, Any]) -> Dict[str, Any]:
    from services.repair_context_bundle import build_repair_context_bundle
    # Make the evidence package available to both the fast path and the LLM
    # extractor.  It remains advisory; CodeFixer and scope review enforce the
    # actual write boundary.
    bundle = build_repair_context_bundle(
        deps.result,
        context_session=deps.result.get("context_session"),
        authorized_scope={
            "code_roots": list(payload.get("code_roots") or deps.code_roots),
            "allowed_changed_files": list((deps.verification_config or {}).get("allowed_changed_files") or []),
        },
    )
    deps.result["repair_context_bundle"] = bundle
    if isinstance(deps.result.get("code_context"), dict):
        # The extractor already receives code_context; placing the bundle
        # there keeps this enhancement effective for existing tool adapters.
        deps.result["code_context"].setdefault("repair_context_bundle", bundle)
    if deps.apply_fix_fn is not None:
        applied = deps.apply_fix_fn(
            result=deps.result,
            code_roots=list(payload.get("code_roots") or deps.code_roots),
            report_dir=Path(payload.get("report_dir") or deps.report_dir),
            backup_original_sources=bool(payload.get("backup_original_sources", deps.backup_original_sources)),
        )
        if not isinstance(applied, dict):
            raise TypeError("apply_fix_fn must return an object")
        deps.applied_fix_result = applied
        deps.changed_files = [
            str(item.get("file")) for item in applied.get("applied", [])
            if isinstance(item, dict) and item.get("file")
        ]
        session = deps.result.get("context_session") if isinstance(deps.result.get("context_session"), dict) else None
        if session is not None:
            from services.evidence_graph import append_graph_observation
            session["evidence_graph"] = append_graph_observation(
                session.get("evidence_graph"), "edit", applied,
                relation="changes", hypothesis_ids=[str(x.get("id")) for x in (session.get("hypotheses") or []) if isinstance(x, dict)],
            )
        return applied
    guard = deps.uaf_nullptr_guard_policy() if callable(deps.uaf_nullptr_guard_policy) else None
    fixer = CodeFixer(deps.llm_adapter, uaf_nullptr_guard_policy=guard)
    trace = getattr(deps, "trace", None)
    if trace is not None and getattr(trace, "budget", None) is not None and deps.llm_adapter is not None:
        try:
            trace.budget.consume("llm")
        except RuntimeError as exc:
            return {"success": False, "error": str(exc), "applied": []}
    fix_result = fixer.generate_and_apply(
        result=deps.result,
        code_roots=list(payload.get("code_roots") or deps.code_roots),
        report_dir=Path(payload.get("report_dir") or deps.report_dir),
        backup_original_sources=bool(payload.get("backup_original_sources", deps.backup_original_sources)),
        tool_executor=deps.tool_executor,
        tool_authorization=deps.tool_authorization,
    )
    applied = fix_result.to_dict()
    session = deps.result.get("context_session") if isinstance(deps.result.get("context_session"), dict) else None
    if session is not None:
        from services.evidence_graph import append_graph_observation
        session["evidence_graph"] = append_graph_observation(
            session.get("evidence_graph"), "edit", applied,
            relation="changes", hypothesis_ids=[str(x.get("id")) for x in (session.get("hypotheses") or []) if isinstance(x, dict)],
        )
    deps.applied_fix_result = applied
    deps.changed_files = [
        str(item.get("file"))
        for item in (applied.get("applied") or [])
        if isinstance(item, dict) and item.get("file")
    ]
    return applied


def _inspect_diff_handler(deps: RepairActionDeps, payload: Dict[str, Any]) -> Dict[str, Any]:
    applied = deps.applied_fix_result or {}
    declared_changed = list(payload.get("changed_files") or deps.changed_files)
    review_config = deps.verification_config if isinstance(deps.verification_config, dict) else {}
    if deps.isolated_workspace is not None:
        actual = collect_workspace_diff(deps.isolated_workspace)
        changed = list(actual.get("changed_files") or [])
        diff_text = str(actual.get("diff_text") or "")
        changed_contents = dict(actual.get("changed_contents") or {})
        original_contents = dict(actual.get("original_contents") or {})
        workspace = str(deps.isolated_workspace.root)
    else:
        changed = list(declared_changed)
        changed_contents = {}
        original_contents = {}
        diff_parts: List[str] = []
        for item in (applied.get("applied") or []):
            if not isinstance(item, dict) or not item.get("file"):
                continue
            path = str(Path(str(item["file"])).expanduser().resolve())
            try:
                changed_contents[path] = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                changed_contents[path] = ""
            backup = Path(str(item.get("backup_path") or ""))
            if backup.is_file():
                old = backup.read_text(encoding="utf-8", errors="replace")
                original_contents[path] = old
                diff_parts.extend(difflib.unified_diff(
                    old.splitlines(), changed_contents[path].splitlines(),
                    fromfile=path, tofile=path, lineterm="",
                ))
        diff_text = "\n".join(diff_parts)
        workspace = str(Path(deps.code_roots[0]).resolve()) if deps.code_roots else ""
    candidates = extract_candidate_nodes(
        deps.result.get("code_context") if isinstance(deps.result.get("code_context"), dict) else {}
    )
    candidate_files = [str(item.get("file")) for item in candidates if item.get("file")]
    allowed_files = review_config.get("allowed_files") or review_config.get("allowed_changed_files") or candidate_files
    if deps.isolated_workspace is not None:
        allowed_files = [map_original_path(deps.isolated_workspace, str(path)) for path in allowed_files]
    changed_functions = [
        str(item.get("function_signature") or "")
        for item in (applied.get("applied") or [])
        if isinstance(item, dict) and item.get("function_signature")
    ]
    allowed_functions = [
        str(item.get("signature") or item.get("function_signature") or "")
        for item in candidates
        if item.get("signature") or item.get("function_signature")
    ]
    review = review_changed_files(
        changed,
        allowed_files,
        diff_text=diff_text,
        max_files=review_config.get("max_changed_files", 8),
        max_diff_lines=review_config.get("max_diff_lines", 800),
        changed_contents=changed_contents,
        original_contents=original_contents,
        workspace=workspace,
        max_function_lines=review_config.get("max_function_lines", 400),
        declared_files=declared_changed,
        changed_functions=changed_functions,
        allowed_functions=allowed_functions,
        allow_dependency_changes=bool(review_config.get("allow_dependency_changes", False)),
        allow_public_api_changes=bool(review_config.get("allow_public_api_changes", False)),
    )
    deps.diff_review = review.to_dict()
    return deps.diff_review


def _verification_tool_action_handler(deps: RepairActionDeps, tool_name: str):
    def _handler(payload: Dict[str, Any]) -> Dict[str, Any]:
        config = dict(deps.verification_config or {})
        # Model payload may select a check, but never supplies executable
        # command/argv/shell data. Legacy top-level config remains supported.
        requested_metadata = payload.get("verification") or {}
        if isinstance(requested_metadata, dict):
            for key, value in requested_metadata.items():
                if (not isinstance(config.get("checks"), list)
                        and key not in {"command", "argv", "shell", "checks", "provider"}):
                    config[key] = value
        requested_check_id = str(payload.get("check_id") or config.get("check_id") or "").strip()
        reproduction = payload.get("reproduction_plan")
        if isinstance(reproduction, dict):
            requested_check_id = str(reproduction.get("check_id") or "").strip()
            if not requested_check_id:
                return {"status": "pending", "verification_status": "inconclusive", "failure_class": "schema_error", "provider": tool_name, "error": "reproduction_plan.check_id is required"}
        declared_checks = config.get("checks")
        if (not config.get("command") and isinstance(declared_checks, list) and declared_checks
                and not requested_check_id and config.get("execute_all_declared_checks") is True):
            # A profile is a plan, not a single implicit command.  Execute all
            # declared checks in order and retain each result for baseline /
            # post-fix comparison.  A caller can still select one by id.
            check_results = []
            for check in declared_checks:
                if not isinstance(check, dict):
                    continue
                check_id = str(check.get("id") or "").strip()
                if not check_id:
                    continue
                check_results.append(_handler({**payload, "check_id": check_id}))
            statuses = [str(item.get("status") or "").lower() for item in check_results]
            failed = next((item for item in check_results if str(item.get("status") or "").lower() not in {"passed", "compile_verified", "native_verified", "integration_verified"}), None)
            aggregate = dict(failed or {"status": "passed"})
            aggregate["status"] = "passed" if not failed else str(failed.get("status") or "failed")
            aggregate["checks"] = check_results
            aggregate["check_ids"] = [str(check.get("id")) for check in declared_checks if isinstance(check, dict) and check.get("id")]
            aggregate["profile_execution"] = "all_declared_checks"
            return aggregate
        workspace_path = str(
            payload.get("workspace")
            or (deps.isolated_workspace.root if deps.isolated_workspace is not None else "")
            or (deps.code_roots[0] if deps.code_roots else Path.cwd())
        )
        changed = list(payload.get("changed_files") or deps.changed_files)
        command = config.get("command")
        selected_check = None
        checks = config.get("checks")
        if not command and isinstance(checks, list) and checks:
            requested_id = str(payload.get("check_id") or config.get("check_id") or "").strip()
            for candidate in checks:
                if not isinstance(candidate, dict):
                    continue
                if requested_id and str(candidate.get("id") or "") != requested_id:
                    continue
                selected_check = candidate
                break
            if selected_check is None and not requested_id:
                selected_check = None
            if selected_check is not None:
                command = selected_check.get("command")
                config["mode"] = selected_check.get("kind") or "auto"
                config["timeout_sec"] = selected_check.get("timeout_sec") or selected_check.get("timeout") or 300
                config["iterations"] = selected_check.get("iterations", 1)
                config["expected_signature"] = selected_check.get("expected_signature")
        if not command:
            return {
                # Preserve legacy transport status while exposing the precise
                # additive state used by the evidence-level judge.
                "status": "pending",
                "verification_status": "not_configured",
                "provider": tool_name,
                "error": "verification profile/command is not configured",
                "changed_files": list(changed),
            }
        # Bind selected execution to a deterministic plan fingerprint. Raw
        # model command fields are intentionally excluded from this binding.
        if selected_check is not None and not config.get("plan_fingerprint"):
            try:
                from services.verification_plan import build_verification_plan
                claim = payload.get("verification_claim") or {"statement": "execute declared verification", "minimum_level": selected_check.get("verification_level") or "L1"}
                bound = build_verification_plan(claim, config, {"check_id": selected_check.get("id"), "purpose": (reproduction or {}).get("purpose") if isinstance(reproduction, dict) else (payload.get("purpose") or "reproduce")})
                config["plan_fingerprint"] = bound.fingerprint
            except (TypeError, ValueError):
                return {"status": "pending", "verification_status": "inconclusive", "failure_class": "schema_error", "provider": tool_name, "error": "invalid verification plan"}
        if selected_check is not None:
            try:
                from services.verification_profile import VerificationCheck
                parsed_check = VerificationCheck.from_mapping(selected_check)
                parsed_check.validate_paths(workspace_path, deps.code_roots)
            except ValueError as exc:
                return {"status": "not_configured", "verification_status": "inconclusive",
                        "failure_class": "path_blocked", "provider": tool_name, "error": str(exc)}
            if not _changed_files_in_check_scope(parsed_check, changed, workspace_path):
                return {"status": "not_configured", "verification_status": "inconclusive",
                        "failure_class": "path_blocked", "provider": parsed_check.provider,
                        "error": "changed files exceed verification check scope"}
            working_dir = Path(workspace_path) / str(parsed_check.working_directory or ".")
            fixture_path = Path(workspace_path) / parsed_check.fixture if parsed_check.fixture else None
            if not working_dir.is_dir() or (fixture_path is not None and not fixture_path.exists()):
                return {"status": "configured_but_unavailable", "verification_status": "configured_but_unavailable",
                        "provider": parsed_check.provider, "mode": parsed_check.kind,
                        "failure_class": "empty_result", "error": "configured working directory or fixture is unavailable"}
            import hashlib
            command_fingerprint = hashlib.sha256("\0".join(command).encode()).hexdigest()[:16]
            expected_command_fingerprint = str(payload.get("command_fingerprint") or "")
            if expected_command_fingerprint and expected_command_fingerprint != command_fingerprint:
                return {"status": "not_configured", "verification_status": "inconclusive",
                        "provider": parsed_check.provider, "mode": parsed_check.kind,
                        "failure_class": "schema_error", "error": "verification command fingerprint mismatch"}
        if deps.tool_executor is None:
            return {
                "status": "unavailable",
                "provider": tool_name,
                "error": "tool executor unavailable",
                "changed_files": list(changed),
            }
        tool_payload: Dict[str, Any] = {
            "workspace": workspace_path,
            "changed_files": changed,
            "command": command,
            "timeout_sec": float(config.get("timeout_sec") or 300),
            "mode": str(config.get("mode") or "auto"),
            "check_id": str((selected_check or {}).get("id") or payload.get("check_id") or "") or None,
            "iterations": int(config.get("iterations") or 1),
            "expected_signature": config.get("expected_signature"),
            "provider": str((selected_check or {}).get("provider") or config.get("provider") or "local_command"),
            "fixture": (selected_check or {}).get("fixture"),
            "verification_level": (selected_check or {}).get("verification_level"),
            "purpose": (reproduction or {}).get("purpose") if isinstance(reproduction, dict) else payload.get("purpose"),
            "plan_fingerprint": config.get("plan_fingerprint"),
            "working_directory": (selected_check or {}).get("working_directory"),
            "command_fingerprint": command_fingerprint if selected_check is not None else None,
        }
        if deps.tool_authorization is not None:
            tool_payload["_runtime_authorization"] = deps.tool_authorization
        tool_out = deps.tool_executor(tool_name, tool_payload)
        summary = dict(tool_out) if isinstance(tool_out, dict) else {
            "status": "unavailable",
            "provider": tool_name,
            "error": "verification tool returned non-object output",
        }
        summary["changed_files"] = list(changed)
        summary.setdefault("provider", tool_name)
        if selected_check is not None:
            summary.setdefault("check_id", selected_check.get("id"))
            summary.setdefault("fixture", selected_check.get("fixture"))
            summary.setdefault("verification_level", selected_check.get("verification_level"))
            summary.setdefault("purpose", tool_payload.get("purpose"))
            summary.setdefault("plan_fingerprint", tool_payload.get("plan_fingerprint"))
            summary.setdefault("iterations", tool_payload.get("iterations"))
            summary.setdefault("workspace_revision", (deps.revision_provider() if deps.revision_provider else (None, None))[1])
            import hashlib, json, platform
            environment_data = {"provider": tool_payload.get("provider"), "platform": platform.platform(), "workspace": workspace_path}
            summary.setdefault("environment_fingerprint", hashlib.sha256(json.dumps(environment_data, sort_keys=True).encode()).hexdigest()[:20])
        decision, schema_error = VerificationDecision.from_mapping(summary)
        if decision is None:
            summary = {
                "status": "unavailable",
                "provider": tool_name,
                "mode": str(config.get("mode") or "auto"),
                "error": "verification tool returned malformed output",
                "schema_violation": str(schema_error or "invalid verification decision"),
                "changed_files": list(changed),
            }
        else:
            summary.update(decision.to_dict())
        session = deps.result.get("context_session") if isinstance(deps.result.get("context_session"), dict) else None
        if session is not None:
            from services.evidence_graph import append_graph_observation
            status = str(summary.get("status") or summary.get("verification_status") or "unknown").lower()
            relation = "supports" if status in {"passed", "compile_verified", "native_verified", "integration_verified"} else "informs"
            session["evidence_graph"] = append_graph_observation(
                session.get("evidence_graph"), "verification", summary,
                relation=relation,
                hypothesis_ids=[str(x.get("id")) for x in (session.get("hypotheses") or []) if isinstance(x, dict)],
            )
        if deps.isolated_workspace is not None:
            summary["worktree"] = deps.isolated_workspace.to_dict()
        deps.verification_result = summary
        return summary

    return _handler


def _verify_handler(deps: RepairActionDeps, payload: Dict[str, Any]) -> Dict[str, Any]:
    config = dict(deps.verification_config or {})
    # The action payload is not an execution authority. Profiles and legacy
    # commands must already be present in RepairActionDeps.
    if isinstance(config.get("checks"), list) and not str(config.get("check_id") or "").strip():
        return {"status": "pending", "verification_status": "not_configured",
                "failure_class": "schema_error", "provider": "none", "mode": "auto",
                "error": "no declared verification check was selected",
                "changed_files": list(payload.get("changed_files") or deps.changed_files)}
    explicit_tool = str(config.get("tool") or "").strip()
    explicit_command = config.get("command")
    if explicit_tool in VERIFICATION_ACTION_TOOLS and explicit_command:
        return _verification_tool_action_handler(deps, explicit_tool)(payload)
    workspace_path = str(
        payload.get("workspace")
        or (deps.isolated_workspace.root if deps.isolated_workspace is not None else "")
        or (deps.code_roots[0] if deps.code_roots else Path.cwd())
    )
    changed = list(payload.get("changed_files") or deps.changed_files)
    if isinstance(config.get("checks"), list):
        try:
            from services.verification_profile import VerificationProfile
            selected_check = VerificationProfile.from_mapping(config).check(str(config.get("check_id")))
            selected_check.validate_paths(workspace_path, deps.code_roots)
        except (KeyError, ValueError) as exc:
            return {"status": "not_configured", "verification_status": "inconclusive",
                    "failure_class": "path_blocked", "provider": "none", "mode": "auto",
                    "error": str(exc), "changed_files": changed}
        if not _changed_files_in_check_scope(selected_check, changed, workspace_path):
            return {"status": "not_configured", "verification_status": "inconclusive",
                    "failure_class": "path_blocked", "provider": selected_check.provider,
                    "mode": selected_check.kind, "error": "changed files exceed verification check scope",
                    "changed_files": changed}
        working_dir = Path(workspace_path) / str(selected_check.working_directory or ".")
        fixture_path = Path(workspace_path) / selected_check.fixture if selected_check.fixture else None
        if not working_dir.is_dir() or (fixture_path is not None and not fixture_path.exists()):
            return {"status": "configured_but_unavailable", "verification_status": "configured_but_unavailable",
                    "provider": selected_check.provider, "mode": selected_check.kind,
                    "failure_class": "empty_result", "error": "configured working directory or fixture is unavailable",
                    "changed_files": changed}
    if explicit_tool and explicit_command and deps.tool_executor is not None:
        tool_payload: Dict[str, Any] = {
            "workspace": workspace_path,
            "changed_files": changed,
            "command": explicit_command,
            "timeout_sec": float(config.get("timeout_sec") or 300),
            "mode": str(config.get("mode") or "auto"),
        }
        if deps.tool_authorization is not None:
            tool_payload["_runtime_authorization"] = deps.tool_authorization
        tool_out = deps.tool_executor(explicit_tool, tool_payload)
        summary = dict(tool_out) if isinstance(tool_out, dict) else {
            "status": "unavailable",
            "provider": explicit_tool,
            "error": "verification tool returned non-object output",
        }
        summary["changed_files"] = list(changed)
        summary.setdefault("provider", explicit_tool)
        decision, schema_error = VerificationDecision.from_mapping(summary)
        if decision is None:
            summary = {
                "status": "unavailable",
                "provider": explicit_tool,
                "mode": str(config.get("mode") or "auto"),
                "error": "verification tool returned malformed output",
                "schema_violation": str(schema_error or "invalid verification decision"),
                "changed_files": list(changed),
            }
        else:
            summary.update(decision.to_dict())
        if deps.isolated_workspace is not None:
            summary["worktree"] = deps.isolated_workspace.to_dict()
        deps.verification_result = summary
        return summary
    provider = create_verification_provider(config, approved=False)
    if deps.policy is not None and getattr(provider, "name", "") == "local_command":
        provider.policy = deps.policy
    workspace_path = str(
        payload.get("workspace")
        or (deps.isolated_workspace.root if deps.isolated_workspace is not None else "")
        or (deps.code_roots[0] if deps.code_roots else Path.cwd())
    )
    changed = list(payload.get("changed_files") or deps.changed_files)
    request = VerificationRequest(
        workspace=workspace_path,
        changed_files=changed,
        target=str(config.get("target") or "") or None,
        mode=str(config.get("mode") or "auto"),
        timeout_sec=float(config.get("timeout_sec") or 300),
        report_dir=str(deps.report_dir),
        check_id=str(config.get("check_id") or "") or None,
        purpose=(config.get("reproduction_plan") or {}).get("purpose") if isinstance(config.get("reproduction_plan"), dict) else None,
        plan_fingerprint=str(config.get("plan_fingerprint") or "") or None,
        fixture=selected_check.fixture if isinstance(config.get("checks"), list) else None,
        verification_level=selected_check.verification_level if isinstance(config.get("checks"), list) else None,
        iterations=selected_check.iterations if isinstance(config.get("checks"), list) else int(config.get("iterations") or 1),
        expected_signature=selected_check.expected_signature if isinstance(config.get("checks"), list) else config.get("expected_signature"),
        working_directory=selected_check.working_directory if isinstance(config.get("checks"), list) else config.get("working_directory"),
    )
    validation = provider.validate(request)
    tool_call_id = str(payload.get("tool_call_id") or "verification")
    if validation.status == "pending" and getattr(provider, "name", "") == "local_command":
        fingerprint = str(validation.command_fingerprint or "")
        if not deps.approval:
            verification = validation
            verification.approval = make_approval(
                run_id=deps.run_id,
                tool_call_id=tool_call_id,
                command_fingerprint=fingerprint,
            )
        else:
            checked = validate_approval(
                deps.approval,
                fingerprint=fingerprint,
                run_id=deps.run_id,
                tool_call_id=tool_call_id,
            )
            if checked.get("status") != "granted":
                verification = validation
                verification.error = str(checked.get("validation_error") or "invalid approval")
                verification.approval = checked
            else:
                consumed = consume_approval(
                    checked,
                    fingerprint=fingerprint,
                    run_id=deps.run_id,
                    tool_call_id=tool_call_id,
                )
                provider = create_verification_provider(config, approved=True)
                if deps.policy is not None and getattr(provider, "name", "") == "local_command":
                    provider.policy = deps.policy
                approved_validation = provider.validate(request)
                verification = (
                    provider.execute(request)
                    if approved_validation.status == "passed"
                    else approved_validation
                )
                verification.approval = consumed
                deps.approval = consumed
    else:
        verification = validation if validation.status in {"pending", "unavailable"} else provider.execute(request)
    summary = provider.summarize(verification)
    summary["changed_files"] = list(changed)
    if isinstance(config.get("checks"), list) and config.get("check_id"):
        selected = next((item for item in config["checks"] if isinstance(item, dict) and str(item.get("id")) == str(config["check_id"])), {})
        summary.setdefault("check_id", config.get("check_id"))
        summary.setdefault("provider", selected.get("provider") or summary.get("provider"))
        summary.setdefault("purpose", (config.get("reproduction_plan") or {}).get("purpose") if isinstance(config.get("reproduction_plan"), dict) else None)
        summary.setdefault("plan_fingerprint", config.get("plan_fingerprint"))
        summary.setdefault("fixture", selected.get("fixture"))
        summary.setdefault("verification_level", selected.get("verification_level"))
        summary.setdefault("iterations", selected.get("iterations", 1))
        import hashlib, json, platform
        environment_data = {"provider": summary.get("provider"), "platform": platform.platform(), "workspace": workspace_path}
        summary.setdefault("environment_fingerprint", hashlib.sha256(json.dumps(environment_data, sort_keys=True).encode()).hexdigest()[:20])
        summary.setdefault("workspace_revision", (deps.revision_provider() if deps.revision_provider else (None, None))[1])
    if summary.get("provider") == "none" and summary.get("status") == "unavailable":
        summary["status"] = "pending"
    decision, schema_error = VerificationDecision.from_mapping(summary)
    if decision is None:
        summary = {
            "status": "unavailable",
            "provider": str(summary.get("provider") or "invalid_provider"),
            "mode": str(summary.get("mode") or request.mode),
            "error": "verification provider returned malformed output",
            "schema_violation": str(schema_error or "invalid verification decision"),
            "changed_files": list(changed),
        }
    else:
        # Preserve provider details while ensuring the decision fields are the
        # normalized values consumed by Runtime and report writers.
        summary.update(decision.to_dict())
    if deps.isolated_workspace is not None:
        summary["worktree"] = deps.isolated_workspace.to_dict()
    deps.verification_result = summary
    return summary


def _rollback_handler(deps: RepairActionDeps, payload: Dict[str, Any]) -> Dict[str, Any]:
    applied = deps.applied_fix_result or {}
    items = list(payload.get("applied") or applied.get("applied") or [])
    rolled = rollback_applied_edits(items)
    result = {"status": "completed" if rolled else "unavailable", "files": rolled}
    if applied:
        applied["rolled_back_files"] = rolled
        applied["success"] = False
    deps.applied_fix_result = applied
    return result


def _sync_worktree_handler(deps: RepairActionDeps, payload: Dict[str, Any]) -> Dict[str, Any]:
    if deps.isolated_workspace is None:
        return {"status": "skipped", "files": []}
    changed = list(payload.get("changed_files") or deps.changed_files)
    copied = sync_verified_files_back(deps.isolated_workspace, changed)
    for item in (deps.applied_fix_result or {}).get("applied", []):
        if isinstance(item, dict) and item.get("file"):
            item["file"] = map_original_path(deps.isolated_workspace, item["file"])
    return {"status": "completed" if copied else "no_files_copied", "files": copied}


def _post_fix_diagnosis_handler(deps: RepairActionDeps, payload: Dict[str, Any]) -> Dict[str, Any]:
    record = deps.request_record if isinstance(deps.request_record, dict) else {}
    diagnosis = run_post_fix_diagnosis(
        crash_log=record.get("crash_log"),
        crash_log_content=record.get("crash_log_content"),
        library_dir=record.get("library_dir"),
        code_roots=list(deps.code_roots or record.get("code_roots") or []),
    )
    return diagnosis


def _reanalyze_diagnosis_handler(deps: RepairActionDeps, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Verification failure follow-up: parse_stack_only without re-applying fixes."""
    return _post_fix_diagnosis_handler(deps, payload)


def build_repair_action_executor(
    *,
    state: RuntimeState,
    trace: Optional[RunTrace],
    deps: RepairActionDeps,
    policy: Any = None,
) -> RuntimeActionExecutor:
    executor = RuntimeActionExecutor(
        state=state, trace=trace, policy=policy,
        revision_provider=deps.revision_provider,
        security_analyzer=ActionSecurityAnalyzer(),
    )
    executor.register(RuntimeAction("apply_patch", lambda p: _apply_patch_handler(deps, p),
                                    requires_approval=False, risk="workspace_write", side_effect=True,
                                    input_schema={"code_roots": list, "report_dir": str}))
    executor.register(RuntimeAction("inspect_diff", lambda p: _inspect_diff_handler(deps, p),
                                    input_schema={"artifact_dir": str}))
    executor.register(RuntimeAction("verify", lambda p: _verify_handler(deps, p),
                                    risk="execute", side_effect=True,
                                    input_schema={"artifact_dir": str}))
    executor.register(RuntimeAction("rollback", lambda p: _rollback_handler(deps, p),
                                    idempotent=False, risk="workspace_write", side_effect=True,
                                    input_schema={"artifact_dir": str}))
    executor.register(RuntimeAction("sync_worktree", lambda p: _sync_worktree_handler(deps, p),
                                    risk="workspace_write", side_effect=True,
                                    input_schema={"artifact_dir": str}))
    executor.register(RuntimeAction("post_fix_diagnosis", lambda p: _post_fix_diagnosis_handler(deps, p),
                                    input_schema={"artifact_dir": str}))
    for tool_name in sorted(VERIFICATION_ACTION_TOOLS):
        executor.register(
            RuntimeAction(
                tool_name,
                _verification_tool_action_handler(deps, tool_name),
                requires_approval=True,
                risk="execute",
                side_effect=True,
                idempotent=False,
                input_schema={"artifact_dir": str},
            )
        )
    return executor


def run_reanalyze_on_failure(deps: RepairActionDeps, executor: RuntimeActionExecutor) -> Dict[str, Any]:
    """Always produce deterministic post-failure diagnosis; never patch again."""
    return _reanalyze_diagnosis_handler(deps, {})
