#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码定位（04b code_content_provider）结果的跨运行磁盘缓存。

同一张卡片重复分析时，若每个 code_root 的 git commit 与定位输入都未变化，
04b 的结果是确定性的（rg 扫描 + 调用链展开 + tree-sitter 解析），可直接复用，
省去 ~89s 的重算。

关键正确性约束 —— 每次运行的 worktree 路径不同：
    每次运行会把源码 checkout 到不同的 worktree 目录（runs/<id>/... 或 worktrees/<id>/...），
    因此 code_roots 的绝对路径在运行间不同，而结果里嵌入了 code_root 下的绝对路径。
    直接缓存原始结果会把 RUN1 的（可能已删除的）worktree 路径返回给 RUN2。
    为此：
      - 存储时：把每个 code_root 的绝对路径替换为哨兵 token（\\x00CR0\\x00、\\x00CR1\\x00…，
        按 code_roots 下标）；
      - 加载时：把哨兵 token 替换回「当前运行」的 code_roots[i]。
    替换对结果里所有字符串值做深度遍历（dict/list/str），且按最长前缀优先替换以兼容嵌套 root。
    这样既覆盖路径字段，也覆盖嵌入在文本块里的路径。crash-input 构建路径
    （如 /root/ONLINE_SERVICE/.../engine-dev/...）不是 code_root，运行间稳定，保持不变。

所有缓存 I/O 均以 try/except 兜底：任何缓存异常都退化为重算，绝不让流水线崩溃。
Kill-switch：环境变量 SA_DISABLE_CODE_LOCATION_CACHE 为真值时禁用缓存（默认启用）。
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# 缓存 schema 版本：结果结构/定位算法发生不兼容变化时自增，使旧缓存自然失效。
CACHE_SCHEMA_VERSION = 1

# Kill-switch 环境变量名。
_DISABLE_ENV = "SA_DISABLE_CODE_LOCATION_CACHE"

# 单行路径值的长度上限（超过者视为代码/文本块而非路径，跳过存在性校验）。
_MAX_PATH_LEN = 4096


def _is_disabled() -> bool:
    """缓存是否被 kill-switch 关闭。"""
    return os.environ.get(_DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _sentinel(index: int) -> str:
    """返回第 index 个 code_root 对应的哨兵 token。"""
    return "\x00CR%d\x00" % index


def _git_commit(root: str) -> str:
    """返回 root 的 git HEAD 提交（含 worktree）；非 git 或失败返回空串。"""
    try:
        completed = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    commit = (completed.stdout or "").strip()
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit or "") else ""


def _cache_dir() -> str:
    """缓存目录：${XDG_CACHE_HOME or ~/.cache}/stability-analysis-agent/code-location/。"""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "stability-analysis-agent", "code-location")
    os.makedirs(path, exist_ok=True)
    return path


def _root_path_variants(code_roots: List[str]) -> List[Tuple[str, int]]:
    """每个 code_root 的候选路径字符串（原样 + abspath）到下标的映射，按长度降序。

    长度降序确保嵌套 root（如 /a 与 /a/b）优先匹配更深的那个。
    """
    variants: List[Tuple[str, int]] = []
    for idx, root in enumerate(code_roots):
        seen = set()
        for cand in (str(root), os.path.abspath(str(root))):
            cand = cand.rstrip(os.sep) or cand
            if cand and cand not in seen:
                seen.add(cand)
                variants.append((cand, idx))
    variants.sort(key=lambda it: len(it[0]), reverse=True)
    return variants


def _deep_map_strings(obj: Any, fn) -> Any:
    """深度遍历 dict/list/str，对每个字符串值应用 fn（返回新对象，不改原对象）。"""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_deep_map_strings(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _deep_map_strings(v, fn) for k, v in obj.items()}
    return obj


def _to_sentinels(obj: Any, variants: List[Tuple[str, int]]) -> Any:
    """把结果里所有 code_root 绝对路径替换为哨兵 token（存储前归一化）。"""

    def _repl(s: str) -> str:
        for path, idx in variants:  # 已按长度降序
            if path in s:
                s = s.replace(path, _sentinel(idx))
        return s

    return _deep_map_strings(obj, _repl)


def _from_sentinels(obj: Any, code_roots: List[str]) -> Any:
    """把哨兵 token 替换回当前运行的 code_roots[i]（加载后 rebase）。"""

    def _repl(s: str) -> str:
        if "\x00CR" not in s:
            return s
        for idx, root in enumerate(code_roots):
            token = _sentinel(idx)
            if token in s:
                s = s.replace(token, str(root))
        return s

    return _deep_map_strings(obj, _repl)


def _normalize_input_for_key(determining_input: Any, variants: List[Tuple[str, int]]) -> str:
    """把定位输入序列化为路径无关的字符串：code_root 绝对路径替换为哨兵。"""
    normalized = _to_sentinels(determining_input, variants)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def compute_key(code_roots: List[str], determining_input: Any) -> Optional[str]:
    """计算缓存键；返回 None 表示不缓存（禁用 / 无 root / 任一 root 非 git 提交）。

    键 = sha256(JSON({schema, 排序后的 commit 身份, 路径无关的输入指纹}))[:16]。
    只有全部 root 都能拿到 commit 时才缓存，因为唯有 commit 能保证内容不可变。
    """
    if _is_disabled() or not code_roots:
        return None
    try:
        identities: List[str] = []
        for root in code_roots:
            commit = _git_commit(root)
            if not commit:
                return None  # 任一 root 非 git/无提交 → 放弃缓存（内容不可保证不可变）
            identities.append("commit:%s" % commit)
        identities.sort()
        variants = _root_path_variants(code_roots)
        payload = {
            "schema": CACHE_SCHEMA_VERSION,
            "roots": identities,
            "input": _normalize_input_for_key(determining_input, variants),
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return None


def _collect_located_paths(obj: Any, code_roots: List[str]) -> List[str]:
    """收集结果声称定位到的、位于某个 code_root 下的文件路径（用于存在性抽检）。"""
    prefixes = [str(r).rstrip(os.sep) for r in code_roots if str(r).strip()]
    found: set = set()

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            s = node
            if ("\n" in s) or (len(s) > _MAX_PATH_LEN):
                return
            for pre in prefixes:
                if pre and s.startswith(pre + os.sep):
                    base = os.path.basename(s)
                    if "." in base:  # 看起来像文件而非目录/代码片段
                        found.add(s)
                    break
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)

    _walk(obj)
    return sorted(found)


def load(key: Optional[str], code_roots: List[str]) -> Optional[Any]:
    """按 key 读取缓存并 rebase 到当前 code_roots；校验所定位文件仍存在，否则视为未命中。"""
    if key is None or _is_disabled():
        return None
    try:
        cache_file = os.path.join(_cache_dir(), "%s.json" % key)
        if not os.path.isfile(cache_file):
            return None
        with open(cache_file, "r", encoding="utf-8") as f:
            stored = json.load(f)
        rebased = _from_sentinels(stored, code_roots)
        # 抽检：结果声称定位到的源文件若已不存在（如 worktree 结构变化），视为未命中并重算。
        for path in _collect_located_paths(rebased, code_roots):
            if not os.path.exists(path):
                return None
        return rebased
    except Exception:
        return None


def store(key: Optional[str], code_roots: List[str], result: Any) -> None:
    """把 JSON-safe 结果归一化（code_root 路径→哨兵）后原子写入磁盘；任何异常静默忽略。"""
    if key is None or _is_disabled():
        return
    try:
        variants = _root_path_variants(code_roots)
        snapshot = _to_sentinels(result, variants)
        cache_dir = _cache_dir()
        cache_file = os.path.join(cache_dir, "%s.json" % key)
        fd, tmp_path = tempfile.mkstemp(prefix=".%s." % key, suffix=".tmp", dir=cache_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            os.replace(tmp_path, cache_file)  # 原子替换
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except Exception:
        return
