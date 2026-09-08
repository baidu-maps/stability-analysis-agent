"""Small, platform-neutral verification protocol.

The open-source core deliberately knows nothing about native artifacts, demo
applications, devices, or build pipelines.  Enterprise integrations implement
the two script interfaces and translate the generic context into their own
workflow.
"""
from __future__ import annotations

import importlib
import inspect
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Type, Union


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    INCONCLUSIVE = "inconclusive"


@dataclass
class ReproductionScenario:
    description: str = ""
    preconditions: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    expected_signature: str = ""
    trigger_probability: Optional[float] = None
    probability_unit: str = "per_attempt"
    probability_basis: str = ""
    confidence: str = "unknown"
    source: str = "user"
    confirmed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationContext:
    task_id: str
    workspace: str
    report_dir: str
    diagnosis: Dict[str, Any] = field(default_factory=dict)
    changes: Dict[str, Any] = field(default_factory=dict)
    reproduction: Optional[ReproductionScenario] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["reproduction"] = self.reproduction.to_dict() if self.reproduction else None
        return value


@dataclass
class VerificationResult:
    status: Union[VerificationStatus, str]
    summary: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    log_path: Optional[str] = None
    duration_ms: int = 0
    error: Optional[str] = None
    reproduced: Optional[bool] = None

    def __post_init__(self) -> None:
        raw = self.status.value if isinstance(self.status, VerificationStatus) else str(self.status)
        self.status = VerificationStatus(raw)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


class VerificationScript:
    """Base class for a configured verification script."""

    def run(self, context: VerificationContext) -> VerificationResult:
        raise NotImplementedError


class IssueVerificationScript(VerificationScript):
    """Verify reproduction before a fix and absence of it after a fix.

    Implementations may inspect ``context.metadata['verification_phase']`` to
    distinguish ``before_fix`` from ``after_fix``.  Keeping the method
    signature identical to ``VerificationScript`` also makes configured class
    loading straightforward.
    """


class RegressionVerificationScript(VerificationScript):
    """Verify that the change did not introduce regressions."""


def _load_script(value: Any, expected: Type[VerificationScript]) -> Optional[VerificationScript]:
    if value is None:
        return None
    if isinstance(value, expected):
        return value
    path = value.get("class") if isinstance(value, Mapping) else value
    if not isinstance(path, str) or ":" not in path:
        raise ValueError("verification script class must use module:Class format")
    module_name, class_name = path.split(":", 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    if not inspect.isclass(cls) or not issubclass(cls, expected):
        raise TypeError(f"{path} must inherit {expected.__name__}")
    return cls()


def _normalize_result(value: Any, elapsed_ms: int) -> VerificationResult:
    if isinstance(value, VerificationResult):
        value.duration_ms = value.duration_ms or elapsed_ms
        return value
    if isinstance(value, bool):
        return VerificationResult(
            VerificationStatus.PASSED if value else VerificationStatus.FAILED,
            duration_ms=elapsed_ms,
        )
    if isinstance(value, Mapping):
        payload = dict(value)
        payload.setdefault("duration_ms", elapsed_ms)
        return VerificationResult(**payload)
    raise TypeError("verification script must return VerificationResult, bool, or a mapping")


def _run_with_timeout(script: VerificationScript, context: VerificationContext, timeout_sec: float) -> VerificationResult:
    started = time.monotonic()
    result: List[Any] = []
    failure: List[BaseException] = []

    def invoke() -> None:
        try:
            result.append(script.run(context))
        except BaseException as exc:  # convert script failures into report data
            failure.append(exc)

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    thread.join(timeout=max(0.01, float(timeout_sec)))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if thread.is_alive():
        return VerificationResult(VerificationStatus.ERROR, "验证脚本超时", duration_ms=elapsed_ms,
                                   error=f"timeout after {timeout_sec}s")
    if failure:
        return VerificationResult(VerificationStatus.ERROR, "验证脚本执行异常", duration_ms=elapsed_ms,
                                   error=str(failure[0]))
    try:
        return _normalize_result(result[0], elapsed_ms)
    except Exception as exc:
        return VerificationResult(VerificationStatus.ERROR, "验证脚本返回值无效", duration_ms=elapsed_ms,
                                   error=str(exc))


class EffectivenessVerificationRunner:
    """Run configured issue/regression scripts and report runtime evidence only."""

    def __init__(self, *, issue: Any = None, regression: Any = None,
                 issue_timeout_sec: float = 1800, regression_timeout_sec: float = 3600,
                 stop_on_issue_failure: bool = True) -> None:
        self.issue = _load_script(issue, IssueVerificationScript)
        self.regression = _load_script(regression, RegressionVerificationScript)
        self.issue_timeout_sec = issue_timeout_sec
        self.regression_timeout_sec = regression_timeout_sec
        self.stop_on_issue_failure = bool(stop_on_issue_failure)

    @classmethod
    def from_mapping(cls, config: Optional[Mapping[str, Any]]) -> "EffectivenessVerificationRunner":
        value = config if isinstance(config, Mapping) else {}
        section = value.get("verification") if isinstance(value.get("verification"), Mapping) else value
        if isinstance(section, Mapping) and isinstance(section.get("effectiveness"), Mapping):
            section = section["effectiveness"]
        issue = section.get("issue") if isinstance(section, Mapping) else None
        regression = section.get("regression") if isinstance(section, Mapping) else None
        return cls(
            issue=issue,
            regression=regression,
            issue_timeout_sec=float((issue or {}).get("timeout_sec", 1800)) if isinstance(issue, Mapping) else 1800,
            regression_timeout_sec=float((regression or {}).get("timeout_sec", 3600)) if isinstance(regression, Mapping) else 3600,
            stop_on_issue_failure=bool(section.get("stop_on_issue_failure", True)) if isinstance(section, Mapping) else True,
        )

    def run(self, context: VerificationContext, *, run_issue: bool = True,
            run_regression: bool = True,
            issue_phase: str = "after_fix",
            issue_baseline: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "status": "verification_pending",
            "enabled": True,
            "reproduction": context.reproduction.to_dict() if context.reproduction else None,
            "issue_verification": {"configured": self.issue is not None, "status": "skipped"},
            "regression_verification": {"configured": self.regression is not None, "status": "skipped"},
        }
        if issue_baseline is not None:
            report["issue_baseline"] = dict(issue_baseline)
        issue_result: Optional[VerificationResult] = None
        if run_issue and self.issue is not None:
            context.metadata["verification_phase"] = issue_phase
            issue_result = _run_with_timeout(self.issue, context, self.issue_timeout_sec)
            normalized = issue_result.to_dict()
            if issue_result.reproduced is not None:
                if issue_phase == "before_fix":
                    normalized["status"] = "passed" if issue_result.reproduced else "inconclusive"
                else:
                    normalized["status"] = "failed" if issue_result.reproduced else "passed"
            report["issue_verification"] = {"configured": True, **normalized}

        should_run_regression = run_regression and self.regression is not None
        issue_observed_status = str(report["issue_verification"].get("status") or "")
        if issue_observed_status in {"failed", "error"} and self.stop_on_issue_failure:
            should_run_regression = False
        if should_run_regression:
            result = _run_with_timeout(self.regression, context, self.regression_timeout_sec)
            report["regression_verification"] = {"configured": True, **result.to_dict()}

        issue_status = report["issue_verification"]["status"]
        regression_status = report["regression_verification"]["status"]
        if issue_status == "failed" or regression_status == "failed":
            report["status"] = "verification_failed"
        elif issue_status == "error" or regression_status == "error":
            report["status"] = "verification_error"
        elif issue_status == "inconclusive":
            report["status"] = "inconclusive"
        elif (
            issue_status == "passed"
            and issue_baseline is not None
            and issue_baseline.get("reproduced") is not True
        ):
            # A successful pre-fix invocation is not itself evidence that the
            # original issue was reproduced.  Only an explicit reproduced=true
            # baseline can support a post-fix effectiveness conclusion.
            report["status"] = "inconclusive"
        elif issue_status == "passed" and regression_status in {"passed", "skipped"}:
            report["status"] = "verified" if regression_status == "passed" else "issue_verified"
        elif regression_status == "passed" and issue_status == "skipped":
            report["status"] = "regression_verified"
        elif issue_status == "skipped" and regression_status == "skipped":
            report["status"] = "skipped"
        return report

    def run_issue_baseline(self, context: VerificationContext) -> Optional[Dict[str, Any]]:
        """Run the configured issue script once before applying a patch."""
        if self.issue is None:
            return None
        report = self.run(context, run_regression=False, issue_phase="before_fix")
        return dict(report.get("issue_verification") or {})


# Short compatibility alias for integrations already implementing the generic
# interfaces. New code should use the explicit effectiveness name.
VerificationRunner = EffectivenessVerificationRunner


def write_verification_report(report: Mapping[str, Any], report_dir: Union[str, Path]) -> Path:
    target = Path(report_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    path = target / "09_verification.json"
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
