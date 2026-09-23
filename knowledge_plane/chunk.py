"""Knowledge Pack 文档解析与稳定分块。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .api import _parse_frontmatter


_HEADING_RE = re.compile(r"(?m)^(#{1,4})\s+(.+?)\s*$")


def stable_chunk_id(pack_id: str, doc_path: str, heading: str, body: str) -> str:
    seed = "\0".join((pack_id, doc_path, heading, body.strip()))
    return "packchunk_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def chunk_document(
    path: Path,
    *,
    pack_root: Path,
    pack_id: str,
    max_chars: int = 1800,
) -> List[Dict[str, Any]]:
    """按 Markdown 标题分块，过长段落再按字符窗口切分。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    metadata, body = _parse_frontmatter(text)
    matches = list(_HEADING_RE.finditer(body))
    sections = []
    if not matches:
        sections.append((str(metadata.get("title") or path.stem), body))
    else:
        for index, match in enumerate(matches):
            start = match.end()
            stop = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            sections.append((match.group(2).strip(), body[start:stop].strip()))

    result: List[Dict[str, Any]] = []
    relative_path = str(path.relative_to(pack_root))
    for heading, section in sections:
        if not section:
            continue
        windows = [
            section[offset : offset + max_chars]
            for offset in range(0, len(section), max_chars)
        ]
        for window_index, window in enumerate(windows):
            title = heading if len(windows) == 1 else f"{heading} ({window_index + 1})"
            result.append(
                {
                    "chunk_id": stable_chunk_id(pack_id, relative_path, title, window),
                    "pack_id": pack_id,
                    "doc_path": relative_path,
                    "title": title,
                    "body": window.strip(),
                    "metadata": dict(metadata),
                }
            )
    return result


def iter_pack_chunks(
    pack_root: Path,
    manifest: Dict[str, Any],
) -> Iterable[Dict[str, Any]]:
    pack_id = str(manifest.get("id") or "")
    globs = manifest.get("content_globs") or ["**/*.md"]
    if isinstance(globs, str):
        globs = [globs]
    paths = set()
    for pattern in globs:
        paths.update(pack_root.glob(str(pattern)))
    for path in sorted(paths):
        if path.is_file():
            yield from chunk_document(path, pack_root=pack_root, pack_id=pack_id)
