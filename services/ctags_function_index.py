#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ctags-backed function name -> (file, line) index to avoid full-repo os.walk."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CTAGS_CANDIDATES = ("universal-ctags", "u-ctags", "ctags", "exuberant-ctags", "etags")
_DEFAULT_EXCLUDE = {
    ".git",
    "build",
    "out",
    "third_party",
    "third-party",
    "node_modules",
    ".crash_agent",
}
_SUPPORTED_SOURCE_EXTENSIONS = {
    ".c",
    ".cpp",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".hxx",
    ".m",
    ".mm",
}


class CtagsFunctionIndex:
    """In-memory function index built from ctags -R output."""

    def __init__(self, code_roots: List[str]) -> None:
        roots: List[str] = []
        seen = set()
        for raw in code_roots or []:
            if not raw:
                continue
            ap = os.path.abspath(raw)
            if ap in seen or not os.path.isdir(ap):
                continue
            seen.add(ap)
            roots.append(ap)
        self.code_roots = roots
        self._ready = False
        self._name_to_locs: Dict[str, List[Tuple[str, int]]] = {}
        self._build_sync()

    @staticmethod
    def _find_ctags_binary() -> Optional[str]:
        for name in _CTAGS_CANDIDATES:
            path = shutil.which(name)
            if path:
                return path
        return None

    @staticmethod
    def _is_universal_ctags(ctags: str) -> bool:
        try:
            completed = subprocess.run(
                [ctags, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return False
        text = f"{completed.stdout}\n{completed.stderr}".lower()
        return "universal ctags" in text or "exuberant ctags" in text

    def _collect_source_files(self) -> List[str]:
        files: List[str] = []
        seen = set()
        for root in self.code_roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _DEFAULT_EXCLUDE]
                for filename in filenames:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in _SUPPORTED_SOURCE_EXTENSIONS:
                        continue
                    path = os.path.join(dirpath, filename)
                    try:
                        ap = os.path.abspath(path)
                    except OSError:
                        ap = path
                    if ap in seen:
                        continue
                    seen.add(ap)
                    files.append(ap)
        return files

    @staticmethod
    def _resolve_line_from_tag(file_path: str, pattern: str, name: str) -> Optional[int]:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                lines = handle.readlines()
        except OSError:
            return None
        if pattern.startswith("/") and pattern.endswith("/"):
            body = pattern[1:-1].rstrip("$").lstrip("^")
            body = body.replace("\\/", "/")
            if body:
                for idx, line in enumerate(lines, 1):
                    if body in line:
                        return idx
        for idx, line in enumerate(lines, 1):
            if re.search(rf"\b{re.escape(name)}\s*[\(<]", line):
                return idx
        return None

    def _run_ctags_batches(self, ctags: str, tags_path: str, files: List[str]) -> bool:
        if not files:
            return False
        batch_size = 400
        temp_parts: List[str] = []
        for offset in range(0, len(files), batch_size):
            batch = files[offset : offset + batch_size]
            part_path = f"{tags_path}.part{offset // batch_size}"
            cmd = [ctags, "-f", part_path, *batch]
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                logger.warning(
                    "ctags 批次构建失败（code=%s）: %s",
                    completed.returncode,
                    (completed.stderr or completed.stdout or "").strip()[:200],
                )
                return False
            temp_parts.append(part_path)
        with open(tags_path, "w", encoding="utf-8") as out:
            for part_path in temp_parts:
                try:
                    with open(part_path, "r", encoding="utf-8", errors="ignore") as part:
                        out.write(part.read())
                finally:
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
        return True

    def _build_with_universal_ctags(self, ctags: str, tags_path: str) -> bool:
        cmd = [
            ctags,
            "-R",
            "--fields=+n",
            "--languages=C,C++,Objective-C",
            "-f",
            tags_path,
        ]
        for ex in sorted(_DEFAULT_EXCLUDE):
            cmd.extend(["--exclude", ex])
        cmd.extend(self.code_roots)
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            logger.warning(
                "Universal ctags 索引构建失败（code=%s）: %s",
                completed.returncode,
                (completed.stderr or completed.stdout or "").strip()[:200],
            )
            return False
        return True

    def _build_with_legacy_ctags(self, ctags: str, tags_path: str) -> bool:
        files = self._collect_source_files()
        logger.info("Legacy/BSD ctags：将扫描 %d 个源文件", len(files))
        return self._run_ctags_batches(ctags, tags_path, files)

    @staticmethod
    def _cache_dir() -> str:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        path = os.path.join(base, "stability-analysis-agent", "ctags")
        os.makedirs(path, exist_ok=True)
        return path

    def _cache_key(self) -> str:
        payload = json.dumps(sorted(self.code_roots), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _cache_paths(self) -> Tuple[str, str]:
        key = self._cache_key()
        cache_dir = self._cache_dir()
        return (
            os.path.join(cache_dir, f"{key}.tags"),
            os.path.join(cache_dir, f"{key}.meta.json"),
        )

    def _index_cache_path(self) -> str:
        """已解析索引的 JSON 缓存路径（避免每次加载都 resolve 行号）。"""
        key = self._cache_key()
        cache_dir = self._cache_dir()
        return os.path.join(cache_dir, f"{key}.index.json")

    def _load_from_cache(self) -> bool:
        tags_path, meta_path = self._cache_paths()
        if not os.path.isfile(tags_path) or not os.path.isfile(meta_path):
            return False
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("roots") != self.code_roots:
                return False
            newest_root_mtime = max(os.path.getmtime(r) for r in self.code_roots)
            if float(meta.get("built_at", 0)) < newest_root_mtime:
                return False
            # 优先从已解析索引 JSON 加载（跳过 _resolve_line_from_tag）
            index_path = self._index_cache_path()
            if os.path.isfile(index_path):
                idx_mtime = os.path.getmtime(index_path)
                tags_mtime = os.path.getmtime(tags_path)
                if idx_mtime >= tags_mtime:
                    if self._load_from_index_json(index_path):
                        return True
            # fallback: 解析原始 tags 文件（慢路径）
            with open(tags_path, "r", encoding="utf-8", errors="ignore") as f:
                self._parse_tags_stream(f)
            # 保存已解析索引以加速下次加载
            self._save_index_json()
            self._ready = True
            logger.info(
                "已从缓存加载 ctags 函数索引（%d 符号，roots=%s）",
                len(self._name_to_locs),
                len(self.code_roots),
            )
            return True
        except Exception as exc:
            logger.debug("加载 ctags 缓存失败: %s", exc)
            return False

    def _load_from_index_json(self, index_path: str) -> bool:
        """从已解析的 JSON 索引直接加载（快路径，跳过 _resolve_line_from_tag）。"""
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            mapping: Dict[str, List[Tuple[str, int]]] = {}
            for name, locs in data.items():
                mapping[name] = [(loc[0], loc[1]) for loc in locs]
            self._name_to_locs = mapping
            self._ready = True
            logger.info(
                "已从索引 JSON 快速加载 ctags 函数索引（%d 符号，roots=%s）",
                len(self._name_to_locs),
                len(self.code_roots),
            )
            return True
        except Exception as exc:
            logger.debug("加载索引 JSON 失败: %s", exc)
            return False

    def _save_index_json(self) -> None:
        """将已解析的索引保存为 JSON，下次加载可跳过 _resolve_line_from_tag。"""
        index_path = self._index_cache_path()
        try:
            data = {name: locs for name, locs in self._name_to_locs.items()}
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:
            logger.debug("保存索引 JSON 失败: %s", exc)

    def _save_cache(self, tags_text: str) -> None:
        tags_path, meta_path = self._cache_paths()
        try:
            with open(tags_path, "w", encoding="utf-8") as f:
                f.write(tags_text)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"roots": self.code_roots, "built_at": time.time()},
                    f,
                    ensure_ascii=False,
                )
        except Exception as exc:
            logger.debug("保存 ctags 缓存失败: %s", exc)

    def _build_sync(self) -> None:
        if not self.code_roots:
            self._ready = True
            return
        if self._load_from_cache():
            return
        ctags = self._find_ctags_binary()
        if not ctags:
            logger.info("未找到 ctags 可执行文件，函数定位将回退全仓扫描")
            self._ready = True
            return
        tags_path, _ = self._cache_paths()
        try:
            if self._is_universal_ctags(ctags):
                ok = self._build_with_universal_ctags(ctags, tags_path)
            else:
                ok = self._build_with_legacy_ctags(ctags, tags_path)
            if not ok:
                self._ready = True
                return
            with open(tags_path, "r", encoding="utf-8", errors="ignore") as handle:
                tags_text = handle.read()
            self._parse_tags_stream(tags_text.splitlines())
            self._save_cache(tags_text)
            self._save_index_json()  # 保存已解析索引，下次加载跳过 resolve
            self._ready = True
            logger.info(
                "ctags 函数索引构建完成（%d 符号，roots=%s，ctags=%s）",
                len(self._name_to_locs),
                len(self.code_roots),
                ctags,
            )
        except subprocess.TimeoutExpired:
            logger.warning("ctags 索引构建超时（180s），函数定位将回退全仓扫描")
            self._ready = True
        except Exception as exc:
            logger.warning("ctags 索引构建异常: %s", exc)
            self._ready = True

    def _parse_tags_stream(self, lines) -> None:
        mapping: Dict[str, List[Tuple[str, int]]] = {}
        for raw in lines:
            line = raw.strip() if isinstance(raw, str) else str(raw).strip()
            if not line or line.startswith("!"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[0].strip()
            file_path = parts[1].strip()
            if not name or not file_path:
                continue
            if not os.path.isabs(file_path):
                file_path = os.path.abspath(file_path)
            line_no = None
            for field in parts[2:]:
                if field.startswith("line:"):
                    try:
                        line_no = int(field[5:])
                    except ValueError:
                        line_no = None
                    break
            if line_no is None and len(parts) >= 3:
                try:
                    line_no = int(parts[2])
                except ValueError:
                    line_no = None
            if line_no is None or line_no <= 0:
                pattern = parts[2] if len(parts) >= 3 else ""
                line_no = self._resolve_line_from_tag(file_path, pattern, name)
            if line_no is None or line_no <= 0:
                continue
            bucket = mapping.setdefault(name, [])
            loc = (file_path, line_no)
            if loc not in bucket:
                bucket.append(loc)
        self._name_to_locs = mapping

    def is_ready(self) -> bool:
        return self._ready

    def lookup(self, simple_name: str, code_roots: Optional[List[str]] = None) -> Optional[Tuple[str, int]]:
        name = str(simple_name or "").strip()
        if not name or not self._name_to_locs:
            return None
        roots_abs = [os.path.abspath(r) for r in (code_roots or self.code_roots) if r]
        candidates: List[Tuple[str, int]] = []

        def _under_root(path: str) -> bool:
            try:
                ap = os.path.abspath(path)
            except OSError:
                return False
            for root in roots_abs:
                try:
                    common = os.path.commonpath([ap, root])
                except ValueError:
                    continue
                if common == root:
                    return True
            return not roots_abs

        for key, locs in self._name_to_locs.items():
            if key == name or key.endswith(f"::{name}") or key.endswith(f".{name}"):
                candidates.extend(locs)
            elif f"::{name}" in key:
                candidates.extend(locs)
        if not candidates and name in self._name_to_locs:
            candidates = list(self._name_to_locs[name])
        filtered = [(p, ln) for p, ln in candidates if _under_root(p)]
        if not filtered:
            filtered = candidates
        if not filtered:
            return None
        filtered.sort(key=lambda x: (len(x[0]), x[0]))
        return filtered[0]


_CTYPES_BY_ROOTS: Dict[Tuple[str, ...], CtagsFunctionIndex] = {}
_CTYPES_LOCK = threading.Lock()
_CTYPES_WARM_THREADS: Dict[Tuple[str, ...], threading.Thread] = {}


def get_ctags_index_for_roots(code_roots: List[str]) -> Optional[CtagsFunctionIndex]:
    key = tuple(sorted(os.path.abspath(r) for r in (code_roots or []) if r and os.path.isdir(os.path.abspath(r))))
    if not key:
        return None
    with _CTYPES_LOCK:
        cached = _CTYPES_BY_ROOTS.get(key)
        if cached is not None:
            return cached
        index = CtagsFunctionIndex(list(key))
        _CTYPES_BY_ROOTS[key] = index
        return index


def warm_ctags_index_for_roots(code_roots: List[str]) -> None:
    """Build ctags index in a background thread (no-op if already cached/ready)."""

    key = tuple(sorted(os.path.abspath(r) for r in (code_roots or []) if r and os.path.isdir(os.path.abspath(r))))
    if not key:
        return

    def _worker() -> None:
        get_ctags_index_for_roots(list(key))

    with _CTYPES_LOCK:
        if key in _CTYPES_BY_ROOTS or key in _CTYPES_WARM_THREADS:
            return
        thread = threading.Thread(target=_worker, name=f"ctags-warm-{key[0][-24:]}", daemon=True)
        _CTYPES_WARM_THREADS[key] = thread
        thread.start()
