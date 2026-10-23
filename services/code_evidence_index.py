"""Incremental, dependency-free source evidence index.

This index is intentionally small: CrashRepoMap remains authoritative for
relationships while this service supplies searchable source chunks and stable
fingerprints for ContextEngine retrieval.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from services.repo_scan_limits import (
    MAX_INDEX_FILES,
    MAX_SOURCE_BYTES,
    collapse_nested_roots,
    merge_priority_paths,
    prefer_terms_lower,
    remember_priority_path,
)

SUPPORTED_EXTENSIONS = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".m", ".mm", ".swift", ".java", ".kt", ".py"})
DEFAULT_EXCLUDES = frozenset({".git", ".svn", ".hg", "build", "out", "output", "bin", "obj", "vendor", "third_party", "third-party", "generated", "node_modules"})

def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]

@dataclass
class IndexCandidate:
    file: str
    symbol: str = ""
    line_start: int = 0
    line_end: int = 0
    content: str = ""
    score: float = 0.0
    ranking_reasons: List[str] = field(default_factory=list)
    evidence_type: str = "repository"
    content_fingerprint: str = ""
    filtered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class IndexSnapshot:
    schema_version: int = 1
    roots: List[str] = field(default_factory=list)
    revision: Optional[str] = None
    fingerprint: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class CodeEvidenceIndex:
    def __init__(
        self,
        *,
        cache_dir: Optional[str] = None,
        exclude_dirs: Optional[Iterable[str]] = None,
        max_files: int = MAX_INDEX_FILES,
    ):
        self.exclude_dirs = set(DEFAULT_EXCLUDES) | {str(x) for x in (exclude_dirs or [])}
        cache_base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
        self.cache_dir = Path(cache_dir).expanduser().resolve() if cache_dir else cache_base / "stability-analysis-agent" / "code-evidence-index"
        self.max_files = max(1, int(max_files))
        self._files: Dict[str, Dict[str, Any]] = {}
        self.snapshot = IndexSnapshot()

    def _cache_path(self, roots: Sequence[str]) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{_hash(list(roots))}-v2.json"

    def _load_cache(self, roots: Sequence[str]) -> None:
        path = self._cache_path(roots)
        if path is None or not path.is_file():
            return
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            files = value.get("files") if isinstance(value, dict) else None
            if isinstance(files, dict):
                loaded: Dict[str, Dict[str, Any]] = {}
                for key, value in files.items():
                    if not isinstance(value, dict):
                        continue
                    row = dict(value)
                    row.pop("text", None)
                    loaded[str(key)] = row
                self._files = loaded
        except (OSError, ValueError):
            return

    def _save_cache(self, roots: Sequence[str]) -> None:
        path = self._cache_path(roots)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema_version": 1, "roots": list(roots), "files": self._files},
                                       ensure_ascii=False), encoding="utf-8")
        except OSError:
            return

    def _iter_files(self, roots: Sequence[str], prefer_terms: Optional[Sequence[str]] = None) -> List[str]:
        terms = prefer_terms_lower(prefer_terms)
        matched: List[str] = []
        extras: List[str] = []
        seen = set()
        for raw in roots:
            root = Path(raw).expanduser().resolve()
            if not root.is_dir():
                continue
            for base, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if d not in self.exclude_dirs and not d.startswith(".")]
                for name in names:
                    if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    path = str((Path(base) / name).resolve())
                    if path in seen:
                        continue
                    seen.add(path)
                    remember_priority_path(matched, extras, path, terms, self.max_files)
        return merge_priority_paths(matched, extras, self.max_files)

    @staticmethod
    def _read_limited(path: str) -> str:
        try:
            if Path(path).stat().st_size > MAX_SOURCE_BYTES:
                return ""
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _parse(path: str) -> Dict[str, Any]:
        try:
            stat = Path(path).stat()
        except OSError:
            return {}
        mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)))
        is_test = bool(re.search(r"/(?:test|tests|testing)/", path.lower()))
        if int(stat.st_size) > MAX_SOURCE_BYTES:
            return {
                "path": path,
                "mtime_ns": mtime_ns,
                "size": int(stat.st_size),
                "fingerprint": _hash({"size": int(stat.st_size), "mtime_ns": mtime_ns}),
                "symbols": [],
                "tokens": [],
                "oversized": True,
                "is_test": is_test,
            }
        text = CodeEvidenceIndex._read_limited(path)
        if not text and int(stat.st_size) > 0:
            return {}
        lines = text.splitlines()
        symbols: List[Dict[str, Any]] = []
        pattern = re.compile(
            r"^\s*(?:[\w:<>,*&~]+\s+)+([A-Za-z_]\w*(?:::\w+)*)\s*(?:<[^>]*>)?\s*\([^;{}]*\)\s*(?:const\s*)?\{"
        )
        for number, line in enumerate(lines, 1):
            match = pattern.search(line)
            if match:
                symbols.append({"name": match.group(1), "line": number})
        tokens: List[str] = []
        seen_tokens = set()
        for token in re.findall(r"[A-Za-z_]\w*", text):
            if len(token) <= 2 or token in seen_tokens:
                continue
            seen_tokens.add(token)
            tokens.append(token)
            if len(tokens) >= 48:
                break
        return {
            "path": path,
            "mtime_ns": mtime_ns,
            "size": int(stat.st_size),
            "fingerprint": _hash(text),
            "symbols": symbols,
            "tokens": tokens,
            "is_test": is_test,
        }

    def update(
        self,
        code_roots: List[str],
        revision: Optional[str] = None,
        prefer_terms: Optional[Sequence[str]] = None,
    ) -> IndexSnapshot:
        roots = collapse_nested_roots(code_roots or [])
        roots = sorted(roots)
        if not self._files:
            self._load_cache(roots)
        paths = self._iter_files(roots, prefer_terms)
        current = set(paths)
        self._files = {path: value for path, value in self._files.items() if path in current}
        changed = 0
        for path in paths:
            try:
                stat = Path(path).stat()
                stamp = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)))
            except OSError:
                continue
            old = self._files.get(path)
            # mtime/size are a fast path, but network filesystems and tools can
            # preserve both while replacing content.  Recheck the fingerprint
            # before trusting a cached parse so retrieval never serves stale
            # source evidence.
            if old is not None and old.get("mtime_ns") == stamp and old.get("size") == int(stat.st_size):
                if old.get("oversized") or int(stat.st_size) > MAX_SOURCE_BYTES:
                    continue
                current_fingerprint = _hash(self._read_limited(path))
                if current_fingerprint and current_fingerprint == old.get("fingerprint"):
                    continue
            parsed = self._parse(path)
            if parsed:
                parsed.pop("text", None)
                self._files[path] = parsed
                changed += 1
        rows = [dict(data) for data in self._files.values()]
        fingerprint = _hash({"roots": roots, "files": rows, "revision": revision})
        self.snapshot = IndexSnapshot(roots=roots, revision=revision, fingerprint=fingerprint, files=rows)
        self._save_cache(roots)
        return self.snapshot

    def search(self, query: str, *, mode: str = "full_text", limit: int = 20) -> List[IndexCandidate]:
        terms = {x.lower() for x in re.findall(r"[A-Za-z_]\w*", str(query or "")) if len(x) > 1}
        if not terms:
            return []
        ranked: List[tuple] = []
        for path, row in self._files.items():
            if row.get("oversized"):
                continue
            names = [str(item.get("name") or "") for item in row.get("symbols") or [] if isinstance(item, dict)]
            tokens = [str(item).lower() for item in row.get("tokens") or []]
            score = 0.0
            reasons: List[str] = []
            symbol_hits = 0
            for name in names:
                lower_name = name.lower()
                simple = lower_name.split("::")[-1]
                if lower_name in terms or simple in terms:
                    symbol_hits += 1
            if mode in {"symbol", "symbols"}:
                if symbol_hits:
                    score += 1.0 + symbol_hits * 0.5
                    reasons.append("symbol_match")
            else:
                token_hits = sum(1 for token in tokens if token in terms)
                if token_hits:
                    score += min(0.8, token_hits / 20.0)
                    reasons.append("full_text_match")
                if symbol_hits:
                    score += 0.8
                    reasons.append("symbol_match")
            if not score:
                continue
            line = 1
            symbol_name = ""
            for item in row.get("symbols") or []:
                if not isinstance(item, dict):
                    continue
                candidate = str(item.get("name") or "")
                simple = candidate.lower().split("::")[-1]
                if candidate.lower() in terms or simple in terms:
                    line = int(item.get("line") or 1)
                    symbol_name = candidate
                    break
            ranked.append((score, path, line, symbol_name, reasons, str(row.get("fingerprint") or "")))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        output: List[IndexCandidate] = []
        for score, path, line, symbol_name, reasons, fingerprint in ranked[: max(1, int(limit))]:
            text = self._read_limited(path)
            snippet = ""
            if text:
                rows = text.splitlines()
                if line <= 1:
                    line = next(
                        (index + 1 for index, value in enumerate(rows) if any(term in value.lower() for term in terms)),
                        1,
                    )
                snippet = "\n".join(rows[max(0, line - 2): line + 8])
            output.append(
                IndexCandidate(
                    path,
                    symbol_name,
                    line,
                    line + 8,
                    snippet,
                    min(1.0, score),
                    reasons,
                    content_fingerprint=fingerprint,
                )
            )
        return output
