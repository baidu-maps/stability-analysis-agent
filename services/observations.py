"""Structured observations exposed to agent context, reports, and judges."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional


OBSERVATION_KINDS = frozenset({
    "tool_result",
    "tool_error",
    "policy_decision",
    "runtime_event",
    "verification",
    "judge_feedback",
    "memory_feedback",
})


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class Observation:
    kind: str
    source: str
    status: str
    summary: str
    round: int = 0
    details: Dict[str, Any] = field(default_factory=dict)
    actionable: bool = False
    observation_id: str = ""
    created_at: str = ""

    def normalized(self) -> "Observation":
        kind = self.kind if self.kind in OBSERVATION_KINDS else "runtime_event"
        created_at = self.created_at or datetime.now(timezone.utc).isoformat()
        observation_id = self.observation_id or "obs_" + _digest({
            "kind": kind,
            "source": self.source,
            "status": self.status,
            "summary": self.summary,
            "round": self.round,
            "details": self.details,
        })
        return Observation(
            kind=kind,
            source=str(self.source or "runtime"),
            status=str(self.status or "unknown"),
            summary=str(self.summary or "").strip(),
            round=max(0, int(self.round or 0)),
            details=dict(self.details or {}),
            actionable=bool(self.actionable),
            observation_id=observation_id,
            created_at=created_at,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self.normalized())


class ObservationStore:
    """Append-only, deduplicated observation ledger for one runtime."""

    def __init__(self, observations: Optional[Iterable[Observation]] = None):
        self._items: Dict[str, Observation] = {}
        for observation in observations or ():
            self.add(observation)

    def add(self, observation: Observation) -> Observation:
        normalized = observation.normalized()
        self._items.setdefault(normalized.observation_id, normalized)
        return self._items[normalized.observation_id]

    def record(
        self,
        *,
        kind: str,
        source: str,
        status: str,
        summary: str,
        round_index: int = 0,
        details: Optional[Dict[str, Any]] = None,
        actionable: bool = False,
    ) -> Dict[str, Any]:
        return self.add(Observation(
            kind=kind,
            source=source,
            status=status,
            summary=summary,
            round=round_index,
            details=dict(details or {}),
            actionable=actionable,
        )).to_dict()

    def items(self, *, since_round: Optional[int] = None) -> List[Dict[str, Any]]:
        values = list(self._items.values())
        if since_round is not None:
            values = [item for item in values if item.round >= int(since_round)]
        return [item.to_dict() for item in values]

    def markdown(self, *, since_round: Optional[int] = None, max_chars: int = 4000) -> str:
        items = self.items(since_round=since_round)
        if not items:
            return ""
        lines = ["## 运行观察与可执行反馈"]
        for item in items:
            action = "；需要处理" if item.get("actionable") else ""
            lines.append(
                f"- [{item.get('status')}] {item.get('source')}: {item.get('summary')}{action}"
            )
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        suffix = "\n...[observations truncated]"
        return text[: max(0, max_chars - len(suffix))] + suffix

    def snapshot(self) -> Dict[str, Any]:
        items = self.items()
        return {
            "schema_version": 1,
            "items": items,
            "count": len(items),
            "actionable_count": sum(1 for item in items if item.get("actionable")),
        }

