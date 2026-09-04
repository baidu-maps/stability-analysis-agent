"""Typed, serializable context parts used by the runtime context adapter."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

PART_KINDS = frozenset({"stable_evidence", "hypothesis", "tool_call", "tool_result", "observation", "summary", "diff", "error"})

def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]

@dataclass
class ContextPart:
    kind: str
    content: str = ""
    part_id: str = ""
    round: int = 0
    action_id: Optional[str] = None
    observation_id: Optional[str] = None
    content_ref: Optional[str] = None
    content_hash: str = ""
    tokens: int = 0
    priority: str = "stable"
    atomic_group: Optional[str] = None
    source: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = str(self.kind or "observation")
        if self.kind not in PART_KINDS:
            self.kind = "observation"
        self.content = str(self.content or "")
        self.content_hash = self.content_hash or _hash(self.content)
        self.tokens = int(self.tokens or max(0, len(self.content) // 4))
        self.part_id = self.part_id or "part_" + _hash({"kind": self.kind, "hash": self.content_hash, "round": self.round})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Any, *, default_kind: str = "observation") -> "ContextPart":
        if isinstance(value, ContextPart):
            return value
        data = dict(value) if isinstance(value, dict) else {"content": str(value or "")}
        data.setdefault("kind", default_kind)
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

def parts_from_evidence(items: Iterable[Dict[str, Any]]) -> List[ContextPart]:
    result: List[ContextPart] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        priority = str(item.get("priority") or "stable")
        kind = str(item.get("kind") or "observation")
        if kind in {"source", "source_code", "crash_log"}:
            kind = "stable_evidence" if priority == "stable" else "observation"
        result.append(ContextPart.from_mapping({**item, "kind": kind, "priority": priority}))
    return result
