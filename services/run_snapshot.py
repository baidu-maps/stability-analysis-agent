"""Unified harness run snapshot schema for CLI, daemon and run_store."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class HarnessRunSnapshot:
    run_id: str
    transport_status: str
    created_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    output_format: str = "markdown"
    report_dir: Optional[str] = None
    workspace_dir: Optional[str] = None
    original_code_roots: List[str] = field(default_factory=list)
    isolated_code_roots: List[str] = field(default_factory=list)
    workspace_manifest: Any = None
    patch_path: Optional[str] = None
    last_progress: Optional[str] = None
    last_progress_percent: Optional[float] = None
    completion_reason: Optional[str] = None
    runtime_state: Optional[Dict[str, Any]] = None
    runtime_trace: Optional[Dict[str, Any]] = None
    approval: Optional[Dict[str, Any]] = None
    pending_verification: Optional[Dict[str, Any]] = None
    pending_tool_approval: Optional[Dict[str, Any]] = None
    pending_changed_files: List[str] = field(default_factory=list)
    request: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = field(default_factory=list)

    def unified_timeline(self) -> List[Dict[str, Any]]:
        """Merge harness trace events and transport SSE events for auditing."""
        merged: List[Dict[str, Any]] = []
        trace = self.runtime_trace if isinstance(self.runtime_trace, dict) else {}
        for item in trace.get("events") or []:
            if not isinstance(item, dict):
                continue
            merged.append({**item, "source": "harness"})
        for idx, item in enumerate(self.events or []):
            if not isinstance(item, dict):
                continue
            merged.append({
                "source": "transport",
                "seq": item.get("seq", idx),
                "event": item.get("type") or item.get("event"),
                "payload": item.get("data") if "data" in item else item,
            })
        merged.sort(key=lambda x: (x.get("seq") if isinstance(x.get("seq"), int) else 10**9, str(x.get("event") or "")))
        return merged

    def timeline_summary(self) -> Dict[str, Any]:
        timeline = self.unified_timeline()
        return {
            "total": len(timeline),
            "harness_events": sum(1 for x in timeline if x.get("source") == "harness"),
            "transport_events": sum(1 for x in timeline if x.get("source") == "transport"),
        }

    def to_dict(self) -> Dict[str, Any]:
        summary = self.timeline_summary()
        return {
            "run_id": self.run_id,
            "transport_status": self.transport_status,
            "status": self.transport_status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
            "output_format": self.output_format,
            "report_dir": self.report_dir,
            "workspace_dir": self.workspace_dir,
            "original_code_roots": list(self.original_code_roots),
            "isolated_code_roots": list(self.isolated_code_roots),
            "workspace_manifest": self.workspace_manifest,
            "patch_path": self.patch_path,
            "last_progress": self.last_progress,
            "last_progress_percent": self.last_progress_percent,
            "completion_reason": self.completion_reason,
            "runtime_state": self.runtime_state,
            "runtime_trace": self.runtime_trace,
            "approval": self.approval,
            "pending_verification": self.pending_verification,
            "pending_tool_approval": self.pending_tool_approval,
            "pending_changed_files": list(self.pending_changed_files),
            "request": self.request,
            "result": self.result,
            "events": list(self.events),
            "timeline_summary": summary,
        }

    @classmethod
    def from_daemon_run(cls, run: Any) -> "HarnessRunSnapshot":
        request = getattr(run, "request", None)
        result = getattr(run, "result", None)
        events = getattr(run, "event_log", []) or []
        return cls(
            run_id=str(getattr(run, "run_id", "")),
            transport_status=str(getattr(run, "transport_status", "unknown")),
            created_at=getattr(run, "created_at", None),
            started_at=getattr(run, "started_at", None),
            finished_at=getattr(run, "finished_at", None),
            exit_code=getattr(run, "exit_code", None),
            error=getattr(run, "error", None),
            output_format=str(getattr(run, "output_format", "markdown")),
            report_dir=getattr(run, "report_dir", None),
            workspace_dir=getattr(run, "workspace_dir", None),
            original_code_roots=list(getattr(run, "original_code_roots", []) or []),
            isolated_code_roots=list(getattr(run, "isolated_code_roots", []) or []),
            workspace_manifest=getattr(run, "workspace_manifest", None),
            patch_path=getattr(run, "patch_path", None),
            last_progress=getattr(run, "last_progress", None),
            last_progress_percent=getattr(run, "last_progress_percent", None),
            completion_reason=getattr(run, "completion_reason", None),
            runtime_state=getattr(run, "runtime_state", None),
            runtime_trace=getattr(run, "runtime_trace", None),
            approval=getattr(run, "approval", None),
            pending_verification=getattr(run, "pending_verification", None),
            pending_tool_approval=getattr(run, "pending_tool_approval", None),
            pending_changed_files=list(getattr(run, "pending_changed_files", []) or []),
            request=request.to_dict() if request is not None and hasattr(request, "to_dict") else None,
            result=result.to_dict() if result is not None and hasattr(result, "to_dict") else None,
            events=[event.to_dict() for event in events if hasattr(event, "to_dict")][-512:],
        )

    @classmethod
    def from_report_dir(cls, report_dir: Path) -> "HarnessRunSnapshot":
        root = Path(report_dir).expanduser().resolve()

        def _load(name: str) -> Dict[str, Any]:
            path = root / name
            if not path.is_file():
                return {}
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                return {}

        summary = _load("00_run_summary.json")
        request = _load("00_run_request.json")
        trace = _load("00_runtime_trace.json")
        pending_tool = _load("09_pending_tool_approval.json")
        verification = _load("09_verification.json")
        status = str(summary.get("status") or summary.get("workflow", {}).get("status") or "unknown")
        return cls(
            run_id=str(summary.get("run_id") or request.get("run_id") or root.name),
            transport_status=status,
            completion_reason=summary.get("completion_reason"),
            report_dir=str(root),
            runtime_state=summary.get("runtime_state") if isinstance(summary.get("runtime_state"), dict) else None,
            runtime_trace=trace or summary.get("trace"),
            pending_verification=verification if verification.get("status") == "pending" else None,
            pending_tool_approval=pending_tool or None,
            request=request or None,
        )

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "HarnessRunSnapshot":
        if not isinstance(payload, dict):
            raise ValueError("snapshot must be an object")
        fields = cls.__dataclass_fields__
        kwargs = {key: payload.get(key) for key in fields if key in payload}
        if "transport_status" not in kwargs:
            kwargs["transport_status"] = str(payload.get("status") or "unknown")
        for list_field in ("original_code_roots", "isolated_code_roots", "pending_changed_files", "events"):
            if list_field not in kwargs:
                kwargs[list_field] = list(payload.get(list_field) or [])
        return cls(**kwargs)  # type: ignore[arg-type]

    def apply_to_daemon_run(self, run: Any) -> None:
        transport = self.transport_status or "queued"
        run.transport_status = transport
        runtime_state = dict(self.runtime_state or {})
        if runtime_state:
            run.runtime_state = runtime_state
        for name, value in (
            ("completion_reason", self.completion_reason),
            ("report_dir", self.report_dir),
            ("workspace_dir", self.workspace_dir),
            ("runtime_trace", self.runtime_trace),
            ("approval", self.approval),
            ("pending_verification", self.pending_verification),
            ("pending_tool_approval", self.pending_tool_approval),
        ):
            if value is not None:
                setattr(run, name, value)
        run.original_code_roots = list(self.original_code_roots)
        run.isolated_code_roots = list(self.isolated_code_roots)
        run.pending_changed_files = list(self.pending_changed_files)
        if self.events:
            event_log = getattr(run, "event_log", None)
            if isinstance(event_log, list):
                run.event_log = list(self.events)
