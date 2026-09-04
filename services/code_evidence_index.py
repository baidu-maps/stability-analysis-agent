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
    def __init__(self, *, cache_dir: Optional[str] = None, exclude_dirs: Optional[Iterable[str]] = None):
        self.exclude_dirs = set(DEFAULT_EXCLUDES) | {str(x) for x in (exclude_dirs or [])}
        cache_base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
        self.cache_dir = Path(cache_dir).expanduser().resolve() if cache_dir else cache_base / "stability-analysis-agent" / "code-evidence-index"
        self._files: Dict[str, Dict[str, Any]] = {}
        self.snapshot = IndexSnapshot()

    def _cache_path(self, roots: Sequence[str]) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{_hash(list(roots))}.json"

    def _load_cache(self, roots: Sequence[str]) -> None:
        path = self._cache_path(roots)
        if path is None or not path.is_file():
            return
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            files = value.get("files") if isinstance(value, dict) else None
            if isinstance(files, dict):
                self._files = {str(k): dict(v) for k, v in files.items() if isinstance(v, dict)}
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

    def _iter_files(self, roots: Sequence[str]) -> List[str]:
        result: List[str] = []
        for raw in roots:
            root = Path(raw).expanduser().resolve()
            if not root.is_dir():
                continue
            for base, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if d not in self.exclude_dirs and not d.startswith(".")]
                for name in names:
                    if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS:
                        result.append(str((Path(base) / name).resolve()))
        return sorted(set(result))

    @staticmethod
    def _parse(path: str) -> Dict[str, Any]:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            stat = Path(path).stat()
        except OSError:
            return {}
        lines = text.splitlines()
        symbols: List[Dict[str, Any]] = []
        pattern = re.compile(r"^\s*(?:[\w:<>,*&~]+\s+)+([A-Za-z_]\w*(?:::\w+)*)\s*(?:<[^>]*>)?\s*\([^;{}]*\)\s*(?:const\s*)?\{")
        for number, line in enumerate(lines, 1):
            match = pattern.search(line)
            if match:
                symbols.append({"name": match.group(1), "line": number})
        return {"path": path, "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))),
                "size": int(stat.st_size), "fingerprint": _hash(text), "text": text,
                "symbols": symbols, "is_test": bool(re.search(r"/(?:test|tests|testing)/", path.lower()))}

    def update(self, code_roots: List[str], revision: Optional[str] = None) -> IndexSnapshot:
        roots = sorted(str(Path(x).expanduser().resolve()) for x in code_roots or [] if Path(str(x)).expanduser().is_dir())
        if not self._files:
            self._load_cache(roots)
        paths = self._iter_files(roots)
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
                try:
                    current_fingerprint = _hash(Path(path).read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    current_fingerprint = ""
                if current_fingerprint and current_fingerprint == old.get("fingerprint"):
                    continue
            parsed = self._parse(path)
            if parsed:
                self._files[path] = parsed
                changed += 1
        rows = [{k: v for k, v in data.items() if k != "text"} for data in self._files.values()]
        fingerprint = _hash({"roots": roots, "files": rows, "revision": revision})
        self.snapshot = IndexSnapshot(roots=roots, revision=revision, fingerprint=fingerprint, files=rows)
        self._save_cache(roots)
        return self.snapshot

    def search(self, query: str, *, mode: str = "full_text", limit: int = 20) -> List[IndexCandidate]:
        terms = {x.lower() for x in re.findall(r"[A-Za-z_]\w*", str(query or "")) if len(x) > 1}
        if not terms:
            return []
        output: List[IndexCandidate] = []
        for path, row in self._files.items():
            text = str(row.get("text") or "")
            lower = text.lower()
            names = {str(x.get("name") or "") for x in row.get("symbols") or []}
            score = 0.0
            reasons: List[str] = []
            if mode in {"symbol", "symbols"}:
                hits = sum(1 for name in names if name.lower() in terms)
                if hits:
                    score += 1.0 + hits * 0.5; reasons.append("symbol_match")
            else:
                hits = sum(lower.count(term) for term in terms)
                if hits:
                    score += min(0.8, hits / 20.0); reasons.append("full_text_match")
                symbol_hits = sum(1 for name in names if name.lower() in terms)
                if symbol_hits:
                    score += 0.8; reasons.append("symbol_match")
            if not score:
                continue
            line = next((i + 1 for i, value in enumerate(text.splitlines()) if any(term in value.lower() for term in terms)), 1)
            snippet = "\n".join(text.splitlines()[max(0, line - 2):line + 8])
            output.append(IndexCandidate(path, "", line, line + 8, snippet, min(1.0, score), reasons, content_fingerprint=str(row.get("fingerprint") or "")))
        output.sort(key=lambda item: (-item.score, item.file, item.line_start))
        return output[:max(1, int(limit))]
