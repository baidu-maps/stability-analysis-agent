"""Optional post-fix verification providers.

Verification is deliberately decoupled from diagnosis and fixing. A provider
may be unavailable when a project has no local build entry point; that is a
valid result and must not be reported as a failed diagnosis.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import hashlib
import uuid
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence
import re


@dataclass(frozen=True)
class VerificationRequest:
    workspace: str
    changed_files: List[str] = field(default_factory=list)
    target: Optional[str] = None
    mode: str = "auto"  # syntax/build/test/reproduce/auto
    timeout_sec: float = 300.0
    report_dir: Optional[str] = None
    check_id: Optional[str] = None
    purpose: Optional[str] = None
    plan_fingerprint: Optional[str] = None
    fixture: Optional[str] = None
    verification_level: Optional[str] = None
    iterations: int = 1
    expected_signature: Optional[str] = None
    working_directory: Optional[str] = None


@dataclass(frozen=True)
class VerificationCandidate:
    """A discovered command; discovery never executes it."""

    provider: str
    mode: str
    command: List[str]
    reason: str
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "mode": self.mode,
            "command": list(self.command),
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class VerificationCapabilities:
    provider: str
    modes: List[str]
    available: bool
    reason: Optional[str] = None
    requires_network: bool = False
    requires_approval: bool = False
    candidates: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "modes": list(self.modes),
            "available": self.available,
            "reason": self.reason,
            "requires_network": self.requires_network,
            "requires_approval": self.requires_approval,
            "candidates": list(self.candidates),
        }


@dataclass
class VerificationResult:
    status: str  # passed/failed/pending/unavailable/skipped/timeout
    provider: str
    mode: str
    checks: List[Dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    output: str = ""
    error: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    command_fingerprint: Optional[str] = None
    approval: Optional[Dict[str, Any]] = None
    command: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    changed_files: List[str] = field(default_factory=list)
    reproduced: Optional[bool] = None
    failure_class: Optional[str] = None
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    verification_status: Optional[str] = None
    check_id: Optional[str] = None
    purpose: Optional[str] = None
    plan_fingerprint: Optional[str] = None
    fixture: Optional[str] = None
    verification_level: Optional[str] = None
    iterations: Optional[int] = None
    crash_count: Optional[int] = None
    crash_rate: Optional[float] = None
    stack_signature_match: Optional[bool] = None
    environment_fingerprint: Optional[str] = None
    workspace_revision: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "status": self.status,
            "provider": self.provider,
            "mode": self.mode,
            "checks": list(self.checks),
            "duration_ms": self.duration_ms,
            "output": self.output,
        }
        if self.error:
            result["error"] = self.error
        if self.capabilities is not None:
            result["capabilities"] = self.capabilities
        if self.command_fingerprint:
            result["command_fingerprint"] = self.command_fingerprint
        if self.approval is not None:
            result["approval"] = self.approval
        result.update({
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "changed_files": list(self.changed_files),
            "reproduced": self.reproduced,
            "failure_class": self.failure_class,
            "diagnostics": list(self.diagnostics),
            "verification_status": self.verification_status,
            "check_id": self.check_id, "purpose": self.purpose,
            "plan_fingerprint": self.plan_fingerprint, "fixture": self.fixture,
            "verification_level": self.verification_level, "iterations": self.iterations,
            "crash_count": self.crash_count, "crash_rate": self.crash_rate,
            "stack_signature_match": self.stack_signature_match,
            "environment_fingerprint": self.environment_fingerprint,
            "workspace_revision": self.workspace_revision,
        })
        return result


def verification_observation(result: Any, *, changed_files: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Normalize a provider result into the observation contract used by ContextEngine."""
    payload = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
    status = str(payload.get("status") or "unknown")
    output = str(payload.get("output") or "")
    return {
        "kind": "verification",
        "source": payload.get("provider") or "verification",
        "status": status,
        "summary": (payload.get("error") or output[-1000:]) or status,
        "provider": payload.get("provider"),
        "command": payload.get("command") or [],
        "exit_code": payload.get("exit_code"),
        "stdout": payload.get("stdout") or output,
        "stderr": payload.get("stderr") or "",
        "changed_files": list(changed_files or payload.get("changed_files") or []),
        "reproduced": payload.get("reproduced"),
        "failure_class": payload.get("failure_class"),
        "diagnostics": list(payload.get("diagnostics") or parse_verification_diagnostics(payload.get("stderr") or output)),
        "actionable": status in {"failed", "timeout", "pending"},
    }


def parse_verification_diagnostics(output: str) -> List[Dict[str, Any]]:
    """Parse common compiler/test-runner diagnostics without requiring a toolchain."""
    rows: List[Dict[str, Any]] = []
    pattern = re.compile(r"(?m)^(?P<file>[^:\n]+):(?P<line>\d+)(?::(?P<column>\d+))?:\s*(?P<level>fatal error|error|warning|note)\s*:\s*(?P<message>.+)$", re.I)
    for match in pattern.finditer(str(output or "")):
        rows.append({"file": match.group("file"), "line": int(match.group("line")),
                     "column": int(match.group("column") or 0),
                     "severity": match.group("level").lower(), "message": match.group("message").strip()})
    return rows[:100]


def make_approval(*, run_id: str, tool_call_id: str, command_fingerprint: str,
                  scope: str = "single_command", expires_at: Optional[float] = None) -> Dict[str, Any]:
    """Create the persisted approval entity used by daemon verification."""
    return {"approval_id": f"approval_{uuid.uuid4().hex[:16]}", "run_id": run_id,
            "tool_call_id": tool_call_id, "command_fingerprint": command_fingerprint,
            "scope": scope, "created_at": time.time(), "expires_at": expires_at,
            "status": "required", "granted_by": None, "used_at": None}


def validate_approval(approval: Mapping[str, Any], *, fingerprint: str,
                      run_id: Optional[str] = None, tool_call_id: Optional[str] = None,
                      scope: str = "single_command", now: Optional[float] = None) -> Dict[str, Any]:
    """Return an approval copy with an explicit valid/invalid/expired state."""
    result = dict(approval) if isinstance(approval, Mapping) else {}
    status = str(result.get("status") or "")
    if status != "granted":
        result["validation_error"] = "approval_not_granted"
        return result
    expires = result.get("expires_at")
    if expires and float(expires) <= float(now if now is not None else time.time()):
        result.update(status="expired", validation_error="approval_expired")
        return result
    bindings = (
        ("command_fingerprint", str(fingerprint or "")),
        ("run_id", str(run_id)) if run_id is not None else None,
        ("tool_call_id", str(tool_call_id)) if tool_call_id is not None else None,
        ("scope", str(scope)),
    )
    for binding in bindings:
        if binding is None:
            continue
        key, expected = binding
        if str(result.get(key) or "") != expected:
            result.update(status="invalid", validation_error=f"{key}_mismatch")
            return result
    result.pop("validation_error", None)
    return result


def approval_is_valid(approval: Mapping[str, Any], *, fingerprint: str,
                      run_id: Optional[str] = None, tool_call_id: Optional[str] = None,
                      scope: str = "single_command", now: Optional[float] = None) -> bool:
    return validate_approval(
        approval, fingerprint=fingerprint, run_id=run_id,
        tool_call_id=tool_call_id, scope=scope, now=now,
    ).get("status") == "granted"


def consume_approval(approval: Mapping[str, Any], *, fingerprint: str,
                     run_id: str, tool_call_id: str,
                     scope: str = "single_command", now: Optional[float] = None) -> Dict[str, Any]:
    """Atomically validate the one-shot approval and return its consumed copy."""
    if not approval_is_valid(approval, fingerprint=fingerprint, run_id=run_id,
                             tool_call_id=tool_call_id, scope=scope, now=now):
        raise PermissionError("approval is missing, mismatched, expired, or already consumed")
    result = dict(approval)
    result["status"] = "consumed"
    result["used_at"] = float(now if now is not None else time.time())
    return result


class VerificationProvider(Protocol):
    name: str

    def capabilities(self, request: VerificationRequest) -> VerificationCapabilities:
        ...

    def verify(self, request: VerificationRequest) -> VerificationResult:
        ...

    def discover(self, request: VerificationRequest) -> List[VerificationCandidate]: ...
    def validate(self, request: VerificationRequest) -> VerificationResult: ...
    def execute(self, request: VerificationRequest) -> VerificationResult: ...
    def summarize(self, result: VerificationResult) -> Dict[str, Any]: ...


class NoopVerificationProvider:
    name = "none"

    def capabilities(self, request: VerificationRequest) -> VerificationCapabilities:
        candidates = [item.to_dict() for item in discover_verification_candidates(request.workspace)]
        return VerificationCapabilities(
            provider=self.name,
            modes=[],
            available=False,
            reason="未配置验证 provider 或本地构建入口",
            candidates=candidates,
        )

    def verify(self, request: VerificationRequest) -> VerificationResult:
        capabilities = self.capabilities(request)
        return VerificationResult(
            status="unavailable",
            provider=self.name,
            mode=request.mode,
            error=capabilities.reason,
            capabilities=capabilities.to_dict(),
            verification_status="not_configured",
        )

    def discover(self, request): return discover_verification_candidates(request.workspace)
    def validate(self, request): return self.verify(request)
    def execute(self, request): return self.verify(request)
    def summarize(self, result): return result.to_dict()


class CommandVerificationProvider:
    """Run an explicitly configured argv without invoking a shell."""

    name = "local_command"

    def __init__(self, command: Sequence[str], *, modes: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None, policy: Any = None, approved: bool = False):
        self.command = [str(item) for item in command if str(item)]
        self.modes = list(modes or ("auto", "syntax", "build", "test", "reproduce"))
        self.env = {str(key): str(value) for key, value in (env or {}).items()}
        self.policy = policy
        self.approved = bool(approved)

    def capabilities(self, request: VerificationRequest) -> VerificationCapabilities:
        candidates = [item.to_dict() for item in discover_verification_candidates(request.workspace)]
        return VerificationCapabilities(
            provider=self.name,
            modes=list(self.modes),
            available=bool(self.command) and Path(request.workspace).is_dir(),
            reason=None if self.command and Path(request.workspace).is_dir() else "验证命令为空或 workspace 不存在",
            candidates=candidates,
        )

    def _argv(self, request: VerificationRequest) -> List[str]:
        workspace = str(Path(request.workspace).expanduser().resolve())
        replacements = {
            "{workspace}": workspace,
            "{target}": request.target or "",
            "{changed_files}": os.linesep.join(request.changed_files),
        }
        return [replacements.get(item, item) for item in self.command]

    def discover(self, request):
        return discover_verification_candidates(request.workspace)

    def validate(self, request):
        capabilities = self.capabilities(request)
        if not capabilities.available:
            return VerificationResult("unavailable", self.name, request.mode, error=capabilities.reason, capabilities=capabilities.to_dict())
        argv = self._argv(request)
        fingerprint = hashlib.sha256("\0".join(argv).encode()).hexdigest()[:16]
        if self.policy is not None:
            decision = self.policy.check_command(argv, workspace=request.workspace, approved=self.approved)
            if decision.decision == "approval_required":
                return VerificationResult("pending", self.name, request.mode, error=decision.reason, capabilities=capabilities.to_dict(),
                                          command_fingerprint=hashlib.sha256("\0".join(argv).encode()).hexdigest()[:16],
                                          approval={"required": True, "decision": decision.to_dict()})
            if not decision.allowed:
                return VerificationResult("unavailable", self.name, request.mode, error=decision.reason, capabilities=capabilities.to_dict())
        if self.approved:
            return VerificationResult("passed", self.name, request.mode, capabilities=capabilities.to_dict(),
                                      command_fingerprint=fingerprint,
                                      approval={"required": bool(self.policy), "granted": self.approved})
        return VerificationResult("pending", self.name, request.mode, capabilities=capabilities.to_dict(),
                                  command_fingerprint=hashlib.sha256("\0".join(argv).encode()).hexdigest()[:16],
                                  approval={"required": True})

    def execute(self, request):
        return self.verify(request)

    def summarize(self, result):
        return result.to_dict()

    def verify(self, request: VerificationRequest) -> VerificationResult:
        capabilities = self.capabilities(request)
        if request.mode not in self.modes and request.mode != "auto":
            return VerificationResult(
                status="unavailable", provider=self.name, mode=request.mode,
                error=f"provider 不支持验证模式: {request.mode}",
                capabilities=capabilities.to_dict(),
            )
        if not capabilities.available:
            return VerificationResult(
                status="unavailable", provider=self.name, mode=request.mode,
                error=capabilities.reason, capabilities=capabilities.to_dict(),
            )
        started = time.perf_counter()
        argv = self._argv(request)
        fingerprint = hashlib.sha256("\0".join(argv).encode()).hexdigest()[:16]
        if not self.approved:
            return VerificationResult(
                status="pending", provider=self.name, mode=request.mode,
                error="验证命令需要显式 approval", capabilities=capabilities.to_dict(),
                command_fingerprint=fingerprint,
                approval={"required": True, "granted": False},
            )
        if self.policy is not None:
            decision = self.policy.check_command(argv, workspace=request.workspace, approved=self.approved)
            if not decision.allowed:
                return VerificationResult(status="pending" if decision.decision == "approval_required" else "unavailable", provider=self.name, mode=request.mode,
                                           error=decision.reason, capabilities=capabilities.to_dict(),
                                           command_fingerprint=fingerprint,
                                           approval={"required": True, "granted": self.approved, "decision": decision.to_dict()})
        env = os.environ.copy()
        env.update(self.env)
        try:
            workspace = Path(request.workspace).expanduser().resolve()
            working_directory = (workspace / request.working_directory).resolve() if request.working_directory else workspace
            outputs, errors, returncodes = [], [], []
            crash_count = 0
            for _ in range(max(1, int(request.iterations or 1))):
                proc = subprocess.run(argv, cwd=str(working_directory), env=env, capture_output=True,
                                      text=True, check=False, timeout=max(1.0, float(request.timeout_sec)))
                outputs.append(proc.stdout or "")
                errors.append(proc.stderr or "")
                returncodes.append(proc.returncode)
                combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
                if request.expected_signature:
                    crash_count += int(request.expected_signature in combined)
                elif request.mode in {"reproduce", "replay", "native_replay"}:
                    crash_count += int(proc.returncode != 0)
            stdout, stderr = "".join(outputs), "".join(errors)
            output = (stdout + ("\n" + stderr if stderr else "")).strip()
            returncode = next((code for code in returncodes if code != 0), 0)
            return VerificationResult(
                status="passed" if returncode == 0 else "failed",
                provider=self.name,
                mode=request.mode,
                checks=[{"name": "command", "status": "passed" if returncode == 0 else "failed", "returncode": returncode, "argv": argv}],
                duration_ms=int(round((time.perf_counter() - started) * 1000)),
                output=output[-20000:],
                error=None if returncode == 0 else f"验证命令退出码: {returncode}",
                capabilities=capabilities.to_dict(),
                command_fingerprint=fingerprint,
                approval={"required": bool(self.policy), "granted": self.approved},
                command=argv,
                exit_code=returncode,
                stdout=stdout,
                stderr=stderr,
                changed_files=list(request.changed_files),
                reproduced=crash_count > 0,
                failure_class=(None if returncode == 0 else ("test_failure" if request.mode == "test" else "compile_error" if request.mode in {"build", "compile", "target_compile"} else "reproduce_failure")),
                check_id=request.check_id, purpose=request.purpose, plan_fingerprint=request.plan_fingerprint,
                fixture=request.fixture, verification_level=request.verification_level,
                iterations=max(1, int(request.iterations or 1)), crash_count=crash_count,
                crash_rate=crash_count / float(max(1, int(request.iterations or 1))),
                stack_signature_match=bool(request.expected_signature and crash_count),
            )
        except subprocess.TimeoutExpired as exc:
            return VerificationResult(
                status="timeout", provider=self.name, mode=request.mode,
                duration_ms=int(round((time.perf_counter() - started) * 1000)),
                output=str(exc), error="验证命令超时", capabilities=capabilities.to_dict(),
                command_fingerprint=fingerprint,
                command=argv, changed_files=list(request.changed_files),
                failure_class="timeout",
            )
        except OSError as exc:
            return VerificationResult(
                status="unavailable", provider=self.name, mode=request.mode,
                duration_ms=int(round((time.perf_counter() - started) * 1000)),
                error=f"无法启动验证命令: {exc}", capabilities=capabilities.to_dict(),
                command_fingerprint=fingerprint,
            )


def create_verification_provider(config: Any = None, *, approved: bool = False) -> VerificationProvider:
    """Create a provider from metadata; default remains intentionally no-op."""
    if not isinstance(config, dict):
        return NoopVerificationProvider()
    if isinstance(config.get("checks"), list):
        from services.verification_profile import VerificationProfile
        try:
            profile = VerificationProfile.from_mapping(config)
            # Direct provider construction keeps legacy first-check behavior;
            # RuntimeAction enforces explicit model selection before reaching it.
            requested_id = str(config.get("check_id") or profile.checks[0].id).strip()
            check = profile.check(requested_id)
        except (KeyError, ValueError):
            return NoopVerificationProvider()
        config = {**config, "command": list(check.command), "modes": [check.kind, "auto"],
                  "workspace": config.get("workspace") or profile.workspace,
                  "profile_id": profile.profile_id, "check_id": check.id,
                  "provider": check.provider}
    provider_name = str(config.get("provider") or "local_command").strip().lower()
    if provider_name not in {"local_command", "test_runner", "device_runner", "custom"}:
        return NoopVerificationProvider()
    command = config.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        return NoopVerificationProvider()
    modes = config.get("modes")
    env = config.get("env")
    policy = None
    policy_config = config.get("policy")
    if isinstance(policy_config, dict):
        from services.policy import PolicyEngine
        policy = PolicyEngine(
            allowed_commands=policy_config.get("allowed_commands"),
            allowed_roots=policy_config.get("allowed_roots"),
            allow_network=bool(policy_config.get("allow_network", False)),
            allow_destructive=bool(policy_config.get("allow_destructive", False)),
        )
    provider = CommandVerificationProvider(command, modes=modes if isinstance(modes, list) else None,
                                           env=env if isinstance(env, dict) else None,
                                           policy=policy, approved=bool(approved))
    provider.name = provider_name
    return provider


def discover_verification_candidates(workspace: str) -> List[VerificationCandidate]:
    """Discover likely entry points without running commands or reading deeply."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return []
    candidates: List[VerificationCandidate] = []
    if (root / "CMakeLists.txt").is_file():
        candidates.append(VerificationCandidate(
            "local_command", "build", ["cmake", "--build", "{workspace}/build"],
            "发现 CMakeLists.txt；需要先配置 build 目录", 0.85,
        ))
    for gradle in (root / "gradlew", root / "gradlew.bat"):
        if gradle.is_file():
            command = [str(gradle.name), "test"]
            candidates.append(VerificationCandidate(
                "local_command", "test", command,
                "发现 Gradle wrapper；默认候选任务为 test", 0.9,
            ))
            break
    if (root / "Package.swift").is_file():
        candidates.append(VerificationCandidate(
            "local_command", "test", ["swift", "test"],
            "发现 Swift Package manifest", 0.85,
        ))
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() or (root / "tox.ini").is_file():
        candidates.append(VerificationCandidate(
            "local_command", "test", ["python3", "-m", "pytest"],
            "发现 Python 测试配置；默认候选为 pytest", 0.7,
        ))
    xcode_projects = list(root.glob("*.xcodeproj")) + list(root.glob("*.xcworkspace"))
    if xcode_projects:
        candidates.append(VerificationCandidate(
            "local_command", "build", ["xcodebuild", "-project", xcode_projects[0].name, "-scheme", "{target}", "build"],
            "发现 Xcode 工程；scheme 需要显式确认", 0.65,
        ))
        candidates.append(VerificationCandidate(
            "local_command", "test",
            ["xcodebuild", "-project", xcode_projects[0].name, "-scheme", "{target}", "test"],
            "发现 Xcode 工程；默认 test scheme 需确认", 0.6,
        ))
    for build_script in ("build.sh", "mk/cmake/build.sh", "scripts/build.sh"):
        script_path = root / build_script
        if script_path.is_file():
            candidates.append(VerificationCandidate(
                "local_command", "build", ["bash", str(script_path)],
                f"发现构建脚本 {build_script}", 0.88,
            ))
            break
    if (root / "Makefile").is_file():
        candidates.append(VerificationCandidate(
            "local_command", "build", ["make", "-C", "{workspace}"],
            "发现 Makefile", 0.8,
        ))
        candidates.append(VerificationCandidate(
            "local_command", "test", ["make", "-C", "{workspace}", "test"],
            "发现 Makefile；尝试 make test", 0.75,
        ))
    if (root / "meson.build").is_file():
        candidates.append(VerificationCandidate(
            "local_command", "build", ["meson", "compile", "-C", "{workspace}/build"],
            "发现 meson.build；需已配置 build 目录", 0.82,
        ))
        candidates.append(VerificationCandidate(
            "local_command", "test", ["meson", "test", "-C", "{workspace}/build"],
            "发现 meson.build；默认 meson test", 0.78,
        ))
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            import json
            meta = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = meta.get("scripts") if isinstance(meta, dict) else {}
            if isinstance(scripts, dict) and scripts.get("test"):
                candidates.append(VerificationCandidate(
                    "local_command", "test", ["npm", "test"],
                    "发现 package.json scripts.test", 0.72,
                ))
        except (OSError, ValueError):
            pass
    return candidates


def load_verification_presets(path: Optional[str] = None) -> Dict[str, Any]:
    """Load named verification presets from JSON config."""
    import json
    candidates = [
        Path(path).expanduser() if path else None,
        Path(__file__).resolve().parents[1] / "configs" / "verification_presets.example.json",
    ]
    for item in candidates:
        if item is None or not item.is_file():
            continue
        try:
            value = json.loads(item.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def merge_preset_candidates(
    workspace: str,
    presets: Optional[Dict[str, Any]] = None,
) -> List[VerificationCandidate]:
    """Combine workspace discovery with configured presets."""
    discovered = discover_verification_candidates(workspace)
    preset_blob = presets if isinstance(presets, dict) else load_verification_presets()
    preset_items = preset_blob.get("presets") if isinstance(preset_blob.get("presets"), dict) else {}
    for name, item in preset_items.items():
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, list) or not command:
            continue
        discovered.append(VerificationCandidate(
            str(item.get("provider") or "preset"),
            str(item.get("mode") or "auto"),
            [str(x) for x in command],
            str(item.get("reason") or f"preset:{name}"),
            float(item.get("confidence") or 0.6),
        ))
    return discovered


def select_auto_verification_candidate(
    candidates: List[VerificationCandidate],
) -> Optional[VerificationCandidate]:
    if not candidates:
        return None
    builds = [item for item in candidates if str(item.mode).strip().lower() == "build"]
    tests = [item for item in candidates if str(item.mode).strip().lower() == "test"]
    pool = builds or tests or list(candidates)
    return sorted(pool, key=lambda item: (-float(item.confidence or 0.0), str(item.mode)))[0]


def build_auto_verification_config(
    workspace: str,
    code_roots: Optional[List[str]] = None,
    problem: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    value = problem if isinstance(problem, dict) else {}
    if value.get("skip_verify") is True:
        return None
    if value.get("runtime_available") is False and not value.get("verification_profile"):
        return None
    roots = [str(item) for item in (code_roots or []) if str(item)]
    root = str(workspace or (roots[0] if roots else "")).strip()
    if not root:
        return None
    discovered = discover_verification_candidates(root)
    candidate = select_auto_verification_candidate(discovered)
    if candidate is None:
        return None
    tool = "run_build" if candidate.mode == "build" else "run_tests"
    if candidate.mode not in {"build", "test"}:
        tool = "run_build"
    return {
        "command": list(candidate.command),
        "mode": candidate.mode,
        "provider": candidate.provider,
        "tool": tool,
        "auto_selected": True,
        "auto_selected_reason": candidate.reason,
        "workspace": root,
    }


def build_verification_config_with_reproduce_priority(
    verification_config: Optional[Dict[str, Any]],
    *,
    workspace: str,
    code_roots: Optional[List[str]] = None,
    problem: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize explicit verification config without executing discovered candidates."""
    config = dict(verification_config or {})
    value = problem if isinstance(problem, dict) else {}
    if config.get("skip_verify") or value.get("skip_verify"):
        config["skip_verify"] = True
        return config
    # Repository discovery is advisory only. No profile means static L0/L1.
    runtime_available = bool(value.get("runtime_available"))
    profile = str(value.get("verification_profile") or config.get("verification_profile") or "").strip()
    wants_reproduce = runtime_available or "reproduce" in profile.lower()
    if wants_reproduce and not config.get("reproduce_priority_applied"):
        reproduce_cmd = config.get("reproduce_command")
        if isinstance(reproduce_cmd, list) and reproduce_cmd:
            config.setdefault("checks", [])
            checks = config["checks"] if isinstance(config["checks"], list) else []
            config["checks"] = [{"tool": "reproduce_crash", "command": reproduce_cmd}] + checks
        elif config.get("command"):
            config.setdefault("checks", [])
            checks = config["checks"] if isinstance(config["checks"], list) else []
            primary = {
                "tool": str(config.get("tool") or "run_build"),
                "command": list(config.get("command") or []),
                "mode": str(config.get("mode") or "auto"),
            }
            config["checks"] = [{"tool": "reproduce_crash", "mode": "reproduce"}] + checks + [primary]
            config.pop("command", None)
            config.pop("tool", None)
        config["reproduce_priority_applied"] = True
    return config
