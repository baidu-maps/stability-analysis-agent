#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一代码定位服务 — 从 code_content_provider_tool.py 抽取的核心定位能力。

包含：
- FileLocator: 7级源文件定位（addr2line 路径 → 本地路径）
- SymbolLocator: 函数定义定位 + 函数体提取
- CallerLocator: 反向调用者搜索 + 并行扫描
- VariableLocator: 变量提取 + 读写关系判定 + 跨文件追踪
- CodeLocatorService: 统一门面，组合以上 4 个 Locator
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple, Union

try:
    from tree_sitter_languages import get_parser as _ts_get_parser
except Exception:
    _ts_get_parser = None

logger = logging.getLogger(__name__)


def normalize_source_line(line: str) -> str:
    """去掉 readlines() 行尾 \\n/\\r，便于安全拼接为多行文本。"""
    return str(line).rstrip("\r\n")


def join_source_line_slice(lines: List[str], start: int, end_inclusive: int) -> str:
    """将源文件行区间拼成多行文本（避免 readlines + join 产生双换行）。"""
    if not lines or start > end_inclusive:
        return ""
    start = max(0, start)
    end_inclusive = min(len(lines) - 1, end_inclusive)
    if start > end_inclusive:
        return ""
    return "\n".join(normalize_source_line(lines[i]) for i in range(start, end_inclusive + 1))


def normalize_source_lines(lines: List[str]) -> List[str]:
    return [normalize_source_line(ln) for ln in lines]


# ==============================================================================
# Exceptions
# ==============================================================================


class FindSourceFileTimeout(Exception):
    """单次 find_source_file 调用超时。"""


class CodeContextPhaseTimeout(Exception):
    """代码上下文整阶段 wall-clock 超时。"""

    def __init__(self, phase: str = ""):
        self.phase = phase or "unknown"
        super().__init__(self.phase)


# ==============================================================================
# Data Classes
# ==============================================================================


@dataclass
class CallChainFunction:
    """调用链函数信息"""
    name: str
    file: str
    snippet: List[str]
    parent_fun: Optional[str] = None
    chain_origin: Optional[str] = None


@dataclass
class VariableFunction:
    """变量相关函数信息"""
    variable: str
    relation: str  # "read", "write", "assign", "delete"
    name: str
    file: str
    snippet: List[str]


# ==============================================================================
# Configuration
# ==============================================================================


@dataclass
class LocatorConfig:
    """Locator 配置（构建后不应修改）。"""
    supported_extensions: frozenset = field(default_factory=lambda: frozenset({
        '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx',
        '.java', '.kt', '.swift', '.m', '.mm', '.py', '.js', '.ts'
    }))
    exclude_dirs: frozenset = field(default_factory=lambda: frozenset({
        'test', 'tests', 'testing', 'test_utils',
        'third_party', 'third-party', 'thirdparty', 'vendor', 'external',
        'build', 'builds', 'out', 'output', 'bin', 'obj',
        'generated', 'gen', 'generated_files',
        'node_modules', '.git', '.svn', '.hg',
        'cmake-build', 'cmake_build', '.idea', '.vscode',
        'docs', 'documentation', 'doc',
    }))
    include_subdirs: Optional[frozenset] = None
    max_file_size: int = 1024 * 1024
    max_code_length: int = 2000
    find_source_timeout_sec: float = 600.0
    code_context_timeout_sec: float = 360.0
    max_direct_callers: int = 10
    max_shared_var_related_functions: int = 20
    min_key_read_related_functions: int = 2
    max_nearby_module_scan_files: int = 300
    max_crash_caller_search_files: int = 600
    max_workers: Optional[int] = None
    parallel_threshold: int = 100
    use_ctags_index: bool = False


# ==============================================================================
# LocatorContext — 每次分析运行的共享可变状态
# ==============================================================================


class LocatorContext:
    """Per-analysis-run mutable state shared across all locators."""

    def __init__(self, config: LocatorConfig, code_index_service=None, ts_parser=None):
        self.config = config
        self.code_index_service = code_index_service
        self._ts_parser = ts_parser

        # File content cache
        self._file_content_cache: Dict[str, str] = {}
        self._file_content_cache_bytes: int = 0
        self._FILE_CONTENT_CACHE_MAX_BYTES: int = 200 * 1024 * 1024
        self._FILE_CONTENT_CACHE_MAX_FILES: int = 500

        # Function definition cache
        self._function_def_cache: Dict[str, Tuple[str, int]] = {}

        # Nearby module cache
        self._nearby_module_files_cache: Dict[tuple, List[str]] = {}

        # Search stats
        self.search_stats: Dict[str, Any] = {
            'files_scanned': 0,
            'files_skipped_size': 0,
            'files_skipped_excluded': 0,
            'files_skipped_extension': 0,
            'files_read': 0,
            'search_time': 0.0,
        }

        # Timeout state — find_source
        self._find_source_budget_deadline: Optional[float] = None
        self._find_source_budget_total_sec: float = config.find_source_timeout_sec
        self._find_source_lookup_calls: int = 0
        self._find_source_deadline: Optional[float] = None
        self._find_source_cur_timeout_sec: float = config.find_source_timeout_sec

        # Timeout state — code_context phase
        self._code_context_deadline: Optional[float] = None

        # Ctags index (lazy)
        self._ctags_index = None

    # ------------------------------------------------------------------
    # Timeout management
    # ------------------------------------------------------------------

    def find_source_budget_remaining_sec(self) -> float:
        if self._find_source_budget_deadline is None:
            return self._find_source_budget_total_sec
        return max(0.0, self._find_source_budget_deadline - time.monotonic())

    def _compute_dynamic_find_source_timeout_sec(self) -> float:
        remaining = self.find_source_budget_remaining_sec()
        base = self._find_source_budget_total_sec
        calls = self._find_source_lookup_calls
        if remaining <= 0.0:
            return 0.0
        if remaining >= base * 0.8 and calls <= 2:
            return base
        if remaining <= base * 0.35:
            return remaining * 0.5
        return remaining * 0.75

    def find_source_deadline_begin(self) -> None:
        if self._find_source_budget_deadline is None:
            self._find_source_budget_deadline = time.monotonic() + self._find_source_budget_total_sec
        self._find_source_lookup_calls += 1
        self._find_source_cur_timeout_sec = self._compute_dynamic_find_source_timeout_sec()
        self._find_source_deadline = time.monotonic() + self._find_source_cur_timeout_sec

    def find_source_check_time(self) -> None:
        if self._find_source_deadline is None:
            return
        if time.monotonic() >= self._find_source_deadline:
            raise FindSourceFileTimeout()

    def find_source_deadline_clear(self) -> None:
        self._find_source_deadline = None

    def code_context_phase_start(self) -> None:
        if self.config.code_context_timeout_sec > 0:
            self._code_context_deadline = time.monotonic() + self.config.code_context_timeout_sec

    def code_context_phase_check(self, phase: str = "") -> None:
        if self._code_context_deadline is None:
            return
        if time.monotonic() >= self._code_context_deadline:
            raise CodeContextPhaseTimeout(phase)

    def code_context_phase_remaining_sec(self) -> Optional[float]:
        if self._code_context_deadline is None:
            return None
        return max(0.0, self._code_context_deadline - time.monotonic())

    def session_reset(self) -> None:
        """Reset per-run state (called at start of each code_content_provider run)."""
        self._find_source_budget_deadline = None
        self._find_source_lookup_calls = 0
        self._find_source_deadline = None
        self._find_source_cur_timeout_sec = self.config.find_source_timeout_sec
        self._code_context_deadline = None
        self.search_stats = {
            'files_scanned': 0,
            'files_skipped_size': 0,
            'files_skipped_excluded': 0,
            'files_skipped_extension': 0,
            'files_read': 0,
            'search_time': 0.0,
        }

    # ------------------------------------------------------------------
    # Shared utilities
    # ------------------------------------------------------------------

    def read_file_cached(self, file_path: str) -> Optional[str]:
        """Read file with per-run caching (bounded by 200MB/500 files)."""
        cached = self._file_content_cache.get(file_path)
        if cached is not None:
            return cached
        try:
            if not os.path.isfile(file_path):
                return None
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            content_size = len(content)
            if (self._file_content_cache_bytes + content_size <= self._FILE_CONTENT_CACHE_MAX_BYTES
                    and len(self._file_content_cache) < self._FILE_CONTENT_CACHE_MAX_FILES):
                self._file_content_cache[file_path] = content
                self._file_content_cache_bytes += content_size
            return content
        except Exception:
            return None

    def is_supported_file(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in self.config.supported_extensions

    def is_file_readable(self, file_path: str) -> bool:
        return os.path.isfile(file_path) and os.access(file_path, os.R_OK)

    def should_skip_file(self, file_path: str, root: str) -> bool:
        """Check if file should be skipped (size, extension, excluded dirs)."""
        try:
            fsize = os.path.getsize(file_path)
            if fsize > self.config.max_file_size:
                self.search_stats['files_skipped_size'] += 1
                return True
        except OSError:
            return True
        if not self.is_supported_file(file_path):
            self.search_stats['files_skipped_extension'] += 1
            return True
        try:
            rel = os.path.relpath(file_path, root)
        except ValueError:
            rel = file_path
        parts = Path(rel).parts
        for part in parts[:-1]:
            if part in self.config.exclude_dirs:
                self.search_stats['files_skipped_excluded'] += 1
                return True
        if self.config.include_subdirs is not None:
            if not parts or parts[0] not in self.config.include_subdirs:
                return True
        return False

    def should_skip_directory(self, dir_name: str) -> bool:
        return dir_name in self.config.exclude_dirs

    # ------------------------------------------------------------------
    # ripgrep acceleration
    # ------------------------------------------------------------------

    _rg_available: Optional[bool] = None  # 类级缓存

    @classmethod
    def _check_rg_available(cls) -> bool:
        try:
            subprocess.run(["rg", "--version"], check=False, capture_output=True, timeout=5)
            return True
        except (FileNotFoundError, OSError):
            return False

    def rg_grep_files(
        self,
        pattern: str,
        search_paths: List[str],
        max_files: int = 5000,
    ) -> Optional[List[str]]:
        """用 rg --files-with-matches 快速获取包含匹配的文件列表。

        Returns:
            [file_path, ...] 或 None（rg 不可用时）。
        """
        if LocatorContext._rg_available is None:
            LocatorContext._rg_available = self._check_rg_available()
        if not LocatorContext._rg_available:
            return None
        if not search_paths:
            return None

        cmd = ["rg", "--files-with-matches", "--color", "never"]
        ext_list = sorted(ext.lstrip(".") for ext in self.config.supported_extensions)
        cmd += ["--glob", "*.{" + ",".join(ext_list) + "}"]
        for d in sorted(self.config.exclude_dirs):
            cmd += ["--glob", f"!{d}/"]
        cmd += ["--max-filesize", str(self.config.max_file_size)]
        cmd.append(pattern)
        cmd += search_paths

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
        except subprocess.TimeoutExpired:
            logger.debug("rg --files-with-matches 超时(60s)")
            return None
        except Exception:
            LocatorContext._rg_available = False
            return None

        if proc.returncode not in (0, 1):
            return None

        files: List[str] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line:
                files.append(line)
            if len(files) >= max_files:
                break
        return files

    def rg_grep_lines(
        self,
        pattern: str,
        search_paths: List[str],
        max_matches: int = 5000,
    ) -> Optional[List[Tuple[str, int, str]]]:
        """用 rg 快速搜索匹配行。

        Returns:
            [(file_path, line_number, line_text), ...] 或 None（rg 不可用时）。
        """
        if LocatorContext._rg_available is None:
            LocatorContext._rg_available = self._check_rg_available()
        if not LocatorContext._rg_available:
            return None
        if not search_paths:
            return None

        cmd = ["rg", "-n", "--no-heading", "--color", "never"]
        ext_list = sorted(ext.lstrip(".") for ext in self.config.supported_extensions)
        cmd += ["--glob", "*.{" + ",".join(ext_list) + "}"]
        for d in sorted(self.config.exclude_dirs):
            cmd += ["--glob", f"!{d}/"]
        cmd += ["--max-filesize", str(self.config.max_file_size)]
        cmd.append(pattern)
        cmd += search_paths

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
        except subprocess.TimeoutExpired:
            logger.debug("rg grep_lines 超时(60s)")
            return None
        except Exception:
            LocatorContext._rg_available = False
            return None

        if proc.returncode not in (0, 1):
            return None

        results: List[Tuple[str, int, str]] = []
        for raw_line in (proc.stdout or "").splitlines():
            if len(results) >= max_matches:
                break
            m = re.match(r"^(.*?):(\d+):(.*)$", raw_line)
            if m:
                results.append((m.group(1), int(m.group(2)), m.group(3)))
        return results

    def is_external_path(self, resolved_file: str, code_roots: Optional[List[str]] = None) -> bool:
        """
        Check if path is a system/toolchain path (should skip).
        If code_roots provided, path falling within any root is NOT external.
        """
        if not resolved_file:
            return False
        if not os.path.isabs(resolved_file):
            return False

        normalized = resolved_file.replace("\\", "/")

        # Common external path prefixes
        external_prefixes = (
            "/usr/",
            "/System/",
            "/Applications/Xcode.app/",
            "/Library/Developer/",
            "/opt/homebrew/",
            "/Users/",
        )

        # If path falls within any code_root, it's NOT external
        if code_roots:
            try:
                resolved_abs = os.path.abspath(resolved_file)
                for cr in code_roots:
                    try:
                        cr_abs = os.path.abspath(cr)
                        common_path = os.path.commonpath([cr_abs, resolved_abs])
                        if common_path == cr_abs:
                            return False
                    except Exception:
                        continue
            except Exception:
                pass

        # /Users/ sub-paths: further check for SDK/cache markers
        if normalized.startswith("/Users/"):
            user_external_markers = (
                "/Library/Android/sdk/",
                "/Library/Developer/Xcode/",
                "/.cache/",
                "/.gradle/",
                "/.conan/",
            )
            if any(marker in normalized for marker in user_external_markers):
                return True
            return False

        lowered = normalized.lower()
        # Toolchain/NDK markers
        toolchain_markers = (
            "/openharmony/",
            "/ohos/",
            "/ndk/",
            "/llvm/",
            "libcxx",
            "libc++",
            "/command-line-tools/",
            "/env/",
            "/sysroot/",
            "c++/v1/",
            "/compilekit/",
            "android-ndk",
            "prebuilt/linux-x86_64",
            "/toolchains/llvm/",
        )
        if any(m in lowered for m in toolchain_markers):
            return True
        if "openharmony" in lowered:
            return True

        return normalized.startswith(external_prefixes)

    def pick_code_root_for_file(self, file_path: str, code_roots: List[str]) -> str:
        """Find the code_root that contains file_path."""
        try:
            ap = os.path.abspath(file_path)
        except Exception:
            ap = file_path
        for root in code_roots:
            try:
                root_abs = os.path.abspath(root)
                if ap.startswith(root_abs + os.sep) or ap == root_abs:
                    return root_abs
            except Exception:
                continue
        return code_roots[0] if code_roots else ""

    def normalize_scan_file_list(self, paths: Optional[List[str]]) -> List[str]:
        """Deduplicate, normalize, and filter file paths."""
        if not paths:
            return []
        out: List[str] = []
        seen: Set[str] = set()
        for p in paths:
            if not p or not str(p).strip():
                continue
            try:
                ap = os.path.abspath(str(p))
            except Exception:
                continue
            if ap in seen:
                continue
            seen.add(ap)
            if os.path.isfile(ap) and self.is_supported_file(ap):
                out.append(ap)
        return out

    def truncate_snippet(self, snippet: List[str], max_length: Optional[int] = None) -> List[str]:
        """Truncate code snippet to max_length lines with smart head/tail ratio."""
        limit = max_length if max_length is not None else self.config.max_code_length
        if limit <= 0 or len(snippet) <= limit:
            return snippet
        head_count = max(1, int(limit * 0.3))
        tail_count = max(1, int(limit * 0.4))
        mid_budget = limit - head_count - tail_count
        if mid_budget < 1:
            head_count = limit // 2
            tail_count = limit - head_count
            return snippet[:head_count] + ["// ... (truncated) ..."] + snippet[-tail_count:]
        return (
            snippet[:head_count]
            + ["// ... (truncated) ..."]
            + snippet[-tail_count:]
        )

    def ensure_ctags_index(self, code_roots: List[str]) -> None:
        """Lazily initialize ctags index if not already done."""
        if self._ctags_index is not None:
            return
        if not self.config.use_ctags_index:
            return
        if os.environ.get("STABILITY_AGENT_DISABLE_CODE_ACCELERATION", "").strip().lower() in {"1", "true"}:
            return
        try:
            from services.ctags_function_index import get_ctags_index_for_roots
            self._ctags_index = get_ctags_index_for_roots(code_roots)
        except Exception as exc:
            logger.debug("ctags index init failed: %s", exc)


# ==============================================================================
# FileLocator — 7级源文件定位
# ==============================================================================


class FileLocator:
    """7-tier source file resolution: addr2line path -> local source file."""

    def __init__(self, ctx: LocatorContext):
        self._ctx = ctx

    def find_source_file(self, resolved_file: str, code_roots: List[str]) -> Optional[str]:
        """Main entry: resolve addr2line path to a local source file."""
        if not resolved_file or not code_roots:
            return None
        resolved_file = str(resolved_file).strip()
        if not resolved_file:
            return None
        if self._ctx.is_external_path(resolved_file, code_roots):
            return None

        self._ctx.find_source_deadline_begin()
        try:
            # Tier 1-4: try each code root
            for code_root in code_roots:
                try:
                    code_root_abs = os.path.abspath(code_root)
                except Exception:
                    continue
                # Tier 1: Direct hit
                hit = self._try_direct_hit(resolved_file, code_root_abs)
                if hit:
                    return hit
                # Tier 2: Code root dirname anchor
                hit = self._try_suffix_after_code_root_dirname(resolved_file, code_root_abs)
                if hit:
                    return hit
                # Tier 3: Tail path concatenation
                hit = self._try_tail_path_concatenation(resolved_file, code_root_abs)
                if hit:
                    return hit
                # Tier 4: Parent dir + filename glob
                hit = self._try_parent_dir_filename_match(resolved_file, code_root_abs)
                if hit:
                    return hit

            # Tier 5: CodeIndexService lookup
            idx = self._ctx.code_index_service
            if idx is not None and hasattr(idx, 'is_ready') and idx.is_ready():
                basename = os.path.basename(resolved_file)
                candidates = idx.lookup(basename)
                if candidates:
                    if len(candidates) == 1:
                        if self._ctx.is_file_readable(candidates[0]):
                            return candidates[0]
                    else:
                        best = self._select_best_candidate(candidates, resolved_file)
                        if best:
                            return best
                        for c in candidates:
                            if self._ctx.is_file_readable(c):
                                return c

            # Tier 6/7: Fallback search
            for code_root in code_roots:
                try:
                    code_root_abs = os.path.abspath(code_root)
                except Exception:
                    continue
                hit = self._fallback_search(resolved_file, code_root_abs)
                if hit:
                    return hit

            return None
        except FindSourceFileTimeout:
            logger.debug("find_source_file timeout: %s", resolved_file)
            return None
        finally:
            self._ctx.find_source_deadline_clear()

    def _try_direct_hit(self, resolved_file: str, code_root_abs: str) -> Optional[str]:
        """Tier 1: Check if resolved path directly exists within code root."""
        self._ctx.find_source_check_time()
        if os.path.isabs(resolved_file):
            if self._ctx.is_file_readable(resolved_file):
                try:
                    common = os.path.commonpath([resolved_file, code_root_abs])
                    if common == code_root_abs:
                        return resolved_file
                except (ValueError, OSError):
                    pass
        else:
            candidate = os.path.join(code_root_abs, resolved_file)
            if self._ctx.is_file_readable(candidate):
                return os.path.abspath(candidate)
            candidate = os.path.join(code_root_abs, os.path.basename(resolved_file))
            if self._ctx.is_file_readable(candidate):
                return os.path.abspath(candidate)
        return None

    def _try_suffix_after_code_root_dirname(self, resolved_file: str, code_root_abs: str) -> Optional[str]:
        """Tier 2: Map CI build paths by finding code-root dirname anchor."""
        self._ctx.find_source_check_time()
        root_basename = os.path.basename(code_root_abs)
        if not root_basename:
            return None
        marker = f"/{root_basename}/"
        normalized = resolved_file.replace("\\", "/")
        positions = []
        start = 0
        while True:
            idx = normalized.find(marker, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + 1
        # Try rightmost first
        for pos in reversed(positions):
            suffix = normalized[pos + len(marker):]
            if not suffix or ".." in suffix:
                continue
            candidate = os.path.join(code_root_abs, suffix)
            try:
                candidate = str(Path(candidate).resolve())
                Path(candidate).relative_to(Path(code_root_abs).resolve())
            except (ValueError, OSError):
                continue
            if self._ctx.is_file_readable(candidate):
                return candidate
        return None

    def _try_tail_path_concatenation(self, resolved_file: str, code_root_abs: str) -> Optional[str]:
        """Tier 3: Extract trailing N-level dir + filename, try concatenating."""
        self._ctx.find_source_check_time()
        normalized = resolved_file.replace("\\", "/")
        parts = [p for p in normalized.split("/") if p]
        if not parts:
            return None
        max_levels = min(32, len(parts))
        for level in range(1, max_levels + 1):
            self._ctx.find_source_check_time()
            tail = os.sep.join(parts[-level:])
            candidate = os.path.join(code_root_abs, tail)
            if self._ctx.is_file_readable(candidate):
                return os.path.abspath(candidate)
        return None

    def _try_parent_dir_filename_match(self, resolved_file: str, code_root_abs: str) -> Optional[str]:
        """Tier 4: Glob-based */<parent>/<filename> search with bounded depth."""
        self._ctx.find_source_check_time()
        normalized = resolved_file.replace("\\", "/")
        parts = [p for p in normalized.split("/") if p]
        if len(parts) < 2:
            return None
        parent_dir = parts[-2]
        filename = parts[-1]
        root_path = Path(code_root_abs)
        for depth in range(1, 4):
            self._ctx.find_source_check_time()
            pattern = "/".join(["*"] * depth) + f"/{parent_dir}/{filename}"
            matches = []
            try:
                for match in root_path.glob(pattern):
                    if match.is_file():
                        matches.append(str(match))
                    if len(matches) > 10:
                        break
            except (OSError, StopIteration):
                continue
            if matches:
                if len(matches) == 1:
                    return matches[0]
                matches.sort(key=len)
                return matches[0]
        return None

    def _select_best_candidate(self, candidates: List[str], resolved_file: str) -> Optional[str]:
        """Score-based selection when multiple file candidates exist."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0] if self._ctx.is_file_readable(candidates[0]) else None

        resolved_parts = [p for p in resolved_file.replace("\\", "/").split("/") if p]
        best_score = -1
        best_path = None

        for candidate in candidates:
            if not self._ctx.is_file_readable(candidate):
                continue
            cand_parts = [p for p in candidate.replace("\\", "/").split("/") if p]
            # Tail matching score
            score = 0
            for i in range(1, min(len(resolved_parts), len(cand_parts)) + 1):
                if resolved_parts[-i].lower() == cand_parts[-i].lower():
                    score += i * 3
                else:
                    break
            # Directory name overlap
            resolved_dirs = set(p.lower() for p in resolved_parts[:-1])
            cand_dirs = set(p.lower() for p in cand_parts[:-1])
            common_dirs = resolved_dirs & cand_dirs
            score += len(common_dirs) * 5
            # Path length similarity
            length_diff = abs(len(resolved_parts) - len(cand_parts))
            score += max(0, 10 - length_diff * 2)

            if score > best_score:
                best_score = score
                best_path = candidate

        return best_path

    def _fallback_search(self, resolved_file: str, code_root: str) -> Optional[str]:
        """Tier 6/7: Limited search in common source directories."""
        filename = os.path.basename(resolved_file)
        if not filename:
            return None
        # Try root directly
        candidate = os.path.join(code_root, filename)
        if self._ctx.is_file_readable(candidate):
            return candidate
        # Search common dirs with limited depth
        common_dirs = ['src', 'source', 'lib', 'common', 'include', 'inc']
        for common_dir in common_dirs:
            self._ctx.find_source_check_time()
            search_root = os.path.join(code_root, common_dir)
            if not os.path.isdir(search_root):
                continue
            walk_count = 0
            for root, dirs, files in os.walk(search_root):
                depth = root[len(search_root):].count(os.sep)
                if depth >= 2:
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if not self._ctx.should_skip_directory(d)]
                if filename in files:
                    return os.path.join(root, filename)
                walk_count += 1
                if walk_count % 200 == 0:
                    self._ctx.find_source_check_time()
        return None

    def collect_stack_priority_source_files(
        self, add2line_data: Optional[Dict[str, Any]], code_roots: List[str]
    ) -> List[str]:
        """Extract source file paths from addr2line resolved frames."""
        if not add2line_data or not isinstance(add2line_data, dict):
            return []
        frames = add2line_data.get("resolved_frames") or []
        if not isinstance(frames, list):
            return []
        out: List[str] = []
        seen: Set[str] = set()
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            resolved_file = frame.get("resolved_file") or frame.get("file")
            if not resolved_file or not str(resolved_file).strip():
                continue
            resolved_file = str(resolved_file).strip()
            if self._ctx.is_external_path(resolved_file, code_roots):
                continue
            local = self.find_source_file(resolved_file, code_roots)
            if not local:
                continue
            try:
                ap = os.path.abspath(local)
            except Exception:
                ap = local
            if ap in seen or not self._ctx.is_supported_file(ap):
                continue
            seen.add(ap)
            out.append(ap)
        return out

    def collect_nearby_module_files(
        self,
        stack_priority_files: List[str],
        code_roots: List[str],
        parent_levels: int = 2,
        descend_depth: int = 3,
        max_files: Optional[int] = None,
    ) -> List[str]:
        """Collect source files near stack frame files for expanded scanning."""
        if not stack_priority_files:
            return []
        _max = max_files if max_files is not None else self._ctx.config.max_nearby_module_scan_files
        # Cache key
        cache_key = (
            tuple(sorted(stack_priority_files)),
            tuple(sorted(code_roots)),
            _max,
        )
        cached = self._ctx._nearby_module_files_cache.get(cache_key)
        if cached is not None:
            return cached

        out: List[str] = []
        seen: Set[str] = set()
        for sf in stack_priority_files:
            try:
                seen.add(os.path.abspath(sf))
            except Exception:
                pass

        for sf in stack_priority_files:
            if len(out) >= _max:
                break
            try:
                sf_abs = os.path.abspath(sf)
                sf_dir = os.path.dirname(sf_abs)
            except Exception:
                continue
            cr = self._ctx.pick_code_root_for_file(sf, code_roots)
            # Walk parent directories
            current_dir = sf_dir
            for _ in range(parent_levels):
                if len(out) >= _max:
                    break
                parent = os.path.dirname(current_dir)
                if parent == current_dir:
                    break
                current_dir = parent
                # Descend from this parent
                try:
                    for root, dirs, files in os.walk(current_dir):
                        depth = root[len(current_dir):].count(os.sep)
                        if depth >= descend_depth:
                            dirs.clear()
                            continue
                        dirs[:] = [d for d in dirs if not self._ctx.should_skip_directory(d)]
                        for fn in files:
                            if len(out) >= _max:
                                break
                            fp = os.path.join(root, fn)
                            try:
                                ap = os.path.abspath(fp)
                            except Exception:
                                continue
                            if ap in seen:
                                continue
                            if not self._ctx.is_supported_file(ap):
                                continue
                            if self._ctx.should_skip_file(ap, cr):
                                continue
                            seen.add(ap)
                            out.append(ap)
                        if len(out) >= _max:
                            break
                except OSError:
                    continue

        self._ctx._nearby_module_files_cache[cache_key] = out
        return out


# ==============================================================================
# SymbolLocator — 函数定义定位 + 函数体提取
# ==============================================================================


class SymbolLocator:
    """Function definition finding and body extraction."""

    def __init__(self, ctx: LocatorContext):
        self._ctx = ctx
        self._last_extract_backend: str = "unknown"

    def find_function_definition_location(
        self, simple_name: str, code_roots: List[str]
    ) -> Optional[Tuple[str, int]]:
        """Find function definition (file, line) by name.

        Strategy order:
        1. In-memory cache (instant)
        2. ctags index (if enabled and ready)
        3. rg pre-filter + definition check (fast, ~0.1s)
        4. os.walk fallback (slow, only if rg unavailable)
        """
        if not simple_name or not code_roots:
            return None
        # Check cache
        cache_key = f"__funcdef__:{simple_name}"
        cached = self._ctx._function_def_cache.get(cache_key)
        if cached is not None:
            return cached
        # Try ctags index (only if enabled)
        self._ctx.ensure_ctags_index(code_roots)
        if self._ctx._ctags_index is not None and self._ctx._ctags_index.is_ready():
            result = self._ctx._ctags_index.lookup(simple_name, code_roots)
            if result is not None:
                self._ctx._function_def_cache[cache_key] = result
                return result
        # Try rg-based search (fast path). When rg runs successfully but finds no
        # definition, do not repeat the same text search with an os.walk fallback.
        rg_pattern = rf"\b{re.escape(simple_name)}\s*\("
        rg_hits = self._ctx.rg_grep_lines(rg_pattern, code_roots)
        if rg_hits is not None:
            for fp, line_no, line_text in rg_hits:
                if self.is_function_definition_line(line_text):
                    result = (fp, line_no)
                    self._ctx._function_def_cache[cache_key] = result
                    return result
            return None
        # Fallback: os.walk + regex (slow path, only if rg unavailable)
        pattern = re.compile(rf"\b{re.escape(simple_name)}\s*\(")
        for code_root in code_roots:
            if not os.path.isdir(code_root):
                continue
            file_count = 0
            for dirpath, dirnames, filenames in os.walk(code_root):
                dirnames[:] = [d for d in dirnames if not self._ctx.should_skip_directory(d)]
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    if not self._ctx.is_supported_file(fp):
                        continue
                    if not self._ctx.is_file_readable(fp):
                        continue
                    file_count += 1
                    if file_count % 50 == 0:
                        self._ctx.code_context_phase_check("function_def_scan")
                    content = self._ctx.read_file_cached(fp)
                    if not content:
                        continue
                    if simple_name not in content:
                        continue
                    for idx, line in enumerate(content.split("\n"), 1):
                        if pattern.search(line) and self.is_function_definition_line(line):
                            result = (fp, idx)
                            self._ctx._function_def_cache[cache_key] = result
                            return result
        return None

    def _find_function_def_via_rg(
        self, simple_name: str, code_roots: List[str]
    ) -> Optional[Tuple[str, int]]:
        """Use rg to quickly find function definition location."""
        rg_pattern = rf"\b{re.escape(simple_name)}\s*\("
        hits = self._ctx.rg_grep_lines(rg_pattern, code_roots)
        if hits is None:
            return None  # rg not available, caller should fallback
        # Filter hits to find a definition (not a call)
        for fp, line_no, line_text in hits:
            if self.is_function_definition_line(line_text):
                return (fp, line_no)
        return None

    def find_function_definition_line_by_name(
        self, lines: List[str], function_name: str, anchor_line_index: int
    ) -> Optional[int]:
        """Within a file, find function definition line closest to anchor."""
        if not lines or not function_name:
            return None
        simple_name = self.extract_simple_function_name(function_name)
        if not simple_name:
            return None
        pattern = re.compile(rf"\b~?{re.escape(simple_name)}\s*(?:<[^(){{}};]*>)?\s*\(")
        candidates = []
        for i, line in enumerate(lines):
            if pattern.search(line) and self.is_function_definition_line(line):
                candidates.append(i)
        if not candidates:
            return None
        return min(candidates, key=lambda i: abs(i - anchor_line_index))

    def extract_full_function_code(
        self, lines: List[str], target_line_index: int, target_function_name: Optional[str] = None
    ) -> Optional[str]:
        """Extract complete function body from source lines. 3-strategy approach."""
        self._last_extract_backend = "none"
        if not lines or target_line_index < 0:
            return None
        # Strategy 1: Token + brace (most reliable for C++)
        if target_function_name:
            result = self._extract_by_token_regex(
                lines, target_line_index, self.extract_simple_function_name(target_function_name)
            )
            if result:
                self._last_extract_backend = "token_regex"
                return result
        # Strategy 2: Tree-sitter (if available)
        if self._ctx._ts_parser is not None:
            source_text = join_source_line_slice(lines, 0, len(lines) - 1)
            sig, body = self._ts_extract_function(source_text, target_line_index, target_function_name)
            if body:
                self._last_extract_backend = "tree_sitter"
                return body
        # Strategy 3: Regex + brace counting fallback
        fallback = self._extract_by_brace_counting(lines, target_line_index, target_function_name)
        if fallback:
            self._last_extract_backend = "brace_counting"
        return fallback

    def extract_function_signature(
        self, lines: List[str], target_line_index: int, target_function_name: Optional[str] = None
    ) -> str:
        """Extract function signature at target line."""
        if not lines:
            return "unknown function"
        # Try tree-sitter first
        if self._ctx._ts_parser is not None:
            source_text = join_source_line_slice(lines, 0, len(lines) - 1)
            sig, _ = self._ts_extract_function(source_text, target_line_index, target_function_name)
            if sig:
                return sig
        # Regex fallback
        if target_function_name:
            simple_name = self.extract_simple_function_name(target_function_name)
            pattern = re.compile(rf"\b~?{re.escape(simple_name)}\s*(?:<[^(){{}};]*>)?\s*\(")
            search_start = max(0, target_line_index - 220)
            for i in range(target_line_index, search_start - 1, -1):
                if i < len(lines) and pattern.search(lines[i]):
                    return lines[i].strip()
        return "unknown function"

    def extract_function_name_at_line(self, lines: List[str], line_number: int) -> Optional[str]:
        """Find the enclosing function name at given line number."""
        if not lines or line_number < 1:
            return None
        target_idx = line_number - 1
        # Search backward for function definition
        search_start = max(0, target_idx - 50)
        pattern = re.compile(
            r"(?:[\w:<>,~*&\s]+?\s+)?"  # return type
            r"((?:[\w:]+::)*~?[\w]+)\s*\("  # function name with optional class prefix
        )
        for i in range(target_idx, search_start - 1, -1):
            if i >= len(lines):
                continue
            line = lines[i].strip()
            if not line or line.startswith("//") or line.startswith("/*") or line.startswith("*"):
                continue
            # Skip control flow
            if re.match(r"^\s*(if|else|for|while|switch|case|do|return|try|catch)\b", line):
                continue
            m = pattern.search(line)
            if m and self.is_function_definition_line(line):
                name = m.group(1)
                # Remove class prefix for simple name
                if "::" in name:
                    parts = name.split("::")
                    return parts[-1] if parts[-1] else name
                return name
        return None

    def extract_simple_function_name(self, full_function_name: str) -> str:
        """Extract simple function name from qualified name: Class::fn(...) -> fn"""
        if not full_function_name:
            return ""
        s = full_function_name.strip()
        # Remove params
        paren_idx = s.find("(")
        if paren_idx > 0:
            s = s[:paren_idx].strip()
        # Remove template args after params are stripped, including namespace-qualified template args.
        s = self._strip_template_args(s)
        # Take last :: component
        if "::" in s:
            parts = s.split("::")
            s = parts[-1].strip()
        # Remove return type prefix
        parts = s.split()
        if parts:
            s = parts[-1]
        # Remove pointer/reference prefixes
        s = s.lstrip("*&~")
        return s

    @staticmethod
    def _strip_template_args(text: str) -> str:
        """Remove C++ template argument lists while preserving surrounding tokens."""
        out: List[str] = []
        depth = 0
        for ch in str(text or ""):
            if ch == "<":
                depth += 1
                continue
            if ch == ">" and depth > 0:
                depth -= 1
                continue
            if depth == 0:
                out.append(ch)
        return "".join(out)

    def is_function_definition_line(self, line: str) -> bool:
        """Check if a line is a function definition (not control flow)."""
        stripped = line.strip()
        if not stripped:
            return False
        # 明确排除函数声明/原型（以 ';' 收尾且不含函数体起始）。
        if stripped.endswith(";") and "{" not in stripped:
            return False
        # Exclude control flow
        control_keywords = (
            'if', 'else', 'for', 'while', 'switch', 'case', 'default',
            'do', 'return', 'try', 'catch', 'throw', 'goto', 'break', 'continue',
        )
        first_word = re.match(r"(\w+)", stripped)
        if first_word and first_word.group(1) in control_keywords:
            return False
        # Must have function-like pattern: name(
        if not re.search(r"\w+\s*\(", stripped):
            return False
        # Must not be just a function call (no return type / class prefix)
        # Function defs typically have: type name( or Class::name(
        if re.search(r"(?:[\w:<>,~*&\s]+\s+)[\w:~]+\s*\(", stripped):
            return True
        if re.search(r"\w+::\w+\s*\(", stripped):
            return True
        return False

    def _is_definition_head_nearby(self, lines: List[str], sig_start: int) -> bool:
        """判断 sig_start 是否更像函数定义头（而非声明/调用）。"""
        if sig_start < 0 or sig_start >= len(lines):
            return False
        max_lookahead = min(len(lines), sig_start + 12)
        for i in range(sig_start, max_lookahead):
            s = lines[i].strip()
            if not s:
                continue
            # 声明先于函数体，判为非定义。
            if ";" in s and "{" not in s:
                return False
            if "{" in s:
                return True
        return False

    # --- Private helpers ---

    def _extract_by_token_regex(
        self, lines: List[str], target_line_index: int, token: str
    ) -> Optional[str]:
        """Extract function by finding token in signature, then brace-matching."""
        token = self.extract_simple_function_name(token)
        if not token:
            return None
        pattern = re.compile(rf"\b~?{re.escape(token)}\s*(?:<[^(){{}};]*>)?\s*\(")
        # Search backward for signature
        search_start = max(0, target_line_index - 400)
        sig_start = None
        for i in range(target_line_index, search_start - 1, -1):
            if i >= len(lines):
                continue
            line = lines[i]
            if pattern.search(line):
                stripped = line.strip()
                # Skip control flow
                if re.match(r"^\s*(else\s+if|if|for|while|switch|catch)\b", stripped):
                    continue
                if self.is_function_definition_line(stripped):
                    if not self._is_definition_head_nearby(lines, i):
                        continue
                    sig_start = i
                    break
        if sig_start is None:
            return None
        sig_start = self._include_template_prefix(lines, sig_start)
        # Find opening brace
        brace_start = None
        for i in range(sig_start, min(sig_start + 250, len(lines))):
            if '{' in lines[i]:
                brace_start = i
                break
        if brace_start is None:
            return None
        # Count braces to find end
        depth = 0
        func_end = None
        for i in range(brace_start, len(lines)):
            for ch in lines[i]:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        func_end = i
                        break
            if func_end is not None:
                break
        if func_end is None:
            return None
        extracted = join_source_line_slice(lines, sig_start, func_end)
        # 兜底校验：提取片段的签名区必须命中目标 token，避免跨函数误抽取。
        if token and not self._signature_matches_token(extracted, token):
            return None
        return extracted

    def _ts_extract_function(
        self, source_text: str, target_line_index: int, target_function_name: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Use tree-sitter to extract function signature and body."""
        if self._ctx._ts_parser is None:
            return None, None
        try:
            source_bytes = source_text.encode("utf-8")
            tree = self._ctx._ts_parser.parse(source_bytes)
            root = tree.root_node
        except Exception:
            return None, None

        # Find function_definition nodes
        candidates = []

        def _visit(node):
            if node.type == "function_definition":
                start_line = node.start_point[0]
                end_line = node.end_point[0]
                candidates.append((node, start_line, end_line))
            for child in node.children:
                _visit(child)

        _visit(root)
        if not candidates:
            return None, None

        # Find best match: smallest span containing target line
        best = None
        best_span = float('inf')
        token = self.extract_simple_function_name(target_function_name or "") if target_function_name else ""
        for node, start, end in candidates:
            if start <= target_line_index <= end:
                if token:
                    text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
                    if not self._signature_matches_token(text, token):
                        continue
                span = end - start
                if span < best_span:
                    best_span = span
                    best = node
            elif target_function_name:
                # 非 target_line 命中时，仅允许“签名区”匹配 token；禁止函数体全文匹配导致串函数。
                text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
                if self._signature_matches_token(text, token):
                    span = end - start
                    if best is None or span < best_span:
                        best_span = span
                        best = node

        if best is None:
            return None, None

        # Use byte offsets to slice from UTF-8 bytes, then decode back to str
        full_text = source_bytes[best.start_byte:best.end_byte].decode("utf-8", errors="replace")
        # Split into signature and body
        brace_idx = full_text.find("{")
        if brace_idx > 0:
            signature = full_text[:brace_idx].strip()
        else:
            signature = full_text.split("\n")[0].strip()
        if token and not self._signature_matches_token(full_text, token):
            return None, None
        return signature, full_text

    def _signature_matches_token(self, function_text: str, token: str) -> bool:
        """仅在函数签名区匹配函数 token，避免被函数体中的调用语句误命中。"""
        tok = self.extract_simple_function_name(token)
        if not tok:
            return True
        text = str(function_text or "")
        sig = text.split("{", 1)[0]
        pat = re.compile(rf"\b~?{re.escape(tok)}\s*(?:<[^(){{}};]*>)?\s*\(")
        return bool(pat.search(sig))

    @staticmethod
    def _include_template_prefix(lines: List[str], sig_start: int) -> int:
        """Include immediately preceding template<...> lines in extracted function snippets."""
        start = sig_start
        i = sig_start - 1
        while i >= 0:
            stripped = lines[i].strip()
            if not stripped:
                break
            if stripped.startswith("template") or stripped in (">",):
                start = i
                i -= 1
                continue
            # Support multi-line template parameter lists immediately above the function.
            if start < sig_start and (stripped.endswith(",") or stripped.startswith("class ") or stripped.startswith("typename ")):
                start = i
                i -= 1
                continue
            break
        return start

    def _extract_by_brace_counting(
        self, lines: List[str], target_line_index: int, target_function_name: Optional[str]
    ) -> Optional[str]:
        """Fallback: regex + brace counting."""
        # Search backward for function definition
        search_start = max(0, target_line_index - 200)
        func_start = None
        for i in range(target_line_index, search_start - 1, -1):
            if i >= len(lines):
                continue
            if self.is_function_definition_line(lines[i]):
                func_start = i
                break
        if func_start is None:
            return None
        # Find opening brace
        brace_start = None
        for i in range(func_start, min(func_start + 50, len(lines))):
            if '{' in lines[i]:
                brace_start = i
                break
        if brace_start is None:
            return None
        # Count braces
        depth = 0
        func_end = None
        for i in range(brace_start, len(lines)):
            for ch in lines[i]:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        func_end = i
                        break
            if func_end is not None:
                break
        if func_end is None:
            return None
        return join_source_line_slice(lines, func_start, func_end)


# ==============================================================================
# CallerLocator — 反向调用者搜索
# ==============================================================================


class CallerLocator:
    """Caller finding: reverse call-site search + parallel scanning."""

    def __init__(self, ctx: LocatorContext, symbol_locator: SymbolLocator, file_locator: FileLocator):
        self._ctx = ctx
        self._symbol = symbol_locator
        self._file = file_locator

    def find_callers_of_crash_site(
        self,
        crash_function_name: str,
        code_roots: List[str],
        stack_priority_files: Optional[List[str]] = None,
        max_search_files: Optional[int] = None,
    ) -> List[CallChainFunction]:
        """Find functions that call the crash function.
        Order: stack files → nearby modules (budget-limited), no full code-root walk.
        """
        # Extract simple function name using multi-step logic
        simple_name = self._extract_call_target_name(crash_function_name)
        if not simple_name:
            return []
        # Normalize stack files
        stack_norm: List[str] = []
        for raw in stack_priority_files or []:
            if not raw:
                continue
            try:
                ap = os.path.abspath(raw)
            except Exception:
                ap = raw
            if os.path.isfile(ap) and self._ctx.is_supported_file(ap):
                stack_norm.append(ap)
        if not stack_norm:
            return []
        budget = max(32, min(max_search_files or self._ctx.config.max_crash_caller_search_files, 2000))
        # Collect nearby module files
        nearby_pool: List[str] = []
        nearby_cap = max(0, budget - len(stack_norm))
        if nearby_cap > 0:
            nearby_pool = self._file.collect_nearby_module_files(
                stack_norm, code_roots,
                max_files=min(nearby_cap * 4, self._ctx.config.max_nearby_module_scan_files * 4),
            )
        # Prioritize by stack proximity
        search_files = self._prioritize_paths_by_stack_proximity(
            stack_norm, nearby_pool, budget, call_hint_name=simple_name
        )
        if not search_files:
            return []
        logger.debug(
            "CallerLocator: searching %d files for calls to '%s' (budget=%d)",
            len(search_files), simple_name, budget,
        )
        return self._regex_scan_callers(simple_name, search_files, code_roots)

    def _extract_call_target_name(self, crash_function_name: str) -> str:
        """Extract simple function name from resolved/mangled crash function name."""
        if not crash_function_name:
            return ""
        s = crash_function_name.strip()
        # Handle Itanium mangled names (_Z...)
        if s.startswith("_Z"):
            m = re.match(r"^_Z(\d+)(.*)", s)
            if m:
                try:
                    n = int(m.group(1))
                    rest = m.group(2)
                    if n <= len(rest):
                        name = rest[:n]
                        if re.match(r'^[A-Za-z_]\w*$', name):
                            return name
                except ValueError:
                    pass
            # Nested mangled: extract last segment
            parts: List[str] = []
            i = 2
            n_len = len(s)
            while i < n_len:
                if s[i].isdigit():
                    j = i
                    while j < n_len and s[j].isdigit():
                        j += 1
                    try:
                        seg_len = int(s[i:j])
                    except ValueError:
                        break
                    seg_end = j + seg_len
                    if seg_len <= 0 or seg_end > n_len:
                        break
                    seg = s[j:seg_end]
                    if re.match(r'^[A-Za-z_]\w*$', seg):
                        parts.append(seg)
                    i = seg_end
                    continue
                i += 1
            if parts:
                return parts[-1]
        # Extract function name from qualified C++ signature
        m = re.search(r"(?:(?:^|::)\s*)([~]?[A-Za-z_]\w*)\s*(?:\[[^\]]+\])?\s*\(", s)
        if m:
            name = m.group(1)
            # Further simplify: remove namespace prefix
            return name.split("::")[-1] if "::" in name else name
        # Fallback: split on '(' and '::'
        if "(" in s:
            head = s.split("(", 1)[0]
            if "::" in head:
                return head.split("::")[-1].strip()
            return head.strip()
        return self._symbol.extract_simple_function_name(s)

    def _prioritize_paths_by_stack_proximity(
        self,
        stack_files: List[str],
        candidates: List[str],
        limit: int,
        call_hint_name: Optional[str] = None,
    ) -> List[str]:
        """Rank files by stack proximity and likely-call pre-filter."""
        if limit <= 0:
            return []
        out: List[str] = []
        seen: Set[str] = set()
        # Always include stack files first
        for raw in stack_files:
            try:
                ap = os.path.abspath(raw)
            except Exception:
                ap = raw
            if ap in seen or not os.path.isfile(ap):
                continue
            seen.add(ap)
            out.append(ap)
            if len(out) >= limit:
                return out
        stack_abs = list(seen)
        # Score remaining candidates
        scored: List[Tuple[int, int, str]] = []
        for raw in candidates or []:
            try:
                ca = os.path.abspath(raw)
            except Exception:
                ca = raw
            if ca in seen or not os.path.isfile(ca):
                continue
            # Compute path proximity score
            best_common = 0
            for sa in stack_abs:
                try:
                    common = os.path.commonpath([ca, sa])
                    if common and os.path.isdir(common):
                        best_common = max(best_common, len(common))
                except (ValueError, OSError):
                    continue
            # Call-likelihood boost
            call_boost = 0
            if call_hint_name:
                call_boost = 1 if self._file_likely_calls_function(ca, call_hint_name) else 0
            scored.append((call_boost, best_common, ca))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        for _, _, ca in scored:
            if len(out) >= limit:
                break
            if ca in seen:
                continue
            seen.add(ca)
            out.append(ca)
        return out

    @staticmethod
    def _file_likely_calls_function(path: str, simple_name: str) -> bool:
        """Lightweight pre-filter: does file likely contain a call to simple_name?"""
        if not simple_name or not path:
            return False
        needles = (
            f"{simple_name}(",
            f"->{simple_name}(",
            f".{simple_name}(",
            f"::{simple_name}(",
        )
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                chunk = f.read(131072)
            return any(n in chunk for n in needles)
        except OSError:
            return False

    def find_implicit_ctor_usage_callers(
        self,
        crash_function_name: str,
        code_roots: List[str],
        max_hits: int = 400,
    ) -> List[CallChainFunction]:
        """Find implicit constructor usage sites."""
        # Extract class name from constructor
        class_name = self._extract_class_from_ctor(crash_function_name)
        if not class_name:
            return []
        pattern = re.compile(rf"\b{re.escape(class_name)}\b")
        results: List[CallChainFunction] = []
        seen_keys: Set[str] = set()

        for code_root in code_roots:
            if not os.path.isdir(code_root):
                continue
            for dirpath, dirnames, filenames in os.walk(code_root):
                dirnames[:] = [d for d in dirnames if not self._ctx.should_skip_directory(d)]
                for fn in filenames:
                    if len(results) >= max_hits:
                        return results
                    fp = os.path.join(dirpath, fn)
                    if not self._ctx.is_supported_file(fp):
                        continue
                    content = self._ctx.read_file_cached(fp)
                    if not content or class_name not in content:
                        continue
                    lines = content.split("\n")
                    for idx, line in enumerate(lines):
                        if not pattern.search(line):
                            continue
                        if self._is_implicit_ctor_use(line, class_name):
                            func_name = self._symbol.extract_function_name_at_line(lines, idx + 1)
                            if not func_name:
                                continue
                            key = f"{func_name}:{fp}"
                            if key in seen_keys:
                                continue
                            seen_keys.add(key)
                            code = self._symbol.extract_full_function_code(lines, idx, func_name)
                            if not code:
                                continue
                            snippet = [ln.rstrip() for ln in code.split("\n") if ln.strip()]
                            if self._ctx.config.max_code_length > 0:
                                snippet = self._ctx.truncate_snippet(snippet)
                            results.append(CallChainFunction(
                                name=func_name,
                                file=fp,
                                snippet=snippet,
                                chain_origin="implicit_ctor_usage",
                            ))
        return results

    def _regex_scan_callers(
        self, simple_function_name: str, candidate_files: List[str], code_roots: List[str]
    ) -> List[CallChainFunction]:
        """Regex-based caller scan across candidate files."""
        pattern = re.compile(rf"\b{re.escape(simple_function_name)}\s*\(")
        results: List[CallChainFunction] = []
        seen_keys: Set[str] = set()
        max_callers = self._ctx.config.max_direct_callers

        for fp in candidate_files:
            if len(results) >= max_callers:
                break
            self._ctx.code_context_phase_check("caller_scan")
            content = self._ctx.read_file_cached(fp)
            if not content:
                continue
            # Pre-filter
            if simple_function_name not in content:
                continue
            lines = content.split("\n")
            for idx, line in enumerate(lines):
                if len(results) >= max_callers:
                    break
                if not pattern.search(line):
                    continue
                stripped = line.strip()
                # Skip definitions of the function itself
                if self._symbol.is_function_definition_line(stripped) and simple_function_name in stripped:
                    # Check if this IS the definition (not just a call in a definition line)
                    if re.search(rf"(?:[\w:<>,~*&\s]+\s+)[\w:]*{re.escape(simple_function_name)}\s*\(", stripped):
                        continue
                # Skip comments
                if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                    continue
                # Extract enclosing function
                caller_name = self._symbol.extract_function_name_at_line(lines, idx + 1)
                if not caller_name or caller_name == simple_function_name:
                    continue
                key = f"{caller_name}:{fp}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                # Extract caller function code
                code = self._symbol.extract_full_function_code(lines, idx, caller_name)
                if not code:
                    continue
                # Verify the call exists in extracted code
                if simple_function_name not in code:
                    continue
                snippet = [ln.rstrip() for ln in code.split("\n") if ln.strip()]
                if self._ctx.config.max_code_length > 0:
                    snippet = self._ctx.truncate_snippet(snippet)
                results.append(CallChainFunction(
                    name=caller_name,
                    file=fp,
                    snippet=snippet,
                    chain_origin="call_expression",
                ))
        return results

    def _extract_class_from_ctor(self, function_name: str) -> Optional[str]:
        """Extract class name from constructor: Class::Class(...) -> Class"""
        if not function_name:
            return None
        # Pattern: Class::Class or ~Class::~Class
        m = re.search(r"(\w+)::~?\1\s*\(", function_name)
        if m:
            return m.group(1)
        return None

    def _is_implicit_ctor_use(self, line: str, class_name: str) -> bool:
        """Check if line suggests implicit constructor usage."""
        stripped = line.strip()
        if not stripped:
            return False
        # Variable declaration: Type name;
        if re.search(rf"\b{re.escape(class_name)}\s+\w+\s*[;={{]", stripped):
            return True
        # new Type
        if f"new {class_name}" in stripped:
            return True
        # Temporary: Type(...)
        if re.search(rf"\b{re.escape(class_name)}\s*\(", stripped):
            # Exclude definition of constructor itself
            if f"::{class_name}" in stripped:
                return False
            return True
        return False


# ==============================================================================
# VariableLocator — 变量定位 + 读写分析
# ==============================================================================


class VariableLocator:
    """Variable usage finding and shared-variable function scanning."""

    def __init__(self, ctx: LocatorContext, symbol_locator: SymbolLocator, file_locator: FileLocator):
        self._ctx = ctx
        self._symbol = symbol_locator
        self._file = file_locator

    def find_variable_functions_for_vars(
        self,
        shared_vars: List[str],
        crash_function_name: str,
        code_roots: List[str],
        stack_priority_files: Optional[List[str]] = None,
        crash_local_files: Optional[List[str]] = None,
        owner_class: Optional[str] = None,
        owner_member_fields: Optional[List[str]] = None,
        owner_definition_files: Optional[List[str]] = None,
    ) -> List[VariableFunction]:
        """Find functions that use shared variables (4-tier scanning)."""
        if not shared_vars:
            return []
        cap = max(1, self._ctx.config.max_shared_var_related_functions)
        enough_threshold = max(cap * 2, 8)
        unique_functions: Dict[tuple, VariableFunction] = {}
        scanned_abs: Set[str] = set()
        owner_fields_set = set(owner_member_fields or [])
        owner_def_abs: Set[str] = set()
        for p in owner_definition_files or []:
            if p:
                try:
                    owner_def_abs.add(os.path.abspath(p))
                except Exception:
                    owner_def_abs.add(str(p))
        for p in crash_local_files or []:
            if p:
                try:
                    owner_def_abs.add(os.path.abspath(p))
                except Exception:
                    owner_def_abs.add(str(p))
        # Pre-compute bare variable names for fast pre-filter
        bare_vars = [v.split("::")[-1] if "::" in v else v for v in shared_vars]

        def _enough() -> bool:
            return len(unique_functions) >= enough_threshold

        def _scan_files(file_paths: List[str], tier_label: str) -> None:
            if _enough():
                return
            norm = self._ctx.normalize_scan_file_list(file_paths)
            for fp in norm:
                if _enough():
                    return
                try:
                    ap = os.path.abspath(fp)
                except Exception:
                    ap = fp
                if ap in scanned_abs:
                    continue
                scanned_abs.add(ap)
                self._ctx.code_context_phase_check("shared_var_scan")
                content = self._ctx.read_file_cached(fp)
                if not content:
                    continue
                # File-level pre-filter
                if not any(bv in content for bv in bare_vars):
                    continue
                lines = content.split("\n")
                for i, line in enumerate(lines, 1):
                    if _enough():
                        return
                    for var in shared_vars:
                        bare_var = var.split("::")[-1] if "::" in var else var
                        if not self.contains_variable_usage(line, bare_var):
                            continue
                        if not self.is_variable_usage_line(line, bare_var):
                            continue
                        # Owner-scoped filter for non-priority tiers
                        if not self._line_usage_matches_owner_scoped_var(
                            line, bare_var, owner_class, owner_fields_set,
                            tier_label, fp, owner_def_abs,
                        ):
                            continue
                        func_name = self._symbol.extract_function_name_at_line(lines, i)
                        if not func_name:
                            continue
                        code = self._symbol.extract_full_function_code(lines, i - 1, func_name)
                        if not code:
                            continue
                        relation = self.determine_variable_relation(line, var)
                        snippet = [ln.rstrip() for ln in code.split("\n") if ln.strip()]
                        if self._ctx.config.max_code_length > 0:
                            snippet = self._ctx.truncate_snippet(snippet)
                        scoped_var = self._scoped_var_key(bare_var, owner_class)
                        key = (func_name, fp, scoped_var or bare_var, relation)
                        if key not in unique_functions:
                            unique_functions[key] = VariableFunction(
                                variable=scoped_var or bare_var,
                                relation=relation,
                                name=func_name,
                                file=fp,
                                snippet=snippet,
                            )

        # Tier 1: Crash file + adjacent sources
        if crash_local_files:
            _scan_files(crash_local_files, "tier1_crash_local")

        # Tier 2: Stack frame source files
        if not _enough() and stack_priority_files:
            _scan_files(stack_priority_files, "tier2_stack_frames")

        # Tier 3: Nearby module files
        if not _enough() and stack_priority_files:
            nearby = self._file.collect_nearby_module_files(stack_priority_files, code_roots)
            _scan_files(nearby, "tier3_nearby_modules")

        # Tier 4: Full code-root walk (only if insufficient)
        if not _enough():
            all_files: List[str] = []
            # 尝试 rg 预筛：只找包含变量名的文件
            rg_pattern = "|".join(re.escape(bv) for bv in bare_vars) if bare_vars else None
            rg_hit_files = self._ctx.rg_grep_files(rg_pattern, code_roots) if rg_pattern else None
            if rg_hit_files is not None:
                # rg 成功：直接使用命中的文件列表（已自动过滤扩展名和排除目录）
                all_files = rg_hit_files[:5000]
                logger.info(f"rg 预筛变量扫描 Tier4: {len(all_files)} 个文件包含变量名")
            else:
                # fallback: 原有 os.walk
                for code_root in code_roots:
                    if not os.path.isdir(code_root):
                        continue
                    walk_i = 0
                    for dirpath, dirnames, filenames in os.walk(code_root):
                        dirnames[:] = [d for d in dirnames if not self._ctx.should_skip_directory(d)]
                        for fn in filenames:
                            fp = os.path.join(dirpath, fn)
                            if self._ctx.is_supported_file(fp):
                                all_files.append(fp)
                        walk_i += 1
                        if walk_i % 80 == 0:
                            self._ctx.code_context_phase_check("shared_var_full_repo")
                        if len(all_files) > 5000:
                            break
                    if len(all_files) > 5000:
                        break
            _scan_files(all_files, "tier4_full_walk")

        return list(unique_functions.values())[:cap]

    def _line_usage_matches_owner_scoped_var(
        self,
        line: str,
        bare_var: str,
        owner_class: Optional[str],
        owner_fields: Optional[Set[str]],
        tier_label: str,
        file_path: str,
        owner_definition_files: Optional[Set[str]],
    ) -> bool:
        """Filter: only keep hits related to crash's owning class in non-priority tiers."""
        # Priority tiers pass through without filter
        if tier_label in ("tier1_crash_local", "tier2_stack_frames"):
            return True
        if not owner_class or not owner_fields:
            return True
        if bare_var not in owner_fields:
            return False
        # Files in owner definition set always pass
        if owner_definition_files:
            try:
                ap = os.path.abspath(file_path)
            except Exception:
                ap = file_path
            if ap in owner_definition_files:
                return True
        # Check line content for owner class scope
        ln = line or ""
        if re.search(rf"\b{re.escape(owner_class)}\s*::", ln):
            return True
        if re.search(rf"(?:->|\.)\s*{re.escape(bare_var)}\b", ln):
            return True
        if "this->" in ln.replace(" ", "") and re.search(rf"\b{re.escape(bare_var)}\b", ln):
            return True
        return False

    def extract_variables_from_line(self, line: str) -> List[str]:
        """Extract variable names from a crash line."""
        if not line:
            return []
        variables: List[str] = []
        seen: Set[str] = set()
        # Pointer dereference: var->member
        for m in re.finditer(r"(\w+)\s*->", line):
            name = m.group(1)
            if name not in seen and len(name) > 1:
                seen.add(name)
                variables.append(name)
        # Member access: obj.member
        for m in re.finditer(r"(\w+)\s*\.\s*\w+", line):
            name = m.group(1)
            if name not in seen and len(name) > 1:
                seen.add(name)
                variables.append(name)
        # Assignment LHS
        for m in re.finditer(r"(\w+)\s*=(?!=)", line):
            name = m.group(1)
            if name not in seen and len(name) > 1:
                seen.add(name)
                variables.append(name)
        # Array access
        for m in re.finditer(r"(\w+)\s*\[", line):
            name = m.group(1)
            if name not in seen and len(name) > 1:
                seen.add(name)
                variables.append(name)
        # Filter out keywords
        keywords = {
            'if', 'else', 'for', 'while', 'return', 'switch', 'case', 'break',
            'continue', 'void', 'int', 'char', 'bool', 'auto', 'const', 'static',
            'this', 'new', 'delete', 'sizeof', 'nullptr', 'NULL', 'true', 'false',
        }
        return [v for v in variables if v not in keywords]

    def contains_variable_usage(self, line: str, variable_name: str) -> bool:
        """Quick check if line contains variable reference."""
        if not variable_name or variable_name not in line:
            return False
        return bool(re.search(rf"\b{re.escape(variable_name)}\b", line))

    def is_variable_usage_line(self, line: str, variable_name: str) -> bool:
        """Check if line is actual variable usage (not definition/comment)."""
        stripped = line.strip()
        if not stripped:
            return False
        # Exclude comments
        if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            return False
        # Exclude function definitions
        if self._symbol.is_function_definition_line(stripped):
            return False
        # Check usage patterns
        patterns = [
            rf"\b{re.escape(variable_name)}\s*=",
            rf"\b{re.escape(variable_name)}\s*->",
            rf"\b{re.escape(variable_name)}\s*\.",
            rf"\b{re.escape(variable_name)}\s*\[",
            rf"=\s*{re.escape(variable_name)}\b",
            rf"\(\s*{re.escape(variable_name)}\s*[,)]",
        ]
        for pat in patterns:
            if re.search(pat, stripped):
                return True
        return False

    def determine_variable_relation(self, line: str, variable_name: str) -> str:
        """Classify variable relation: read/write/assign/delete."""
        stripped = line.strip()
        bare = variable_name.split("::")[-1] if "::" in variable_name else variable_name
        # Delete
        if any(kw in stripped for kw in ['delete', 'free']):
            if bare in stripped:
                return 'delete'
        # Pointer dereference assignment (var->field = ...)
        if f"{bare}->" in stripped and "=" in stripped:
            return 'read'
        # Assignment LHS
        if "=" in stripped:
            parts = stripped.split("=")
            for part in parts[:-1]:
                if re.search(rf"\b{re.escape(bare)}\b", part):
                    # Check it's not ==, !=, <=, >=
                    eq_idx = stripped.find("=")
                    if eq_idx > 0 and stripped[eq_idx - 1] not in ('!', '<', '>', '='):
                        if eq_idx < len(stripped) - 1 and stripped[eq_idx + 1] != '=':
                            return 'write'
        # Assignment RHS or other usage
        return 'read'

    @staticmethod
    def _scoped_var_key(var_name: str, owner_class: Optional[str] = None) -> str:
        """Generate scoped variable key for deduplication."""
        if owner_class and "::" not in var_name:
            return f"{owner_class}::{var_name}"
        return var_name


# ==============================================================================
# CodeLocatorService — 统一门面
# ==============================================================================


class CodeLocatorService:
    """Unified facade composing FileLocator, SymbolLocator, CallerLocator, VariableLocator."""

    def __init__(
        self,
        config: LocatorConfig,
        code_index_service=None,
        ts_parser=None,
    ):
        self._ctx = LocatorContext(config, code_index_service, ts_parser)
        self.file_locator = FileLocator(self._ctx)
        self.symbol_locator = SymbolLocator(self._ctx)
        self.caller_locator = CallerLocator(self._ctx, self.symbol_locator, self.file_locator)
        self.variable_locator = VariableLocator(self._ctx, self.symbol_locator, self.file_locator)

    @property
    def ctx(self) -> LocatorContext:
        return self._ctx

    @property
    def search_stats(self) -> Dict[str, Any]:
        return self._ctx.search_stats

    # --- Top-level convenience methods ---

    def find_source_file(self, resolved_file: str, code_roots: List[str]) -> Optional[str]:
        """Resolve addr2line path to local source file."""
        return self.file_locator.find_source_file(resolved_file, code_roots)

    def find_function_definition(
        self, simple_name: str, code_roots: List[str]
    ) -> Optional[Tuple[str, int]]:
        """Find function definition (file, line)."""
        return self.symbol_locator.find_function_definition_location(simple_name, code_roots)

    def extract_function_body(
        self, lines: List[str], target_line_index: int, target_function_name: Optional[str] = None
    ) -> Optional[str]:
        """Extract complete function body."""
        return self.symbol_locator.extract_full_function_code(lines, target_line_index, target_function_name)

    def find_callers(
        self,
        crash_function_name: str,
        code_roots: List[str],
        stack_priority_files: Optional[List[str]] = None,
        max_search_files: Optional[int] = None,
    ) -> List[CallChainFunction]:
        """Find functions that call the crash function."""
        return self.caller_locator.find_callers_of_crash_site(
            crash_function_name, code_roots, stack_priority_files, max_search_files
        )

    def find_variable_usages(
        self,
        shared_vars: List[str],
        crash_function_name: str,
        code_roots: List[str],
        stack_priority_files: Optional[List[str]] = None,
        crash_local_files: Optional[List[str]] = None,
        owner_class: Optional[str] = None,
        owner_member_fields: Optional[List[str]] = None,
        owner_definition_files: Optional[List[str]] = None,
    ) -> List[VariableFunction]:
        """Find functions that use shared variables."""
        return self.variable_locator.find_variable_functions_for_vars(
            shared_vars, crash_function_name, code_roots,
            stack_priority_files, crash_local_files,
            owner_class, owner_member_fields, owner_definition_files,
        )
