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


def resolve_pack_dirs(
    explicit_dirs: Optional[Sequence[str]] = None,
    *,
    include_env: bool = True,
) -> List[Path]:
    """返回 Pack 搜索目录；显式配置优先，并追加环境变量目录（``include_env=False`` 时不追加）。"""
    paths: List[Path] = []
    for item in explicit_dirs or []:
        if str(item).strip():
            paths.append(Path(item).expanduser().resolve())
    if include_env:
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


def _frontmatter_symbols(front: str) -> List[str]:
    """抓取 frontmatter 里 symbols 列表（支持行内与跨行 [a, b, ...]）。"""
    m = re.search(r"symbols:\s*\[(.*?)\]", front, re.DOTALL)
    if m:
        return [p.strip().strip("\"'") for p in m.group(1).split(",") if p.strip()]
    for line in front.splitlines():
        if line.strip().startswith("symbols:"):
            raw = line.split(":", 1)[1].strip()
            val = _parse_scalar(raw) if raw else []
            if isinstance(val, list):
                return [str(x).strip() for x in val if str(x).strip()]
    return []


def collect_seed_symbols(
    stack_text: str,
    explicit_dirs: Optional[Sequence[str]] = None,
    *,
    max_symbols: int = 40,
) -> List[str]:
    """返回与崩溃栈相关的 Pack 文档 frontmatter symbols 并集。

    某文档只要有一个 symbol 出现在栈文本里即视为相关，再把它声明的全部 symbols
    （含自由函数/全局变量，如 GetShaderValue、g_strShaderVert）作为代码定位种子，
    用于把这些定义文件引入上下文——它们通常不在崩溃帧上。
    """
    stack = str(stack_text or "")
    if not stack.strip():
        return []
    out: List[str] = []
    seen: set = set()
    try:
        pack_dirs = resolve_pack_dirs(explicit_dirs)
    except Exception:
        return []
    for pack_dir in pack_dirs:
        try:
            md_files = list(pack_dir.rglob("*.md"))
        except OSError:
            continue
        for md in md_files:
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            fm = _FRONTMATTER_RE.match(text)
            if not fm:
                continue
            symbols = _frontmatter_symbols(fm.group(1))
            if not symbols or not any(s and s in stack for s in symbols):
                continue
            for s in symbols:
                s = str(s).strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
                    if len(out) >= max_symbols:
                        return out
    return out


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    metadata: Dict[str, Any] = {}
    pending_key = ""
    pending_value = ""
    for raw_line in match.group(1).splitlines():
        # 跨行列表 ``key: [a, b,`` / ``  c]``：拼到右括号闭合再解析。
        if pending_key:
            pending_value += " " + raw_line.strip()
            if pending_value.count("[") <= pending_value.count("]"):
                metadata[pending_key] = _parse_scalar(pending_value)
                pending_key, pending_value = "", ""
            continue
        if not raw_line.strip() or raw_line.lstrip().startswith("#") or ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        value = value.strip()
        if value.startswith("[") and value.count("[") > value.count("]"):
            pending_key, pending_value = key.strip(), value
            continue
        metadata[key.strip()] = _parse_scalar(value)
    if pending_key:
        metadata[pending_key] = _parse_scalar(pending_value + "]")
    return metadata, text[match.end() :]


def _as_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _commit_in_checkout(commit: str, roots: Sequence[str]) -> bool:
    """``commit`` 是否已包含在某个代码根（含其子模块）的当前检出里。"""
    import subprocess

    for root in roots:
        candidates = [Path(root)]
        try:
            candidates.extend(p.parent for p in Path(root).glob("*/.git"))
        except OSError:
            pass
        for repo in candidates:
            try:
                done = subprocess.run(
                    ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "HEAD"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if done.returncode == 0:
                return True
    return False


def upstream_fixed_commits(metadata: Dict[str, Any], code_roots: Sequence[str]) -> List[str]:
    """返回 frontmatter ``fixed_in`` 中已包含在当前检出的提交。"""
    roots = [str(r) for r in (code_roots or []) if str(r).strip()]
    if not roots:
        return []
    return [c for c in _as_list(metadata.get("fixed_in")) if _commit_in_checkout(c, roots)]


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


def _group_packs(
    pack_dirs: Iterable[Path],
) -> List[Tuple[List[Tuple[Path, Dict[str, Any]]], Dict[str, Any]]]:
    """按 ``id`` 合并分布在多个目录里的同名 Pack。

    随包发布的 Pack 与运行时沉淀的 Pack 可以同 id 共存；合并后一起参与路由和检索。
    展示名、预算等取第一个出现的清单，``match`` 各字段取并集。
    """
    groups: Dict[str, List[Tuple[Path, Dict[str, Any]]]] = {}
    for pack_root, manifest in _iter_pack_manifests(pack_dirs):
        members = groups.setdefault(str(manifest.get("id")), [])
        if all(pack_root != root for root, _ in members):
            members.append((pack_root, manifest))
    result = []
    for members in groups.values():
        merged = dict(members[0][1])
        match: Dict[str, List[Any]] = {}
        for _, manifest in members:
            raw = manifest.get("match") if isinstance(manifest.get("match"), dict) else {}
            for field, values in raw.items():
                items = [values] if isinstance(values, str) else list(values or [])
                bucket = match.setdefault(field, [])
                bucket.extend(item for item in items if item not in bucket)
        merged["match"] = match
        result.append((members, merged))
    return result


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


def _excerpt(
    body: str,
    matched: Sequence[str],
    max_chars: int = 900,
    *,
    from_start: bool = False,
) -> str:
    """截取文档片段。Playbook 从文首取，避免最小修复约束落在 900 字窗口外。"""
    compact = "\n".join(line.rstrip() for line in body.strip().splitlines())
    limit = max(200, int(max_chars or 900))
    if from_start or not matched:
        start = 0
    else:
        lowered = compact.lower()
        positions = [
            lowered.find(str(term).lower())
            for term in matched
            if str(term).strip() and lowered.find(str(term).lower()) >= 0
        ]
        start = max(0, (min(positions) if positions else 0) - 180)
    excerpt = compact[start : start + limit].strip()
    if start > 0:
        excerpt = "…" + excerpt
    if start + limit < len(compact):
        excerpt += "…"
    return excerpt


CANDIDATE_DIRS_ENV = "STABILITY_AGENT_KNOWLEDGE_CANDIDATE_DIRS"
USAGE_STATS_ENV = "STABILITY_AGENT_KNOWLEDGE_STATS"
_INACTIVE_STATUSES = {"demoted", "retired"}


def _load_usage_stats() -> Dict[str, Dict[str, Any]]:
    """读取 ``memory_id -> {hits, adopted, rejected}`` 统计；未配置或损坏时返回空。"""
    path = os.environ.get(USAGE_STATS_ENV, "").strip()
    if not path:
        return {}
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _usage_factor(stats: Dict[str, Any]) -> float:
    """采纳多的文档小幅加权，拒绝多于采纳的降权；只做同分附近的调整。"""
    adopted = int(stats.get("adopted") or 0)
    rejected = int(stats.get("rejected") or 0)
    factor = 1.0 + 0.05 * min(adopted, 6)
    if rejected > adopted:
        factor *= 0.6
    return factor


def _related_members(
    groups: Sequence[Tuple[List[Tuple[Path, Dict[str, Any]]], Dict[str, Any]]],
    manifest: Dict[str, Any],
) -> List[Tuple[str, Path, Dict[str, Any]]]:
    """主包加 ``related_packs`` 关联包（双向）的全部目录，带各自的 pack id。

    主仓库与子模块常编进同一个 so，路由只能选中一个包时，子模块知识会丢；关联包一起检索。
    """
    top_id = str(manifest.get("id"))
    related = set(_as_list(manifest.get("related_packs")))
    result: List[Tuple[str, Path, Dict[str, Any]]] = []
    for members, merged in groups:
        pack = str(merged.get("id"))
        if pack == top_id or pack in related or top_id in _as_list(merged.get("related_packs")):
            ordered = 0 if pack == top_id else 1
            result.extend((pack, root, item, ordered) for root, item in members)
    result.sort(key=lambda item: item[3])
    return [(pack, root, item) for pack, root, item, _ in result]


def retrieve_knowledge(
    *,
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    code_context: Dict[str, Any],
    pack_dirs: Optional[Sequence[str]] = None,
    pack_id: str = "",
    max_hits: int = _DEFAULT_MAX_HITS,
    max_chars: int = _DEFAULT_MAX_CHARS,
    code_roots: Optional[Sequence[str]] = None,
    query_terms: Optional[Sequence[str]] = None,
    include_env_dirs: bool = True,
    candidate_fallback: bool = True,
) -> Dict[str, Any]:
    """路由到最相关的代码库 Pack，并返回可与现有 RAG 合并的命中。

    ``code_roots`` 给出时，文档 frontmatter ``fixed_in`` 里的提交若已包含在当前检出，
    该文档降权并在摘录前注明"上游已修复"，避免对已修复分支重复推理和改码。
    ``query_terms`` 追加到检索词（推理中按函数名再查一次时用）。
    frontmatter ``status`` 为 demoted / retired 的文档不参与。主包没有命中时，
    若配置了 ``STABILITY_AGENT_KNOWLEDGE_CANDIDATE_DIRS``，再到候选池查一次并标为“未准入候选”。
    """
    roots = resolve_pack_dirs(pack_dirs, include_env=include_env_dirs)
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
    fallback_kwargs = dict(
        parse_result=parse_result, resolved_stack=resolved_stack, code_context=code_context,
        pack_id=pack_id, max_hits=max_hits, max_chars=max_chars, code_roots=code_roots,
        query_terms=query_terms,
    )
    if not roots:
        base.update(skipped=True, skip_reason="knowledge_pack_dirs_not_configured")
        return _candidate_fallback(base, fallback_kwargs) if candidate_fallback else base

    query = _collect_query_text(parse_result, resolved_stack, code_context)
    if query_terms:
        query = "\n".join([query, *[str(t) for t in query_terms if str(t).strip()]])
    query_lower = query.lower()
    groups = _group_packs(roots)
    candidates = []
    for members, manifest in groups:
        score, reasons = _match_pack(manifest, query_lower)
        if pack_id and str(manifest.get("id")) == pack_id:
            score += 100.0
            reasons.insert(0, f"forced:{pack_id}")
        candidates.append((score, members, manifest, reasons))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates or candidates[0][0] <= 0:
        base.update(skipped=True, skip_reason="no_matching_knowledge_pack")
        return _candidate_fallback(base, fallback_kwargs) if candidate_fallback else base

    route_score, _members, manifest, route_reason = candidates[0]
    members = _related_members(groups, manifest)
    usage = _load_usage_stats()
    terms = _normalized_terms(query)
    hits: List[Dict[str, Any]] = []
    documents: Dict[Tuple[str, str], Tuple[Path, Path]] = {}
    for member_pack, pack_root, member_manifest in members:
        content_globs = member_manifest.get("content_globs") or ["**/*.md"]
        if isinstance(content_globs, str):
            content_globs = [content_globs]
        for pattern in content_globs:
            for path in pack_root.glob(str(pattern)):
                # 同 id 多目录里相对路径相同的文档只取先出现的一份。
                key = (member_pack, str(path.relative_to(pack_root)))
                documents.setdefault(key, (pack_root, path))
    for key in sorted(documents):
        member_pack, rel_path = key
        pack_root, path = documents[key]
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        metadata, body = _parse_frontmatter(text)
        if str(metadata.get("status") or "").lower() in _INACTIVE_STATUSES:
            continue
        score, matched = _document_score(metadata, body, query_lower, terms)
        if score <= 0:
            continue
        score *= _usage_factor(usage.get(f"{member_pack}/{rel_path}") or {})
        fixed_commits = upstream_fixed_commits(metadata, code_roots or [])
        if fixed_commits:
            score *= 0.3
        provenance = str(metadata.get("source_type") or "curated_playbook")
        pack_budget = manifest.get("prompt_budget") if isinstance(manifest.get("prompt_budget"), dict) else {}
        excerpt_budget = int(pack_budget.get("max_chars") or max_chars or _DEFAULT_MAX_CHARS)
        excerpt = _excerpt(
            body,
            matched,
            max_chars=excerpt_budget,
            from_start=provenance == "curated_playbook",
        )
        if fixed_commits:
            excerpt = (
                f"（当前检出已包含上游修复提交 {', '.join(fixed_commits)}：先核对该修复是否生效，"
                "不要按本文再次改码。）\n" + excerpt
            )
        hits.append(
            {
                "layer": str(metadata.get("kb_layer") or "code"),
                "backend": "pack_document",
                "pack_id": member_pack,
                "memory_id": f"{member_pack}/{rel_path}",
                "path": rel_path,
                "title": str(metadata.get("title") or path.stem),
                "confidence": str(metadata.get("confidence") or "medium"),
                "provenance": provenance,
                "score": round(score, 3),
                "matched_terms": matched,
                "upstream_fixed": fixed_commits,
                "excerpt": excerpt,
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
                "pack_roots": [str(root) for _, root, _ in members],
            },
            {
                "stage": "knowledge_pack_retrieve",
                "candidate_count": len(hits),
                "selected_count": len(selected),
                "budget_chars": max_chars,
            },
        ],
    )
    if not selected and candidate_fallback:
        return _candidate_fallback(base, fallback_kwargs)
    return base


def _candidate_fallback(base: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """记忆没命中时到候选池再查一次；命中标为“未准入候选”，置信度降为 low。"""
    dirs = [str(p) for p in _split_paths(os.environ.get(CANDIDATE_DIRS_ENV, ""))]
    if not dirs:
        return base
    found = retrieve_knowledge(pack_dirs=dirs, include_env_dirs=False, candidate_fallback=False, **kwargs)
    if not found.get("hits"):
        return base
    for hit in found["hits"]:
        hit["provenance"] = "candidate"
        hit["confidence"] = "low"
        hit["title"] = f"（未准入候选）{hit['title']}"
    lines = ["候选知识（尚未准入记忆，仅供参考，结论需结合本次代码核实）:"]
    for hit in found["hits"]:
        lines.append(f"- {hit['title']}（{hit['path']}）\n  {hit['excerpt']}")
    found["knowledge_context"] = "\n".join(lines)
    found["candidate_fallback"] = True
    found["decision_trace"] = list(base.get("decision_trace") or []) + list(found.get("decision_trace") or [])
    return found
