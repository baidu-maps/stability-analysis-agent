#!/usr/bin/env python3
"""Small registry for structured domain knowledge; external packs can register entries."""
from __future__ import annotations

from dataclasses import asdict
from typing import Dict, Iterable, List, Optional

from .models import KnowledgeEntry


class KnowledgeRegistry:
    def __init__(self, entries: Optional[Iterable[KnowledgeEntry]] = None) -> None:
        self._entries: Dict[str, KnowledgeEntry] = {}
        for entry in entries or []:
            self.register(entry)

    def register(self, entry: KnowledgeEntry) -> None:
        if not entry.id.strip():
            raise ValueError("knowledge entry id is required")
        self._entries[entry.id] = entry

    def get(self, entry_id: str) -> Optional[KnowledgeEntry]:
        return self._entries.get(entry_id)

    def search(self, *, domain: Optional[str] = None, module: Optional[str] = None, text: str = "") -> List[KnowledgeEntry]:
        query = text.lower()
        result = []
        for entry in self._entries.values():
            if domain and entry.domain != domain:
                continue
            if module and entry.module != module:
                continue
            if query and not any(term.lower() in query for term in entry.evidence_patterns + [entry.root_cause]):
                continue
            result.append(entry)
        return result

    def to_dict(self) -> List[dict]:
        return [asdict(item) for item in self._entries.values()]


default_registry = KnowledgeRegistry()


def register_builtin_knowledge() -> int:
    """Load compact specialist knowledge packs into the shared registry."""
    count = 0
    try:
        from tools.api_fault.core import knowledge_entries
    except Exception:
        pass
    else:
        for entry in knowledge_entries():
            default_registry.register(entry)
            count += 1
    # Also try loading from YAML knowledge files
    try:
        from .knowledge_loader import merge_into_registry
        count += merge_into_registry(default_registry)
    except Exception:
        pass
    return count
