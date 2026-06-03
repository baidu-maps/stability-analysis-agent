#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lightweight filename index for mapping basename -> absolute paths under code roots."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_EXCLUDE_DIRS = {
    "test",
    "tests",
    "testing",
    "test_utils",
    "third_party",
    "third-party",
    "thirdparty",
    "vendor",
    "external",
    "build",
    "builds",
    "out",
    "output",
    "bin",
    "obj",
    "generated",
    "gen",
    "generated_files",
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    "cmake-build",
    "cmake_build",
    ".idea",
    ".vscode",
    "docs",
    "documentation",
    "doc",
    ".crash_agent",
}

_SUPPORTED_EXTENSIONS = {
    ".c",
    ".cpp",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".hxx",
    ".m",
    ".mm",
    ".swift",
}


class CodeIndexService:
    """Async filename index for a single code root."""

    def __init__(self, code_root: str, *, exclude_dirs: Optional[List[str]] = None) -> None:
        self.code_root = os.path.abspath(code_root)
        self.exclude_dirs = set(_DEFAULT_EXCLUDE_DIRS)
        if exclude_dirs:
            self.exclude_dirs.update(exclude_dirs)
        self._index: DefaultDict[str, List[str]] = defaultdict(list)
        self._scanned_files: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._ready = False
        self._scan_thread: Optional[threading.Thread] = None
        # 缓存存储在用户 home 目录下（遵循 XDG_CACHE_HOME 规范），
        # 不污染用户代码库。用 code_root 路径的 hash 隔离不同项目。
        self._cache_dir = self._resolve_cache_dir(self.code_root)
        self._index_cache_path = os.path.join(self._cache_dir, "code_index.json")
        self._mtime_cache_path = os.path.join(self._cache_dir, "scanned_files.json")
        self._loaded_from_cache = self._try_load_cache()
        if self._loaded_from_cache:
            self._ready = True
        self._start_background_scan()

    @staticmethod
    def _resolve_cache_dir(code_root: str) -> str:
        """将索引缓存目录放在用户 home 下（遵循 XDG_CACHE_HOME），用 code_root 的 hash 隔离。

        目录结构: $XDG_CACHE_HOME/stability-analysis-agent/code-index/<hash16>/
        其中 hash 由 code_root 绝对路径的 SHA-256 前 16 位生成，保证不同项目互不干扰。
        """
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        root_hash = hashlib.sha256(os.path.abspath(code_root).encode("utf-8")).hexdigest()[:16]
        path = os.path.join(base, "stability-analysis-agent", "code-index", root_hash)
        return path

    def _try_load_cache(self) -> bool:
        if not os.path.isfile(self._index_cache_path):
            return False
        try:
            with open(self._index_cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            index = payload.get("index") if isinstance(payload, dict) else None
            if not isinstance(index, dict):
                return False
            with self._lock:
                self._index = defaultdict(list, {str(k): list(v) for k, v in index.items()})
            if os.path.isfile(self._mtime_cache_path):
                with open(self._mtime_cache_path, "r", encoding="utf-8") as f:
                    scanned = json.load(f)
                if isinstance(scanned, dict):
                    self._scanned_files = {str(k): float(v) for k, v in scanned.items()}
            logger.info(
                "已从缓存加载代码文件名索引: %s（%d 个文件名）",
                self.code_root,
                len(self._index),
            )
            return True
        except Exception as exc:
            logger.debug("加载 code_index 缓存失败: %s", exc)
            return False

    def _save_cache(self) -> None:
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
            with self._lock:
                index_payload = {k: v for k, v in self._index.items()}
                scanned_payload = dict(self._scanned_files)
            with open(self._index_cache_path, "w", encoding="utf-8") as f:
                json.dump({"code_root": self.code_root, "index": index_payload}, f, ensure_ascii=False)
            with open(self._mtime_cache_path, "w", encoding="utf-8") as f:
                json.dump(scanned_payload, f, ensure_ascii=False)
        except Exception as exc:
            logger.debug("保存 code_index 缓存失败: %s", exc)

    def _start_background_scan(self) -> None:
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._scan_thread = threading.Thread(
            target=self._scan_worker,
            name=f"code-index-{os.path.basename(self.code_root)}",
            daemon=True,
        )
        self._scan_thread.start()

    def _scan_worker(self) -> None:
        try:
            updated = self._scan_directory(incremental=bool(self._loaded_from_cache))
            with self._lock:
                self._ready = True
            self._save_cache()
            logger.info(
                "代码文件名索引扫描完成: %s（%d 文件名，更新 %d 文件）",
                self.code_root,
                len(self._index),
                updated,
            )
        except Exception as exc:
            with self._lock:
                self._ready = True
            logger.warning("代码文件名索引扫描失败（已降级）: %s", exc)

    def _should_skip_dir(self, dir_name: str) -> bool:
        return dir_name in self.exclude_dirs

    def _scan_directory(self, *, incremental: bool) -> int:
        updated = 0
        if not os.path.isdir(self.code_root):
            return 0
        for root, dirs, files in os.walk(self.code_root):
            dirs[:] = [d for d in dirs if not self._should_skip_dir(d)]
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext not in _SUPPORTED_EXTENSIONS:
                    continue
                file_path = os.path.join(root, file)
                try:
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    continue
                if incremental:
                    prev = self._scanned_files.get(file_path)
                    if prev is not None and prev == mtime:
                        continue
                basename = os.path.basename(file_path)
                with self._lock:
                    paths = self._index[basename]
                    if file_path not in paths:
                        paths.append(file_path)
                    self._scanned_files[file_path] = mtime
                updated += 1
        return updated

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def wait_ready(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.is_ready():
                return True
            time.sleep(0.05)
        return self.is_ready()

    def lookup(self, filename: str) -> List[str]:
        base = os.path.basename(str(filename or "").strip())
        if not base:
            return []
        with self._lock:
            return list(self._index.get(base, []))


class CodeIndexMultiRoot:
    """Filename index spanning multiple code roots (used by CLI / agent)."""

    def __init__(self, code_roots: List[str], *, exclude_dirs: Optional[List[str]] = None) -> None:
        roots = []
        seen = set()
        for raw in code_roots or []:
            if not raw:
                continue
            try:
                ap = os.path.abspath(raw)
            except OSError:
                ap = raw
            if ap in seen or not os.path.isdir(ap):
                continue
            seen.add(ap)
            roots.append(ap)
        self.code_roots = roots
        self._services = [CodeIndexService(r, exclude_dirs=exclude_dirs) for r in roots]

    def is_ready(self) -> bool:
        return any(s.is_ready() for s in self._services) if self._services else False

    def wait_ready(self, timeout: float = 3.0) -> bool:
        if not self._services:
            return False
        per = max(0.2, timeout / len(self._services))
        return any(s.wait_ready(per) for s in self._services)

    def lookup(self, filename: str) -> List[str]:
        out: List[str] = []
        seen = set()
        for service in self._services:
            for path in service.lookup(filename):
                if path in seen:
                    continue
                seen.add(path)
                out.append(path)
        out.sort(key=len)
        return out


_INDEX_BY_ROOTS: Dict[Tuple[str, ...], CodeIndexMultiRoot] = {}
_INDEX_LOCK = threading.Lock()


def get_code_index_for_roots(code_roots: List[str]) -> CodeIndexMultiRoot:
    key = tuple(sorted(os.path.abspath(r) for r in (code_roots or []) if r and os.path.isdir(os.path.abspath(r))))
    with _INDEX_LOCK:
        cached = _INDEX_BY_ROOTS.get(key)
        if cached is not None:
            return cached
        service = CodeIndexMultiRoot(list(key))
        _INDEX_BY_ROOTS[key] = service
        return service
