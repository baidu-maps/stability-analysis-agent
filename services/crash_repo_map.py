"""Crash-oriented repository map inspired by Aider's symbol graph.

The map is deliberately an independent service: it produces navigation hints
and ranking metadata, while source code and executable observations remain the
authoritative evidence in ContextEngine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


SUPPORTED_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".m", ".mm", ".swift", ".java", ".kt", ".py",
})
DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git", ".svn", ".hg", ".idea", ".vscode", "build", "builds",
    "out", "output", "obj", "bin", "node_modules", "third_party",
    "third-party", "thirdparty", "vendor", "external", "generated",
    "gen", "docs", "doc", ".crash_agent",
})
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_FUNCTION_DEF = re.compile(
    r"(?m)^\s*(?:(?:template\s*<[^\n>]+>\s*)?"
    r"[A-Za-z_][\w:<>,*&~\s]*?\s+)?"
    r"([A-Za-z_]\w*(?:::\w+)*(?:<[^;{}()]*>)?)\s*\([^;{}]*\)\s*\{"
)
_TYPE_DEF = re.compile(r"(?m)^\s*(?:class|struct|enum|namespace)\s+([A-Za-z_]\w*)")
_FIELD_DEF = re.compile(
    r"(?m)^\s*(?:[A-Za-z_]\w*(?:::\w+)*(?:<[^;{}]*>)?\s+)+"
    r"(m_[A-Za-z_]\w*|[A-Za-z_]\w*)\s*(?:[=;])"
)
_KEYWORDS = frozenset(
    "if else for while switch case return class struct enum namespace template"
    " public private protected const static virtual auto void int bool true false"
    " nullptr null new delete sizeof this try catch throw import from def func"
    .split()
)


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _norm_tokens(values: Any) -> Set[str]:
    if isinstance(values, str):
        values = [values]
    result: Set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        result.add(text)
        result.add(text.split("::")[-1])
    return result


@dataclass(frozen=True)
class RepoMapEntry:
    file: str
    score: float
    symbols: Tuple[str, ...] = ()
    references: Tuple[str, ...] = ()
    ranking_reasons: Tuple[str, ...] = ()
    line_numbers: Tuple[int, ...] = ()
    is_test: bool = False

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        for key in ("symbols", "references", "ranking_reasons", "line_numbers"):
            value[key] = list(value[key])
        return value


@dataclass
class RepoMapSnapshot:
    schema_version: int = 1
    roots: List[str] = field(default_factory=list)
    fingerprint: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "RepoMapSnapshot":
        payload = value if isinstance(value, dict) else {}
        return cls(
            schema_version=int(payload.get("schema_version") or 1),
            roots=[str(x) for x in payload.get("roots") or []],
            fingerprint=str(payload.get("fingerprint") or ""),
            files=[dict(x) for x in payload.get("files") or [] if isinstance(x, dict)],
            symbols=[dict(x) for x in payload.get("symbols") or [] if isinstance(x, dict)],
            edges=[dict(x) for x in payload.get("edges") or [] if isinstance(x, dict)],
            generated_at=str(payload.get("generated_at") or ""),
        )


class CrashRepoMap:
    """Incremental symbol/file graph with crash-anchor personalization."""

    def __init__(
        self,
        code_roots: Optional[Sequence[str]] = None,
        *,
        cache_dir: Optional[str] = None,
        exclude_dirs: Optional[Iterable[str]] = None,
        max_files: int = 20000,
    ) -> None:
        self.code_roots = self._normalize_roots(code_roots or [])
        self.exclude_dirs = set(DEFAULT_EXCLUDE_DIRS) | {str(x) for x in (exclude_dirs or [])}
        self.max_files = max(1, int(max_files))
        if cache_dir:
            self.cache_dir = Path(cache_dir).expanduser().resolve()
        else:
            cache_base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
            self.cache_dir = cache_base / "stability-analysis-agent" / "crash-repo-map"
        self.cache_path = self.cache_dir / f"{_fingerprint(self.code_roots)}.json"
        self.snapshot = RepoMapSnapshot(roots=list(self.code_roots))
        self.cache_hit = False

    @staticmethod
    def _normalize_roots(roots: Sequence[str]) -> List[str]:
        result: List[str] = []
        seen: Set[str] = set()
        for root in roots:
            try:
                value = str(Path(str(root)).expanduser().resolve())
            except Exception:
                continue
            if value in seen or not Path(value).is_dir():
                continue
            seen.add(value)
            result.append(value)
        return result

    def _iter_files(self) -> List[str]:
        files: List[str] = []
        for root in self.code_roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [item for item in dirnames if item not in self.exclude_dirs and not item.startswith(".")]
                for name in filenames:
                    if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    files.append(str((Path(dirpath) / name).resolve()))
                    if len(files) >= self.max_files:
                        return sorted(files)
        return sorted(set(files))

    @staticmethod
    def _parse_file(path: str) -> Dict[str, Any]:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            stat = Path(path).stat()
        except OSError:
            return {}
        defs: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, int, str]] = set()
        for matcher, kind in ((_FUNCTION_DEF, "function"), (_TYPE_DEF, "type"), (_FIELD_DEF, "field")):
            for match in matcher.finditer(text):
                name = str(match.group(1) or "").strip()
                line = text.count("\n", 0, match.start()) + 1
                key = (name, line, kind)
                if name and key not in seen:
                    seen.add(key)
                    defs.append({"name": name, "line": line, "kind": kind})
        defined = {item["name"] for item in defs}
        tokens = {
            token for token in _IDENTIFIER.findall(text)
            if token not in _KEYWORDS and token not in defined and len(token) > 1
        }
        return {
            "path": path,
            "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))),
            "size": int(stat.st_size),
            "symbols": defs,
            "references": sorted(tokens),
            "is_test": bool(re.search(r"(?:^|/)(?:test|tests|testing|unittest)(?:/|$)", path.lower())),
        }

    def _load_cache(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def build(self, code_roots: Optional[Sequence[str]] = None) -> RepoMapSnapshot:
        if code_roots is not None:
            self.code_roots = self._normalize_roots(code_roots)
        cached = self._load_cache()
        cached_files = cached.get("files") if isinstance(cached.get("files"), dict) else {}
        file_rows: List[Dict[str, Any]] = []
        for path in self._iter_files():
            old = cached_files.get(path) if isinstance(cached_files, dict) else None
            try:
                stat = Path(path).stat()
                signature = [int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))), int(stat.st_size)]
            except OSError:
                continue
            if isinstance(old, dict) and old.get("signature") == signature and isinstance(old.get("data"), dict):
                data = dict(old["data"])
                self.cache_hit = True
            else:
                data = self._parse_file(path)
            if data:
                file_rows.append({"signature": signature, "data": data})

        files = [row["data"] for row in file_rows]
        symbol_files: Dict[str, Set[str]] = {}
        symbols: List[Dict[str, Any]] = []
        for item in files:
            for symbol in item.get("symbols") or []:
                entry = {"file": item["path"], **dict(symbol)}
                symbols.append(entry)
                symbol_files.setdefault(str(symbol.get("name")), set()).add(item["path"])
        edges: List[Dict[str, Any]] = []
        for item in files:
            weights: Dict[str, int] = {}
            for reference in item.get("references") or []:
                for target in symbol_files.get(str(reference), set()):
                    if target != item["path"]:
                        weights[target] = weights.get(target, 0) + 1
            for target, weight in sorted(weights.items(), key=lambda pair: (-pair[1], pair[0])):
                edges.append({"source": item["path"], "target": target, "weight": weight})
        compact_files = [
            {
                "path": item["path"],
                "mtime_ns": item.get("mtime_ns"),
                "size": item.get("size"),
                "symbols": item.get("symbols") or [],
                "references": item.get("references") or [],
                "is_test": bool(item.get("is_test")),
            }
            for item in files
        ]
        fingerprint = _fingerprint({"roots": self.code_roots, "files": compact_files})
        self.snapshot = RepoMapSnapshot(
            roots=list(self.code_roots),
            fingerprint=fingerprint,
            files=compact_files,
            symbols=symbols,
            edges=edges,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_payload = {
                "schema_version": 1,
                "roots": self.code_roots,
                "files": {row["data"]["path"]: row for row in file_rows},
                "snapshot": self.snapshot.to_dict(),
            }
            self.cache_path.write_text(json.dumps(cache_payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return self.snapshot

    def rank(
        self,
        snapshot: Optional[RepoMapSnapshot] = None,
        anchors: Optional[Dict[str, Any]] = None,
        *,
        max_files: int = 20,
        max_tokens: int = 0,
    ) -> List[RepoMapEntry]:
        current = snapshot or self.snapshot
        anchor = anchors if isinstance(anchors, dict) else {}
        stack_files = _norm_tokens(anchor.get("stack_files") or anchor.get("files"))
        stack_symbols = _norm_tokens(anchor.get("stack_symbols") or anchor.get("functions") or anchor.get("symbols"))
        fields = _norm_tokens(anchor.get("fields"))
        callers = _norm_tokens(anchor.get("callers"))
        changed = _norm_tokens(anchor.get("changed_files"))
        include_tests = bool(anchor.get("include_tests") or anchor.get("purpose") in {"test", "verification"})
        definitions_by_file: Dict[str, List[str]] = {}
        refs_by_file: Dict[str, List[str]] = {}
        scores: Dict[str, float] = {}
        reasons: Dict[str, Set[str]] = {}
        for item in current.files:
            path = str(item.get("path") or "")
            if not path:
                continue
            symbols = [str(x.get("name")) for x in item.get("symbols") or [] if isinstance(x, dict)]
            refs = [str(x) for x in item.get("references") or []]
            definitions_by_file[path] = symbols
            refs_by_file[path] = refs
            scores[path] = 0.0
            reasons[path] = set()
            lower_path = path.lower()
            if path in stack_files or any(token and token in lower_path for token in {x.lower() for x in stack_files}):
                scores[path] += 10.0
                reasons[path].add("stack_frame_match")
            if path in changed:
                scores[path] += 4.0
                reasons[path].add("recent_change")
            if item.get("is_test") and not include_tests:
                scores[path] -= 2.0
            elif item.get("is_test") and include_tests:
                scores[path] += 3.0
                reasons[path].add("test_reference")
            for symbol in symbols:
                simple = symbol.split("::")[-1]
                if symbol in stack_symbols or simple in stack_symbols:
                    scores[path] += 8.0
                    reasons[path].add("stack_symbol_match")
                if symbol in fields or simple in fields:
                    scores[path] += 5.0
                    reasons[path].add("field_reference")
                if symbol in callers or simple in callers:
                    scores[path] += 4.0
                    reasons[path].add("caller_match")
            if any(ref in stack_symbols or ref in fields or ref in callers for ref in refs):
                scores[path] += 2.0
                reasons[path].add("symbol_reference")

        outgoing: Dict[str, List[Tuple[str, int]]] = {}
        for edge in current.edges:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source and target:
                outgoing.setdefault(source, []).append((target, int(edge.get("weight") or 1)))
        # A few personalized propagation passes approximate Aider's graph rank
        # without adding a mandatory networkx dependency to the core package.
        for _ in range(3):
            propagated = dict(scores)
            for source, targets in outgoing.items():
                if not scores.get(source):
                    continue
                for target, weight in targets:
                    propagated[target] = propagated.get(target, 0.0) + scores[source] * min(0.8, 0.1 * weight)
            scores = propagated
        max_chars = max(0, int(max_tokens or 0)) * 4
        selected: List[RepoMapEntry] = []
        used = 0
        ordered = sorted(scores, key=lambda path: (-scores[path], path))
        for path in ordered:
            if len(selected) >= max(1, int(max_files)):
                break
            score = scores[path]
            if score <= 0 and selected:
                break
            symbols = tuple(definitions_by_file.get(path, []))
            refs = tuple(refs_by_file.get(path, [])[:20])
            lines = tuple(
                int(x.get("line")) for x in current.files[[f.get("path") for f in current.files].index(path)].get("symbols") or []
                if isinstance(x, dict) and str(x.get("line") or "").isdigit()
            )
            entry = RepoMapEntry(
                file=path,
                score=round(float(score), 4),
                symbols=symbols,
                references=refs,
                ranking_reasons=tuple(sorted(reasons.get(path, set()))),
                line_numbers=lines,
                is_test=bool(next((f.get("is_test") for f in current.files if f.get("path") == path), False)),
            )
            estimate = len(path) + sum(len(item) for item in symbols) + sum(len(item) for item in refs)
            if max_chars and used + estimate > max_chars and selected:
                continue
            selected.append(entry)
            used += estimate
        return selected


def render_repo_map(entries: Sequence[RepoMapEntry], *, max_chars: int = 6000) -> str:
    """Render only navigation metadata; source remains resolver-owned evidence."""
    if not entries:
        return ""
    lines = ["## 代码仓库地图（导航摘要，不是根因证据）"]
    for entry in entries:
        symbols = ", ".join(entry.symbols[:8]) or "(no definitions)"
        reasons = ", ".join(entry.ranking_reasons) or "graph_related"
        lines.append(f"- `{entry.file}` score={entry.score} [{reasons}]\n  symbols: {symbols}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    suffix = "\n...[repo map truncated]"
    return text[: max(0, max_chars - len(suffix))] + suffix
