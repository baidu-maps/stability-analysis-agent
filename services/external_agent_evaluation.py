"""Generate contamination-resistant tasks for external coding-agent comparison."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


EVALUATION_DIR = "external_agent_evaluation"
TASK_FILE = "benchmark_task.md"
INPUT_MANIFEST_FILE = "input_manifest.json"
SUBMISSION_SCHEMA_FILE = "submission_schema.json"
COMPARISON_JSON_FILE = "comparison_report.json"
COMPARISON_MARKDOWN_FILE = "comparison_report.md"


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _capabilities(config: Any) -> List[Dict[str, Any]]:
    if not isinstance(config, Mapping):
        return []
    try:
        from services.verification_plan import capabilities_from_profile
        from services.verification_profile import VerificationProfile

        profile = VerificationProfile.from_mapping(config)
        return [item.to_dict() for item in capabilities_from_profile(profile)]
    except (TypeError, ValueError):
        return []


def _allowed_changed_files(config: Any) -> List[str]:
    if not isinstance(config, Mapping):
        return []
    values: List[str] = []
    for check in config.get("checks") or []:
        if isinstance(check, Mapping):
            values.extend(str(item) for item in (check.get("allowed_changed_files") or []) if isinstance(item, str))
    values.extend(str(item) for item in (config.get("allowed_changed_files") or []) if isinstance(item, str))
    return list(dict.fromkeys(item for item in values if item))


def _submission_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "External crash investigation submission",
        "type": "object",
        "required": ["schema_version", "tool", "investigation_journal", "root_cause", "supporting_evidence", "changed_files", "confidence", "limitations"],
        "properties": {
            "schema_version": {"const": 1},
            "tool": {"type": "string"},
            "model": {"type": ["string", "null"]},
            "investigation_journal": {"type": "array", "items": {"type": "object", "required": ["step", "goal", "action", "evidence", "observation", "hypothesis_update"]}},
            "root_cause": {"type": "object", "required": ["category", "statement", "location"]},
            "supporting_evidence": {"type": "array", "items": {"type": "object"}},
            "contradicting_evidence": {"type": "array", "items": {"type": "object"}},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "patch_summary": {"type": "string"},
            "verification_claim": {"type": "object"},
            "selected_check_id": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    }


def build_external_agent_evaluation(
    report_dir: Path,
    *,
    request: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Write a neutral external-agent task rooted at the shared symbolized input."""
    root = Path(report_dir).expanduser().resolve()
    resolved = root / "03_add2line_resolver.json"
    if not resolved.is_file():
        return None
    request_value = request if isinstance(request, dict) else {}
    result_value = result if isinstance(result, dict) else {}
    paths = request_value.get("paths") if isinstance(request_value.get("paths"), dict) else {}
    effective = request_value.get("effective_parameters") if isinstance(request_value.get("effective_parameters"), dict) else {}
    code_roots = [str(item) for item in (paths.get("code_roots") or []) if isinstance(item, str)]
    verification = effective.get("verification") if isinstance(effective.get("verification"), dict) else None
    capabilities = _capabilities(verification)
    workspace_snapshot = request_value.get("workspace_snapshot") if isinstance(request_value.get("workspace_snapshot"), dict) else {}
    runtime_state = ((result_value.get("metadata") or {}).get("runtime_state")
                     if isinstance(result_value.get("metadata"), dict) else None)
    runtime_state = runtime_state if isinstance(runtime_state, dict) else {}
    source_revision = workspace_snapshot.get("source_revision") or runtime_state.get("source_revision")
    worktree_revision = workspace_snapshot.get("worktree_revision") or runtime_state.get("worktree_revision")
    if not source_revision and code_roots:
        try:
            from services.workspace_revision import workspace_revisions
            source_revision, worktree_revision = workspace_revisions(code_roots)
        except Exception:
            pass
    crash_input = request_value.get("crash_input") if isinstance(request_value.get("crash_input"), dict) else {}
    evaluation = root / EVALUATION_DIR
    evaluation.mkdir(parents=True, exist_ok=True)
    (evaluation / "submissions").mkdir(exist_ok=True)
    manifest = {
        "schema_version": 1,
        "task_version": "crash_solution_comparison_v1",
        "run_id": request_value.get("run_id"),
        "shared_inputs": {
            "parsed_crash": "../01_crash_log_parser.json" if (root / "01_crash_log_parser.json").is_file() else None,
            "resolved_stack": "../03_add2line_resolver.json",
            "crash_input": {key: crash_input.get(key) for key in ("source", "resolved_path", "original_path", "sha256") if crash_input.get(key) is not None},
            "code_roots": code_roots,
        },
        "input_hashes": {
            "parsed_crash": _sha256(root / "01_crash_log_parser.json"),
            "resolved_stack": _sha256(resolved),
        },
        "expected_source_revision": source_revision,
        "observed_worktree_revision": worktree_revision,
        "verification_capabilities": capabilities,
        "verification_profile_configured": bool(verification),
        "authorized_changed_files": _allowed_changed_files(verification),
        "submission_schema": SUBMISSION_SCHEMA_FILE,
        "excluded_agent_artifacts": [
            "../04a_crash_diagnosis.json", "../04b_code_content_provider.json",
            "../05_memory_context.json", "../context_session.json", "../round_0/07_ai_gen_res.md",
            "../08_apply_ai_fixes.json", "../09_verification.json",
        ],
    }
    (evaluation / INPUT_MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (evaluation / SUBMISSION_SCHEMA_FILE).write_text(json.dumps(_submission_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    capability_lines = [
        "- `%s`: %s (%s, provider=%s)" % (
            item.get("check_id"), item.get("description") or item.get("kind"),
            item.get("verification_level"), item.get("provider"),
        ) for item in capabilities
    ] or ["- 未声明可执行验证能力；不得猜测或执行 build/test/reproduce 命令。"]
    task = """# External Coding Agent Crash Benchmark Task

你正在参加一次隔离的 Crash 诊断与修复对比。地址解析已经完成。请从共享输入出发，自主搜索和读取代码，定位直接原因和根本原因；证据充分时再修改代码。

## Shared Inputs

- 输入清单：`input_manifest.json`
- 地址解析结果：`../03_add2line_resolver.json`
- Crash 解析结果：`../01_crash_log_parser.json`（若存在）
- 代码根：见 `input_manifest.json.shared_inputs.code_roots`
- 预期源码 revision：见 `input_manifest.json.expected_source_revision`

不得读取 `input_manifest.json.excluded_agent_artifacts` 中列出的当前 Crash Agent 产物。它们包含当前 Agent 的诊断、代码上下文、记忆、修复或验证结果，会污染对比。

## Verification Capabilities

%s

只能选择以上已声明的 `check_id`。不得生成新命令、补充未授权参数、安装依赖或自动创建 harness。没有能力时只提交静态结论和限制。

## Investigation Contract

- 每个关键结论必须引用 stack frame、符号或 `file:line` 源码证据。
- 区分事实、推断、反证和缺失证据。
- 记录每个可观察调查步骤：目标、假设、工具动作、取得的证据、观察和假设更新。
- 这里只要求可审计的决策日志，不要求披露隐藏的内部思维过程。
- 不得越出代码根或授权修改范围；不要把一次未复现解释为修复成功。
- 若当前工作区 revision 与清单不一致，停止修改并在 limitations 中报告。

## Submission

最终答案必须提供符合 `submission_schema.json` 的单个 JSON 对象。若修改了代码，同时提供 unified diff。建议将结果保存为：

```text
external_agent_evaluation/submissions/<tool>/result.json
external_agent_evaluation/submissions/<tool>/patch.diff
external_agent_evaluation/submissions/<tool>/action_trace.jsonl
```

`<tool>` 使用 `codex`、`cursor` 或其他稳定的小写工具标识。
    """ % "\n".join(capability_lines)
    task = task.replace("`input_manifest.json`", "`%s`" % (evaluation / INPUT_MANIFEST_FILE), 1)
    task = task.replace("`../03_add2line_resolver.json`", "`%s`" % resolved, 1)
    parsed_path = root / "01_crash_log_parser.json"
    task = task.replace("`../01_crash_log_parser.json`", "`%s`" % parsed_path, 1)
    if code_roots:
        task = task.replace("- 代码根：见 `input_manifest.json.shared_inputs.code_roots`",
                            "- 代码根：" + ", ".join("`%s`" % item for item in code_roots))
    (evaluation / TASK_FILE).write_text(task, encoding="utf-8")
    return {"directory": str(evaluation), "task": TASK_FILE, "manifest": INPUT_MANIFEST_FILE,
            "submission_schema": SUBMISSION_SCHEMA_FILE, "capability_count": len(capabilities)}


def build_external_agent_comparison(report_dir: Path) -> Dict[str, Any]:
    """Summarize normalized external submissions without pretending to be a gold judge."""
    root = Path(report_dir).expanduser().resolve()
    evaluation = root / EVALUATION_DIR
    submissions: List[Dict[str, Any]] = []
    for path in sorted((evaluation / "submissions").glob("*/result.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            submissions.append({"tool": path.parent.name, "valid": False, "error": str(exc)})
            continue
        required = set(_submission_schema()["required"])
        missing = sorted(required - set(value)) if isinstance(value, dict) else sorted(required)
        submissions.append({
            "tool": str(value.get("tool") or path.parent.name) if isinstance(value, dict) else path.parent.name,
            "valid": not missing,
            "missing_fields": missing,
            "root_cause": value.get("root_cause") if isinstance(value, dict) else None,
            "evidence_count": len(value.get("supporting_evidence") or []) if isinstance(value, dict) else 0,
            "investigation_steps": len(value.get("investigation_journal") or []) if isinstance(value, dict) else 0,
            "changed_files": list(value.get("changed_files") or []) if isinstance(value, dict) else [],
            "selected_check_id": value.get("selected_check_id") if isinstance(value, dict) else None,
            "confidence": value.get("confidence") if isinstance(value, dict) else None,
            "limitations": list(value.get("limitations") or []) if isinstance(value, dict) else [],
            "result_path": path.relative_to(root).as_posix(),
        })
    current_diagnosis: Any = None
    current_verification: Any = None
    for path, target in ((root / "04a_crash_diagnosis.json", "diagnosis"), (root / "09_verification.json", "verification")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if target == "diagnosis": current_diagnosis = loaded
        else: current_verification = loaded
    payload = {
        "schema_version": 1,
        "comparison_kind": "descriptive_external_agent_comparison",
        "current_agent": {"diagnosis": current_diagnosis, "verification": current_verification},
        "external_submissions": submissions,
        "limitations": [
            "没有 benchmark gold/reference 时，本报告只比较调查和结果差异，不能单独判定准确率。",
            "准确率应由 examples case expected evidence 或人工盲审补充。",
        ],
    }
    evaluation.mkdir(parents=True, exist_ok=True)
    (evaluation / COMPARISON_JSON_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = ["| Tool | Valid | Evidence | Steps | Changed files | Confidence |", "|---|---:|---:|---:|---:|---:|"]
    for item in submissions:
        rows.append("| %s | %s | %s | %s | %s | %s |" % (
            item.get("tool"), "yes" if item.get("valid") else "no", item.get("evidence_count", 0),
            item.get("investigation_steps", 0), len(item.get("changed_files") or []), item.get("confidence"),
        ))
    markdown = "# External Agent Comparison Report\n\n" + "\n".join(rows) + "\n\n"
    markdown += "> 本报告为描述性对比。没有 gold/reference 时不能据此单独判断哪个 Agent 更准确。\n"
    (evaluation / COMPARISON_MARKDOWN_FILE).write_text(markdown, encoding="utf-8")
    return payload
