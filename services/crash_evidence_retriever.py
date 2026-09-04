"""Crash-personalized multi-source retrieval with optional reranking."""
from __future__ import annotations
from typing import Any, Callable, Dict, Iterable, List, Optional

from services.code_evidence_index import CodeEvidenceIndex, IndexCandidate

class CrashEvidenceRetriever:
    def __init__(self, index: CodeEvidenceIndex, *, repo_map: Any = None,
                 reranker: Optional[Callable[[List[Dict[str, Any]], Dict[str, Any]], List[Dict[str, Any]]]] = None,
                 semantic_search: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None):
        self.index, self.repo_map, self.reranker = index, repo_map, reranker
        self.semantic_search = semantic_search

    def _query(self, anchors: Dict[str, Any]) -> str:
        values: List[str] = []
        for key in ("stack_symbols", "stack_frames", "fields", "types", "callers", "diagnostic_keywords"):
            value = anchors.get(key) or []
            values.extend(str(x.get("function") or x.get("symbol") or x) if isinstance(x, dict) else str(x) for x in value)
        values.extend(str(x.get("statement") or "") for x in (anchors.get("hypotheses") or []) if isinstance(x, dict))
        focus = anchors.get("focus_goal") or anchors.get("focus") or ""
        if isinstance(focus, dict):
            focus = focus.get("goal") or focus.get("statement") or ""
        if focus:
            values.append(str(focus))
        for observation in anchors.get("recent_observations") or []:
            if isinstance(observation, dict):
                values.append(str(observation.get("summary") or observation.get("error") or ""))
        return " ".join(values)

    def retrieve(self, anchors: Optional[Dict[str, Any]] = None, *, limit: int = 12) -> List[Dict[str, Any]]:
        context = dict(anchors or {})
        query = self._query(context)
        candidates = self.index.search(query, mode="symbol", limit=limit * 2)
        candidates += self.index.search(query, mode="full_text", limit=limit * 2)
        unique: Dict[str, IndexCandidate] = {}
        for item in candidates:
            key = f"{item.file}:{item.line_start}"
            if key not in unique or item.score > unique[key].score:
                unique[key] = item
        values = [item.to_dict() for item in unique.values()]
        # RepoMap contributes navigation candidates even when the lightweight
        # text index has no exact hit (for example, an unresolved template or
        # an indirect caller).  It is deliberately capped and marked as
        # repository metadata; callers still need to request source content.
        map_entries = getattr(self.repo_map, "files", None) if self.repo_map is not None else None
        if isinstance(map_entries, list):
            for entry in map_entries:
                if not isinstance(entry, dict) or not entry.get("file"):
                    continue
                path = str(entry.get("file"))
                symbols = entry.get("symbols") or []
                symbol_names = [str(x.get("name") if isinstance(x, dict) else x) for x in symbols]
                if query and not any(term.lower() in (path + " " + " ".join(symbol_names)).lower()
                                     for term in query.split() if len(term) > 1):
                    continue
                values.append({"file": path, "symbol": symbol_names[0] if symbol_names else "",
                               "line_start": int(entry.get("line") or 0),
                               "line_end": int(entry.get("line") or 0), "content": "",
                               "score": 0.25, "ranking_reasons": ["repo_map_relation"],
                               "evidence_type": "repository"})
        if self.semantic_search and query:
            try:
                semantic = self.semantic_search(query, limit * 2)
                for item in semantic or []:
                    if not isinstance(item, dict) or not item.get("file"):
                        continue
                    value = dict(item)
                    value["score"] = min(0.65, float(value.get("score") or 0.0))
                    value["ranking_reasons"] = sorted(set(list(value.get("ranking_reasons") or []) + ["semantic_similarity"]))
                    values.append(value)
            except Exception:
                pass
        stack_files = {str(x) for x in context.get("stack_files") or []}
        fields = {str(x).lower() for x in context.get("fields") or []}
        validation_focus = any(str(x).lower() in {"test", "verify", "reproduce"} for x in context.get("next_actions") or [])
        for item in values:
            path = str(item.get("file") or "")
            content = str(item.get("content") or "").lower()
            reasons = list(item.get("ranking_reasons") or [])
            score = float(item.get("score") or 0.0)
            if path in stack_files or any(path.endswith(value) for value in stack_files):
                score += 1.0; reasons.append("stack_frame_match")
            if fields and any(field in content for field in fields):
                score += 0.35; reasons.append("field_reference")
            if validation_focus and any(part in path.lower().split("/") for part in ("test", "tests", "testing")):
                score += 0.25; reasons.append("test_reference")
            item["score"] = round(score, 4)
            item["ranking_reasons"] = sorted(set(reasons))
        if self.reranker:
            try:
                ranked = self.reranker(values, context)
                if isinstance(ranked, list): values = [x for x in ranked if isinstance(x, dict)]
            except Exception:
                pass
        values.sort(key=lambda x: (-float(x.get("score") or 0), str(x.get("file") or "")))
        for item in values[:limit]:
            item.setdefault("provider", "codebase_search")
            item.setdefault("evidence_type", "repository")
            item.setdefault("confidence", min(1.0, float(item.get("score") or 0)))
            item.setdefault("cost", {"chars": len(str(item.get("content") or "")), "tokens": max(1, len(str(item.get("content") or "")) // 4)})
        return values[:limit]
