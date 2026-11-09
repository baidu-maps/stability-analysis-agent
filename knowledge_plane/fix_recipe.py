"""知识域确定性修复配方（fix recipe）。

命中某个 crash-pattern 的 ``*.fix.json`` 配方后，直接产出精确的 ``old_text→new_text``
edit，不依赖大模型自行推理补丁。配方只做"精确文本替换"（edit_type=member_declaration），
且必须在候选源文件里能精确定位 ``old_text`` 才会产出，避免误改。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from knowledge_plane.api import resolve_pack_dirs


def _load_recipes(pack_dirs: Sequence[Path]) -> List[Dict[str, Any]]:
    """加载 pack 目录下全部 ``*.fix.json`` 配方。"""
    recipes: List[Dict[str, Any]] = []
    for d in pack_dirs:
        try:
            files = list(Path(d).rglob("*.fix.json"))
        except OSError:
            continue
        for fp in files:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("edits"), list):
                recipes.append(data)
    return recipes


def _find_target_file(
    suffix: str,
    old: str,
    files: Sequence[str],
    search_roots: Sequence[str],
) -> Optional[str]:
    """先在候选文件里找含 ``old`` 的文件，找不到再按 ``root/suffix`` 精确拼路径。

    配方要改的文件（如任务组定义所在头文件）常不在崩溃帧上，不一定进入代码上下文；
    这里只做精确拼接，不遍历仓库。
    """
    file_basename = suffix.rsplit("/", 1)[-1]
    for f in files:
        nf = f.replace("\\", "/")
        if not nf.endswith(suffix) and not nf.endswith(file_basename):
            continue
        try:
            content = Path(f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if old in content:
            return f
    for root in search_roots:
        candidate = Path(str(root)) / suffix
        try:
            if candidate.is_file() and old in candidate.read_text(encoding="utf-8", errors="ignore"):
                return str(candidate)
        except OSError:
            continue
    return None


def collect_recipe_edits(
    trigger_text: str,
    candidate_files: Sequence[str],
    pack_dirs: Optional[Sequence[str]] = None,
    search_roots: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """返回命中配方、且能在候选文件中精确定位 old_text 的 member_declaration edits。

    - ``trigger_text``：路由文本（分析文本 + 候选文件路径），用于 match.require_all。
    - ``candidate_files``：本次代码上下文里的真实源文件绝对路径列表。
    - ``search_roots``：代码根目录；候选文件里找不到时按 ``root/file_suffix`` 精确定位。
    - 只有 ``old_text`` 确实出现在目标文件里才产出对应 edit。
    - 配方声明 ``exclusive: true`` 时，产出的 edit 带 ``exclusive`` 标记，
      调用方应只落盘配方 edit，丢弃大模型自行推理的其它改动。
    """
    text = str(trigger_text or "")
    files = [str(f) for f in (candidate_files or []) if str(f).strip()]
    roots = [str(r) for r in (search_roots or []) if str(r).strip()]
    if not text or not (files or roots):
        return []
    try:
        dirs = resolve_pack_dirs(pack_dirs)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for recipe in _load_recipes(dirs):
        match = recipe.get("match") or {}
        require_all = match.get("require_all") or []
        if require_all and not all(str(tok) in text for tok in require_all):
            continue
        exclusive = bool(recipe.get("exclusive"))
        for edit in recipe.get("edits") or []:
            if not isinstance(edit, dict):
                continue
            suffix = str(edit.get("file_suffix") or "").strip().replace("\\", "/")
            old = str(edit.get("old_text") or "")
            new = str(edit.get("new_text") or "")
            if not suffix or not old or not new or old == new:
                continue
            target = _find_target_file(suffix, old, files, roots)
            if not target:
                continue
            key = f"{target}::{old}"
            if key in seen:
                continue
            seen.add(key)
            item = {
                "file": target,
                "edit_type": "member_declaration",
                "old_text": old,
                "new_text": new,
                "reason": str(edit.get("reason") or "knowledge fix recipe"),
                "source": "knowledge_fix_recipe",
            }
            if exclusive:
                item["exclusive"] = True
            out.append(item)
    return out
