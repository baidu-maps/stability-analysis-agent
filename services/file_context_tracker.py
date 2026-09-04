"""Track file snapshots used by an agent before edits are applied."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional


def content_fingerprint(content: str) -> str:
    return hashlib.sha256(str(content).encode("utf-8", errors="replace")).hexdigest()[:20]


@dataclass
class FileContextRecord:
    file: str
    fingerprint: str
    read_revision: Optional[str] = None
    line_start: int = 0
    line_end: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FileContextTracker:
    """Small, serializable read-before-write guard (Git agnostic)."""
    def __init__(self, records: Optional[Dict[str, Any]] = None):
        self._records: Dict[str, FileContextRecord] = {}
        for key, value in (records or {}).items():
            if isinstance(value, dict):
                self._records[str(key)] = FileContextRecord(
                    file=str(value.get("file") or key),
                    fingerprint=str(value.get("fingerprint") or value.get("read_fingerprint") or ""),
                    read_revision=value.get("read_revision") or value.get("workspace_revision"),
                    line_start=int(value.get("line_start") or (value.get("read_line_range") or [0, 0])[0]),
                    line_end=int(value.get("line_end") or (value.get("read_line_range") or [0, 0])[-1]),
                )

    def record_read(self, file_path: str, content: str, line_start: int = 0,
                    line_end: int = 0, workspace_revision: Optional[str] = None) -> FileContextRecord:
        path = str(Path(file_path).expanduser().resolve())
        record = FileContextRecord(path, content_fingerprint(content), workspace_revision,
                                   int(line_start or 0), int(line_end or 0))
        self._records[path] = record
        return record

    def current_record(self, file_path: str) -> Optional[FileContextRecord]:
        return self._records.get(str(Path(file_path).expanduser().resolve()))

    def check_stale(self, file_path: str, content: Optional[str] = None,
                    workspace_revision: Optional[str] = None) -> Dict[str, Any]:
        path = str(Path(file_path).expanduser().resolve())
        expected = self._records.get(path)
        if expected is None:
            return {"stale": False, "known": False, "file": path}
        if content is None:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                return {"stale": True, "known": True, "file": path, "error": str(exc),
                        "expected_fingerprint": expected.fingerprint}
        actual = content_fingerprint(content)
        revision_changed = bool(expected.read_revision and workspace_revision and
                                expected.read_revision != workspace_revision)
        return {"stale": actual != expected.fingerprint or revision_changed, "known": True,
                "file": path, "expected_fingerprint": expected.fingerprint,
                "actual_fingerprint": actual, "expected_revision": expected.read_revision,
                "actual_revision": workspace_revision}

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {key: value.to_dict() for key, value in self._records.items()}

    @classmethod
    def from_snapshot(cls, snapshot: Any) -> "FileContextTracker":
        return cls(snapshot if isinstance(snapshot, dict) else {})
