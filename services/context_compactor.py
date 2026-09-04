"""Deterministic context compaction for long-running investigation sessions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List


@dataclass
class CompactedContext:
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class ContextCompactor:
    """Keep control state and recent actionable evidence ahead of old transcripts."""

    PRIORITY = {"control": 0, "hypothesis": 1, "recent_observation": 2,
                "ledger": 3, "repo_map": 4, "stable": 5, "history": 6}

    def compact(self, sections: Iterable[Dict[str, Any]], *, max_chars: int,
                max_tokens: int = 0, round_index: int = 0,
                summary_provider: Any = None, token_counter: Any = None) -> CompactedContext:
        max_chars = max(0, int(max_chars))
        if max_tokens:
            max_chars = min(max_chars, max(1, int(max_tokens)) * 4)
        items = []
        for raw in sections:
            try:
                item = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
            except Exception:
                continue
            if str(item.get("content") or ""):
                items.append(item)
        before = sum(len(str(item.get("content") or "")) for item in items)
        # Summaries are deliberately limited to low-priority history. A failed or
        # malformed provider must never make control/context construction fail.
        summarized = 0
        if callable(summary_provider):
            for item in items:
                if str(item.get("priority") or "stable") not in {"history", "stable"}:
                    continue
                content = str(item.get("content") or "")
                if len(content) < 512:
                    continue
                try:
                    candidate = summary_provider(content)
                    if isinstance(candidate, dict):
                        candidate = candidate.get("summary")
                    if isinstance(candidate, str) and candidate.strip() and len(candidate) < len(content):
                        item["content"] = candidate.strip()
                        summarized += 1
                except Exception:
                    continue
        def _order(item: Dict[str, Any]) -> tuple:
            priority = self.PRIORITY.get(str(item.get("priority") or "stable"), 5)
            # Within a priority tier, retain higher evidence value per token.
            value = float(item.get("evidence_score") or item.get("relevance") or 0.0)
            cost = max(1, len(str(item.get("content") or "")) // 4)
            return (priority, -(value / cost if value else 0.0))
        items.sort(key=_order)
        retained: List[str] = []
        used = 0
        dropped = 0
        # Atomic groups are selected as a unit, so a tool call cannot be kept
        # without its result (or a source snippet without its provenance).
        groups: List[List[Dict[str, Any]]] = []
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            key = str(item.get("atomic_group") or "")
            if key:
                grouped.setdefault(key, []).append(item)
            else:
                groups.append([item])
        groups.extend(grouped.values())
        groups.sort(key=lambda group: min(_order(item) for item in group))
        for group in groups:
            group_text = "\n\n".join(str(item.get("content") or "") for item in group)
            if used + len(group_text) > max_chars and any(str(item.get("priority")) == "control" for item in group):
                group_text = group_text[: max(0, max_chars - used)]
            if used + len(group_text) > max_chars:
                dropped += len(group)
                continue
            retained.append(group_text)
            used += len(group_text)
        """
        for item in items:
            content = str(item.get("content") or "")
            limit = max(0, int(max_chars) - used)
            if limit <= 0:
                dropped += 1
                continue
            if len(content) > limit and str(item.get("priority")) != "control":
                content = content[:max(0, limit - 32)] + "\n...[compacted]"
            elif len(content) > limit:
                content = content[:limit]
            retained.append(content)
            used += len(content)
        """
        text = "\n\n".join(retained)
        counter = token_counter if callable(token_counter) else (lambda value: max(1, len(str(value)) // 4))
        try:
            tokens_before = sum(counter(item.get("content") or "") for item in items)
            tokens_after = counter(text)
        except Exception:
            counter = lambda value: max(1, len(str(value)) // 4)
            tokens_before = sum(counter(item.get("content") or "") for item in items)
            tokens_after = counter(text)
        metadata = {
            "schema_version": 1, "round": int(round_index),
            "chars_before": before, "chars_after": len(text),
            "dropped_duplicates": dropped,
            "retained_sections": len(retained),
            "atomic_groups": len(groups),
            "max_tokens": int(max_tokens or 0), "summary_provider_used": summarized,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
        }
        if max_tokens:
            metadata["estimated_tokens_before"] = max(0, before // 4)
            metadata["estimated_tokens_after"] = max(0, len(text) // 4)
        return CompactedContext(text=text, metadata=metadata)
