#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the real AI repair pipeline and compare final source with an expected patch."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import difflib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import socket
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from services.evaluation import evaluate_case


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IGNORES = {
    ".crash_agent",
    ".DS_Store",
    "__pycache__",
    "build",
    "cli_reports",
    "reports",
}


class RegressionConfigError(ValueError):
    """Case configuration is invalid."""


@dataclasses.dataclass(frozen=True)
class RegressionCase:
    case_id: str
    manifest_path: Path
    example_root: Path
    crash_log: Path
    library_dir: Path
    code_root: Path
    expected_patch: Path
    allowed_changed_files: Tuple[str, ...]
    platforms: Tuple[str, ...]
    timeout_seconds: int
    agent_args: Tuple[str, ...]
    ignored_names: Tuple[str, ...]


@dataclasses.dataclass
class RegressionResult:
    case_id: str
    verdict: str
    reason: str
    duration_ms: int
    actual_changed_files: List[str]
    expected_changed_files: List[str]
    unauthorized_changed_files: List[str]
    mismatched_files: List[str]
    agent_return_code: Optional[int]
    agent_fix_success: Optional[bool]
    output_dir: str
    command: List[str]
    entrypoint: str = "cli"
    report_dir: str = ""
    attempt: int = 1
    evaluation: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


AgentExecutor = Callable[[Sequence[str], Path, Path, int, Mapping[str, str]], subprocess.CompletedProcess]


def _resolve_repo_path(raw: str, *, base: Path = PROJECT_ROOT) -> Path:
    path = Path(raw).expanduser()
    resolved = (base / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise RegressionConfigError(f"path escapes repository: {raw}") from exc
    return resolved


def load_case(path: Path) -> RegressionCase:
    manifest_path = path.expanduser().resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version") or 0) != 1:
        raise RegressionConfigError(f"unsupported schema_version in {manifest_path}")
    case_id = str(payload.get("id") or "").strip()
    if not case_id:
        raise RegressionConfigError("case id is required")
    example_root = _resolve_repo_path(str(payload.get("example_root") or ""))
    inputs = payload.get("inputs") or {}

    def example_path(name: str) -> Path:
        raw = str(inputs.get(name) or "").strip()
        if not raw:
            raise RegressionConfigError(f"inputs.{name} is required")
        resolved = (example_root / raw).resolve()
        try:
            resolved.relative_to(example_root)
        except ValueError as exc:
            raise RegressionConfigError(f"inputs.{name} escapes example_root") from exc
        return resolved

    expected_patch = _resolve_repo_path(str(payload.get("expected_patch") or ""))
    allowed = tuple(sorted({str(value).strip().replace("\\", "/") for value in payload.get("allowed_changed_files", []) if str(value).strip()}))
    if not allowed:
        raise RegressionConfigError("allowed_changed_files must not be empty")
    case = RegressionCase(
        case_id=case_id,
        manifest_path=manifest_path,
        example_root=example_root,
        crash_log=example_path("crash_log"),
        library_dir=example_path("library_dir"),
        code_root=example_path("code_root"),
        expected_patch=expected_patch,
        allowed_changed_files=allowed,
        platforms=tuple(str(value).lower() for value in payload.get("platforms", [])),
        timeout_seconds=max(1, int(payload.get("timeout_seconds") or 600)),
        agent_args=tuple(str(value) for value in payload.get("agent_args", [])),
        ignored_names=tuple(sorted(DEFAULT_IGNORES | {str(value) for value in payload.get("ignored_names", [])})),
    )
    for required in (case.example_root, case.crash_log, case.library_dir, case.code_root, case.expected_patch):
        if not required.exists():
            raise RegressionConfigError(f"case input does not exist: {required}")
    return case


def _should_ignore(relative: Path, ignored_names: Set[str]) -> bool:
    return any(part in ignored_names for part in relative.parts)


def _source_snapshot(root: Path, ignored_names: Iterable[str]) -> Dict[str, bytes]:
    ignored = set(ignored_names)
    result: Dict[str, bytes] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if _should_ignore(relative, ignored):
            continue
        result[relative.as_posix()] = path.read_bytes()
    return result


def _changed_files(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> List[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _normalized_source(content: bytes) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return ("\n".join(lines) + "\n").encode("utf-8")


def _mismatched_files(actual: Mapping[str, bytes], expected: Mapping[str, bytes]) -> List[str]:
    return sorted(
        path
        for path in set(actual) | set(expected)
        if _normalized_source(actual.get(path, b"")) != _normalized_source(expected.get(path, b""))
    )


def _render_diff(before: Mapping[str, bytes], after: Mapping[str, bytes], paths: Iterable[str]) -> str:
    chunks: List[str] = []
    for relative in sorted(paths):
        old = before.get(relative, b"")
        new = after.get(relative, b"")
        try:
            old_lines = _normalized_source(old).decode("utf-8").splitlines(keepends=True)
            new_lines = _normalized_source(new).decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            chunks.append(f"Binary files a/{relative} and b/{relative} differ\n")
            continue
        chunks.extend(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{relative}", tofile=f"b/{relative}"))
    return "".join(chunks)


def _default_agent_executor(
    command: Sequence[str],
    cwd: Path,
    output_json: Path,
    timeout_seconds: int,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess:
    del output_json
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )


class AIRegressionRunner:
    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        python_executable: str = sys.executable,
        agent_executor: AgentExecutor = _default_agent_executor,
        entrypoint: str = "cli",
    ) -> None:
        self.project_root = project_root.resolve()
        self.python_executable = python_executable
        self.agent_executor = agent_executor
        if entrypoint not in {"cli", "daemon"}:
            raise RegressionConfigError(f"unsupported entrypoint: {entrypoint}")
        self.entrypoint = entrypoint

    def _command(self, case: RegressionCase, workspace: Path) -> List[str]:
        return [
            self.python_executable,
            str(self.project_root / "cli" / "main.py"),
            "--crash-log-file", str(case.crash_log),
            "--library-dir", str(case.library_dir),
            "--code-roots", str(workspace),
            "--scope", "full",
            "--apply-ai-fixes",
            "--backup-original-sources",
            "--output-format", "json",
            *case.agent_args,
        ]

    @staticmethod
    def _daemon_request(case: RegressionCase, workspace: Path) -> Dict[str, Any]:
        request: Dict[str, Any] = {
            "crash_log": str(case.crash_log),
            "library_dir": str(case.library_dir),
            "code_roots": [str(workspace)],
            "scope": "full",
            "apply_ai_fixes": True,
            "backup_original_sources": True,
            "output_format": "json",
        }
        value_options = {
            "--prompt-mode": "prompt_mode",
            "--agent-loop": "agent_loop",
            "--engine": "engine",
            "--llm-mode": "llm_mode",
            "--llm-profile": "llm_profile",
            "--max-agent-rounds": "max_agent_rounds",
            "--max-context-requests-per-round": "max_context_requests_per_round",
        }
        boolean_options = {
            "--streaming": "streaming",
            "--force-disassembly": "force_disassembly",
            "--force-anr-analysis": "force_anr_analysis",
            "--force-memory-analysis": "force_memory_analysis",
            "--force-timeline-analysis": "force_timeline_analysis",
            "--include-memory-in-05": "include_memory_in_05",
        }
        args = list(case.agent_args)
        index = 0
        while index < len(args):
            option = args[index]
            if option in boolean_options:
                request[boolean_options[option]] = True
                index += 1
                continue
            if option in value_options and index + 1 < len(args):
                value: Any = args[index + 1]
                if option in {"--max-agent-rounds", "--max-context-requests-per-round"}:
                    value = int(value)
                request[value_options[option]] = value
                index += 2
                continue
            raise RegressionConfigError(f"agent_args option is not supported by daemon entrypoint: {option}")
        return request

    @staticmethod
    def _http_json(method: str, url: str, payload: Optional[Mapping[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def _execute_daemon(
        self,
        case: RegressionCase,
        workspace: Path,
        timeout_seconds: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        base_url = f"http://127.0.0.1:{port}"
        daemon_command = [
            self.python_executable,
            str(self.project_root / "daemon" / "server.py"),
            "--host", "127.0.0.1",
            "--port", str(port),
        ]
        daemon = subprocess.Popen(
            daemon_command,
            cwd=str(self.project_root),
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                if daemon.poll() is not None:
                    stdout, stderr = daemon.communicate()
                    return subprocess.CompletedProcess(daemon_command, daemon.returncode or 1, stdout, stderr)
                try:
                    status, health = self._http_json("GET", f"{base_url}/health")
                    if status == 200 and health.get("ok"):
                        break
                except (OSError, ValueError, urllib.error.URLError):
                    time.sleep(0.05)
            else:
                return subprocess.CompletedProcess(daemon_command, 124, "", "daemon startup timed out")

            status, created = self._http_json("POST", f"{base_url}/runs", self._daemon_request(case, workspace))
            if status != 200 or not created.get("run_id"):
                return subprocess.CompletedProcess(daemon_command, 1, json.dumps(created), "daemon rejected run")
            run_id = str(created["run_id"])
            while time.monotonic() < deadline:
                status, state = self._http_json("GET", f"{base_url}/runs/{run_id}")
                if status == 200 and state.get("status") in {"done", "error", "canceled"}:
                    result_status, result = self._http_json("GET", f"{base_url}/runs/{run_id}/result")
                    output = str(result.get("output") or "")
                    error = str(result.get("error") or state.get("error") or "")
                    return_code = int(state.get("exit_code") or 0) if state.get("status") == "done" else 1
                    if result_status != 200:
                        return_code = 1
                    return subprocess.CompletedProcess(daemon_command, return_code, output, error)
                time.sleep(0.1)
            return subprocess.CompletedProcess(daemon_command, 124, "", "daemon run timed out")
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=5)

    @staticmethod
    def _report_dir_from_output(stdout: str, stderr: str) -> str:
        import re
        text = f"{stdout}\n{stderr}"
        match = re.search(r"(?:report|cli_report) 已保存到:\s*(.+)", text)
        return match.group(1).strip() if match else ""

    def _latest_cli_report(self, started_at: dt.datetime, case: RegressionCase) -> str:
        del case
        started_timestamp = started_at.timestamp()
        for name in ("reports", "cli_reports"):
            reports_root = self.project_root / name
            if not reports_root.is_dir():
                continue
            candidates = []
            for path in reports_root.iterdir():
                if not path.is_dir():
                    continue
                if (
                    "analysis_full" in path.name
                    and path.stat().st_mtime >= started_timestamp
                    and any((path / name).is_file() for name in ("08_apply_ai_fixes.json", "00_run_summary.json"))
                ):
                    candidates.append(path)
            if candidates:
                return str(max(candidates, key=lambda item: item.stat().st_mtime))
        return ""

    @staticmethod
    def _json_result_from_output(stdout: str) -> Dict[str, Any]:
        decoder = json.JSONDecoder()
        for index, char in enumerate(stdout):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stdout[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and ("status" in value or "applied_ai_fixes" in value):
                return value
        return {}

    @staticmethod
    def _evaluation_payload(report_dir: str, agent_payload: Dict[str, Any], changed_files: List[str]) -> Dict[str, Any]:
        """Build evaluation input from persisted report artifacts, not CLI text."""
        payload = dict(agent_payload) if isinstance(agent_payload, dict) else {}
        root = Path(report_dir) if report_dir else None
        def load(name: str) -> Dict[str, Any]:
            if root is None:
                return {}
            try:
                value = json.loads((root / name).read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                return {}
        fixes = load("08_apply_ai_fixes.json")
        verification = load("09_verification.json")
        diagnosis = load("04a_crash_diagnosis.json")
        summary = load("00_run_summary.json")
        evidence = load("09_evidence.json")
        runtime_trace = load("00_runtime_trace.json")
        if fixes:
            payload["applied_ai_fixes"] = fixes
        if verification:
            payload["verification"] = verification
        if diagnosis:
            payload["crash_diagnosis"] = diagnosis
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata["runtime"] = summary.get("runtime") or summary.get("runtime_state") or metadata.get("runtime")
        metadata["execution_events"] = summary.get("execution_events") or metadata.get("execution_events", [])
        metadata["runtime_trace"] = runtime_trace or summary.get("trace") or metadata.get("runtime_trace", {})
        metadata["evidence_items"] = evidence.get("items", [])
        metadata["evidence_package"] = evidence.get("evidence_package", {})
        payload["metadata"] = metadata
        payload["status"] = str((summary.get("workflow") or {}).get("status") or summary.get("status") or payload.get("status") or "")
        payload["completion_reason"] = summary.get("completion_reason") or payload.get("completion_reason")
        if isinstance(summary.get("diff_review"), dict):
            payload["diff_review"] = summary["diff_review"]
        applied = payload.get("applied_ai_fixes") if isinstance(payload.get("applied_ai_fixes"), dict) else {}
        # Source snapshots are repository-relative; use that same coordinate
        # system for deterministic authorization evaluation.
        applied["applied"] = [{"file": item} for item in changed_files]
        payload["applied_ai_fixes"] = applied
        return payload

    def run(
        self,
        case: RegressionCase,
        output_dir: Path,
        *,
        keep_workspace: bool = False,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> RegressionResult:
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        started = dt.datetime.now(dt.timezone.utc)
        system_name = platform.system().lower()
        if case.platforms and system_name not in case.platforms:
            return self._finish(case, output_dir, started, "skipped", f"platform {system_name} is not supported", [], [], [], [], None, None, [], entrypoint=self.entrypoint)

        temporary = tempfile.TemporaryDirectory(prefix=f"ai-regression-{case.case_id}-")
        temp_root = Path(temporary.name)
        actual_root = temp_root / "actual"
        expected_root = temp_root / "expected"
        shutil.copytree(case.code_root, actual_root, ignore=shutil.ignore_patterns(*case.ignored_names))
        shutil.copytree(case.code_root, expected_root, ignore=shutil.ignore_patterns(*case.ignored_names))
        before = _source_snapshot(case.code_root, case.ignored_names)

        patch_proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(case.expected_patch)],
            cwd=str(expected_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if patch_proc.returncode != 0:
            temporary.cleanup()
            raise RegressionConfigError(f"expected patch cannot be applied: {patch_proc.stderr.strip()}")
        expected = _source_snapshot(expected_root, case.ignored_names)
        expected_changed = _changed_files(before, expected)

        output_json = output_dir / "_agent_result.json"
        command = self._command(case, actual_root)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if extra_env:
            env.update({str(key): str(value) for key, value in extra_env.items()})
        try:
            # Run from the repository root so the CLI owns its normal
            # ``reports/<timestamp>_analysis_*`` artifact location.
            if self.entrypoint == "daemon":
                process = self._execute_daemon(case, actual_root, case.timeout_seconds, env)
                command = list(process.args)
            else:
                process = self.agent_executor(command, self.project_root, output_json, case.timeout_seconds, env)
        except subprocess.TimeoutExpired as exc:
            process = subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "AI regression timed out")
        agent_payload: Dict[str, Any] = {}
        report_dir = self._report_dir_from_output(str(process.stdout or ""), str(process.stderr or ""))
        if not report_dir:
            report_dir = self._latest_cli_report(started, case)
        if report_dir:
            for candidate in (
                Path(report_dir) / "08_apply_ai_fixes.json",
                Path(report_dir) / "final_result.json",
                Path(report_dir) / "00_run_summary.json",
            ):
                if candidate.is_file():
                    output_json = candidate
                    break
        if output_json.is_file():
            try:
                agent_payload = json.loads(output_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                agent_payload = {}
        if not agent_payload:
            agent_payload = self._json_result_from_output(str(process.stdout or ""))
        applied = agent_payload.get("applied_ai_fixes") if isinstance(agent_payload, dict) else None
        if applied is None and isinstance(agent_payload, dict) and isinstance(agent_payload.get("applied"), list):
            applied = agent_payload
        fix_success = bool(applied.get("success")) if isinstance(applied, dict) else None

        actual = _source_snapshot(actual_root, case.ignored_names)
        actual_changed = _changed_files(before, actual)
        allowed = set(case.allowed_changed_files)
        unauthorized = sorted(set(actual_changed) - allowed)
        mismatched = _mismatched_files(actual, expected)
        if mismatched:
            (output_dir / f"{case.case_id}.diff").write_text(_render_diff(expected, actual, mismatched), encoding="utf-8")

        if process.returncode != 0:
            verdict, reason = "failed", f"agent exited with code {process.returncode}"
        elif self.entrypoint == "cli" and not output_json.is_file():
            verdict, reason = "failed", "standard CLI report was not found"
        elif fix_success is not True:
            verdict, reason = "failed", "AI fix was not applied successfully"
        elif unauthorized:
            verdict, reason = "failed", f"unauthorized files changed: {', '.join(unauthorized)}"
        elif set(actual_changed) != set(expected_changed):
            verdict, reason = "failed", "changed file set differs from expected patch"
        elif mismatched:
            verdict, reason = "failed", f"final source differs: {', '.join(mismatched)}"
        else:
            verdict, reason = "passed", "final source matches the expected patch"

        evaluation_payload = self._evaluation_payload(report_dir, agent_payload, actual_changed)

        if keep_workspace:
            kept = output_dir / "workspace"
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(actual_root, kept)
        temporary.cleanup()
        return self._finish(
            case, output_dir, started, verdict, reason,
            actual_changed, expected_changed, unauthorized, mismatched,
            process.returncode, fix_success, command, report_dir, self.entrypoint,
            evaluation_payload,
        )

    @staticmethod
    def _finish(
        case: RegressionCase,
        output_dir: Path,
        started: dt.datetime,
        verdict: str,
        reason: str,
        actual_changed: List[str],
        expected_changed: List[str],
        unauthorized: List[str],
        mismatched: List[str],
        return_code: Optional[int],
        fix_success: Optional[bool],
        command: List[str],
        report_dir: str = "",
        entrypoint: str = "cli",
        evaluation_payload: Optional[Dict[str, Any]] = None,
    ) -> RegressionResult:
        duration = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        evaluation = evaluate_case(
            case.case_id,
            result=evaluation_payload
            or {
                "applied_ai_fixes": {
                    "success": bool(fix_success),
                    "applied": [{"file": f} for f in actual_changed],
                }
            },
            allowed_files=case.allowed_changed_files,
            duration_ms=duration,
        )
        if report_dir:
            try:
                from services.evaluation import write_evaluation_artifact

                write_evaluation_artifact(report_dir, evaluation)
            except Exception:
                pass
        result = RegressionResult(
            case_id=case.case_id,
            verdict=verdict,
            reason=reason,
            duration_ms=duration,
            actual_changed_files=actual_changed,
            expected_changed_files=expected_changed,
            unauthorized_changed_files=unauthorized,
            mismatched_files=mismatched,
            agent_return_code=return_code,
            agent_fix_success=fix_success,
            output_dir=str(output_dir),
            command=command,
            entrypoint=entrypoint,
            report_dir=report_dir,
            evaluation=evaluation.to_dict(),
        )
        return result


def _default_output_dir() -> Path:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    return PROJECT_ROOT / "test" / "ai_regression" / "results" / stamp


def _load_cases(case_arg: str, suite_arg: str) -> List[RegressionCase]:
    if bool(case_arg) == bool(suite_arg):
        raise RegressionConfigError("provide exactly one of --case or --suite")
    root = _resolve_repo_path(case_arg) if case_arg else _resolve_repo_path(suite_arg)
    paths = [root] if root.is_file() else sorted(root.glob("*.json"))
    if not paths:
        raise RegressionConfigError("no case manifests found")
    return [load_case(path) for path in paths]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run full AI repair and compare final code with an expected patch")
    parser.add_argument("--case", default="", help="单个 Case JSON")
    parser.add_argument("--suite", default="", help="Case JSON 目录，按文件名排序串行执行")
    parser.add_argument("--repetitions", type=int, default=1, help="每个 Case 连续运行次数")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--result-dir", default="", help="精简回归结果目录")
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--entrypoint", choices=("cli", "daemon"), default="cli", help="执行入口；两种入口复用同一 Case 和源码断言")
    args = parser.parse_args(argv)
    cases = _load_cases(args.case, args.suite)
    if args.repetitions < 1:
        raise RegressionConfigError("--repetitions must be >= 1")
    root = Path(args.result_dir).expanduser().resolve() if args.result_dir else _default_output_dir()
    root.mkdir(parents=True, exist_ok=True)
    runner = AIRegressionRunner(entrypoint=args.entrypoint)
    results: List[RegressionResult] = []
    for case in cases:
        for attempt in range(1, args.repetitions + 1):
            result_dir = root / f"{case.case_id}_{attempt}"
            result = runner.run(case, result_dir, keep_workspace=args.keep_workspace)
            result.attempt = attempt
            result.output_dir = str(result_dir)
            (result_dir / "result.json").write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            results.append(result)
            print(f"[{result.verdict.upper():7}] {case.case_id} #{attempt}: {result.reason}")
            if args.stop_on_failure and result.verdict == "failed":
                break
        if args.stop_on_failure and results and results[-1].verdict == "failed":
            break
    counts = {name: sum(1 for item in results if item.verdict == name) for name in ("passed", "failed", "skipped")}
    summary = {
        "schema_version": 1,
        "case_count": len(cases),
        "attempt_count": len(results),
        **counts,
        "verdict": "passed" if counts["failed"] == 0 else "failed",
        "results": [
            {"case_id": item.case_id, "attempt": item.attempt, "entrypoint": item.entrypoint, "verdict": item.verdict, "reason": item.reason, "report_dir": item.report_dir, "result": f"{item.case_id}_{item.attempt}/result.json"}
            for item in results
        ],
    }
    (root / "batch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nAI Crash 修复回归: {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped")
    print(f"结果目录: {root}")
    return 0 if summary["verdict"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
