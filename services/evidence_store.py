"""Deduplicated evidence package used by reports and future prompt adapters."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    content: str
    source: str
    evidence_id: str = ""
    file: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    confidence: Optional[float] = None
    relevance: Optional[float] = None
    round: int = 0
    layer: str = "fact"  # fact or inference
    references: tuple = ()
    def normalized(self) -> "EvidenceItem":
        if self.evidence_id:
            return self
        raw = json.dumps({"kind": self.kind, "content": self.content, "source": self.source,
                         "file": self.file, "line_start": self.line_start, "line_end": self.line_end},
                         ensure_ascii=False, sort_keys=True).encode("utf-8")
        return EvidenceItem(**{**asdict(self), "evidence_id": "ev_" + hashlib.sha256(raw).hexdigest()[:16]})
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self.normalized())

class EvidenceStore:
    def __init__(self, items: Optional[Iterable[EvidenceItem]] = None):
        self._items: Dict[str, EvidenceItem] = {}
        for item in items or []:
            self.add(item)
    def add(self, item: EvidenceItem) -> EvidenceItem:
        item = item.normalized()
        old = self._items.get(item.evidence_id)
        if old is None or (item.relevance or 0) > (old.relevance or 0):
            self._items[item.evidence_id] = item
        return self._items[item.evidence_id]
    def add_dict(self, value: Dict[str, Any], *, source: str = "unknown") -> EvidenceItem:
        allowed = {k: value[k] for k in ("kind", "content", "source", "evidence_id", "file", "line_start", "line_end", "confidence", "relevance", "round", "layer", "references") if k in value}
        allowed.setdefault("kind", "context"); allowed.setdefault("content", ""); allowed.setdefault("source", source)
        return self.add(EvidenceItem(**allowed))
    def items(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        values = sorted(self._items.values(), key=lambda x: (x.relevance or 0, x.confidence or 0), reverse=True)
        return [x.to_dict() for x in values[:limit] if x.content]
    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"items": self.items()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class EvidenceContextManager:
    """Select a bounded, ranked evidence package for a model turn."""
    def __init__(self, store: Optional[EvidenceStore] = None, *, max_chars: int = 24000):
        self.store = store or EvidenceStore()
        self.max_chars = max(1000, int(max_chars))

    def add(self, item: EvidenceItem) -> EvidenceItem:
        return self.store.add(item)

    @staticmethod
    def _compress(content: str, limit: int) -> str:
        if len(content) <= limit:
            return content
        marker = "\n...[evidence compressed by context budget]...\n"
        if limit <= len(marker) + 32:
            return content[:limit]
        available = limit - len(marker)
        head = max(1, int(available * 0.7))
        return content[:head] + marker + content[-(available - head):]

    def select_prompt(self, prompt: str, *, max_chars: Optional[int] = None,
                      max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Bound one assembled prompt while preserving its head and task tail."""
        # Honor callers' small deterministic budgets (used by CLI previews and
        # tests); control-contract preservation is handled by the compressor.
        char_budget = max(100, int(max_chars or self.max_chars))
        if max_tokens:
            char_budget = min(char_budget, max(100, int(max_tokens) * 4))
        selected = self._compress(str(prompt or ""), char_budget)
        return {
            "content": selected,
            "chars": len(selected),
            "max_chars": char_budget,
            "compressed": len(selected) < len(str(prompt or "")),
            "source_chars": len(str(prompt or "")),
        }

    @staticmethod
    def _format_evidence_items_markdown(items: Sequence[Dict[str, Any]]) -> str:
        blocks: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            title = str(item.get("file") or item.get("kind") or "evidence")
            block = "\n".join([
                f"#### 证据: {title}",
                f"- evidence_id: {item.get('evidence_id')}",
                f"- source: {item.get('source')}",
                content,
            ]).strip()
            if block:
                blocks.append(block)
        if not blocks:
            return ""
        return "\n\n".join(blocks)

    def assemble_context_loop_prompt(
        self,
        base_prompt: str,
        *,
        evidence_package: Optional[Dict[str, Any]] = None,
        is_final_round: bool = False,
        early_final_reason: Optional[str] = None,
        max_chars: Optional[int] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Assemble and bound the only prompt text allowed to reach the LLM."""
        from services.context_loop_contract import assemble_loop_prompt

        return assemble_loop_prompt(
            base_prompt,
            evidence_package=evidence_package,
            is_final_round=is_final_round,
            early_final_reason=early_final_reason,
            include_json_reminder=True,
        )

    def package(self, *, max_chars: Optional[int] = None, max_tokens: Optional[int] = None,
                min_round: Optional[int] = None) -> Dict[str, Any]:
        budget = max(1000, int(max_chars or self.max_chars))
        if max_tokens:
            budget = min(budget, max(1000, int(max_tokens) * 4))
        selected: List[Dict[str, Any]] = []
        used = 0
        token_budget = max(0, int(max_tokens or 0))
        used_tokens = 0
        dropped = 0
        truncated = 0
        for item in self.store.items():
            if min_round is not None and int(item.get("round") or 0) < int(min_round):
                continue
            content = str(item.get("content") or "")
            if not content:
                continue
            remaining = budget - used
            remaining_tokens = token_budget - used_tokens if token_budget else 0
            if token_budget:
                remaining = min(remaining, max(0, remaining_tokens * 4))
            if remaining <= 0:
                dropped += 1
                continue
            if selected and len(content) > remaining:
                dropped += 1
                continue
            bounded = self._compress(content, remaining)
            if len(bounded) < len(content):
                truncated += 1
            selected_item = dict(item)
            selected_item["content"] = bounded
            selected_item["truncated"] = len(bounded) < len(content)
            selected.append(selected_item)
            used += len(bounded)
            item_tokens = max(1, (len(bounded) + 3) // 4)
            used_tokens += item_tokens
        return {"items": selected, "item_count": len(selected), "chars": used,
                "tokens": used_tokens, "max_chars": budget, "max_tokens": token_budget,
                "dropped_count": dropped, "truncated_count": truncated,
                "generated_at": datetime.now(timezone.utc).isoformat()}
