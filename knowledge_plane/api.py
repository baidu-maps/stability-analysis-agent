"""Codebase Knowledge Pack 的加载、路由与轻量混合检索。

Pack 文件是知识源，关键词/符号索引用于精确召回；现有 RAG 继续负责规则与
历史案例。两类命中最终由 workflow 合并成同一个 memory_context。
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_:.$<>~-]{2,}")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_DEFAULT_MAX_HITS = 4
_DEFAULT_MAX_CHARS = 4500


def _split_paths(value: str) -> List[Path]:
    return [
        Path(item).expanduser().resolve()
        for item in value.split(os.pathsep)
        if item.strip()
    ]


def resolve_pack_dirs(explicit_dirs: Optional[Sequence[str]] = None) -> List[Path]:
    """返回 Pack 搜索目录；显式配置优先，并追加环境变量目录。"""
    paths: List[Path] = []
    for item in explicit_dirs or []:
        if str(item).strip():
            paths.append(Path(item).expanduser().resolve())
    paths.extend(
        _split_paths(os.environ.get("STABILITY_AGENT_KNOWLEDGE_PACK_DIRS", ""))
    )
    seen = set()
    result: List[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen and path.is_dir():
            seen.add(key)
            result.append(path)
    return result


def _load_mapping(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore

        value = yaml.safe_load(text)
        return value if isinstance(value, dict) else {}
    except (ImportError, Exception):
        return {}


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        if value.startswith("[") and value.endswith("]"):
            return [
                item.strip().strip("\"'")
                for item in value[1:-1].split(",")
                if item.strip()
            ]
        return value.strip("\"'")


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    metadata: Dict[str, Any] = {}
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#") or ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        metadata[key.strip()] = _parse_scalar(value)
    return metadata, text[match.end() :]


def _collect_query_text(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    code_context: Dict[str, Any],
) -> str:
    """从解析/符号化/源码上下文抽取检索 query。

    真实 add2line 结果是 ``resolved_threads[].frames[]``，深度需覆盖到帧字段
    （``resolved_function`` / ``module`` 等）；过浅会导致 Pack 路由永远 0 分。
    """
    values: List[str] = []
    # resolved_threads(1) -> thread(2) -> frames(3) -> frame(4) -> field(5)
    max_depth = 8

    def add_selected(obj: Any, depth: int = 0) -> None:
        if depth > max_depth:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_text = str(key).lower()
                if any(
                    marker in key_text
                    for marker in (
                        "function",
                        "symbol",
                        "module",
                        "library",
                        "image",
                        "signal",
                        "reason",
                        "exception",
                        "file",
                        "thread",
                    )
                ):
                    add_selected(value, depth + 1)
                elif key_text in {
                    "resolved_frames",
                    "resolved_threads",
                    "frames",
                    "inline_frames",
                    "crashed_thread",
                }:
                    add_selected(value, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:40]:
                add_selected(item, depth + 1)
        elif isinstance(obj, bool):
            # bool 是 int 子类；True/False 不能进入 query，否则污染 SO/符号匹配。
            return
        elif isinstance(obj, (str, int, float)):
            text = str(obj).strip()
            if text:
                values.append(text)

    add_selected(parse_result)
    add_selected(resolved_stack)
    add_selected(code_context)
    return "\n".join(values)


def _normalized_terms(text: str) -> List[str]:
    terms: List[str] = []
    seen = set()
    for term in _WORD_RE.findall(text):
        lowered = term.lower()
        if lowered not in seen:
            seen.add(lowered)
            terms.append(lowered)
    return terms


def _iter_pack_manifests(pack_dirs: Iterable[Path]) -> Iterable[Tuple[Path, Dict[str, Any]]]:
    for root in pack_dirs:
        candidates = [root / "pack.yaml", root / "pack.json"]
        candidates.extend(root.glob("*/pack.yaml"))
        candidates.extend(root.glob("*/pack.json"))
        seen = set()
        for manifest_path in candidates:
            if manifest_path in seen or not manifest_path.is_file():
                continue
            seen.add(manifest_path)
            manifest = _load_mapping(manifest_path)
            if manifest.get("id"):
                yield manifest_path.parent, manifest


def _match_pack(manifest: Dict[str, Any], query_lower: str) -> Tuple[float, List[str]]:
    match = manifest.get("match") if isinstance(manifest.get("match"), dict) else {}
    score = 0.0
    reasons: List[str] = []
    for field, weight in (
        ("so_names", 4.0),
        ("package_prefixes", 3.0),
        ("stack_hints", 5.0),
        ("path_hints", 2.0),
    ):
        values = match.get(field) or []
        if isinstance(values, str):
            values = [values]
        for raw in values:
            needle = str(raw).strip().lower().replace("*", "")
            if needle and needle in query_lower:
                score += weight
                reasons.append(f"{field}:{raw}")
    return score, reasons


def _document_score(
    metadata: Dict[str, Any],
    body: str,
    query_lower: str,
    terms: Sequence[str],
) -> Tuple[float, List[str]]:
    searchable = "\n".join(
        [
            str(metadata.get("title") or ""),
            str(metadata.get("domain") or ""),
            str(metadata.get("module") or ""),
            str(metadata.get("symbols") or ""),
            body,
        ]
    ).lower()
    score = 0.0
    matched: List[str] = []
    symbols = metadata.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    for symbol in symbols:
        needle = str(symbol).strip().lower()
        if needle and needle in query_lower:
            score += 8.0
            matched.append(str(symbol))
    for term in terms:
        if len(term) < 4 or term not in searchable:
            continue
        score += 1.5 if "::" in term or term.startswith("lib") else 0.5
        if len(matched) < 8:
            matched.append(term)
    return score, matched


def _excerpt(body: str, matched: Sequence[str], max_chars: int = 900) -> str:
    compact = "\n".join(line.rstrip() for line in body.strip().splitlines())
    lowered = compact.lower()
    positions = [
        lowered.find(str(term).lower())
        for term in matched
        if str(term).strip() and lowered.find(str(term).lower()) >= 0
    ]
    start = max(0, (min(positions) if positions else 0) - 180)
    excerpt = compact[start : start + max_chars].strip()
    if start > 0:
        excerpt = "…" + excerpt
    if start + max_chars < len(compact):
        excerpt += "…"
    return excerpt


def retrieve_knowledge(
    *,
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    code_context: Dict[str, Any],
    pack_dirs: Optional[Sequence[str]] = None,
    pack_id: str = "",
    max_hits: int = _DEFAULT_MAX_HITS,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> Dict[str, Any]:
    """路由到最相关的代码库 Pack，并返回可与现有 RAG 合并的命中。"""
    roots = resolve_pack_dirs(pack_dirs)
    base: Dict[str, Any] = {
        "success": True,
        "skipped": False,
        "skip_reason": None,
        "pack_id": None,
        "route_reason": [],
        "hits": [],
        "knowledge_context": "",
        "decision_trace": [],
    }
    if not roots:
        base.update(skipped=True, skip_reason="knowledge_pack_dirs_not_configured")
        return base

    query = _collect_query_text(parse_result, resolved_stack, code_context)
    query_lower = query.lower()
    candidates = []
    for pack_root, manifest in _iter_pack_manifests(roots):
        score, reasons = _match_pack(manifest, query_lower)
        if pack_id and str(manifest.get("id")) == pack_id:
            score += 100.0
            reasons.insert(0, f"forced:{pack_id}")
        candidates.append((score, pack_root, manifest, reasons))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates or candidates[0][0] <= 0:
        base.update(skipped=True, skip_reason="no_matching_knowledge_pack")
        return base

    route_score, pack_root, manifest, route_reason = candidates[0]
    terms = _normalized_terms(query)
    hits: List[Dict[str, Any]] = []
    content_globs = manifest.get("content_globs") or ["**/*.md"]
    if isinstance(content_globs, str):
        content_globs = [content_globs]
    paths = set()
    for pattern in content_globs:
        paths.update(pack_root.glob(str(pattern)))
    for path in sorted(paths):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        metadata, body = _parse_frontmatter(text)
        score, matched = _document_score(metadata, body, query_lower, terms)
        if score <= 0:
            continue
        hits.append(
            {
                "layer": str(metadata.get("kb_layer") or "code"),
                "backend": "pack_document",
                "pack_id": str(manifest.get("id")),
                "path": str(path.relative_to(pack_root)),
                "title": str(metadata.get("title") or path.stem),
                "confidence": str(metadata.get("confidence") or "medium"),
                "provenance": str(metadata.get("source_type") or "curated_playbook"),
                "score": round(score, 3),
                "matched_terms": matched,
                "excerpt": _excerpt(body, matched),
                "metadata": metadata,
            }
        )
    hits.sort(key=lambda item: float(item["score"]), reverse=True)
    selected: List[Dict[str, Any]] = []
    used_chars = 0
    for hit in hits:
        excerpt = str(hit["excerpt"])
        if selected and used_chars + len(excerpt) > max(500, int(max_chars)):
            break
        selected.append(hit)
        used_chars += len(excerpt)
        if len(selected) >= max(1, int(max_hits)):
            break

    lines: List[str] = []
    if selected:
        lines.append(f"代码库领域知识（{manifest.get('display_name') or manifest.get('id')}）:")
        for hit in selected:
            lines.append(
                f"- [{hit['confidence']}] {hit['title']}（{hit['path']}）\n"
                f"  {hit['excerpt']}"
            )
    base.update(
        pack_id=str(manifest.get("id")),
        route_reason=route_reason,
        hits=selected,
        knowledge_context="\n".join(lines).strip(),
        decision_trace=[
            {
                "stage": "knowledge_pack_route",
                "pack_id": str(manifest.get("id")),
                "score": route_score,
                "reasons": route_reason,
            },
            {
                "stage": "knowledge_pack_retrieve",
                "candidate_count": len(hits),
                "selected_count": len(selected),
                "budget_chars": max_chars,
            },
        ],
    )
    return base
