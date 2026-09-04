"""Declarative, explicit verification profiles.

Profiles are intentionally boring data: they describe commands a user or
repository has authorized. Discovery may suggest candidates, but this module
never turns a candidate into an executable action.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

CHECK_KINDS = frozenset({"compile", "replay", "target_compile", "static_check", "native_test",
                         "native_replay", "sanitizer", "stress", "fuzz", "integration",
                         "build", "test", "reproduce"})
PROVIDERS = frozenset({"local_command", "test_runner", "device_runner", "custom"})
VERIFICATION_LEVELS = frozenset({"L0", "L1", "L2", "L3", "L4"})
OVERRIDABLE_FIELDS = frozenset({"iterations", "timeout_sec"})


def _default_level(kind: str) -> str:
    if kind in {"compile", "target_compile", "build", "static_check"}:
        return "L2"
    if kind == "integration":
        return "L4"
    return "L3"


@dataclass(frozen=True)
class VerificationCheck:
    id: str
    kind: str
    command: List[str]
    working_directory: Optional[str] = None
    allowed_changed_files: List[str] = field(default_factory=list)
    iterations: int = 1
    expected_signature: Optional[str] = None
    requires_approval: bool = True
    timeout_sec: float = 300.0
    provider: str = "local_command"
    description: str = ""
    evidence_types: List[str] = field(default_factory=list)
    fixture: Optional[str] = None
    verification_level: str = "L3"
    pre_fix: bool = True
    post_fix: bool = True
    allowed_override_fields: List[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], index: int = 0) -> "VerificationCheck":
        if not isinstance(value, Mapping):
            raise ValueError("verification check must be an object")
        command = value.get("command")
        if isinstance(command, str):
            raise ValueError("verification check command must be an argv list")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("verification check command must be a non-empty string list")
        kind = str(value.get("kind") or value.get("mode") or "build").strip().lower()
        if kind == "auto":
            kind = "build"
        if kind not in CHECK_KINDS:
            raise ValueError("unsupported verification check kind: %s" % kind)
        raw_iterations = value.get("iterations", 1)
        if isinstance(raw_iterations, bool) or not isinstance(raw_iterations, int):
            raise ValueError("verification check iterations must be an integer")
        iterations = raw_iterations
        if iterations < 1 or iterations > 1000000:
            raise ValueError("verification check iterations out of range")
        files = value.get("allowed_changed_files") or []
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise ValueError("allowed_changed_files must be a string list")
        try:
            timeout = float(value.get("timeout_sec") or value.get("timeout") or 300.0)
        except (TypeError, ValueError):
            raise ValueError("verification check timeout must be numeric")
        if timeout <= 0 or timeout > 86400:
            raise ValueError("verification check timeout out of range")
        working_directory = value.get("working_directory")
        if working_directory is not None and (not isinstance(working_directory, str) or not working_directory.strip()):
            raise ValueError("verification check working_directory must be a non-empty string")
        provider = str(value.get("provider") or "local_command").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError("unsupported verification provider: %s" % provider)
        level = str(value.get("verification_level") or _default_level(kind)).strip().upper()
        if level not in VERIFICATION_LEVELS:
            raise ValueError("invalid verification level: %s" % level)
        evidence_types = value.get("evidence_types") or []
        if not isinstance(evidence_types, list) or not all(isinstance(item, str) and item for item in evidence_types):
            raise ValueError("evidence_types must be a string list")
        overrides = value.get("allowed_override_fields") or []
        if not isinstance(overrides, list) or not all(isinstance(item, str) for item in overrides):
            raise ValueError("allowed_override_fields must be a string list")
        unknown_overrides = set(overrides) - OVERRIDABLE_FIELDS
        if unknown_overrides:
            raise ValueError("unsupported override fields: %s" % ", ".join(sorted(unknown_overrides)))
        fixture = value.get("fixture")
        if fixture is not None and (not isinstance(fixture, str) or not fixture.strip()):
            raise ValueError("verification check fixture must be a non-empty string")
        description = value.get("description") or ""
        if not isinstance(description, str):
            raise ValueError("verification check description must be a string")
        return cls(str(value.get("id") or "check_%d" % (index + 1)), kind, list(command),
                   working_directory, list(files), iterations,
                   value.get("expected_signature"), bool(value.get("requires_approval", True)), timeout,
                   provider, description.strip(), list(evidence_types), fixture,
                   level, bool(value.get("pre_fix", True)), bool(value.get("post_fix", True)), list(overrides))

    def validate_paths(self, workspace: str, code_roots: Optional[Sequence[str]] = None) -> None:
        """Validate configured paths without requiring generated artifacts to exist."""
        root = Path(workspace).expanduser().resolve()
        allowed_roots = [root]
        for item in code_roots or ():
            candidate = Path(item).expanduser().resolve()
            if candidate == root or root in candidate.parents:
                allowed_roots.append(candidate)

        def inside(value: str) -> bool:
            target = Path(value).expanduser()
            target = target.resolve() if target.is_absolute() else (root / target).resolve()
            return any(target == allowed or allowed in target.parents for allowed in allowed_roots)

        if self.working_directory and not inside(self.working_directory):
            raise ValueError("verification working_directory is outside workspace")
        if self.fixture and not inside(self.fixture):
            raise ValueError("verification fixture is outside workspace")
        for pattern in self.allowed_changed_files:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError("allowed_changed_files must be workspace-relative patterns")


@dataclass(frozen=True)
class VerificationProfile:
    profile_id: str
    workspace: Optional[str]
    checks: List[VerificationCheck]
    frontend_available: bool = False
    runtime_available: bool = False
    pre_fix_baseline: bool = False
    environment: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Any) -> "VerificationProfile":
        if not isinstance(value, Mapping):
            raise ValueError("verification profile must be an object")
        if isinstance(value.get("verification"), Mapping) and "checks" not in value:
            value = {**dict(value), "checks": [dict(value["verification"])]}
        elif "checks" not in value and value.get("command"):
            value = {**dict(value), "checks": [dict(value)]}
        checks = value.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError("verification profile checks must be a non-empty list")
        parsed = [VerificationCheck.from_mapping(item, i) for i, item in enumerate(checks)]
        ids = [item.id for item in parsed]
        if len(set(ids)) != len(ids):
            raise ValueError("verification check ids must be unique")
        workspace = value.get("workspace")
        if workspace is not None and (not isinstance(workspace, str) or not workspace.strip()):
            raise ValueError("verification profile workspace must be a non-empty string")
        environment = value.get("environment") or value.get("environment_constraints") or {}
        if not isinstance(environment, Mapping):
            raise ValueError("verification profile environment must be an object")
        return cls(str(value.get("profile_id") or "profile"), workspace, parsed,
                   bool(value.get("frontend_available", False)), bool(value.get("runtime_available", False)),
                   bool(value.get("pre_fix_baseline", False)), dict(environment))

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": 1, "profile_id": self.profile_id, "workspace": self.workspace,
                "checks": [asdict(item) for item in self.checks],
                "frontend_available": self.frontend_available, "runtime_available": self.runtime_available,
                "pre_fix_baseline": self.pre_fix_baseline, "environment": dict(self.environment)}

    def check(self, check_id: str) -> VerificationCheck:
        for item in self.checks:
            if item.id == check_id:
                return item
        raise KeyError("verification check is not declared: %s" % check_id)


def normalize_verification_config(config: Any) -> Dict[str, Any]:
    """Return additive profile metadata while preserving legacy command configs."""
    if not isinstance(config, Mapping):
        return {"status": "not_configured", "profile": None}
    if "checks" in config or isinstance(config.get("verification"), Mapping):
        profile = VerificationProfile.from_mapping(config)
        return {"status": "configured", "profile": profile.to_dict()}
    if config.get("command"):
        profile = VerificationProfile.from_mapping(config)
        return {"status": "configured", "profile": profile.to_dict(), "legacy": dict(config)}
    return {"status": "not_configured", "profile": None}
