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
import sqlite3
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
    """On-disk function index built from ctags output.

    Lookups go through sqlite. The symbol table is not kept as a Python dict,
    so a cold index of engine-dev does not stay resident for the rest of the run.
    """

    def __init__(self, code_roots: List[str]) -> None:
        from services.repo_scan_limits import collapse_nested_roots

        self.code_roots = collapse_nested_roots(code_roots or [])
        self._ready = False
        self._conn: Optional[sqlite3.Connection] = None
        self._db_path: Optional[str] = None
        self._symbol_count = 0
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

    @staticmethod
    def _cache_dir() -> str:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        path = os.path.join(base, "stability-analysis-agent", "ctags")
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
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

    def _root_identities(self) -> Tuple[List[str], bool]:
        """按内容而非易变路径标识各 root。

        同一仓库同一 commit 的不同 worktree（如每任务新建的 runs/<id>/... 路径）
        会得到相同身份 → 命中同一份 ctags 缓存，避免每次运行重建索引。
        返回 (sorted identities, 是否全部有 commit)。
        """
        identities: List[str] = []
        all_commit = True
        for root in self.code_roots:
            commit = self._git_commit(root)
            if commit:
                identities.append(f"commit:{commit}")
            else:
                all_commit = False
                try:
                    mtime = int(os.path.getmtime(root))
                except OSError:
                    mtime = 0
                identities.append(f"path:{os.path.abspath(root)}:mtime:{mtime}")
        return sorted(identities), all_commit

    def _cache_key(self) -> str:
        identities, _ = self._root_identities()
        payload = json.dumps(identities, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _cache_paths(self) -> Tuple[str, str]:
        key = self._cache_key()
        cache_dir = self._cache_dir()
        return (
            os.path.join(cache_dir, f"{key}.tags"),
            os.path.join(cache_dir, f"{key}.meta.json"),
        )

    def _index_db_path(self) -> str:
        """sqlite 查询索引路径。符号留在磁盘上，不把整表载入进程。"""
        key = self._cache_key()
        return os.path.join(self._cache_dir(), f"{key}.index.sqlite")

    def _open_db(self, path: Optional[str]) -> None:
        """打开或复用 sqlite。``path`` 为空时使用内存库，供单测直接灌 tags。"""
        if self._conn is not None and self._db_path == path:
            return
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        self._db_path = path
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=OFF")
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS symbols ("
            "name TEXT NOT NULL, simple_name TEXT NOT NULL, "
            "file TEXT NOT NULL, line INTEGER NOT NULL, "
            "UNIQUE(name, file, line))"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbols_simple ON symbols(simple_name)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)")

    @staticmethod
    def _simple_name(name: str) -> str:
        if "::" in name:
            return name.rsplit("::", 1)[-1]
        if "." in name:
            return name.rsplit(".", 1)[-1]
        return name

    def _count_symbols(self) -> int:
        if self._conn is None:
            return 0
        row = self._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()
        return int(row[0] if row else 0)

    def _load_from_cache(self) -> bool:
        tags_path, meta_path = self._cache_paths()
        if not os.path.isfile(tags_path) or not os.path.isfile(meta_path):
            return False
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            identities, all_commit = self._root_identities()
            if meta.get("identities") != identities:
                return False
            # 身份完全由 commit 决定时，内容不可变，无需再按目录 mtime 判新鲜度
            # （worktree 每次 checkout 都会刷新 mtime，会误判缓存失效导致重建）。
            if not all_commit:
                newest_root_mtime = max(os.path.getmtime(r) for r in self.code_roots)
                if float(meta.get("built_at", 0)) < newest_root_mtime:
                    return False
            db_path = self._index_db_path()
            tags_mtime = os.path.getmtime(tags_path)
            if os.path.isfile(db_path) and os.path.getmtime(db_path) >= tags_mtime:
                self._open_db(db_path)
                self._symbol_count = self._count_symbols()
                self._ready = True
                logger.info(
                    "已打开 ctags sqlite 索引（%d 符号，roots=%s）",
                    self._symbol_count,
                    len(self.code_roots),
                )
                return True
            self._open_db(db_path)
            with open(tags_path, "r", encoding="utf-8", errors="ignore") as handle:
                self._parse_tags_stream(handle)
            self._ready = True
            logger.info(
                "已从 tags 文件建立 sqlite 函数索引（%d 符号，roots=%s）",
                self._symbol_count,
                len(self.code_roots),
            )
            return True
        except Exception as exc:
            logger.debug("加载 ctags 缓存失败: %s", exc)
            return False

    def _save_build_meta(self) -> None:
        """只写 meta.json；tags 文件由 ctags -f 直接落在缓存路径，无需读出再回写。"""
        _, meta_path = self._cache_paths()
        identities, _ = self._root_identities()
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"roots": self.code_roots, "identities": identities, "built_at": time.time()},
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
            if not self._is_universal_ctags(ctags):
                logger.info("ctags 不是 Universal Ctags，函数索引跳过并回退全仓扫描")
                self._ready = True
                return
            ok = self._build_with_universal_ctags(ctags, tags_path)
            if not ok:
                self._ready = True
                return
            # 流式逐行解析 tags 文件：多 repo 大仓的 tags 可达 GB 级，
            # 整文件 read() + splitlines() 会造成约 3-4 倍瞬时内存峰值。
            # 缓存命中路径（_load_from_cache）本就走文件句柄，这里保持一致。
            self._open_db(self._index_db_path())
            with open(tags_path, "r", encoding="utf-8", errors="ignore") as handle:
                self._parse_tags_stream(handle)
            self._save_build_meta()
            self._ready = True
            logger.info(
                "ctags 函数索引构建完成（%d 符号，roots=%s，ctags=%s）",
                self._symbol_count,
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
        """把 tags 流写入 sqlite。调用方不再持有 name -> locations 字典。"""
        if self._conn is None:
            self._open_db(self._db_path)
        conn = self._conn
        if conn is None:
            return
        conn.execute("DELETE FROM symbols")
        batch: List[Tuple[str, str, str, int]] = []

        def _flush() -> None:
            if not batch:
                return
            conn.executemany(
                "INSERT OR IGNORE INTO symbols(name, simple_name, file, line) VALUES (?, ?, ?, ?)",
                batch,
            )
            batch.clear()

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
            batch.append((name, self._simple_name(name), file_path, int(line_no)))
            if len(batch) >= 1000:
                _flush()
        _flush()
        conn.commit()
        self._symbol_count = self._count_symbols()

    def is_ready(self) -> bool:
        return self._ready

    def lookup(self, simple_name: str, code_roots: Optional[List[str]] = None) -> Optional[Tuple[str, int]]:
        """按符号名查询一个定义位置。先走索引列，未命中再做有限的限定名匹配。"""
        name = str(simple_name or "").strip()
        if not name or self._conn is None:
            return None
        rows = self._conn.execute(
            "SELECT file, line FROM symbols WHERE simple_name = ? OR name = ?",
            (name, name),
        ).fetchall()
        if not rows:
            rows = self._conn.execute(
                "SELECT file, line FROM symbols WHERE name LIKE ? LIMIT 50",
                (f"%::{name}%",),
            ).fetchall()
        roots_abs = [os.path.abspath(r) for r in (code_roots or self.code_roots) if r]

        def _under_root(path: str) -> bool:
            try:
                absolute = os.path.abspath(path)
            except OSError:
                return False
            for root in roots_abs:
                try:
                    common = os.path.commonpath([absolute, root])
                except ValueError:
                    continue
                if common == root:
                    return True
            return not roots_abs

        candidates = [(str(path), int(line_no)) for path, line_no in rows if path and line_no]
        filtered = [(path, line_no) for path, line_no in candidates if _under_root(path)]
        if not filtered:
            filtered = candidates
        if not filtered:
            return None
        filtered.sort(key=lambda item: (len(item[0]), item[0]))
        return filtered[0]


_CTYPES_BY_ROOTS: Dict[Tuple[str, ...], CtagsFunctionIndex] = {}
_CTYPES_LOCK = threading.Lock()
_CTYPES_WARM_THREADS: Dict[Tuple[str, ...], threading.Thread] = {}


def get_ctags_index_for_roots(code_roots: List[str]) -> Optional[CtagsFunctionIndex]:
    from services.repo_scan_limits import collapse_nested_roots

    key = tuple(collapse_nested_roots(code_roots or []))
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

    from services.repo_scan_limits import collapse_nested_roots

    key = tuple(collapse_nested_roots(code_roots or []))
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
