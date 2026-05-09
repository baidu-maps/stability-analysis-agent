#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代码内容提供工具 - 重构版本 V2
根据add2line解析结果和代码根目录，生成大模型用于生成崩溃修复建议的结构化JSON内容
"""

import json
import os
import logging
import re
import time
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass, asdict, replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from tree_sitter_languages import get_parser as _ts_get_parser
except Exception:
    _ts_get_parser = None

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class _FindSourceFileTimeout(Exception):
    """单次「解析源文件路径」总超时，用于从深层循环中跳出。"""


class _CodeContextPhaseTimeout(Exception):
    """第三步「代码上下文」整阶段 wall-clock 超时，用于中止重扫描并返回可继续下游的 JSON。"""

    def __init__(self, phase: str = ""):
        self.phase = phase or "unknown"
        super().__init__(self.phase)


@dataclass
class CrashSummary:
    """崩溃摘要信息"""
    file: str
    function: str
    crash_line_number: int
    stack_address: str
    error_type: str
    thread_id: str
    # 可选：崩溃位置来源说明（当首帧被 atos 解析为库实现并已按 mangled 名推断时填写）
    crash_location_source: Optional[str] = None   # "from_add2line" | "from_log_deduce"
    crash_line_note: Optional[str] = None          # 说明文字，如精确行号未知时的提示
    # 可选：当前认为的崩溃行对应源码（from_add2line 时为精确行，from_log_deduce 时通常为函数定义行）
    crash_line_code: Optional[str] = None

@dataclass
class CrashFunction:
    """崩溃函数信息"""
    name: str
    signature: str
    snippet: List[str]       # 完整函数代码（从函数定义行到匹配的闭合括号）
    crash_line: str
    # 可选：崩溃位置来源说明（当首帧被 atos 解析为库实现并已按 mangled 名推断时填写）
    crash_location_source: Optional[str] = None
    crash_line_note: Optional[str] = None
    # 说明 snippet 为完整函数体而非截断片段
    snippet_scope: Optional[str] = "full_function"
    # 与源文件一致的 snippet 起止行号（1-based，含端点），便于回溯
    snippet_start_line: Optional[int] = None
    snippet_end_line: Optional[int] = None
    # 与 crash_line 文本对齐的 1-based 行号（附近重选展示行时填充；未重选时为 None）
    crash_line_number: Optional[int] = None

@dataclass
class CallChainFunction:
    """调用链函数信息"""
    name: str
    file: str
    snippet: List[str]
    # 可选：当该函数作为 pre_call_fun_in_same_parent_fun 出现时，标记其所在的直接调用崩溃函数的父函数名
    parent_fun: Optional[str] = None
    # 来源：call_expression 静态扫描 | implicit_ctor_usage 隐式构造使用点（声明/new/临时量等）
    chain_origin: Optional[str] = None

@dataclass
class VariableFunction:
    """变量相关函数信息"""
    variable: str
    relation: str  # "read", "write", "assign", "delete"
    name: str
    file: str
    snippet: List[str]

@dataclass
class ThreadContext:
    """线程上下文信息"""
    thread_id: str
    call_chain_from_add2line: List[str]
    shared_vars: Optional[List[str]] = None
    sync_primitives: Optional[List[str]] = None
    # 与 call_chain_from_add2line 等长：每帧的 resolved_file / resolved_line，供图构建优先按 addr2line 定位
    call_chain_frame_details: Optional[List[Dict[str, Any]]] = None

@dataclass
class RelatedFunction:
    """相关函数信息（同一类中的其他函数）"""
    name: str
    file: str
    snippet: List[str]
    relation_type: str  # "same_class", "same_variable", "memory_operation", "thread_operation"
    description: str  # 说明为什么这个函数相关

@dataclass
class GraphNode:
    """崩溃相关代码图中的节点（函数 / 变量 / 线程 等）"""
    id: str
    type: str  # "function" | "variable" | "thread"
    name: str
    file: Optional[str] = None
    signature: Optional[str] = None
    snippet: Optional[List[str]] = None
    # 与源文件一致的 snippet 起止行号（1-based，含端点）；仅 type=function 时通常有值
    snippet_start_line: Optional[int] = None
    snippet_end_line: Optional[int] = None
    # 可选角色：例如变量节点中标记 "crash_func_shared_var" 表示来自崩溃函数的共享变量
    role: Optional[str] = None

@dataclass
class GraphEdge:
    """崩溃相关代码图中的边，描述节点之间的关系"""
    from_id: str
    to_id: str
    type: str  # "calls_direct" | "use_shared_var"
    thread_id: Optional[str] = None
    relation: Optional[str] = None   # 如变量读写关系等

@dataclass
class ExecutionPath:
    """基于代码结构推断的调用路径（按入口函数 / 线程组织）"""
    id: str
    thread_id: Optional[str]
    nodes: List[str]  # 一串 GraphNode.id
    description: Optional[str] = None

@dataclass
class CrashGraph:
    """崩溃相关代码的图结构视图"""
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    # 基于源码静态分析推断出的调用路径（例如入口函数 → 崩溃函数）
    call_chain_from_code: List[ExecutionPath]
    # 按 add2line 符号化结果构造的调用链视图（保持与 thread_context 一致）
    call_chain_from_add2line: List[Dict[str, Any]]
    # 仅当 edges 为空时填充，说明未生成边的原因（供 CLI/下游展示）
    edges_empty_reason: Optional[str] = None

@dataclass
class CrashAnalysisData:
    """崩溃分析数据结构（顶层仍保留 crash_summary/crash_func，其余统一收敛到 graph）"""
    crash_summary: CrashSummary
    crash_func: CrashFunction
    graph: CrashGraph

def _normalize_code_roots_arg(code_root: Union[str, List[str], None]) -> List[str]:
    """将 CLI/调用方传入的单根或多根参数规范为去重后的绝对路径列表。"""
    if code_root is None:
        return []
    if isinstance(code_root, str):
        if not str(code_root).strip():
            return []
        return [str(Path(code_root).resolve())]
    out: List[str] = []
    seen: set[str] = set()
    for p in code_root:
        if not p or not str(p).strip():
            continue
        a = str(Path(p).resolve())
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _file_under_any_project_root(path: Path, code_roots_abs: List[str]) -> bool:
    try:
        rp = path.resolve()
        for r in code_roots_abs:
            try:
                rp.relative_to(Path(r))
                return True
            except ValueError:
                continue
    except Exception:
        return False
    return False


class CodeContentProvider:
    """代码内容提供器 - 重构版本 V2"""
    
    def __init__(self, exclude_dirs: Optional[List[str]] = None, 
                 include_subdirs: Optional[List[str]] = None,
                 code_index_service=None,
                 code_parser_backend: Optional[str] = None,
                 max_static_call_chain_depth: Optional[int] = None,
                 max_direct_callers: Optional[int] = None,
                 max_shared_var_related_functions: Optional[int] = None,
                 max_symbol_only_rescues: Optional[int] = None,
                 find_source_timeout_sec: Optional[float] = None,
                 code_context_timeout_sec: Optional[float] = None):
        """
        初始化代码内容提供器
        
        Args:
            exclude_dirs: 要排除的目录名列表（如 ['test', 'third_party', 'build']）
            include_subdirs: 要包含的子目录列表（如 ['src', 'lib']），None 表示包含所有
            code_index_service: 代码索引服务实例（可选）
            max_static_call_chain_depth: 静态调用链 graph.call_chain_from_code 的最大节点数（含崩溃函数），默认 5，至少 1
            max_direct_callers: 静态分析得到的「直接调用崩溃函数」候选最多保留个数，默认 10，至少 1，上限 512
            max_shared_var_related_functions: 共享变量相关函数记录最多保留条数，默认 10，至少 1，上限 512
            max_symbol_only_rescues: add2line 缺失 file:line 时，按符号名兜底定位的最多尝试帧数，默认 5，允许 0（关闭）
            find_source_timeout_sec: 单次 _find_source_file 总超时（秒）；None 表示 360
            code_context_timeout_sec: 第三步整阶段 wall-clock 上限（秒）；None 表示 180；0 表示不限制
        """
        self.supported_extensions = {
            '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx',
            '.java', '.kt', '.swift', '.m', '.mm', '.py', '.js', '.ts'
        }
        self.max_file_size = 1024 * 1024  # 1MB
        self.max_code_length = 2000  # 代码片段长度限制
        # 静态「崩溃函数调用链」路径节点数（含崩溃函数）：至少 1（仅崩溃函数）；上限由 max_static_call_chain_depth 控制
        _depth = 5 if max_static_call_chain_depth is None else int(max_static_call_chain_depth)
        self.max_static_call_chain_nodes = max(1, min(_depth, 128))
        self.min_static_call_chain_nodes = 1
        _mdc = 10 if max_direct_callers is None else int(max_direct_callers)
        self.max_direct_callers = max(1, min(_mdc, 512))
        _msv = 10 if max_shared_var_related_functions is None else int(max_shared_var_related_functions)
        self.max_shared_var_related_functions = max(1, min(_msv, 512))
        _msr = 5 if max_symbol_only_rescues is None else int(max_symbol_only_rescues)
        self.max_symbol_only_rescues = max(0, min(_msr, 512))
        
        # 默认排除的目录（常见的不需要搜索的目录）
        default_exclude_dirs = [
            'test', 'tests', 'testing', 'test_utils',
            'third_party', 'third-party', 'thirdparty', 'vendor', 'external',
            'build', 'builds', 'out', 'output', 'bin', 'obj',
            'generated', 'gen', 'generated_files',
            'node_modules', '.git', '.svn', '.hg',
            'cmake-build', 'cmake_build', '.idea', '.vscode',
            'docs', 'documentation', 'doc'
        ]
        
        self.exclude_dirs = set((exclude_dirs or []) + default_exclude_dirs)
        self.include_subdirs = set(include_subdirs) if include_subdirs else None
        
        # 代码索引服务（可选）
        self.code_index_service = code_index_service
        
        # 解析后端：tree_sitter（默认） / regex（仅显式指定），不再静默回退，便于定位环境问题
        backend_raw = (code_parser_backend or "tree-sitter").strip().lower()
        if backend_raw in {"tree-sitter", "tree_sitter", "treesitter", "ts"}:
            self.code_parser_backend = "tree_sitter"
        else:
            self.code_parser_backend = "regex"
        self._ts_parser = None
        if self.code_parser_backend == "tree_sitter":
            if _ts_get_parser is None:
                raise RuntimeError(
                    "未安装 tree_sitter_languages，无法使用 tree-sitter 代码解析后端。"
                    "请执行: pip install tree-sitter-languages"
                    "；若需使用 regex 后端，请显式传入 --code-parser-backend regex"
                )
            try:
                self._ts_parser = _ts_get_parser("cpp")
            except Exception as e:
                raise RuntimeError(
                    f"tree-sitter C++ 解析器初始化失败: {e}。"
                    "请检查依赖或通过 --code-parser-backend regex 显式使用 regex。"
                ) from e
        
        # 搜索统计
        self.search_stats = {
            'files_scanned': 0,
            'files_skipped_size': 0,
            'files_skipped_excluded': 0,
            'files_skipped_extension': 0,
            'files_read': 0,
            'search_time': 0.0
        }
        
        # 「addr2line 路径 → 本地源文件」查找预算（秒）。
        # 兼容原语义：该值仍是用户给定的核心超时参数；
        # 新策略：在一次代码上下文生成过程中按“剩余预算”动态调整单次查找窗口。
        _fs_to = 360.0 if find_source_timeout_sec is None else float(find_source_timeout_sec)
        self.find_source_timeout_sec = max(1.0, min(_fs_to, 3600.0))
        self._find_source_deadline: Optional[float] = None
        self._find_source_cur_timeout_sec: float = self.find_source_timeout_sec
        self._find_source_budget_deadline: Optional[float] = None
        self._find_source_budget_total_sec: float = self.find_source_timeout_sec
        self._find_source_lookup_calls: int = 0

        # 第三步「代码上下文」整阶段 wall-clock（与 find_source 预算独立；到点必停并输出可序列化结果）
        _cct = 180.0 if code_context_timeout_sec is None else float(code_context_timeout_sec)
        if _cct <= 0:
            self.code_context_timeout_sec = 0.0
        else:
            self.code_context_timeout_sec = max(1.0, min(_cct, 7200.0))
        self._code_context_deadline: Optional[float] = None
        
        # 线程安全相关关键词
        self.thread_keywords = {
            'thread', 'pthread', 'std::thread', 'mutex', 'lock', 'unlock',
            'atomic', 'volatile', 'shared_ptr', 'weak_ptr', 'condition_variable'
        }
        
        # 内存管理相关关键词
        self.memory_keywords = {
            'malloc', 'free', 'new', 'delete', 'new[]', 'delete[]',
            'calloc', 'realloc', 'shared_ptr', 'unique_ptr', 'weak_ptr'
        }

        # ========== 缓存机制（性能优化）==========
        self._function_def_cache: Dict[str, Tuple[str, int]] = {}  # 函数定义位置缓存 (file_path, line)
        self._file_mtime_cache: Dict[str, float] = {}  # 文件修改时间缓存
        self._cpp_candidate_files_cache: Dict[Tuple[str, str], List[str]] = {}

        # ========== 调用链搜索缓存（跨进程/跨次分析复用）==========
        # 缓存格式: { (code_root, function_name): [CallChainFunction, ...] }
        self._call_chain_cache: Dict[Tuple[str, str], List[Any]] = {}
        # 缓存文件路径（可选持久化到磁盘）
        self._cache_file_path: Optional[Path] = None
        # 类型信息缓存：按“源文件路径”缓存可见类信息（成员类型 + 继承关系）
        self._class_info_by_source_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}

        # ========== 并行扫描配置 ==========
        # 并行 worker 数量，None 表示自动（CPU核心数），0 表示禁用并行
        self.max_workers: Optional[int] = None
        # 触发并行扫描的最小文件数阈值
        self.parallel_threshold: int = 100

        # ========== 代码片段截断策略配置 ==========
        # 截断模式: "smart"(智能截断保留头尾), "head"(只保留头部), "full"(不截断)
        self._truncation_mode = "smart"
        # 截断时保留的头部比例
        self._truncation_head_ratio = 0.3
        # 截断时保留的尾部比例
        self._truncation_tail_ratio = 0.4

    def _truncate_snippet(self, snippet: List[str], max_length: int = None) -> List[str]:
        """
        智能截断代码片段

        Args:
            snippet: 原始代码片段（行列表）
            max_length: 最大字符数限制（默认使用 self.max_code_length）

        Returns:
            截断后的代码片段
        """
        max_length = max_length or self.max_code_length

        # 计算当前总长度
        total_length = sum(len(line) for line in snippet)
        if total_length <= max_length:
            return snippet

        # 根据截断模式选择策略
        if self._truncation_mode == "full":
            return snippet
        elif self._truncation_mode == "head":
            # 只保留头部
            result = []
            current_length = 0
            for line in snippet:
                if current_length + len(line) > max_length:
                    result.append(f"... [{len(snippet) - len(result)} more lines]")
                    break
                result.append(line)
                current_length += len(line)
            return result
        else:  # smart mode
            # 智能截断：保留头尾
            head_size = int(max_length * self._truncation_head_ratio)
            tail_size = int(max_length * self._truncation_tail_ratio)

            result = []
            current_length = 0

            # 头部
            for line in snippet:
                if current_length + len(line) > head_size:
                    break
                result.append(line)
                current_length += len(line)

            # 检查是否需要截断
            remaining_length = total_length - current_length
            if remaining_length > tail_size:
                # 添加省略提示
                omitted_lines = len(snippet) - len(result) - self._estimate_tail_lines(snippet, tail_size)
                result.append(f"... [{omitted_lines} lines omitted] ...")

                # 尾部
                tail_start = len(snippet) - self._estimate_tail_lines(snippet, tail_size)
                for line in snippet[tail_start:]:
                    result.append(line)
            else:
                # 剩余内容直接添加
                for line in snippet[len(result):]:
                    result.append(line)

            return result

    def _estimate_tail_lines(self, snippet: List[str], target_length: int) -> int:
        """估算从尾部需要保留多少行"""
        current_length = 0
        count = 0
        for line in reversed(snippet):
            current_length += len(line)
            count += 1
            if current_length >= target_length:
                break
        return count

    # ========== 增量更新支持 ==========

    def _is_cache_valid(self, file_path: str) -> bool:
        """
        检查文件缓存是否有效（基于修改时间）

        Args:
            file_path: 文件路径

        Returns:
            缓存是否有效
        """
        if not os.path.isfile(file_path):
            return False
        try:
            current_mtime = os.path.getmtime(file_path)
            cached_mtime = self._file_mtime_cache.get(file_path)
            return cached_mtime is not None and cached_mtime == current_mtime
        except OSError:
            return False

    def _update_file_cache(self, file_path: str) -> None:
        """更新文件的修改时间缓存"""
        try:
            if os.path.isfile(file_path):
                self._file_mtime_cache[file_path] = os.path.getmtime(file_path)
        except OSError:
            pass

    def _get_cached_function_defs(self, file_path: str, function_name: str) -> Optional[Tuple[str, int]]:
        """
        获取缓存的函数定义位置

        Args:
            file_path: 文件路径
            function_name: 函数名

        Returns:
            (文件路径, 行号) 或 None
        """
        if not self._is_cache_valid(file_path):
            return None
        cache_key = f"{file_path}:{function_name}"
        return self._function_def_cache.get(cache_key)

    def _cache_function_def(self, file_path: str, function_name: str, line_no: int) -> None:
        """
        缓存函数定义位置

        Args:
            file_path: 文件路径
            function_name: 函数名
            line_no: 行号
        """
        cache_key = f"{file_path}:{function_name}"
        self._function_def_cache[cache_key] = (file_path, line_no)
        self._update_file_cache(file_path)

    def clear_caches(self) -> None:
        """清空所有缓存"""
        self._function_def_cache.clear()
        self._file_mtime_cache.clear()
        self._cpp_candidate_files_cache.clear()
        logger.info("已清空代码内容提供器的缓存")

    # ========== 性能优化：并行文件扫描 ==========

    def _parallel_scan_files(self, file_paths: List[str], max_workers: int = 4) -> Dict[str, Any]:
        """
        并行扫描多个文件，提取函数定义信息

        Args:
            file_paths: 文件路径列表
            max_workers: 最大工作线程数

        Returns:
            扫描结果字典 {file_path: {functions: [...], success: bool}}
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: Dict[str, Any] = {}

        def _scan_single_file(file_path: str) -> Tuple[str, Dict[str, Any]]:
            result = {"functions": [], "success": False, "error": None}
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                functions = []
                for i, line in enumerate(lines, 1):
                    probe = self._cpp_signature_probe_string(lines, i - 1)
                    if probe:
                        functions.append({"line": i, "text": probe})
                result["functions"] = functions
                result["success"] = True
            except (IOError, OSError) as e:
                result["error"] = str(e)
            return (file_path, result)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_scan_single_file, fp): fp for fp in file_paths}
            for future in as_completed(futures):
                try:
                    file_path, result = future.result()
                    results[file_path] = result
                except Exception as e:
                    file_path = futures[future]
                    results[file_path] = {"functions": [], "success": False, "error": str(e)}

        return results
    
    def _is_supported_file(self, file_path: str) -> bool:
        """检查是否为支持的文件类型"""
        return Path(file_path).suffix.lower() in self.supported_extensions
    
    def _is_file_readable(self, file_path: str) -> bool:
        """检查文件是否可读"""
        try:
            return os.path.isfile(file_path) and os.access(file_path, os.R_OK)
        except Exception:
            return False
    
    def _should_skip_file(self, file_path: str, root: str) -> bool:
        """
        判断是否应该跳过该文件
        
        Returns:
            True 表示应该跳过，False 表示应该处理
        """
        # 检查文件大小（在读取前检查，避免读取大文件）
        try:
            file_size = os.path.getsize(file_path)
            if file_size > self.max_file_size:
                self.search_stats['files_skipped_size'] += 1
                logger.debug(f"跳过过大文件 ({file_size / 1024 / 1024:.2f}MB): {file_path}")
                return True
        except Exception:
            pass
        
        # 检查文件扩展名
        if not self._is_supported_file(file_path):
            self.search_stats['files_skipped_extension'] += 1
            return True
        
        # 检查是否在排除的目录中
        rel_path = os.path.relpath(file_path, root) if root else file_path
        path_parts = Path(rel_path).parts
        
        for part in path_parts:
            if part in self.exclude_dirs:
                self.search_stats['files_skipped_excluded'] += 1
                logger.debug(f"跳过排除目录中的文件: {file_path} (目录: {part})")
                return True
        
        # 如果指定了包含的子目录，检查是否在包含列表中
        if self.include_subdirs:
            # 检查路径的任何部分是否在包含列表中
            if not any(part in self.include_subdirs for part in path_parts):
                self.search_stats['files_skipped_excluded'] += 1
                logger.debug(f"跳过不在包含目录中的文件: {file_path}")
                return True
        
        return False
    
    def _should_skip_directory(self, dir_name: str) -> bool:
        """判断是否应该跳过该目录（在 os.walk 中使用）"""
        return dir_name in self.exclude_dirs

    def _is_external_path(self, resolved_file: str, code_roots_abs: List[str]) -> bool:
        """
        判断 resolved_file 是否为所有工程根之外的外部系统/工具链路径。
        命中后应直接跳过，避免无意义的全仓库查找与噪音节点。
        """
        if not resolved_file:
            return False
        if not os.path.isabs(resolved_file):
            return False

        normalized = resolved_file.replace("\\", "/")

        # 常见外部路径前缀：系统目录、Xcode/Android NDK SDK 工具链、用户缓存等
        external_prefixes = (
            "/usr/",
            "/System/",
            "/Applications/Xcode.app/",
            "/Library/Developer/",
            "/opt/homebrew/",
            "/Users/",
        )

        # 若路径落在任一工程根下，则不是“外部路径”
        try:
            resolved_abs = os.path.abspath(resolved_file)
            for cr in code_roots_abs or []:
                try:
                    common_path = os.path.commonpath([cr, resolved_abs])
                    if common_path == cr:
                        return False
                except Exception:
                    continue
        except Exception:
            pass

        # 用户目录下进一步识别常见 SDK/NDK/缓存路径，避免误伤普通工作区
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
        # Harmony / OpenHarmony / OHOS NDK、通用工具链与 libc++ 头（addr2line 常解析到此，需走外部帧 hint）
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

    def _append_external_frame_semantic_hint(
        self,
        semantic_hints: List[Dict[str, Any]],
        original_idx: int,
        frame: Dict[str, Any],
    ) -> None:
        """
        为「不在工程根下可展开」的堆栈帧写入 stack_semantic_hints，
        供 05_ai_final_tip 按原始帧号输出 [外部库语义] 行，与 [源码函数] 行合并成完整栈语义。
        """
        rf = (frame.get("resolved_function") or "").strip()
        resolved_file = (frame.get("resolved_file") or "").strip()
        ext_sem = self._extract_external_frame_semantics(rf)
        type_hints = list(ext_sem.get("type_hints") or [])
        op_text = (ext_sem.get("operation_text") or "").strip()
        low = rf.lower()
        if (
            "mutex" in low
            or "lock_guard" in low
            or "unique_lock" in low
            or "__libcpp_mutex" in low
            or "__mutex" in low
        ) and not op_text:
            ext_sem["operation_text"] = (
                "标准库/运行时互斥锁或 RAII 守卫（线程同步语义，非业务逻辑路径）"
            )
        semantic_hints.append(
            {
                "hint_kind": "external_template_type",
                "severity": "info",
                "source_frame_index": original_idx,
                "resolved_function": rf,
                "resolved_file": resolved_file,
                "resolved_line": frame.get("resolved_line", 0),
                "summary": "外部库帧（原始符号保留 + 可选类型语义提炼）。",
                "details": {
                    "type_hints": type_hints,
                    "owner_type": ext_sem.get("owner_type"),
                    "operation_kind": ext_sem.get("operation_kind"),
                    "operation_text": ext_sem.get("operation_text"),
                    "resolved_function_raw": rf,
                    "template_owner": "external_library",
                },
                "attach_policy": "attach_to_next_project_frame",
            }
        )

    def _extract_template_type_hints(self, resolved_function: str) -> List[str]:
        """从函数签名中提取模板类型线索（用于外部帧语义提示）。"""
        if not resolved_function:
            return []
        hints: List[str] = []

        # 常见 shared_ptr<T> / vector<T> / map<K,V> 的模板实参提取
        for m in re.finditer(r"<([^<>]+)>", resolved_function):
            raw = m.group(1).strip()
            if not raw:
                continue
            # 只取逗号前第一个类型，降低噪音
            first = raw.split(",")[0].strip()
            first = re.sub(r"\bconst\b", "", first).replace("&", "").replace("*", "").strip()
            if "::" in first:
                first_simple = first.split("::")[-1]
            else:
                first_simple = first
            if first_simple and re.match(r"^[A-Za-z_]\w*$", first_simple):
                hints.append(first_simple)

        # 去重保序
        out: List[str] = []
        seen = set()
        for h in hints:
            if h in seen:
                continue
            seen.add(h)
            out.append(h)
        return out

    def _extract_external_frame_semantics(self, resolved_function: str) -> Dict[str, Any]:
        """提取外部帧语义：所有者模板类型 + 调用动作（如拷贝构造）。"""
        semantics: Dict[str, Any] = {
            "type_hints": self._extract_template_type_hints(resolved_function),
            "owner_type": None,
            "operation_kind": "unknown",
            "operation_text": "",
        }
        if not resolved_function:
            return semantics

        # 优先处理常见 shared_ptr<T>::shared_ptr(...) 语义
        sp_m = re.search(r"shared_ptr\s*<\s*(.+?)\s*>\s*::\s*shared_ptr(?:\[[^\]]+\])?\s*\(", resolved_function)
        if sp_m:
            pointee = sp_m.group(1).strip()
            pointee_simple = pointee.split("::")[-1].strip()
            owner_simple = f"shared_ptr<{pointee_simple}>"
            semantics["owner_type"] = owner_simple
            semantics["operation_kind"] = "constructor"
            semantics["operation_text"] = f"执行 {owner_simple} 构造函数"

            pm = re.search(r"\((.*)\)", resolved_function)
            params = pm.group(1) if pm else ""
            normalized = params.replace(" ", "")
            # const shared_ptr<T>& / shared_ptr<T> const& 视为拷贝构造
            if ("const&" in normalized or "&" in normalized) and "shared_ptr<" in normalized:
                semantics["operation_kind"] = "copy_constructor"
                semantics["operation_text"] = f"执行 {owner_simple} 拷贝构造函数"
            return semantics

        # 例如：vector<T>::~vector(...) / map<K,V>::map(...)
        owner_m = re.search(r"([A-Za-z_][\w:]*\s*<.+?>)\s*::\s*([~]?\w+)\s*\(", resolved_function)
        if owner_m:
            owner = owner_m.group(1).strip()
            method = owner_m.group(2).strip()
            owner_simple = re.sub(r"(?:\w+::)+", "", owner).replace(" ", "")
            semantics["owner_type"] = owner_simple

            # constructor / copy constructor / destructor 判定
            owner_base = owner_simple.split("<", 1)[0]
            if method == owner_base:
                params = ""
                pm = re.search(r"\((.*)\)", resolved_function)
                if pm:
                    params = pm.group(1)
                normalized = params.replace(" ", "")
                if "const&" in normalized and owner_base in normalized:
                    semantics["operation_kind"] = "copy_constructor"
                    semantics["operation_text"] = f"执行 {owner_simple} 拷贝构造函数"
                else:
                    semantics["operation_kind"] = "constructor"
                    semantics["operation_text"] = f"执行 {owner_simple} 构造函数"
            elif method.startswith("~"):
                semantics["operation_kind"] = "destructor"
                semantics["operation_text"] = f"执行 {owner_simple} 析构函数"
            else:
                semantics["operation_kind"] = "member_function"
                semantics["operation_text"] = f"执行 {owner_simple}::{method}"

        return semantics

    def _extract_decl_window(self, file_path: str, line_no: int, radius: int = 4) -> List[str]:
        """提取声明附近窗口，供 implicit/defaulted 场景提示。"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            if not lines:
                return []
            idx = max(0, min(len(lines) - 1, (line_no or 1) - 1))
            start = max(0, idx - radius)
            end = min(len(lines), idx + radius + 1)
            return [ln.rstrip("\n") for ln in lines[start:end] if ln.strip()]
        except Exception:
            return []

    def _extract_type_declaration_block(self, file_path: str, type_name: str) -> List[str]:
        """按类型名精准提取 struct/class 声明块，避免截窗混入相邻定义。"""
        if not file_path or not type_name:
            return []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if not content.strip():
                return []

            # 优先匹配 typedef struct tagXxx { ... } Xxx;
            typedef_pattern = re.compile(
                rf"typedef\s+struct\s+{re.escape(type_name)}\b[\s\S]*?\}}\s*[A-Za-z_]\w*\s*;",
                re.MULTILINE,
            )
            m = typedef_pattern.search(content)
            if not m:
                # 兼容普通 struct/class 定义
                struct_pattern = re.compile(
                    rf"(?:struct|class)\s+{re.escape(type_name)}\b[\s\S]*?\}}\s*;",
                    re.MULTILINE,
                )
                m = struct_pattern.search(content)

            if not m:
                return []

            block = m.group(0)
            lines = [ln.rstrip("\n") for ln in block.splitlines() if ln.strip()]
            return lines
        except Exception:
            return []

    def _is_implicit_default_special_member(
        self, resolved_function: str, source_file: str, resolved_line: int
    ) -> Tuple[bool, List[str], List[str], str]:
        """
        判断是否为隐式默认 special member（当前重点析构函数）。
        返回: (is_implicit_defaulted, declaration_window, member_type_hints, operation_text)
        """
        if not resolved_function or "::~" not in resolved_function:
            return False, [], [], ""
        if not source_file or not self._is_file_readable(source_file):
            return False, [], [], ""

        # 解析析构函数名：Class::~Class(...)
        m = re.search(r"::\s*~\s*([A-Za-z_]\w*)\s*\(", resolved_function)
        if not m:
            return False, [], [], ""
        dtor_name = m.group(1)

        try:
            with open(source_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            # 文件内存在显式析构声明/定义则不视为隐式默认
            if re.search(rf"~\s*{re.escape(dtor_name)}\s*\(", content):
                return False, [], [], ""
        except Exception:
            return False, [], [], ""

        decl_window = self._extract_type_declaration_block(source_file, dtor_name)
        if not decl_window:
            # 兜底：极少数情况下类型声明无法精准匹配，再回退窗口截取
            decl_window = self._extract_decl_window(source_file, resolved_line, radius=5)
        member_hints: List[str] = []
        for line in decl_window:
            for t in self._extract_template_type_hints(line):
                member_hints.append(t)

        # 去重
        member_hints = sorted(set(member_hints))
        operation_text = f"执行 {dtor_name} 默认析构函数"
        return True, decl_window, member_hints, operation_text

    def _maybe_append_addr2line_line_unknown_hint(
        self,
        semantic_hints: List[Dict[str, Any]],
        original_idx: int,
        frame: Dict[str, Any],
        actual_file_path: str,
    ) -> None:
        """
        工程内帧已解析出文件与符号，但 addr2line 行号为 0 时追加语义提示，避免 03 与 02 对照时信息丢失。
        """
        try:
            rline_chk = int(frame.get("resolved_line") or 0)
        except (TypeError, ValueError):
            rline_chk = 0
        if rline_chk > 0:
            return
        rf = (frame.get("resolved_function") or "").strip()
        if not rf and not actual_file_path:
            return
        semantic_hints.append(
            {
                "hint_kind": "addr2line_line_unknown",
                "severity": "warning",
                "source_frame_index": original_idx,
                "resolved_function": frame.get("resolved_function", ""),
                "resolved_file": actual_file_path,
                "resolved_line": 0,
                "summary": (
                    "addr2line 已解析到工程内源文件与符号，但行号为 0（无精确源码行）。"
                    "常见原因：调试信息不完整、编译优化或内联导致行号缺失；"
                    "02 中该帧仍保留 file/symbol，主崩溃节点无法按此行定位。"
                ),
                "details": {
                    "reason": "resolved_line_zero",
                },
                "attach_policy": "attach_to_next_project_frame",
            }
        )

    def _ts_enclosing_record_name_at_line(self, source_text: str, line_index_0based: int) -> Optional[str]:
        """返回包含该行、且跨度最小的 struct/class 的名称（用于成员子对象默认构造场景）。"""
        if not (self._ts_parser and source_text):
            return None
        try:
            tree = self._ts_parser.parse(source_text.encode("utf-8", errors="ignore"))
            root = tree.root_node
        except Exception:
            return None
        best = None
        best_span: Optional[int] = None
        stack = [root]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            if node.type in ("struct_specifier", "class_specifier"):
                try:
                    st = node.start_point[0]
                    ed = node.end_point[0]
                except Exception:
                    continue
                if st <= line_index_0based <= ed:
                    span = ed - st
                    if best is None or span < (best_span or 10**9):
                        best = node
                        best_span = span
            try:
                stack.extend(child for child in node.children if child is not None)
            except Exception:
                continue
        if best is None:
            return None
        try:
            nm = best.child_by_field_name("name")
        except Exception:
            nm = None
        if nm is not None:
            return source_text[nm.start_byte:nm.end_byte].strip()
        return None

    def _enclosing_struct_name_for_field_line(
        self, lines: List[str], line_number_1based: int
    ) -> Optional[str]:
        """
        从某行向上找最近的 struct/class 定义行，取类型名（成员子对象场景）。
        tree-sitter 在部分头文件中 name 切片不可靠时，正则更稳。
        """
        L0 = line_number_1based - 1
        if L0 < 0 or L0 >= len(lines):
            return None
        src = "\n".join(lines)
        ts_name: Optional[str] = None
        if self._ts_parser:
            ts_name = self._ts_enclosing_record_name_at_line(src, L0)
        if ts_name and re.match(r"^[A-Za-z_]\w*$", ts_name.strip()):
            ts_st = ts_name.strip()
            # tree-sitter 在 class BAIDU_VI_EXPORT Foo 上可能取到导出宏名，交给下方与 decl_line 一致的提取逻辑
            if not self._looks_like_cpp_export_macro_typename(ts_st):
                return ts_st
        for i in range(L0, -1, -1):
            line = lines[i]
            m = re.match(
                r"^\s*(?:template\s*<[^>]+>\s*)?(?:struct|class)\s+",
                line,
            )
            if m:
                cn = self._extract_class_or_struct_name_from_decl_line(line.strip())
                if cn:
                    return cn
        return None
    
    def _looks_like_cpp_export_macro_typename(self, name: str) -> bool:
        """是否为 class BAIDU_VI_EXPORT Foo 中的导出宏（误作类型名时需跳过）。"""
        n = (name or "").strip()
        if not n:
            return False
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", n) and ("_EXPORT" in n or n.endswith("_API")):
            return True
        return n in ("API", "DLL")
    
    def _find_source_budget_remaining_sec(self) -> float:
        if self._find_source_budget_deadline is None:
            return self.find_source_timeout_sec
        return max(0.0, self._find_source_budget_deadline - time.monotonic())

    def _compute_dynamic_find_source_timeout_sec(self) -> float:
        base = self.find_source_timeout_sec
        remaining = self._find_source_budget_remaining_sec()
        calls = self._find_source_lookup_calls
        if remaining <= 0.0:
            return 0.0
        # 预算宽松：前几次允许更充分搜索；预算紧张：快速收敛
        if remaining >= base * 0.8 and calls <= 2:
            return min(base, remaining)
        if remaining <= base * 0.35:
            return min(max(1.0, remaining * 0.5), remaining)
        return min(max(1.0, remaining * 0.75), base, remaining)

    def _find_source_deadline_begin(self) -> None:
        if self._find_source_budget_deadline is None:
            self._find_source_budget_total_sec = self.find_source_timeout_sec
            self._find_source_budget_deadline = time.monotonic() + self._find_source_budget_total_sec
            logger.info(
                "源文件定位动态预算已启用：总预算 %.1fs（参数 --find-source-timeout-sec）",
                self._find_source_budget_total_sec,
            )
        self._find_source_lookup_calls += 1
        eff = self._compute_dynamic_find_source_timeout_sec()
        self._find_source_cur_timeout_sec = eff
        if eff <= 0.0:
            raise _FindSourceFileTimeout()
        self._find_source_deadline = time.monotonic() + eff

    def _find_source_deadline_clear(self) -> None:
        self._find_source_deadline = None

    def _find_source_check_time(self) -> None:
        if self._find_source_deadline is None:
            return
        if time.monotonic() >= self._find_source_deadline:
            raise _FindSourceFileTimeout()

    def _session_reset_timeouts_for_code_content_phase(self) -> None:
        """每次进入 code_content_provider 时重置：避免跨次分析泄漏 find_source 预算状态。"""
        self._find_source_budget_deadline = None
        self._find_source_lookup_calls = 0
        self._code_context_deadline = None

    def _code_context_phase_start(self) -> None:
        """第三步整阶段 wall-clock 起点（与单次源文件查找预算独立）。"""
        self._code_context_deadline = None
        sec = float(getattr(self, "code_context_timeout_sec", 0) or 0)
        if sec <= 0:
            logger.info("代码上下文整阶段超时：未启用（code_context_timeout_sec<=0）")
            return
        self._code_context_deadline = time.monotonic() + sec
        logger.info("代码上下文整阶段超时：%.0fs（到点中止重扫描并继续输出 JSON）", sec)

    def _code_context_phase_check(self, phase: str = "") -> None:
        if self._code_context_deadline is None:
            return
        if time.monotonic() >= self._code_context_deadline:
            raise _CodeContextPhaseTimeout(phase or "unknown")

    def _code_context_phase_remaining_sec(self) -> Optional[float]:
        if self._code_context_deadline is None:
            return None
        return max(0.0, self._code_context_deadline - time.monotonic())

    def _find_source_file(self, resolved_file: str, code_roots: List[str]) -> Optional[str]:
        """
        在代码根目录（可多根，顺序有意义）中查找源文件
        
        查找策略（按优先级）：
        1. 直接命中策略：addr2line 返回的路径在 code-root 下可直接访问
        2. 工程根目录名锚点：addr2line 路径中含与 code-root 同名的目录段时，取其后的相对路径拼到 code-root（应对 CI/构建机绝对路径）
        3. 尾部路径拼接策略：提取尾部 N 级目录 + 文件名，逐级尝试拼接
        4. 父目录 + 文件名联合匹配：查找 */<parent_dir>/<filename>
        5. CodeIndexService 查找（索引已就绪时）
        6. Fallback 搜索（索引未就绪时）
        7. 受限 Fallback（无索引或索引未命中时，在常见源码子目录下浅层搜索；不做整棵 code-root 的 os.walk）
        
        采用动态预算超时：
        - CLI --find-source-timeout-sec 作为本阶段总预算；
        - 每次 _find_source_file 调用按剩余预算动态分配单次窗口；
        - 当预算逼近耗尽时，单次窗口自动缩短以快速收敛。
        """
        if not code_roots:
            return None
        code_roots_abs = _normalize_code_roots_arg(code_roots)
        # 工具链 / 系统头 / NDK sysroot 等：不参与工程内解析，避免 glob/浅层遍历在大仓库上耗时
        if resolved_file and self._is_external_path(resolved_file, code_roots_abs):
            logger.debug("跳过外部路径查找（工具链/系统库等）: %s", resolved_file)
            return None

        logger.info(f"查找源文件: {resolved_file} 在目录: {code_roots}")
        
        filename = os.path.basename(resolved_file)
        self._find_source_deadline_begin()
        try:
            for code_root_abs in code_roots_abs:
                self._find_source_check_time()
                # ========== 策略1: 直接命中策略（最高优先级）==========
                result = self._try_direct_hit(resolved_file, code_root_abs)
                if result:
                    return result
                
                # ========== 策略2: 工程根目录名锚点（addr2line 构建路径 → 本地 code-root）==========
                result = self._try_suffix_after_code_root_dirname(resolved_file, code_root_abs)
                if result:
                    return result
                
                # ========== 策略3: 尾部路径拼接策略 ==========
                result = self._try_tail_path_concatenation(resolved_file, code_root_abs)
                if result:
                    return result
                
                # ========== 策略4: 父目录 + 文件名联合匹配 ==========
                result = self._try_parent_dir_filename_match(resolved_file, code_root_abs)
                if result:
                    return result
            
            # ========== 策略5: CodeIndexService 查找 ==========
            self._find_source_check_time()
            if self.code_index_service and self.code_index_service.is_ready():
                candidates = self.code_index_service.lookup(filename)
                if candidates:
                    # 如果只有一个候选，直接返回
                    if len(candidates) == 1:
                        if self._is_file_readable(candidates[0]):
                            logger.info(f"找到源文件（索引查找，唯一匹配）: {candidates[0]}")
                            return candidates[0]
                    else:
                        # 多个候选，尝试根据路径相似度选择最佳匹配
                        best_match = self._select_best_candidate(candidates, resolved_file)
                        if best_match:
                            logger.info(f"找到源文件（索引查找，多候选选择）: {best_match}")
                            return best_match
                        # 如果无法选择，记录警告但返回第一个（保持向后兼容）
                        logger.warning(f"索引中找到 {len(candidates)} 个候选文件，返回第一个: {candidates[0]}")
                        return candidates[0] if self._is_file_readable(candidates[0]) else None
                logger.info(f"索引中未找到文件: {filename}")
            
            # ========== 策略6: Fallback 搜索（索引未就绪时）==========
            self._find_source_check_time()
            if self.code_index_service and not self.code_index_service.is_ready():
                logger.info(f"索引未就绪，使用受限搜索: {filename}")
                for cr in code_roots_abs:
                    self._find_source_check_time()
                    r = self._fallback_search(resolved_file, cr)
                    if r:
                        return r
                return None
            
            # ========== 策略7: 受限 Fallback（无索引服务，或索引已就绪但未命中文件名）==========
            # 替代原「整棵 code-root os.walk」；避免大仓库超时
            self._find_source_check_time()
            if not self.code_index_service or self.code_index_service.is_ready():
                tag = "无索引服务" if not self.code_index_service else "索引未命中后"
                logger.info(f"{tag}，使用受限搜索: {filename}")
                for cr in code_roots_abs:
                    self._find_source_check_time()
                    r = self._fallback_search(resolved_file, cr)
                    if r:
                        return r
            
            logger.info(
                "未找到源文件（已跳过整仓库递归扫描以避免大仓库超时）: %s",
                resolved_file,
            )
            return None
        except _FindSourceFileTimeout:
            logger.warning(
                "查找源文件超时（本次窗口 %.1fs，剩余预算 %.1fs），中止: %s",
                self._find_source_cur_timeout_sec,
                self._find_source_budget_remaining_sec(),
                resolved_file,
            )
            return None
        finally:
            self._find_source_deadline_clear()
    
    def _try_direct_hit(self, resolved_file: str, code_root_abs: str) -> Optional[str]:
        """
        策略1: 直接命中策略（最高优先级）
        若 addr2line 返回的路径在 code-root 下可直接访问，则直接使用
        """
        self._find_source_check_time()
        # 如果是绝对路径，首先检查它是否直接存在且可读
        if os.path.isabs(resolved_file):
            if self._is_file_readable(resolved_file):
                # 检查是否在 code_root 下
                try:
                    resolved_file_abs = os.path.abspath(resolved_file)
                    common_path = os.path.commonpath([code_root_abs, resolved_file_abs])
                    if common_path == code_root_abs:
                        logger.info(f"找到源文件（直接命中）: {resolved_file}")
                        return resolved_file
                except (ValueError, OSError):
                    pass
        
        # 尝试从 code_root 构建相对路径
        try:
            if os.path.isabs(resolved_file):
                resolved_file_abs = os.path.abspath(resolved_file)
                try:
                    relative_path = os.path.relpath(resolved_file_abs, code_root_abs)
                    if not relative_path.startswith('..'):
                        potential_path = os.path.join(code_root_abs, relative_path)
                        if self._is_file_readable(potential_path):
                            logger.info(f"找到源文件（相对路径构建）: {potential_path}")
                            return potential_path
                except ValueError:
                    pass
        except (OSError, ValueError):
            pass
        
        # 如果resolved_file是相对路径或文件名，先尝试直接路径
        potential_paths = [
            os.path.join(code_root_abs, resolved_file),
            os.path.join(code_root_abs, os.path.basename(resolved_file))
        ]
        
        for path in potential_paths:
            if self._is_file_readable(path):
                logger.info(f"找到源文件（直接路径）: {path}")
                return path
        
        return None
    
    def _try_suffix_after_code_root_dirname(self, resolved_file: str, code_root_abs: str) -> Optional[str]:
        """
        将 addr2line 在 CI/构建机上记录的绝对路径映射到本地 code-root。
        若路径中出现与 code-root 最后一级目录名相同的目录段（如 .../engine-dev/src/foo.cpp），
        则取该段之后的相对路径拼到 code-root 下。存在多处同名段时从右向左依次尝试。
        """
        if not resolved_file or not code_root_abs:
            return None
        basename = os.path.basename(os.path.normpath(code_root_abs))
        if not basename:
            return None
        norm = resolved_file.replace("\\", "/")
        marker = f"/{basename}/"
        positions: List[int] = []
        pos = 0
        while True:
            i = norm.find(marker, pos)
            if i == -1:
                break
            positions.append(i)
            pos = i + len(marker)
        for start in reversed(positions):
            self._find_source_check_time()
            rel = norm[start + len(marker) :]
            if not rel:
                continue
            rel = rel.lstrip("/")
            if any(p == ".." for p in rel.split("/")):
                continue
            potential = os.path.normpath(os.path.join(code_root_abs, rel))
            try:
                root_p = Path(code_root_abs).resolve()
                cand_p = Path(potential).resolve()
                cand_p.relative_to(root_p)
            except ValueError:
                continue
            except (OSError, RuntimeError):
                continue
            if self._is_file_readable(potential):
                logger.info(f"找到源文件（工程目录名锚点）: {potential}")
                return potential
        return None
    
    def _try_tail_path_concatenation(self, resolved_file: str, code_root_abs: str) -> Optional[str]:
        """
        策略2: 尾部路径拼接策略（强烈推荐）
        从 addr2line 返回路径中提取尾部的 N 级目录 + 文件名
        逐级尝试将该尾部路径拼接到 code-root 下
        优先从较短的尾部路径开始（如 d/xxx.cpp → c/d/xxx.cpp）
        """
        # 标准化路径分隔符
        normalized_path = resolved_file.replace('\\', '/')
        
        # 移除开头的绝对路径部分，只保留相对路径结构
        # 例如：/build/path/to/src/a/b/file.cpp -> src/a/b/file.cpp 或 a/b/file.cpp
        path_parts = [p for p in normalized_path.split('/') if p and p != '.']
        
        if len(path_parts) < 2:  # 至少需要文件名 + 1级目录
            return None
        
        filename = path_parts[-1]
        
        # 从尾部开始，逐级尝试拼接（1级、2级、3级...）
        # 例如：file.cpp -> dir1/file.cpp -> dir2/dir1/file.cpp
        # CI 路径前缀很长，相对工程根目录常超过 5 级，故提高上限；仍仅做 O(级数) 次 stat，不扫全仓库
        max_levels = min(32, len(path_parts) - 1)
        
        for level in range(1, max_levels + 1):
            self._find_source_check_time()
            # 提取尾部 N 级目录 + 文件名
            tail_parts = path_parts[-level-1:]
            tail_path = '/'.join(tail_parts)
            
            # 尝试在 code_root 下查找
            potential_path = os.path.join(code_root_abs, tail_path)
            if self._is_file_readable(potential_path):
                logger.info(f"找到源文件（尾部路径拼接，{level}级）: {potential_path}")
                return potential_path
            
            # 也尝试使用 os.path.join（处理路径分隔符）
            potential_path = os.path.join(code_root_abs, *tail_parts)
            if self._is_file_readable(potential_path):
                logger.info(f"找到源文件（尾部路径拼接，{level}级）: {potential_path}")
                return potential_path
        
        return None
    
    def _try_parent_dir_filename_match(self, resolved_file: str, code_root_abs: str) -> Optional[str]:
        """
        策略3: 父目录 + 文件名联合匹配
        若完整路径结构差异较大，尝试在 code-root 下查找 */<parent_dir>/<filename>
        若仅命中一个结果，可直接使用
        """
        normalized_path = resolved_file.replace('\\', '/')
        path_parts = [p for p in normalized_path.split('/') if p and p != '.']
        
        if len(path_parts) < 2:
            return None
        
        filename = path_parts[-1]
        parent_dir = path_parts[-2]  # 父目录名
        
        # 在 code_root 下查找 */<parent_dir>/<filename>
        matches = []
        
        # 使用 glob 模式查找（限制搜索深度，避免过慢）
        try:
            from pathlib import Path
            code_root_path = Path(code_root_abs)
            
            # 限制搜索深度为3层，避免在大代码库中过慢
            _glob_i = 0
            for depth in range(1, 4):
                self._find_source_check_time()
                pattern = f"*/{parent_dir}/{filename}"
                if depth > 1:
                    pattern = "*/" * (depth - 1) + pattern
                
                for match_path in code_root_path.glob(pattern):
                    _glob_i += 1
                    if _glob_i % 400 == 0:
                        self._find_source_check_time()
                    if match_path.is_file() and self._is_file_readable(str(match_path)):
                        matches.append(str(match_path))
                
                # 如果找到匹配，停止搜索更深层
                if matches:
                    break
        except Exception as e:
            logger.debug(f"父目录+文件名匹配搜索失败: {e}")
        
        if len(matches) == 1:
            logger.info(f"找到源文件（父目录+文件名匹配，唯一）: {matches[0]}")
            return matches[0]
        elif len(matches) > 1:
            # 多个匹配，尝试选择最佳（优先选择路径较短的）
            matches.sort(key=len)
            logger.info(f"找到源文件（父目录+文件名匹配，多候选，选择最短）: {matches[0]}")
            return matches[0]
        
        return None
    
    def _select_best_candidate(self, candidates: List[str], resolved_file: str) -> Optional[str]:
        """
        从多个候选文件中选择最佳匹配
        优先考虑路径相似度
        """
        if not candidates:
            return None
        
        if len(candidates) == 1:
            return candidates[0] if self._is_file_readable(candidates[0]) else None
        
        # 提取 resolved_file 的路径特征
        normalized_resolved = resolved_file.replace('\\', '/')
        resolved_parts = [p for p in normalized_resolved.split('/') if p]
        
        best_match = None
        best_score = -1
        
        for candidate in candidates:
            self._find_source_check_time()
            if not self._is_file_readable(candidate):
                continue
            
            score = 0
            normalized_candidate = candidate.replace('\\', '/')
            candidate_parts = [p for p in normalized_candidate.split('/') if p]
            
            # 路径长度相似度（较短的路径优先）
            length_diff = abs(len(candidate_parts) - len(resolved_parts))
            score += max(0, 10 - length_diff * 2)
            
            # 目录名匹配度
            # 检查 resolved_file 中的目录名是否出现在 candidate 中
            resolved_dirs = set(resolved_parts[:-1])  # 排除文件名
            candidate_dirs = set(candidate_parts[:-1])
            common_dirs = resolved_dirs & candidate_dirs
            score += len(common_dirs) * 5
            
            # 路径尾部相似度（尾部匹配更重要）
            min_len = min(len(resolved_parts), len(candidate_parts))
            for i in range(1, min_len + 1):
                if resolved_parts[-i] == candidate_parts[-i]:
                    score += i * 3  # 越靠近尾部，权重越高
                else:
                    break
            
            if score > best_score:
                best_score = score
                best_match = candidate
        
        return best_match
    
    def _full_recursive_search(self, resolved_file: str, code_root: str) -> Optional[str]:
        """
        完整递归搜索（大仓库上极慢；_find_source_file 已不再调用，保留供少数直接调用或测试）。
        """
        filename = os.path.basename(resolved_file)
        name_without_ext = os.path.splitext(filename)[0]
        file_ext = os.path.splitext(filename)[1]
        
        best_match = None
        best_score = 0
        
        for root, dirs, files in os.walk(code_root):
            # 过滤排除的目录
            dirs[:] = [d for d in dirs if d not in self.exclude_dirs]
            
            for file in files:
                file_path = os.path.join(root, file)
                if not self._is_file_readable(file_path):
                    continue
                
                # 计算匹配分数
                score = 0
                
                # 完全匹配文件名
                if file == filename:
                    score = 100
                # 匹配文件名（无扩展名）
                elif os.path.splitext(file)[0] == name_without_ext:
                    score = 80
                # 部分匹配
                elif name_without_ext in file:
                    score = 60
                # 扩展名匹配
                elif file_ext and file.endswith(file_ext):
                    score = 40
                
                # 特殊处理：mylib.cpp 和 my_lib.cpp 的匹配
                if filename == "mylib.cpp" and file == "my_lib.cpp":
                    score = 95
                elif filename == "my_lib.cpp" and file == "mylib.cpp":
                    score = 95
                
                # 路径相似度加分
                if resolved_file in file_path or any(part in file_path for part in resolved_file.split('/')):
                    score += 20
                
                if score > best_score:
                    best_score = score
                    best_match = file_path
        
        if best_match and best_score >= 40:  # 最低匹配阈值
            logger.info(f"找到源文件（智能匹配，分数: {best_score}）: {best_match}")
            return best_match
        
        logger.warning(f"未找到源文件: {resolved_file}")
        return None
    
    def _fallback_search(self, resolved_file: str, code_root: str) -> Optional[str]:
        """
        Fallback 搜索（当索引未就绪时使用）
        只在常见的源代码目录中搜索，避免全目录扫描
        """
        filename = os.path.basename(resolved_file)
        common_source_dirs = ['src', 'source', 'lib', 'common', 'include', 'inc']
        
        # 先尝试在根目录
        potential_path = os.path.join(code_root, filename)
        if self._is_file_readable(potential_path):
            logger.info(f"找到源文件（fallback根目录）: {potential_path}")
            return potential_path
        
        # 在常见源代码目录中搜索
        _walk_i = 0
        for source_dir in common_source_dirs:
            self._find_source_check_time()
            source_path = os.path.join(code_root, source_dir)
            if os.path.isdir(source_path):
                for root, dirs, files in os.walk(source_path):
                    _walk_i += 1
                    if _walk_i % 200 == 0:
                        self._find_source_check_time()
                    # 限制搜索深度（最多2层）
                    depth = root[len(source_path):].count(os.sep)
                    if depth > 2:
                        dirs[:] = []  # 不再深入
                        continue
                    
                    if filename in files:
                        file_path = os.path.join(root, filename)
                        if self._is_file_readable(file_path):
                            logger.info(f"找到源文件（fallback受限搜索）: {file_path}")
                            return file_path
        
        logger.debug(f"Fallback搜索未找到文件: {resolved_file}")
        return None

    def _ctor_or_dtor_class_name_from_resolved(self, resolved_function: str) -> Optional[str]:
        """
        从 demangled 符号中识别「类名::类名()」形式的构造 / 「类名::~类名」析构，返回类名。
        用于隐式构造函数等无函数体定义时，按 struct/class 作用域提取片段。
        """
        s = (resolved_function or "").strip()
        if not s:
            return None
        head = s.split("(", 1)[0].strip()
        parts = [p.strip() for p in head.split("::") if p.strip()]
        if len(parts) < 2:
            return None
        last = parts[-1].lstrip("~")
        second_last = parts[-2]
        if last == second_last:
            return second_last
        if parts[-1].startswith("~") and parts[-1][1:] == second_last:
            return second_last
        return None

    def _try_struct_or_class_scope_snippet(
        self,
        lines: List[str],
        resolved_line_1based: int,
        type_name: str,
    ) -> Optional[Tuple[List[str], int, int]]:
        """
        在源文件中定位包含 addr2line 行的 struct/class TypeName { ... }; 块，返回片段与 1-based 起止行号。
        """
        if not type_name or resolved_line_1based < 1 or resolved_line_1based > len(lines):
            return None
        L0 = resolved_line_1based - 1
        struct_start: Optional[int] = None
        for i in range(L0, -1, -1):
            line = lines[i]
            if re.match(
                rf"^\s*(?:template\s*<[^>]+>\s*)?(?:struct|class)\s+{re.escape(type_name)}\b",
                line,
            ):
                struct_start = i
                break
        if struct_start is None:
            return None
        brace = 0
        found_open = False
        struct_end: Optional[int] = None
        for j in range(struct_start, len(lines)):
            for c in lines[j]:
                if c == "{":
                    brace += 1
                    found_open = True
                elif c == "}":
                    brace -= 1
                    if found_open and brace == 0:
                        struct_end = j
                        break
            if struct_end is not None:
                break
        if struct_end is None:
            return None
        if not (struct_start <= L0 <= struct_end):
            return None
        block = lines[struct_start : struct_end + 1]
        snippet = [ln.rstrip() for ln in block]
        if not snippet:
            return None
        return snippet, struct_start + 1, struct_end + 1

    def _locate_snippet_in_file(
        self,
        lines: List[str],
        snippet: List[str],
        anchor_line_1based: int,
    ) -> Optional[Tuple[int, int]]:
        """在文件中定位与 snippet 一致的连续行，优先使 anchor 行落在该区间内。返回 (start1, end1) 闭区间。"""
        n = len(lines)
        if not snippet:
            return None
        L = max(1, min(anchor_line_1based, n))
        L0 = L - 1
        first = snippet[0].strip()
        lo = max(0, L0 - 400)
        hi = min(n - len(snippet), L0 + 80)
        candidates: List[Tuple[int, int, int]] = []
        for i in range(lo, hi + 1):
            if lines[i].strip() != first:
                continue
            ok = True
            for k in range(len(snippet)):
                if i + k >= n or lines[i + k].strip() != snippet[k].strip():
                    ok = False
                    break
            if ok:
                start1, end1 = i + 1, i + len(snippet)
                dist = abs(i - L0)
                candidates.append((start1, end1, dist))
        if not candidates:
            return None
        inside = [(a, b, d) for a, b, d in candidates if a <= L <= b]
        if inside:
            inside.sort(key=lambda x: x[2])
            return inside[0][0], inside[0][1]
        candidates.sort(key=lambda x: x[2])
        return candidates[0][0], candidates[0][1]

    def _ensure_snippet_covers_anchor_line(
        self,
        all_lines: List[str],
        resolved_line_1based: int,
        snippet: List[str],
        half_window: int = 18,
    ) -> Tuple[List[str], int, int]:
        """
        若片段在文件中无法覆盖 addr2line 行，则降级为以该行中心的窗口，并返回 1-based 起止行号。
        """
        n = len(all_lines)
        L = max(1, min(resolved_line_1based, n))
        L0 = L - 1
        loc = self._locate_snippet_in_file(all_lines, snippet, resolved_line_1based)
        if loc:
            start1, end1 = loc
            if start1 <= L <= end1:
                return snippet, start1, end1
        a = max(0, L0 - half_window)
        b = min(n, L0 + half_window + 1)
        return [all_lines[i].rstrip() for i in range(a, b)], a + 1, b

    def _effective_crash_signature(self, function_signature: str, resolved_function: str) -> str:
        """
        合并「源码扫描得到的签名」与 addr2line 的 demangled 符号。
        当崩溃行落在成员初始化、隐式构造等位置时，_extract_function_signature 常得到 unknown function，
        此时用 resolved_function 作为展示签名，避免 03/05 与 02 不一致。
        """
        fs = self._normalize_function_signature(function_signature)
        rf = self._normalize_function_signature(resolved_function)
        if fs and fs != "unknown function":
            return fs
        if rf:
            return rf
        return fs if fs else "unknown function"

    def _normalize_function_signature(self, sig: Optional[str]) -> str:
        """标准化函数签名展示，避免把函数体代码拼进签名。"""
        s = (sig or "").strip()
        if not s:
            return ""
        # 仅保留第一个 '{' 之前内容；若带 '{' 则补一个 '{' 便于表达“函数定义”。
        if "{" in s:
            s = s.split("{", 1)[0].rstrip() + " {"
        # 压缩多空格
        s = " ".join(s.split())
        return s
    
    def _extract_crash_function(self, resolved_function: str, resolved_file: str, 
                               resolved_line: int, code_roots: List[str]) -> Optional[CrashFunction]:
        """提取崩溃函数信息"""
        roots_abs = _normalize_code_roots_arg(code_roots)
        if resolved_file and self._is_external_path(resolved_file, roots_abs):
            logger.debug(
                "跳过外部路径上的崩溃函数抽取: %s:%s",
                resolved_file,
                resolved_line,
            )
            return None
        logger.info(f"提取崩溃函数信息: {resolved_function} 在 {resolved_file}:{resolved_line}")
        
        # 查找源文件
        source_file = self._find_source_file(resolved_file, code_roots)
        if not source_file:
            logger.warning(f"未找到源文件: {resolved_file}")
            return None
        
        try:
            with open(source_file, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()

            # 提取函数名和签名
            function_name = self._extract_function_name_from_resolved(resolved_function)
            function_signature = self._extract_function_signature(
                lines,
                resolved_line - 1,
                target_function_name=function_name,
            )

            # ObjC 专用提取：优先按 selector 抽取实现方法体，避免误落到 .h 声明或相邻函数片段。
            objc_info = self._parse_objc_symbol_class_selector(resolved_function)
            if objc_info:
                _cls, sel = objc_info
                objc_block = self._extract_objc_method_block(lines, sel)
                if objc_block:
                    method_snippet, s1, e1, method_sig = objc_block
                    crash_line_code = ""
                    display_line_no: Optional[int] = None
                    if s1 <= resolved_line <= e1:
                        crash_line_code = lines[resolved_line - 1].strip()
                    anchor_line = resolved_line if s1 <= resolved_line <= e1 else s1
                    picked_line, picked_num = self._pick_best_crash_line_around(
                        lines,
                        anchor_line,
                        crash_line_code,
                        search_range=12,
                        bound_lo=s1,
                        bound_hi=e1,
                    )
                    if picked_line:
                        crash_line_code = picked_line
                    if picked_num and picked_num > 0:
                        display_line_no = picked_num
                    eff_sig = self._effective_crash_signature(method_sig or function_signature, resolved_function)
                    if self.max_code_length > 0:
                        method_snippet = self._truncate_snippet(method_snippet)
                    return CrashFunction(
                        name=function_name,
                        signature=eff_sig,
                        snippet=method_snippet,
                        crash_line=crash_line_code,
                        snippet_start_line=s1,
                        snippet_end_line=e1,
                        crash_line_number=display_line_no,
                    )

            # 隐式构造 / 析构等：addr2line 落在 struct 成员上时，整段「类::类()」按类作用域提取，避免误命中上方其它 operator
            ctor_cls = self._ctor_or_dtor_class_name_from_resolved(resolved_function)
            if ctor_cls:
                scope = self._try_struct_or_class_scope_snippet(lines, resolved_line, ctor_cls)
                if scope:
                    function_snippet, s1, e1 = scope
                    crash_line_code = ""
                    if resolved_line > 0 and resolved_line <= len(lines):
                        crash_line_code = lines[resolved_line - 1].strip()
                        if not crash_line_code:
                            search_range = 5
                            best_crash_line = ""
                            best_score = 0
                            for offset in range(1, search_range + 1):
                                if resolved_line - offset > 0:
                                    line_content = lines[resolved_line - offset - 1].strip()
                                    if line_content and not line_content.startswith("//") and not line_content.startswith("/*"):
                                        score = self._calculate_crash_line_score(line_content)
                                        if score > best_score:
                                            best_score = score
                                            best_crash_line = line_content
                                if resolved_line + offset <= len(lines):
                                    line_content = lines[resolved_line + offset - 1].strip()
                                    if line_content and not line_content.startswith("//") and not line_content.startswith("/*"):
                                        score = self._calculate_crash_line_score(line_content)
                                        if score > best_score:
                                            best_score = score
                                            best_crash_line = line_content
                            if best_crash_line:
                                crash_line_code = best_crash_line
                    if self.max_code_length > 0:
                        function_snippet = self._truncate_snippet(function_snippet)
                    eff_sig = self._effective_crash_signature(function_signature, resolved_function)
                    return CrashFunction(
                        name=function_name,
                        signature=eff_sig,
                        snippet=function_snippet,
                        crash_line=crash_line_code,
                        snippet_scope="struct_scope",
                        snippet_start_line=s1,
                        snippet_end_line=e1,
                    )
            
            # 提取完整函数代码
            full_function = self._extract_full_function_code(
                lines,
                resolved_line - 1,
                target_function_name=function_name,
            )
            if not full_function:
                # 兜底1：按函数名在当前文件中回溯定义位置，再次尝试提取完整函数
                fallback_def_index = self._find_function_definition_line_by_name(
                    lines, function_name, resolved_line - 1
                )
                if fallback_def_index is not None:
                    logger.info(
                        f"按函数名兜底命中定义行: {function_name} (line={fallback_def_index + 1})"
                    )
                    function_signature = self._extract_function_signature(
                        lines,
                        fallback_def_index,
                        target_function_name=function_name,
                    )
                    full_function = self._extract_full_function_code(
                        lines,
                        fallback_def_index,
                        target_function_name=function_name,
                    )

            if not full_function:
                # 兜底2：无法提取完整函数时，返回行窗口片段而不是直接失败
                logger.warning(f"无法提取完整函数代码，降级为行窗口片段: {resolved_function}")
                center = max(0, min(len(lines) - 1, resolved_line - 1))
                start = max(0, center - 20)
                end = min(len(lines), center + 21)
                window = [line.rstrip() for line in lines[start:end]]
                if not any(x.strip() for x in window):
                    return None
                crash_line_code = lines[center].strip() if 0 <= center < len(lines) else ""
                eff_sig = self._effective_crash_signature(function_signature, resolved_function)
                return CrashFunction(
                    name=function_name,
                    signature=eff_sig,
                    snippet=window,
                    crash_line=crash_line_code,
                    snippet_scope="line_window",
                    snippet_start_line=start + 1,
                    snippet_end_line=end,
                )
            
            # 提取崩溃行代码（重选行时限制在同一函数体内，避免窗口跨入相邻函数）
            fn_span = self._line_span_for_extracted_function_text(
                lines, full_function, resolved_line
            )
            crash_line_code = ""
            display_line_no: Optional[int] = None
            if resolved_line > 0 and resolved_line <= len(lines):
                crash_line_code = lines[resolved_line - 1].strip()

                picked_line, picked_num = self._pick_best_crash_line_around(
                    lines,
                    resolved_line,
                    crash_line_code,
                    search_range=12,
                    bound_lo=fn_span[0] if fn_span else None,
                    bound_hi=fn_span[1] if fn_span else None,
                )
                if picked_line and picked_line != crash_line_code:
                    crash_line_code = picked_line
                    logger.info(f"在估算行号 {resolved_line} 附近重选崩溃行: {crash_line_code}")
                if picked_num and picked_num > 0:
                    display_line_no = picked_num
            
            # 构建函数代码片段，并与 addr2line 行对齐（避免误扩到其它函数）
            raw_lines = [line.rstrip() for line in full_function.split("\n")]
            function_snippet, s1, e1 = self._ensure_snippet_covers_anchor_line(
                lines, resolved_line, raw_lines
            )

            # 应用代码片段截断（根据 max_code_length）
            if self.max_code_length > 0:
                function_snippet = self._truncate_snippet(function_snippet)

            eff_sig = self._effective_crash_signature(function_signature, resolved_function)
            return CrashFunction(
                name=function_name,
                signature=eff_sig,
                snippet=function_snippet,
                crash_line=crash_line_code,
                snippet_start_line=s1,
                snippet_end_line=e1,
                crash_line_number=display_line_no,
            )

        except (IOError, OSError) as e:
            logger.error(f"读取或处理源文件时发生 I/O 错误: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误: {e}")
            return None
        except Exception as e:
            logger.error(f"提取崩溃函数失败: {e}")
            return None

    def _fallback_crash_function_line_window(
        self,
        resolved_function: str,
        resolved_file: str,
        resolved_line: int,
        code_roots: List[str],
    ) -> Optional[CrashFunction]:
        """完整函数体提取失败时，仅按 addr2line 行号截取窗口片段。"""
        source_file = self._find_source_file(resolved_file, code_roots)
        if not source_file:
            return None
        try:
            with open(source_file, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except (IOError, OSError):
            return None
        function_name = self._extract_function_name_from_resolved(resolved_function)
        center = max(0, min(len(lines) - 1, resolved_line - 1))
        start = max(0, center - 20)
        end = min(len(lines), center + 21)
        window = [line.rstrip() for line in lines[start:end]]
        if not any(x.strip() for x in window):
            return None
        crash_line_code = lines[center].strip() if 0 <= center < len(lines) else ""
        return CrashFunction(
            name=function_name or "unknown",
            signature=resolved_function,
            snippet=window,
            crash_line=crash_line_code,
            crash_location_source="from_add2line",
            crash_line_note="完整函数提取失败，已降级为行窗口片段",
            snippet_scope="line_window",
            snippet_start_line=start + 1,
            snippet_end_line=end,
        )

    def _extract_function_name_from_resolved(self, resolved_function: str) -> str:
        """从解析后的函数名中提取函数名"""
        s = (resolved_function or "").strip()
        if not s:
            return ""

        # 先处理 Itanium C++ mangled name（如 _Z13crash_nullptrv / _ZN11DataManager8add_dataEim）
        if s.startswith("_Z"):
            # 兜底：处理日志里常见的近似 mangled（如 _Z13crash_oobv）
            m_quick = re.match(r"^_Z\d+([A-Za-z_]\w*)v$", s)
            if m_quick:
                return m_quick.group(1)
            parts: List[str] = []
            i = 2
            n = len(s)
            while i < n:
                if s[i].isdigit():
                    j = i
                    while j < n and s[j].isdigit():
                        j += 1
                    try:
                        seg_len = int(s[i:j])
                    except ValueError:
                        break
                    seg_start = j
                    seg_end = j + seg_len
                    if seg_len <= 0 or seg_end > n:
                        break
                    seg = s[seg_start:seg_end]
                    if re.match(r'^[A-Za-z_]\w*$', seg):
                        parts.append(seg)
                    i = seg_end
                    continue
                i += 1
            if parts:
                return parts[-1]

        # 目标：从形如
        #  - _baidu_framework::CIntelligentChargeData::Handle(...)
        #  - std::__ndk1::shared_ptr<T>::shared_ptr[abi:ne180000](...)
        #  - _baidu_framework::tagHouseDrawObjKey::~tagHouseDrawObjKey()
        #  - ParseRGCOverlay(...)
        #  中提取 “函数名 token”（Handle/shared_ptr/~tagHouseDrawObjKey/ParseRGCOverlay）
        #
        # 注意：resolved_function 里模板参数也包含 '::'，不能用 split('::')[-1]
        m = re.search(
            r"(?:(?:^|::)\s*)([~]?[A-Za-z_]\w*)\s*(?:\[[^\]]+\])?\s*\(",
            s,
        )
        if m:
            return m.group(1)

        # 兜底：直接截取 '(' 前的最后一个片段
        if "(" in s:
            head = s.split("(", 1)[0]
            if "::" in head:
                return head.split("::")[-1].strip()
            return head.strip()

        return s

    @staticmethod
    def _is_probably_system_module_name(module: str) -> bool:
        m = (module or "").strip().lower()
        if not m:
            return False
        if m in {"(null)", "null"}:
            return True
        # iOS/macOS 常见系统镜像名 / 框架前缀（用于主崩溃帧选取时跳过，优先业务模块）
        prefixes = (
            "libsystem",
            "libdispatch",
            "libobjc",
            "libc++",
            "libc++abi",
            "libswift",
            "dyld",
            "corefoundation",
            "coregraphics",
            "coretext",
            "coremedia",
            "uikit",
            "uikitcore",
            "swiftui",
            "graphicsservices",
            "foundation",
            "cfnetwork",
            "security",
            "network",
            "metal",
            "quartzcore",
            "accelerate",
            "iosurface",
            "imageio",
            "libsqlite",
            "libxml",
            "libz.",
            "libcompression",
            "libarchive",
            "libicucore",
            "libmacho",
            "libxpc",
            "libresolv",
            "libcache",
            "libbsm",
            "libcoretls",
            "libnetwork",
            "system.",
        )
        if m.startswith(prefixes):
            return True
        # 形如 com.apple.UIKitCore / .../UIKitCore.framework/...
        if "com.apple." in m or "/system/" in m or "/usr/lib/" in m:
            return True
        return False

    def _parse_objc_symbol_class_selector(self, resolved_function: str) -> Optional[Tuple[str, str]]:
        """
        解析 ObjC 符号，返回 (class_name, selector)。
        例如：-[BMKBaseEngine updateCommonMemCacheWithKey:value:]
        """
        s = (resolved_function or "").strip()
        m = re.match(r"^[\-\+]\[([A-Za-z_]\w*)\s+([^\]]+)\]$", s)
        if not m:
            return None
        class_name = m.group(1).strip()
        selector = m.group(2).strip()
        if not class_name or not selector:
            return None
        return class_name, selector

    @staticmethod
    def _is_cpp_native_symbol(resolved_function: str) -> bool:
        """
        判定是否为应参与 native C++ 上下文提取的符号。
        兼容策略：
        - 保留 Itanium mangled 符号（_Z...）
        - 保留 C++ 限定名（A::b(...)）
        - 保留普通函数签名（foo(...) / operator+ (...) 等）
        - 排除 ObjC 方法符号（-[Class sel:] / +[Class sel:]）
        """
        s = (resolved_function or "").strip()
        if not s:
            return False
        # ObjC 方法符号在 native-only 流程中单独处理，这里直接排除。
        if re.match(r"^[\-\+]\[[^\]]+\]$", s):
            return False
        if s.startswith("_Z"):
            return True
        if "::" in s and "(" in s:
            return True
        # 兼容普通函数签名，如 crash_nullptr() / main(int, char**) / operator new(...)
        if "(" in s and ")" in s:
            head = s.split("(", 1)[0].strip()
            # 允许前导 * / & / ~ 等（如析构、返回值修饰），但需包含至少一个标识符字符。
            if head and re.search(r"[A-Za-z_~]", head):
                return True
        return False

    def _find_objc_method_definition_location(
        self, class_name: str, selector: str, code_roots: List[str]
    ) -> Optional[Tuple[str, int]]:
        """
        ObjC 专用快速定位：按类名优先缩小候选文件范围，再匹配方法签名。
        避免全仓按函数名扫描导致的长耗时。
        """
        cls = (class_name or "").strip()
        sel = (selector or "").strip()
        if not cls or not sel:
            return None

        # 把 selector 拆成关键片段（如 updateCommonMemCacheWithKey:value: -> [update..., value]）
        sel_parts = [p.strip() for p in sel.split(":") if p.strip()]
        if not sel_parts:
            return None

        # cache key 粗粒度缓存，避免重复遍历
        cache_key = f"objc:{cls}:{sel}"
        if cache_key in self._function_def_cache:
            return self._function_def_cache[cache_key]

        impl_ext = {".mm", ".m"}
        header_ext = {".h"}
        for code_root in code_roots or []:
            same_cls_impl: List[str] = []
            all_impl: List[str] = []
            same_cls_header: List[str] = []
            all_header: List[str] = []
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if d not in self.exclude_dirs]
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext not in impl_ext and ext not in header_ext:
                        continue
                    fp = os.path.join(root, file)
                    stem = os.path.splitext(file)[0]
                    same_cls = stem == cls or stem.startswith(cls)
                    if ext in impl_ext:
                        all_impl.append(fp)
                        if same_cls:
                            same_cls_impl.append(fp)
                    else:
                        all_header.append(fp)
                        if same_cls:
                            same_cls_header.append(fp)

            # 优先实现文件，再回退头文件；各阶段都先同类名再全量
            candidate_files = same_cls_impl + all_impl + same_cls_header + all_header

            for file_path in candidate_files:
                try:
                    if not self._is_file_readable(file_path):
                        continue
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    for i, line in enumerate(lines, 1):
                        stripped = line.strip()
                        # ObjC 方法定义首行一般以 -/+ 开头
                        if not stripped.startswith("-") and not stripped.startswith("+"):
                            continue
                        # 跳过头文件中的方法声明
                        if stripped.endswith(";"):
                            continue
                        # 快速判定：必须含 selector 首段
                        if sel_parts[0] not in stripped:
                            continue
                        # 兼容多行签名：在局部窗口内判定 selector 各段按序出现
                        sig_window = " ".join(lines[i - 1 : min(len(lines), i + 6)]).replace("\n", " ")
                        pos = 0
                        ok = True
                        for p in sel_parts:
                            idx = sig_window.find(p, pos)
                            if idx < 0:
                                ok = False
                                break
                            pos = idx + len(p)
                        if not ok:
                            continue
                        # 方法定义行还应含 ':'（多参）或 '('（无参方法签名）
                        if (":" in sel and ":" not in stripped) and ("(" not in stripped):
                            continue
                        self._function_def_cache[cache_key] = (file_path, i)
                        return (file_path, i)
                except (IOError, OSError):
                    continue
        return None

    def _extract_objc_method_block(
        self, lines: List[str], selector: str
    ) -> Optional[Tuple[List[str], int, int, str]]:
        """按 selector 抽取 ObjC 方法定义代码块，返回 (snippet, start_line, end_line, signature)。"""
        sel = (selector or "").strip()
        parts = [p.strip() for p in sel.split(":") if p.strip()]
        if not parts:
            return None
        n = len(lines)
        for i in range(n):
            s = lines[i].strip()
            if not (s.startswith("-") or s.startswith("+")):
                continue
            if s.endswith(";"):
                continue
            sig_window = " ".join(lines[i : min(n, i + 8)]).replace("\n", " ")
            pos = 0
            ok = True
            for p in parts:
                idx = sig_window.find(p, pos)
                if idx < 0:
                    ok = False
                    break
                pos = idx + len(p)
            if not ok:
                continue

            brace_line = -1
            for j in range(i, min(n, i + 16)):
                if "{" in lines[j]:
                    brace_line = j
                    break
            if brace_line < 0:
                continue

            brace = 0
            end = -1
            for j in range(brace_line, n):
                brace += lines[j].count("{")
                brace -= lines[j].count("}")
                if brace <= 0:
                    end = j
                    break
            if end < 0:
                continue

            snippet = [ln.rstrip() for ln in lines[i : end + 1]]
            signature = lines[i].strip()
            return snippet, i + 1, end + 1, signature
        return None

    def _extract_class_name_from_resolved(self, resolved_function: str) -> Optional[str]:
        """从解析后的函数名中提取类名（处理 mangled name）"""
        # 处理 mangled name，如 _ZN11DataManager8add_dataEim
        # 尝试从 mangled name 中提取类名
        if resolved_function.startswith('_Z'):
            # 这是一个 mangled name，尝试提取类名
            # 模式：_ZN + 长度 + 类名 + 长度 + 函数名
            # 例如：_ZN11DataManager8add_dataEim -> DataManager
            match = re.search(r'_ZN(\d+)(\w+)\d+(\w+)', resolved_function)
            if match:
                class_name = match.group(2)
                logger.info(f"从 mangled name 中提取类名: {class_name}")
                return class_name
        
        # 处理正常格式：Class::function_name
        if '::' in resolved_function:
            parts = resolved_function.split('::')
            if len(parts) >= 2:
                class_name = parts[0].split('(')[0].strip()
                # 清理可能的返回类型
                class_name = class_name.split()[-1] if ' ' in class_name else class_name
                return class_name
        
        return None
    
    def _calculate_crash_line_score(self, line_content: str) -> int:
        """计算代码行的重要性分数，用于选择最佳的崩溃行"""
        score = 0
        s = (line_content or "").strip()

        # 最高优先级：显式发信号或终止进程（通常即实际崩溃触发语句）
        signal_abort_patterns = [
            r"\braise\s*\(",
            r"\b(?:std::)?abort\s*\(",
            r"\b(?:std::)?terminate\s*\(",
        ]
        for pattern in signal_abort_patterns:
            if re.search(pattern, line_content):
                score += 155
                break

        # 高优先级：形如 `foo();` 的空实参调用（常见于通过函数指针/回调触发 fault）
        if s:
            mt = re.match(
                r"^([A-Za-z_]\w*)\s*\(\s*\)\s*;",
                s,
            )
            if mt and mt.group(1) not in {
                "if",
                "for",
                "while",
                "switch",
                "catch",
                "return",
                "throw",
                "else",
                "do",
                "try",
            }:
                score += 135

        # 高优先级：直接内存写/越界写等高风险语句
        dangerous_patterns = [
            r'^\s*\*\s*\w+\s*=',            # 指针解引用写 *p = value（避免匹配 int* p = ...）
            r'\w+\s*->\s*\w+\s*=',           # 指针成员写 ptr->member = value
            r'\w+\s*\[[^\]]+\]\s*=',         # 下标写 arr[index] = value
            r'/\s*0(?:\D|$)',                # 显式除零痕迹
        ]
        
        for pattern in dangerous_patterns:
            if re.search(pattern, line_content):
                score += 140
                break

        # 类型转换相关语句（尤其是 RTTI/强转）通常是 BadCast 场景的关键线索
        cast_patterns = [
            r'\bdynamic_cast\s*<',
            r'\bstatic_cast\s*<',
            r'\breinterpret_cast\s*<',
            r'\bconst_cast\s*<',
        ]
        cast_hit = False
        for pattern in cast_patterns:
            if re.search(pattern, line_content):
                score += 160
                cast_hit = True
                break

        # 中高优先级：释放/分配相关语句（有风险但通常不是直接崩溃写点）
        # 与强转同处一行时不再叠加（避免 static_cast+malloc 分数虚高压过后续真实 fault 行）
        memory_ops_patterns = [
            r'\bdelete\b',
            r'\bfree\s*\(',
            r'\bnew\b',
            r'\bmalloc\s*\(',
        ]
        if not cast_hit:
            for pattern in memory_ops_patterns:
                if re.search(pattern, line_content):
                    score += 80
                    break
        
        # 中优先级：包含变量操作的代码行
        variable_patterns = [
            r'\w+\s*=',  # 变量赋值
            r'=\s*\w+',  # 被赋值
            r'\w+\s*->',  # 指针成员访问
            r'\w+\s*\.',  # 对象成员访问
        ]
        
        for pattern in variable_patterns:
            if re.search(pattern, line_content):
                score += 50  # 中分数
                break
        
        # 低优先级：其他代码行
        if line_content and not line_content.startswith('{') and not line_content.startswith('}'):
            score += 10  # 低分数
        
        # 排除大括号和注释
        if line_content in ['{', '}'] or line_content.startswith('//'):
            score = 0
        
        return score

    def _crash_line_context_bonus(self, lines: List[str], line_no_1based: int) -> int:
        """结合上文短窗口的额外加分（如同一作用域内再次出现释放）。"""
        idx = line_no_1based - 1
        if idx < 0 or idx >= len(lines):
            return 0
        ln = lines[idx]
        if not re.search(r"\bfree\s*\(|\bdelete\b", ln):
            return 0
        window = lines[max(0, idx - 28) : idx]
        prev = sum(
            1 for wln in window if re.search(r"\bfree\s*\(|\bdelete\b", wln)
        )
        if prev >= 1:
            # 需明显高于「static_cast + malloc」等分配行的组合分，以便 double-free 场景命中第二次释放
            return 180
        return 0

    def _is_low_value_crash_line(self, line_content: str) -> bool:
        """判断一行代码是否不适合作为最终崩溃行展示。"""
        s = (line_content or "").strip()
        if not s:
            return True
        # 结构性噪音行：块边界/孤立分号/注释
        if s in {"{", "}", ";"}:
            return True
        if s.startswith("//") or s.startswith("/*") or s == "*" or s.startswith("* "):
            return True
        return False

    def _line_span_for_extracted_function_text(
        self, lines: List[str], full_function: str, resolved_line: int
    ) -> Optional[Tuple[int, int]]:
        """将 _extract_full_function_code 得到的函数文本对齐到源文件中的 [1-based 起, 1-based 止] 行号区间。"""
        raw = [ln.rstrip() for ln in full_function.split("\n")]
        while raw and not raw[0].strip():
            raw.pop(0)
        while raw and not raw[-1].strip():
            raw.pop()
        if not raw:
            return None
        n = len(raw)
        best: Optional[Tuple[int, int]] = None
        last_start = len(lines) - n
        if last_start < 0:
            return None
        for start_idx in range(last_start + 1):
            ok = True
            for k in range(n):
                if lines[start_idx + k].strip() != raw[k].strip():
                    ok = False
                    break
            if not ok:
                continue
            span = (start_idx + 1, start_idx + n)
            if span[0] <= resolved_line <= span[1]:
                return span
            if best is None:
                best = span
        return best

    def _pick_best_crash_line_around(
        self,
        lines: List[str],
        resolved_line: int,
        initial_line: str,
        search_range: int = 5,
        bound_lo: Optional[int] = None,
        bound_hi: Optional[int] = None,
    ) -> Tuple[str, Optional[int]]:
        """在目标行附近挑选更有诊断价值的一行代码（通用策略，无场景硬编码）。"""
        stripped_initial = (initial_line or "").strip()

        def total_at(line_no: int) -> int:
            if line_no <= 0 or line_no > len(lines):
                return -1
            cand = lines[line_no - 1].strip()
            if self._is_low_value_crash_line(cand):
                return -1
            return self._calculate_crash_line_score(cand) + self._crash_line_context_bonus(
                lines, line_no
            )

        best_score = -1
        best_dist = 10**9
        best_line = ""
        best_line_no: Optional[int] = None

        if stripped_initial and not self._is_low_value_crash_line(stripped_initial):
            best_score = total_at(resolved_line)
            best_line = stripped_initial
            best_dist = 0
            best_line_no = resolved_line

        for offset in range(1, search_range + 1):
            for cand_line_no in (resolved_line - offset, resolved_line + offset):
                if cand_line_no <= 0 or cand_line_no > len(lines):
                    continue
                if bound_lo is not None and cand_line_no < bound_lo:
                    continue
                if bound_hi is not None and cand_line_no > bound_hi:
                    continue
                cand = lines[cand_line_no - 1].strip()
                if self._is_low_value_crash_line(cand):
                    continue
                cand_score = total_at(cand_line_no)
                if cand_score < 0:
                    continue
                if cand_score > best_score or (cand_score == best_score and offset < best_dist):
                    best_line = cand
                    best_score = cand_score
                    best_dist = offset
                    best_line_no = cand_line_no

        if not best_line:
            best_line = stripped_initial
            if best_line:
                best_line_no = resolved_line

        picked_no: Optional[int] = None
        if best_line and best_line_no is not None:
            picked_no = best_line_no
        elif best_line:
            lo = max(1, resolved_line - search_range)
            hi = min(len(lines), resolved_line + search_range)
            if bound_lo is not None:
                lo = max(lo, bound_lo)
            if bound_hi is not None:
                hi = min(hi, bound_hi)
            norm = " ".join(best_line.split())
            for ln in range(lo, hi + 1):
                raw = lines[ln - 1].strip()
                if raw == best_line or " ".join(raw.split()) == norm:
                    picked_no = ln
                    break

        return best_line, picked_no

    def _refine_crash_func_when_addr2line_outside_snippet(
        self,
        crash_func: CrashFunction,
        crash_frame: Dict[str, Any],
        code_roots: List[str],
    ) -> Tuple[CrashFunction, bool]:
        """
        addr2line 给出的行号若落在已提取函数片段 [snippet_start_line, snippet_end_line] 之外，
        则仅在当前函数体内重选展示行，不把「高分行」换到其它栈帧函数（与栈顶优先策略一致）。
        返回 (possibly_updated_crash_func, did_refine)。
        """
        if crash_frame.get("crash_location_source") == "from_log_deduce":
            return crash_func, False
        s1 = getattr(crash_func, "snippet_start_line", None)
        e1 = getattr(crash_func, "snippet_end_line", None)
        try:
            rl = int(crash_frame.get("resolved_line", 0) or 0)
        except (TypeError, ValueError):
            rl = 0
        if not s1 or not e1 or s1 <= 0 or e1 < s1 or rl <= 0:
            return crash_func, False
        if s1 <= rl <= e1:
            return crash_func, False

        rfile = (crash_frame.get("resolved_file") or "").strip()
        src = self._find_source_file(rfile, code_roots)
        if not src:
            return crash_func, False
        try:
            with open(src, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except (OSError, IOError):
            return crash_func, False

        anchor = min(max(s1, (s1 + e1) // 2), e1)
        initial = lines[anchor - 1].strip() if 0 < anchor <= len(lines) else ""
        span = max(12, (e1 - s1) // 2 + 4)
        picked, picked_no = self._pick_best_crash_line_around(
            lines,
            anchor,
            initial,
            search_range=span,
            bound_lo=s1,
            bound_hi=e1,
        )
        if not picked:
            return crash_func, False
        new_no = picked_no if picked_no and picked_no > 0 else anchor
        return (
            replace(crash_func, crash_line=picked, crash_line_number=new_no),
            True,
        )

    def _extract_function_signature(
        self,
        lines: List[str],
        target_line_index: int,
        target_function_name: Optional[str] = None,
    ) -> str:
        """
        提取函数签名（优先按 resolved_function 的函数名 token 定位），避免把 else if/switch 等语句误判为签名。
        """
        # 0) tree-sitter 后端优先（阶段1：仅用于函数签名/函数体边界稳定化）
        if self.code_parser_backend == "tree_sitter" and self._ts_parser is not None:
            src = "\n".join(lines)
            ts_sig, _ = self._ts_extract_signature_and_body(src, target_line_index, target_function_name)
            if ts_sig:
                return ts_sig

        # 1) regex 方案：按函数名 token 定位多行签名（支持成员函数/全局函数）
        if target_function_name:
            fn = target_function_name.strip()
            if fn:
                # 从目标行向上找包含 “fn(” 的起始行，再向下拼到 “)” 结束
                max_lookback = 220
                start = max(0, target_line_index)
                end = max(0, target_line_index - max_lookback)
                for i in range(start, end - 1, -1):
                    line = lines[i].strip()
                    if not line or line.startswith("//") or line.startswith("/*"):
                        continue
                    # 控制流语句直接跳过（避免 else if 被误命中）
                    lstr = line.lstrip()
                    if re.match(r"^(else\s+if|if|for|while|switch|catch|return|throw|do|try)\b", lstr):
                        continue
                    if not re.search(rf"\b{re.escape(fn)}\s*\(", line):
                        continue
                    # 排除调用语句/声明语句（如 recurse_forever();），避免误把函数体内语句当签名。
                    if ";" in line and "{" not in line:
                        continue

                    sig_lines = [line]
                    # 用括号 balance 判断签名的参数列表是否真正闭合
                    balance = line.count("(") - line.count(")")
                    # 向下拼接参数行，直到遇到可能的签名结束
                    for j in range(i + 1, min(len(lines), i + 30)):
                        nxt = lines[j].strip()
                        if not nxt or nxt.startswith("//") or nxt.startswith("/*"):
                            continue
                        sig_lines.append(nxt)
                        balance += nxt.count("(") - nxt.count(")")
                        if balance <= 0:
                            return " ".join(" ".join(x.split()) for x in sig_lines).strip()

                    # 如果没拼到 ')'，至少返回目前拼出来的（防止完全失败）
                    return " ".join(" ".join(x.split()) for x in sig_lines).strip()

        # 2) fallback：单行正则（带关键字屏蔽，避免 else if/switch 误匹配）
        keywords_start = (
            "else if",
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "return",
            "throw",
            "do",
            "try",
        )

        for i in range(target_line_index, -1, -1):
            line = lines[i].strip()
            if not line or line.startswith('//') or line.startswith('/*'):
                continue

            lstr = line.lstrip()
            if any(lstr.startswith(k) for k in keywords_start):
                continue
            if lstr.startswith("case") or lstr.startswith("default"):
                continue

            # 查找函数定义模式：尽量收窄，避免把语句当成签名
            function_patterns = [
                r'(?:void\s+)?\w+::\w+\s*\([^)]*\)\s*$',          # Class::fn(...) 或 void Class::fn(...)
                r'(?:void\s+)?\w+\s+\w+\s*\([^)]*\)\s*$',            # return_type fn(...)
                r'((?:static|inline|constexpr|extern|virtual)\s+[\w:\<\>\~\*&\s]+\s+[A-Za-z_]\w*\s*\([^)]*\))',
                # 覆盖模板类成员函数：CVList< TYPE, ARG_TYPE >::RemoveAt(...)
                r'([\w:\<\>\,\s\*&\~]+\s+[A-Za-z_]\w*(?:\s*<[^;{}()]+>)?\s*::\s*[A-Za-z_]\w*\s*\([^)]*\))',
            ]

            for pattern in function_patterns:
                match = re.search(pattern, line)
                if match:
                    # 只返回捕获到的分组（若没有 group(1)，则返回整个匹配）
                    return match.group(1) if match.groups() else match.group(0)

        return "unknown function"

    def _ts_extract_signature_and_body(
        self, source_text: str, target_line_index: int, target_function_name: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """用 tree-sitter 提取目标行所在函数的签名与完整函数体。"""
        if not (self._ts_parser and source_text):
            return None, None
        try:
            tree = self._ts_parser.parse(source_text.encode("utf-8", errors="ignore"))
            root = tree.root_node
        except Exception:
            return None, None

        token = (target_function_name or "").strip()
        if "::" in token:
            token = token.split("::")[-1].strip()
        token_pat = re.compile(rf"\b~?{re.escape(token)}\s*\(") if token else None

        lines = source_text.split("\n")
        # tree-sitter 的 start_byte/end_byte 为 UTF-8 字节偏移；必须对 bytes 切片再 decode，否则码位与字节错位
        # 会导致函数首行变成 pline=... / erer::Init 等。
        source_bytes = source_text.encode("utf-8")

        def _ts_fn_slice_start_byte(node) -> int:
            """与下方提取 func_text 一致：大文件 C++ 中 start_byte 可能落在函数体中间，退回行首。"""
            try:
                row = int(node.start_point[0])
            except Exception:
                return node.start_byte
            if row < 0 or row >= len(lines):
                return node.start_byte
            line_start_byte = sum(len(lines[i].encode("utf-8")) + 1 for i in range(row))
            first_line_end = line_start_byte + len(lines[row].encode("utf-8"))
            if node.start_byte > first_line_end:
                return line_start_byte
            return node.start_byte

        def _ts_slice_utf8(bstart: int, bend: int) -> str:
            return source_bytes[bstart:bend].decode("utf-8", errors="ignore")

        best = None
        best_span = None
        stack = [root]
        while stack:
            node = stack.pop()
            try:
                st = node.start_point[0]
                ed = node.end_point[0]
            except Exception:
                continue
            if st <= target_line_index <= ed and node.type == "function_definition":
                fn_start = _ts_fn_slice_start_byte(node)
                fn_text = _ts_slice_utf8(fn_start, node.end_byte)
                sig_raw = fn_text.split("{", 1)[0].strip()
                # 关键修正：仅在“签名区”匹配函数 token，不能在函数体内匹配，
                # 否则会把“调用了目标函数的其它函数”误识别成目标函数本身。
                if token_pat and not token_pat.search(sig_raw):
                    # token 已知时，优先筛掉不相关函数定义
                    pass
                else:
                    span = ed - st
                    if best is None or span < (best_span or 10**9):
                        best = node
                        best_span = span
            # 继续遍历
            try:
                stack.extend(list(node.children))
            except Exception:
                continue

        if best is None:
            return None, None

        fn_start = _ts_fn_slice_start_byte(best)
        func_text = _ts_slice_utf8(fn_start, best.end_byte)
        # 签名取第一个 '{' 之前，压缩空白
        sig_raw = func_text.split("{", 1)[0].strip()
        signature = " ".join(sig_raw.split()) if sig_raw else None
        if signature and token_pat and not token_pat.search(signature):
            return None, None
        body = func_text.strip() if func_text.strip() else None
        return signature, body

    def _ts_extract_function_name_from_signature(self, signature: str) -> Optional[str]:
        """从函数签名中提取函数名 token（支持 Class::method / 析构 / 普通函数）。"""
        if not signature:
            return None
        idx = signature.find("(")
        if idx > 0:
            head = signature[:idx].strip()
            if "::" in head:
                tail = head.split("::")[-1].strip()
                if tail:
                    return tail.lstrip("~")
            parts = head.split()
            if parts:
                return parts[-1].lstrip("~")
        m = re.search(r"([~A-Za-z_]\w*)\s*\(", signature)
        if not m:
            return None
        return m.group(1)

    def _ts_extract_template_params(self, function_signature: str) -> List[str]:
        """
        从函数签名中提取模板参数列表

        Args:
            function_signature: 函数签名（如 "void foo<T>(int a)"）

        Returns:
            模板参数列表（如 ["T", "int"]）
        """
        if not function_signature:
            return []

        # 匹配模板参数列表: <...>
        template_match = re.search(r"<([^>]+)>", function_signature)
        if not template_match:
            return []

        template_content = template_match.group(1)
        # 分割模板参数（考虑嵌套情况）
        params = []
        current = ""
        depth = 0

        for char in template_content:
            if char == "<":
                depth += 1
                current += char
            elif char == ">":
                depth -= 1
                current += char
            elif char == "," and depth == 0:
                if current.strip():
                    params.append(current.strip())
                current = ""
            else:
                current += char

        if current.strip():
            params.append(current.strip())

        return params

    def _ts_detect_lambda(self, source_text: str, line_index: int) -> Optional[Dict[str, Any]]:
        """
        检测指定行是否存在 lambda 表达式

        Args:
            source_text: 源代码文本
            line_index: 目标行索引（0-based）

        Returns:
            如果检测到 lambda，返回相关信息；否则返回 None
        """
        if not self._ts_parser:
            return None

        try:
            tree = self._ts_parser.parse(source_text.encode("utf-8", errors="ignore"))
            root = tree.root_node
        except Exception:
            return None

        # 遍历 AST 查找 lambda 表达式
        stack = [root]
        while stack:
            node = stack.pop()
            try:
                st = node.start_point[0]
                ed = node.end_point[0]
            except Exception:
                continue

            if st <= line_index <= ed:
                # lambda 表达式在 C++ 中表现为 lambda_expression 节点
                if node.type == "lambda_expression":
                    # 提取 lambda 的捕获列表和函数体
                    capture_node = node.child_by_field_name("capture")
                    body_node = node.child_by_field_name("body")

                    capture_list = ""
                    if capture_node:
                        capture_text = source_text[capture_node.start_byte:capture_node.end_byte]
                        capture_list = capture_text.strip()

                    body_text = ""
                    if body_node:
                        body_text = source_text[body_node.start_byte:body_node.end_byte].strip()

                    return {
                        "capture_list": capture_list,
                        "body": body_text,
                        "start_line": st,
                        "end_line": ed
                    }

            try:
                stack.extend(list(node.children))
            except Exception:
                continue

        return None

    def _ts_expand_macros(self, source_text: str, target_line_index: int) -> List[str]:
        """
        简化版宏展开：检测目标行附近的宏定义并提取展开信息

        Args:
            source_text: 源代码文本
            target_line_index: 目标行索引（0-based）

        Returns:
            宏定义信息列表
        """
        if not source_text:
            return []

        lines = source_text.split('\n')
        if target_line_index >= len(lines):
            return []

        # 查找附近的宏定义（向上搜索10行）
        macro_infos = []
        search_start = max(0, target_line_index - 10)

        for i in range(search_start, target_line_index + 1):
            line = lines[i].strip()
            # 检测 #define 宏
            if line.startswith('#define'):
                # 提取宏名和值
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    macro_name = parts[1] if len(parts) > 1 else ""
                    macro_value = parts[2] if len(parts) > 2 else ""
                    macro_infos.append({
                        "line": i,
                        "name": macro_name,
                        "value": macro_value
                    })

        return macro_infos

    def _ts_call_expr_matches_target(self, call_node, source_text: str, target_simple_name: str) -> bool:
        """判断 call_expression 是否调用目标函数（按简单函数名匹配）。"""
        if not (call_node and source_text and target_simple_name):
            return False
        try:
            fn_node = call_node.child_by_field_name("function")
        except Exception:
            fn_node = None
        if fn_node is None:
            return False
        raw = source_text.encode("utf-8")
        callee = raw[fn_node.start_byte : fn_node.end_byte].decode("utf-8", errors="ignore").strip()
        if not callee:
            return False
        # 覆盖：foo(...), obj.foo(...), obj->foo(...), ns::foo(...)
        patterns = [
            rf"(^|::|->|\.){re.escape(target_simple_name)}$",
        ]
        for p in patterns:
            if re.search(p, callee):
                return True
        # 链式成员：callee 文本可能为整段 "a.b.emplace_back" / "p->q->emplace_back"，补一层尾段匹配
        tail = callee
        for sep in ("->", "."):
            if sep in tail:
                tail = tail.split(sep)[-1].strip()
        if tail == target_simple_name or tail.startswith(f"{target_simple_name}("):
            return True
        return False

    def _callee_names_for_call_site_match(self, callee: GraphNode) -> List[str]:
        """从 GraphNode 提取调用点可能出现的被调名（简单名 / 签名尾段），供 AST 与正则匹配。"""
        out: List[str] = []
        sig = (callee.signature or "").strip()
        if sig:
            t = self._ts_extract_function_name_from_signature(sig)
            if t:
                out.append(t)
        nm = (callee.name or "").strip()
        if nm and nm not in out:
            out.append(nm)
        seen: set = set()
        uniq: List[str] = []
        for x in out:
            if x and x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    def _snippet_text_has_call_to_names(self, body: str, names: List[str]) -> bool:
        """正则回退：判断文本中是否存在对任一 names 的函数调用形态（非定义行）。"""
        if not body or not names:
            return False
        for cn in names:
            if not cn:
                continue
            if re.search(rf"(?:^|::|->|\.|\b){re.escape(cn)}\s*\(", body):
                return True
        return False

    def _ts_function_body_has_calls_to_targets(
        self, func_def_node: Any, source_text: str, target_names: List[str]
    ) -> bool:
        """在单个 function_definition 子树内扫描 call_expression，是否匹配任一 target_names。"""
        if not func_def_node or not source_text or not target_names:
            return False
        inner: List[Any] = [func_def_node]
        while inner:
            cur = inner.pop()
            if cur is None:
                continue
            if cur.type == "call_expression":
                for tn in target_names:
                    if self._ts_call_expr_matches_target(cur, source_text, tn):
                        return True
            try:
                inner.extend(list(cur.children))
            except Exception:
                continue
        return False

    def _ts_pick_function_definition_for_graph_node(
        self, caller: GraphNode, source_bytes: bytes, root_node: Any
    ) -> Any:
        """在全文件 AST 中定位与 caller GraphNode 对应的 function_definition 节点。"""
        caller_simple = self._ts_extract_function_name_from_signature((caller.signature or "").strip())
        if not caller_simple:
            caller_simple = (caller.name or "").strip()
        if not caller_simple:
            return None
        ref_row = (caller.snippet_start_line or 0) - 1
        candidates: List[Tuple[Any, int]] = []
        stack: List[Any] = [root_node]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            if node.type == "function_definition":
                fn_text = source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")
                sig_raw = fn_text.split("{", 1)[0].strip()
                signature = " ".join(sig_raw.split()) if sig_raw else ""
                fn_name = self._ts_extract_function_name_from_signature(signature)
                if fn_name == caller_simple:
                    candidates.append((node, int(node.start_point[0])))
            try:
                stack.extend(list(reversed(node.children)))
            except Exception:
                pass
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][0]
        best_n = None
        best_d = 10**9
        for node, row in candidates:
            d = abs(row - ref_row) if ref_row >= 0 else 0
            if d < best_d:
                best_d = d
                best_n = node
        return best_n

    def _verify_stack_caller_resolves_call_to_callee(
        self, caller: GraphNode, callee: GraphNode
    ) -> bool:
        """
        栈序补边前的校验：在 caller 源码中做语法分析（优先 tree-sitter call_expression），
        确认存在对被调 callee 的调用，再认为该 hop 有效；否则不连边。
        """
        names = self._callee_names_for_call_site_match(callee)
        if not names:
            return False
        path = caller.file
        # 1) tree-sitter：定位 caller 的 function_definition，在函数体内匹配 call_expression
        if path and os.path.isfile(path) and self._ts_parser:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    source_text = f.read()
                source_bytes = source_text.encode("utf-8", errors="ignore")
                tree = self._ts_parser.parse(source_bytes)
                fn_node = self._ts_pick_function_definition_for_graph_node(
                    caller, source_bytes, tree.root_node
                )
                if fn_node is not None and self._ts_function_body_has_calls_to_targets(
                    fn_node, source_text, names
                ):
                    return True
            except Exception:
                pass
        # 2) 回退：regex 后端或 TS 未命中时，用 snippet / 行号范围做调用形态匹配
        if caller.snippet:
            body = "\n".join(caller.snippet)
            if self._snippet_text_has_call_to_names(body, names):
                return True
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                ss = (caller.snippet_start_line or 1) - 1
                ee = (caller.snippet_end_line or ss + 1) - 1
                if 0 <= ss <= ee < len(lines):
                    chunk = "".join(lines[ss : ee + 1])
                    if self._snippet_text_has_call_to_names(chunk, names):
                        return True
            except Exception:
                pass
        return False

    def _longest_verified_suffix_chain(
        self, outer_to_crash_ids: List[str], verified_pairs: set
    ) -> List[str]:
        """
        outer_to_crash_ids：从栈外到崩溃函数的有序节点 id 列表。
        verified_pairs：已通过源码校验的 (caller_id, callee_id)，表示 caller 调用 callee。
        从崩溃端向前贪心：仅保留相邻 hop 均在 verified_pairs 中的最长后缀。
        """
        if len(outer_to_crash_ids) < 2:
            return list(outer_to_crash_ids)
        chain = list(outer_to_crash_ids)
        out: List[str] = [chain[-1]]
        for i in range(len(chain) - 2, -1, -1):
            if (chain[i], chain[i + 1]) in verified_pairs:
                out.insert(0, chain[i])
            else:
                break
        return out

    def _collect_stack_priority_source_files(
        self, add2line_data: Optional[Dict[str, Any]], code_roots: List[str]
    ) -> List[str]:
        """
        从 add2line 的 resolved_frames 提取工程内源码路径（按堆栈帧顺序），去重。
        用于静态「直接调用者 / 共享变量」搜索时先于全仓扫描。
        """
        out: List[str] = []
        seen: set = set()
        frames = ((add2line_data or {}).get("resolved_frames")) or []
        roots_abs = _normalize_code_roots_arg(code_roots)
        for frame in frames:
            self._code_context_phase_check("stack_priority_source_files")
            rf = (frame.get("resolved_file") or "").strip()
            if not rf:
                continue
            if self._is_external_path(rf, roots_abs):
                continue
            abs_path = self._find_source_file(rf, code_roots) or rf
            try:
                ap = os.path.abspath(abs_path)
            except Exception:
                ap = abs_path
            if ap in seen:
                continue
            if os.path.isfile(ap) and self._is_supported_file(ap):
                seen.add(ap)
                out.append(ap)
        return out

    def _pick_code_root_for_file(self, file_path: str, code_roots: List[str]) -> str:
        try:
            ap = os.path.abspath(file_path)
        except Exception:
            ap = file_path
        for cr in code_roots or []:
            try:
                if ap.startswith(os.path.abspath(cr)):
                    return cr
            except Exception:
                continue
        return code_roots[0] if code_roots else ""

    def _iter_ts_files_for_call_chain(
        self,
        code_roots: List[str],
        restrict_to_files: Optional[List[str]],
        exclude_files: Optional[set],
    ):
        """供 tree-sitter 调用链扫描：优先仅 restrict 列表，否则遍历 code_roots；exclude 跳过已扫过的栈文件。"""
        exclude_abs: Optional[set] = None
        if exclude_files:
            exclude_abs = set()
            for p in exclude_files:
                try:
                    exclude_abs.add(os.path.abspath(p))
                except Exception:
                    exclude_abs.add(p)
        if restrict_to_files is not None:
            for raw in restrict_to_files:
                if not raw:
                    continue
                try:
                    ap = os.path.abspath(raw)
                except Exception:
                    ap = raw
                if exclude_abs and ap in exclude_abs:
                    continue
                if os.path.isfile(ap) and self._is_supported_file(ap):
                    yield ap
            return
        walk_i = 0
        for code_root in code_roots or []:
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]
                for file in files:
                    walk_i += 1
                    if walk_i % 200 == 0:
                        self._code_context_phase_check("iter_ts_files_walk")
                    file_path = os.path.join(root, file)
                    try:
                        ap = os.path.abspath(file_path)
                    except Exception:
                        ap = file_path
                    if exclude_abs and ap in exclude_abs:
                        continue
                    self.search_stats["files_scanned"] += 1
                    if self._should_skip_file(file_path, code_root):
                        continue
                    yield file_path

    def _collect_nearby_module_files(
        self,
        stack_priority_files: List[str],
        code_roots: List[str],
        parent_levels: int = 2,
        descend_depth: int = 3,
        max_files: int = 4000,
    ) -> List[str]:
        """基于堆栈命中文件，收集“同模块/父模块有限深度”的候选文件，避免全仓补扫。"""
        if not stack_priority_files or not code_roots:
            return []
        seeds: List[Tuple[str, str]] = []
        for raw in stack_priority_files:
            if not raw:
                continue
            try:
                ap = os.path.abspath(raw)
            except Exception:
                ap = raw
            if not os.path.isfile(ap):
                continue
            cr = self._pick_code_root_for_file(ap, code_roots)
            if not cr:
                continue
            seed_dir = os.path.dirname(ap)
            if not seed_dir:
                continue
            seeds.append((seed_dir, cr))
        if not seeds:
            return []

        seen_dirs: set = set()
        candidate_dirs: List[Tuple[str, str]] = []
        for seed_dir, cr in seeds:
            root_abs = os.path.abspath(cr)
            cur = seed_dir
            hops = 0
            while True:
                if not cur:
                    break
                try:
                    cur_abs = os.path.abspath(cur)
                except Exception:
                    cur_abs = cur
                if not cur_abs.startswith(root_abs):
                    break
                dkey = (cur_abs, root_abs)
                if dkey not in seen_dirs and os.path.isdir(cur_abs):
                    seen_dirs.add(dkey)
                    candidate_dirs.append((cur_abs, cr))
                if hops >= parent_levels:
                    break
                parent = os.path.dirname(cur_abs)
                if not parent or parent == cur_abs:
                    break
                cur = parent
                hops += 1

        out: List[str] = []
        seen_files: set = set()
        nf = 0
        for base_dir, cr in candidate_dirs:
            try:
                base_abs = os.path.abspath(base_dir)
            except Exception:
                base_abs = base_dir
            for walk_root, dirs, files in os.walk(base_abs):
                try:
                    rel = os.path.relpath(walk_root, base_abs)
                    cur_depth = 0 if rel == "." else rel.count(os.sep) + 1
                except Exception:
                    cur_depth = 0
                if cur_depth > descend_depth:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]
                for name in files:
                    nf += 1
                    if nf % 80 == 0:
                        self._code_context_phase_check("nearby_module_files")
                    fp = os.path.join(walk_root, name)
                    try:
                        ap = os.path.abspath(fp)
                    except Exception:
                        ap = fp
                    if ap in seen_files:
                        continue
                    if not self._is_supported_file(ap):
                        continue
                    if self._should_skip_file(ap, cr):
                        continue
                    seen_files.add(ap)
                    out.append(ap)
                    if len(out) >= max_files:
                        return out
        return out

    def _find_call_chain_functions_tree_sitter(
        self,
        crash_function_name: str,
        code_roots: List[str],
        restrict_to_files: Optional[List[str]] = None,
        exclude_files: Optional[set] = None,
    ) -> List[CallChainFunction]:
        """tree-sitter AST 版：扫描 call_expression，定位直接调用崩溃函数的上层函数。
        restrict_to_files 非空时仅扫这些文件；exclude_files 在全仓遍历时跳过（用于栈优先后再扫其余文件）。

        优化：支持并行扫描大目录
        """
        import os
        from concurrent.futures import ThreadPoolExecutor, as_completed

        call_chain_functions: List[CallChainFunction] = []
        if not (self._ts_parser and crash_function_name):
            return call_chain_functions

        simple_function_name = self._extract_simple_function_name(crash_function_name)
        demangled = self._extract_simple_name_from_mangled(simple_function_name)
        if demangled:
            simple_function_name = demangled
        if not simple_function_name:
            return call_chain_functions

        # 收集所有待扫描的文件
        all_files = []
        coll_i = 0
        for file_path in self._iter_ts_files_for_call_chain(
            code_roots, restrict_to_files, exclude_files
        ):
            coll_i += 1
            if coll_i % 300 == 0:
                self._code_context_phase_check("ts_collect_file_list")
            cr = self._pick_code_root_for_file(file_path, code_roots)
            if self._should_skip_file(file_path, cr):
                continue
            all_files.append((file_path, cr))

        total_files = len(all_files)
        logger.info(f"tree-sitter 将扫描 {total_files} 个文件 (并行阈值: {self.parallel_threshold})")

        # ========== 优化3：并行扫描 ==========
        # 确定并行 worker 数量
        max_workers = self.max_workers
        if max_workers is None:
            # 自动：使用 CPU 核心数，上限 8
            import multiprocessing
            max_workers = min(multiprocessing.cpu_count(), 8)

        # 判断是否启用并行（文件数 >= 阈值且非禁用）
        use_parallel = total_files >= self.parallel_threshold and max_workers > 0

        if use_parallel:
            logger.info(f"启用并行扫描: {max_workers} workers")
            call_chain_functions = self._parallel_tree_sitter_scan(
                all_files, simple_function_name, code_roots, max_workers
            )
            return call_chain_functions

        # 串行扫描（文件数少时）
        unique: set = set()
        for file_path, cr in all_files:
            self._code_context_phase_check("tree_sitter_serial_file")
            self.search_stats["files_scanned"] += 1
            try:
                self.search_stats["files_read"] += 1
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    source_text = f.read()
                if not source_text.strip():
                    continue
                source_bytes = source_text.encode("utf-8", errors="ignore")
                tree = self._ts_parser.parse(source_bytes)
                root_node = tree.root_node
            except Exception:
                continue

            stack = [root_node]
            while stack:
                node = stack.pop()
                if node is None:
                    continue
                if node.type == "function_definition":
                    fn_text = source_bytes[node.start_byte : node.end_byte].decode(
                        "utf-8", errors="ignore"
                    )
                    sig_raw = fn_text.split("{", 1)[0].strip()
                    signature = " ".join(sig_raw.split()) if sig_raw else ""
                    caller_name = self._ts_extract_function_name_from_signature(signature)
                    if not caller_name:
                        try:
                            stack.extend(list(node.children))
                        except Exception:
                            pass
                        continue
                    if caller_name == simple_function_name:
                        continue

                    has_target_call = False
                    inner = [node]
                    while inner and not has_target_call:
                        cur = inner.pop()
                        if cur is None:
                            continue
                        if cur.type == "call_expression":
                            if self._ts_call_expr_matches_target(cur, source_text, simple_function_name):
                                has_target_call = True
                                break
                        try:
                            inner.extend(list(cur.children))
                        except Exception:
                            continue

                    if has_target_call:
                        snippet = [ln.rstrip() for ln in fn_text.split("\n") if ln.strip()]
                        if self.max_code_length > 0:
                            snippet = self._truncate_snippet(snippet)
                        key = (caller_name, file_path, signature)
                        if key not in unique:
                            unique.add(key)
                            call_chain_functions.append(
                                CallChainFunction(name=caller_name, file=file_path, snippet=snippet)
                            )
                try:
                    stack.extend(list(node.children))
                except Exception:
                    continue
        return call_chain_functions

    def _parallel_tree_sitter_scan(
        self,
        file_infos: List[Tuple[str, str]],
        simple_function_name: str,
        code_roots: List[str],
        max_workers: int = 4,
    ) -> List[CallChainFunction]:
        """并行 tree-sitter 扫描多个文件"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: List[CallChainFunction] = []
        unique: set = set()

        def _scan_single_file(file_info: Tuple[str, str]) -> List[CallChainFunction]:
            """扫描单个文件，查找调用目标函数的函数"""
            file_path, cr = file_info
            local_results = []
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    source_text = f.read()
                if not source_text.strip():
                    return []
                source_bytes = source_text.encode("utf-8", errors="ignore")
                tree = self._ts_parser.parse(source_bytes)
                root_node = tree.root_node
            except Exception:
                return []

            stack = [root_node]
            while stack:
                node = stack.pop()
                if node is None:
                    continue
                if node.type == "function_definition":
                    fn_text = source_bytes[node.start_byte : node.end_byte].decode(
                        "utf-8", errors="ignore"
                    )
                    sig_raw = fn_text.split("{", 1)[0].strip()
                    signature = " ".join(sig_raw.split()) if sig_raw else ""
                    caller_name = self._ts_extract_function_name_from_signature(signature)
                    if not caller_name:
                        try:
                            stack.extend(list(node.children))
                        except Exception:
                            pass
                        continue
                    if caller_name == simple_function_name:
                        continue

                    has_target_call = False
                    inner = [node]
                    while inner and not has_target_call:
                        cur = inner.pop()
                        if cur is None:
                            continue
                        if cur.type == "call_expression":
                            if self._ts_call_expr_matches_target(cur, source_text, simple_function_name):
                                has_target_call = True
                                break
                        try:
                            inner.extend(list(cur.children))
                        except Exception:
                            continue

                    if has_target_call:
                        snippet = [ln.rstrip() for ln in fn_text.split("\n") if ln.strip()]
                        if self.max_code_length > 0:
                            snippet = self._truncate_snippet(snippet)
                        local_results.append(
                            CallChainFunction(name=caller_name, file=file_path, snippet=snippet)
                        )
                try:
                    stack.extend(list(node.children))
                except Exception:
                    continue
            return local_results

        # 并行执行（总阶段超时时尽快 shutdown，避免 with 块末尾长时间 join）
        scanned_count = 0
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {executor.submit(_scan_single_file, info): info for info in file_infos}
            for future in as_completed(futures):
                self._code_context_phase_check("parallel_tree_sitter")
                scanned_count += 1
                if scanned_count % 500 == 0:
                    logger.info(f"并行扫描进度: {scanned_count}/{len(file_infos)}")
                try:
                    file_results = future.result()
                    for cf in file_results:
                        key = (cf.name, cf.file, cf.snippet[0] if cf.snippet else "")
                        if key not in unique:
                            unique.add(key)
                            results.append(cf)
                except Exception:
                    pass
        except _CodeContextPhaseTimeout:
            logger.warning("并行 tree-sitter 调用链扫描因整阶段超时中止，返回已收集结果")
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        self.search_stats["files_scanned"] = len(file_infos)
        self.search_stats["files_read"] = len(file_infos)
        logger.info(f"并行扫描完成: 扫描 {len(file_infos)} 个文件, 找到 {len(results)} 个调用者")
        return results

    @staticmethod
    def _join_source_line_slice(lines_slice: List[str]) -> str:
        """
        将源文件行切片拼成多行文本。
        兼容 readlines()（行尾含 \\n）与 content.split('\\n')（行尾不含）：统一先去掉行尾换行再用 \\n 拼接，
        避免误用 \"\".join 把无换行符的行粘成一行。
        """
        if not lines_slice:
            return ""
        return "\n".join(ln.rstrip("\n\r") for ln in lines_slice).strip()

    def _extract_full_function_code_by_token_regex(
        self,
        lines: List[str],
        target_line_index: int,
        token: str,
    ) -> Optional[str]:
        """
        用函数名 token 在崩溃行上方回溯到签名行，再按大括号配对截取「单个」函数体。
        在已知目标函数名时优先于 tree-sitter：TS 用「最小行跨度」选 function_definition，
        在部分 C++ 源上可能误选到跨多成员函数的节点，导致片段从其它函数体中间开始。
        """
        if not token:
            return None
        sig_re = re.compile(rf"(?:(?:^|::)\s*~?{re.escape(token)}\s*\(|\b~?{re.escape(token)}\s*\()")

        def _is_control_line(l: str) -> bool:
            ll = (l or "").lstrip()
            while ll.startswith("}"):
                ll = ll[1:].lstrip()
            return bool(
                re.match(r"^(else\s+if|if|for|while|switch|catch|return|throw|do|try)\b", ll)
                or ll.startswith("case")
                or ll.startswith("default")
            )

        max_lookback = 400
        search_start = max(0, target_line_index - max_lookback)
        sig_start = -1
        for i in range(target_line_index, search_start - 1, -1):
            l = lines[i].strip()
            if not l or l.startswith("//") or l.startswith("/*"):
                continue
            if _is_control_line(l):
                continue
            if sig_re.search(l):
                # 排除函数调用/声明语句，必须像函数定义起始。
                if ";" in l and "{" not in l:
                    continue
                sig_start = i
                break

        if sig_start == -1:
            return None

        brace_start = -1
        for j in range(sig_start, min(len(lines), sig_start + 250)):
            if "{" in lines[j]:
                brace_start = j
                break

        if brace_start == -1:
            return None

        function_end = len(lines)
        brace_count = 0
        found_opening_brace = False
        for k in range(brace_start, len(lines)):
            for char in lines[k]:
                if char == "{":
                    brace_count += 1
                    found_opening_brace = True
                elif char == "}":
                    brace_count -= 1
                    if found_opening_brace and brace_count == 0:
                        function_end = k + 1
                        break
            if found_opening_brace and brace_count == 0:
                break
        function_lines = lines[sig_start:function_end]
        return self._join_source_line_slice(function_lines)

    def _extract_full_function_code(
        self,
        lines: List[str],
        target_line_index: int,
        target_function_name: Optional[str] = None,
    ) -> Optional[str]:
        """提取完整函数代码"""
        try:
            token = ""
            if target_function_name:
                token = target_function_name.strip().split("::")[-1].strip()

            # 1) 已知目标函数名：优先 token + 大括号配对（比 tree-sitter 的 smallest-span 更稳）
            if token:
                by_regex = self._extract_full_function_code_by_token_regex(
                    lines, target_line_index, token
                )
                if by_regex:
                    return by_regex

            # 2) tree-sitter
            if self.code_parser_backend == "tree_sitter" and self._ts_parser is not None:
                src = "\n".join(lines)
                _, ts_body = self._ts_extract_signature_and_body(src, target_line_index, target_function_name)
                if ts_body:
                    return ts_body

            # 3) 兼容旧逻辑的兜底路径：依赖 `_cpp_signature_probe_string`（含多行成员签名）
            # 从目标行开始向上搜索函数开始
            function_start = -1

            # 向上搜索函数开始，寻找函数定义行
            for i in range(target_line_index, -1, -1):
                line = lines[i].strip()
                if not line or line.startswith('//') or line.startswith('/*'):
                    continue
                
                # 检查是否是函数定义行（含多行成员函数签名）
                if self._cpp_signature_probe_string(lines, i):
                    if token:
                        # 某些多行签名：候选行可能只有 ')' 但不包含 token
                        # 所以在候选行附近向上找 token 证据，避免把 else if / switch 误判为函数开始
                        window_start = max(0, i - 6)
                        window_lines = "\n".join(lines[window_start:i + 1])
                        if not re.search(rf"\b~?{re.escape(token)}\b\s*\(", window_lines):
                            continue
                    function_start = i
                    break
                
                # 如果遇到函数开始的大括号，继续向上搜索函数定义行
                if '{' in line:
                    # 继续向上搜索函数定义行
                    for j in range(i-1, max(i-10, -1), -1):  # 向前搜索最多10行
                        prev_line = lines[j].strip()
                        if prev_line and not prev_line.startswith('//') and not prev_line.startswith('/*'):
                            if self._cpp_signature_probe_string(lines, j):
                                if token:
                                    window_start = max(0, j - 6)
                                    window_lines = "\n".join(lines[window_start:j + 1])
                                    if not re.search(rf"\b~?{re.escape(token)}\b\s*\(", window_lines):
                                        continue
                                function_start = j
                                break
                    if function_start != -1:
                        break
            
            if function_start == -1:
                return None
            
            # 向下搜索函数结束
            function_end = len(lines)
            brace_count = 0
            found_opening_brace = False
            
            for i in range(function_start, len(lines)):
                line = lines[i]
                
                # 计算大括号
                for char in line:
                    if char == '{':
                        brace_count += 1
                        found_opening_brace = True
                    elif char == '}':
                        brace_count -= 1
                        if found_opening_brace and brace_count == 0:
                            function_end = i + 1
                            break
                
                if found_opening_brace and brace_count == 0:
                    break
            
            # 提取完整函数（行切片可能来自 readlines 或 split('\\n')，统一经 _join_source_line_slice）
            function_lines = lines[function_start:function_end]
            return self._join_source_line_slice(function_lines)
            
        except Exception as e:
            logger.debug(f"提取完整函数代码失败: {e}")
            return None

    def _extract_full_function_code_for_caller(
        self, lines: List[str], target_line_index: int, caller_function_name: Optional[str]
    ) -> Optional[str]:
        """
        面向调用链场景的函数体提取：
        优先按 caller_function_name 在 target_line 上方回溯命中对应定义，再提取完整函数体；
        若未命中则回退到通用 _extract_full_function_code。
        """
        if not caller_function_name:
            return self._extract_full_function_code(lines, target_line_index)

        simple_name = caller_function_name.split("::")[-1] if "::" in caller_function_name else caller_function_name
        simple_name = simple_name.strip()
        if not simple_name:
            return self._extract_full_function_code(lines, target_line_index)

        # 对 operator 统一按 "operator" 关键字匹配，兼容 operator() / operator[] / operator+ 等形式
        if simple_name.startswith("operator"):
            name_pattern = r"\boperator\b"
        else:
            name_pattern = rf"\b{re.escape(simple_name)}\s*\("

        start = max(0, target_line_index)
        end = max(0, target_line_index - 80)
        for i in range(start, end - 1, -1):
            line = lines[i].strip()
            if not line:
                continue
            if not self._cpp_signature_probe_string(lines, i):
                continue
            if re.search(name_pattern, line):
                code = self._extract_full_function_code(
                    lines, i, target_function_name=caller_function_name
                )
                if code:
                    return code

        return self._extract_full_function_code(
            lines, target_line_index, target_function_name=caller_function_name
        )

    def _find_function_definition_line_by_name(
        self, lines: List[str], function_name: str, anchor_line_index: int
    ) -> Optional[int]:
        """
        在单个文件中按函数名查找定义行，优先返回离 anchor 行最近的定义。
        用于 addr2line 行号不稳定时的兜底定位。
        """
        if not function_name:
            return None
        escaped = re.escape(function_name)
        candidates: List[int] = []
        for idx, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            probe = self._cpp_signature_probe_string(lines, idx)
            if not probe:
                continue
            if re.search(rf"\b{escaped}\s*\(", probe):
                candidates.append(idx)
        if not candidates:
            return None
        return min(candidates, key=lambda i: abs(i - max(0, anchor_line_index)))
    
    def _is_function_definition_line(self, line: str) -> bool:
        """检查是否是函数定义行"""
        # 关键：排除控制流语句，避免把 `} else if (...) {` / `switch (...) {` 等误判为函数定义。
        # 这些语句行即使满足部分正则形态，也不可能是函数签名定义起点。
        try:
            l = (line or "").lstrip()
            # 兼容 `} else if (...) {` 这种以右大括号结尾的行
            while l.startswith("}"):
                l = l[1:].lstrip()
            control_keywords = (
                r"^(else\s+if|if|for|while|switch|catch|return|throw|do|try)\b",
            )
            if any(re.match(p, l) for p in control_keywords):
                return False
            if l.startswith("case") or l.startswith("default"):
                return False
        except Exception:
            # 保底：发生异常时不影响后续正则判断
            pass

        # 函数定义模式
        function_patterns = [
            r'void\s+\w+::\w+\s*\([^)]*\)\s*\{',      # void Class::function_name(...) {
            r'\w+\s+\w+::\w+\s*\([^)]*\)\s*\{',       # return_type Class::function_name(...) {
            r'void\s+\w+\s*\([^)]*\)\s*\{',           # void function_name(...) {
            r'\w+\s+\w+\s*\([^)]*\)\s*\{',            # return_type function_name(...) {
            r'\w+::\w+\s*\([^)]*\)\s*\{',             # Class::function_name(...) {
            # 支持带 static/inline/constexpr 等限定词的自由函数（含同一行大括号）
            r'(?:static|inline|constexpr|extern|virtual)\s+[\w:\<\>\~\*&\s]+\s+\w+\s*\([^;{}]*\)\s*\{',
            # 支持模板类成员函数（同一行大括号）：VVoid CVList< TYPE, ARG_TYPE >::RemoveAt(...)
            r'[\w:\<\>\,\s\*&\~]+\s+[A-Za-z_]\w*(?:\s*<[^;{}()]+>)?\s*::\s*[A-Za-z_]\w*\s*\([^;{}]*\)\s*\{',
            # 支持函数定义行和大括号分开的情况（大括号在下一行）
            r'void\s+\w+::\w+\s*\([^)]*\)\s*$',       # void Class::function_name(...) (大括号在下一行)
            r'\w+\s+\w+::\w+\s*\([^)]*\)\s*$',        # return_type Class::function_name(...) (大括号在下一行)
            r'\w+::\w+\s*\([^)]*\)\s*$',              # Class::function_name(...) (大括号在下一行)
            # 支持普通自由函数（含 static）定义行与大括号分行
            r'(?:static|inline|constexpr|extern|virtual)?\s*[\w:\<\>\~\*&]+\s+\w+\s*\([^;{}]*\)\s*$',
            # 支持模板类成员函数（大括号在下一行）：VVoid CVList< TYPE, ARG_TYPE >::RemoveAt(...)
            r'[\w:\<\>\,\s\*&\~]+\s+[A-Za-z_]\w*(?:\s*<[^;{}()]+>)?\s*::\s*[A-Za-z_]\w*\s*\([^;{}]*\)\s*$',
            # 支持初始化列表的构造函数
            r'\w+::\w+\s*\([^)]*\)\s*:',              # Class::function_name(...) :
            r'\w+\s+\w+::\w+\s*\([^)]*\)\s*:',        # return_type Class::function_name(...) :
            # C++ operator 重载（含 const；支持同一行大括号/下一行大括号）
            r'[\w:<>\~\*&]+\s+[A-Za-z_][A-Za-z_0-9:]*::operator[^\s(]*\s*\([^;{}]*\)\s*(?:const)?\s*\{',
            r'[\w:<>\~\*&]+\s+[A-Za-z_][A-Za-z_0-9:]*::operator[^\s(]*\s*\([^;{}]*\)\s*(?:const)?\s*$',
        ]
        
        for pattern in function_patterns:
            if re.search(pattern, line):
                return True
        return False

    def _merge_lines_for_cpp_member_signature(self, lines: List[str], start_idx: int) -> Optional[str]:
        """
        将「首行含 Class::method( 且圆括号未在同一行闭合」的成员函数签名与后续续行合并为单行，
        再交给 `_is_function_definition_line` 判断。用于模板参数/参数列表换行排版。
        """
        if start_idx < 0 or start_idx >= len(lines):
            return None
        first_raw = lines[start_idx].rstrip("\n\r")
        stripped_first = first_raw.strip()
        if not stripped_first:
            return None
        try:
            l = stripped_first.lstrip()
            while l.startswith("}"):
                l = l[1:].lstrip()
            control_keywords = (r"^(else\s+if|if|for|while|switch|catch|return|throw|do|try)\b",)
            if any(re.match(p, l) for p in control_keywords):
                return None
            if l.startswith("case") or l.startswith("default"):
                return None
        except Exception:
            pass
        if not re.search(r"\b[A-Za-z_][\w:]*\s*::\s*[A-Za-z_][\w~]*\s*\(", stripped_first):
            return None

        def _bal(s: str) -> int:
            return s.count("(") - s.count(")")

        b = _bal(stripped_first)
        if b <= 0:
            return None
        parts = [stripped_first]
        j = start_idx + 1
        limit = min(len(lines), start_idx + 48)
        while j < limit and b > 0:
            nxt = lines[j].rstrip("\n\r").strip()
            if not nxt:
                j += 1
                continue
            if nxt.startswith("//") or nxt.startswith("/*") or nxt.startswith("*"):
                j += 1
                continue
            if nxt.startswith("#"):
                return None
            parts.append(nxt)
            b = _bal(" ".join(parts))
            j += 1
        if b != 0:
            return None
        merged = " ".join(parts)
        if self._is_function_definition_line(merged):
            return merged
        if j < len(lines):
            nxt = lines[j].rstrip("\n\r").strip()
            if nxt == "{" or (nxt.startswith("{") and not nxt.startswith("{/*")):
                trial = merged + " {"
                if self._is_function_definition_line(trial):
                    return trial
        return None

    def _cpp_signature_probe_string(self, lines: List[str], start_idx: int) -> Optional[str]:
        """单行定义返回该行；多行成员签名返回合并后的等价单行串；否则 None。"""
        if start_idx < 0 or start_idx >= len(lines):
            return None
        first = lines[start_idx].rstrip("\n\r")
        if self._is_function_definition_line(first):
            return first.strip()
        merged = self._merge_lines_for_cpp_member_signature(lines, start_idx)
        return merged

    def _extract_simple_function_name(self, full_function_name: str) -> str:
        """从完整函数名中提取简单函数名"""
        # 处理各种格式的函数名
        if '::' in full_function_name:
            # 类成员函数：Class::function_name(...)
            return full_function_name.split('::')[-1].split('(')[0]
        elif '(' in full_function_name:
            # 普通函数：function_name(...)
            return full_function_name.split('(')[0]
        else:
            return full_function_name

    def _extract_simple_name_from_mangled(self, mangled: str) -> Optional[str]:
        """从 C++ mangled 名提取简单函数名，用于首帧被 atos 错误解析时按名在源码中定位。
        例如 _Z13crash_nullptrv -> crash_nullptr，_Z14signal_handleriP9__siginfoPv -> signal_handler。"""
        if not mangled or not mangled.startswith("_Z"):
            return None
        # Itanium ABI: _Z<decimal_len><name><rest>，name 为紧接长度后的 len 个字符
        match = re.match(r"_Z(\d+)(.*)", mangled)
        if match:
            try:
                n = int(match.group(1))
                rest = match.group(2)
                if n <= len(rest):
                    name = rest[:n]  # 类型后缀在 rest[n:] 中，无需再 strip
                    return name if name else None
            except ValueError:
                pass
        # 回退：在 mangled 中查找常见崩溃/业务函数名
        for known in ("crash_nullptr", "crash_dangling", "crash_oob", "crash_divzero",
                      "crash_bad_cast", "crash_stackoverflow", "signal_handler", "main",
                      "add_node", "remove_node", "add_data", "remove_data"):
            if known in mangled:
                return known
        return None

    def _find_function_definition_location(self, simple_name: str, code_roots: List[str]) -> Optional[Tuple[str, int]]:
        """在 code_roots 下按函数名查找函数定义位置，返回 (文件路径, 行号) 或 None。"""
        _def_loc_files = 0
        for code_root in code_roots or []:
            # 尝试从缓存获取
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if d not in self.exclude_dirs]
                for file in files:
                    _def_loc_files += 1
                    if _def_loc_files % 50 == 0:
                        self._code_context_phase_check("find_function_definition_location")
                    file_path = os.path.join(root, file)
                    if not self._is_supported_file(file_path) or not self._is_file_readable(file_path):
                        continue
                    # 检查缓存
                    cached = self._get_cached_function_defs(file_path, simple_name)
                    if cached:
                        return cached
                    # 继续搜索（因为缓存可能不存在）
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()
                        for i, line in enumerate(lines, 1):
                            probe = self._cpp_signature_probe_string(lines, i - 1)
                            if probe and re.search(
                                rf"\b{re.escape(simple_name)}\s*\(", probe
                            ):
                                # 缓存结果
                                self._cache_function_def(file_path, simple_name, i)
                                return (file_path, i)
                    except (IOError, OSError):
                        continue
        return None

    def _extract_cpp_qualified_parts(self, resolved_function: str) -> Optional[Tuple[str, str]]:
        """
        从 C++ 符号中提取 (owner, method)。
        例：
        - _baidu_vi::CVString::CVString() -> (_baidu_vi::CVString, CVString)
        - Foo::bar(int) -> (Foo, bar)
        """
        s = (resolved_function or "").strip()
        if "::" not in s or "(" not in s:
            return None
        head = s.split("(", 1)[0].strip()
        if "::" not in head:
            return None
        parts = [p.strip() for p in head.split("::") if p.strip()]
        if len(parts) < 2:
            return None
        owner = "::".join(parts[:-1]).strip()
        method = parts[-1].strip()
        if not owner or not method:
            return None
        return owner, method

    def _split_cpp_params_top_level(self, params_text: str) -> List[str]:
        """按顶层逗号切分 C++ 参数列表（忽略模板/括号内逗号）。"""
        s = (params_text or "").strip()
        if not s:
            return []
        out: List[str] = []
        cur: List[str] = []
        angle = 0
        paren = 0
        bracket = 0
        for ch in s:
            if ch == "<":
                angle += 1
            elif ch == ">":
                angle = max(0, angle - 1)
            elif ch == "(":
                paren += 1
            elif ch == ")":
                paren = max(0, paren - 1)
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                bracket = max(0, bracket - 1)
            if ch == "," and angle == 0 and paren == 0 and bracket == 0:
                item = "".join(cur).strip()
                if item:
                    out.append(item)
                cur = []
                continue
            cur.append(ch)
        tail = "".join(cur).strip()
        if tail:
            out.append(tail)
        return out

    def _extract_cpp_param_hints(self, resolved_function: str) -> List[str]:
        """从 C++ 符号参数中提取关键类型 token（用于重载匹配打分）。"""
        s = (resolved_function or "").strip()
        if "(" not in s or ")" not in s:
            return []
        params_text = s[s.find("(") + 1 : s.rfind(")")]
        params = self._split_cpp_params_top_level(params_text)
        hints: List[str] = []
        for p in params:
            t = p.replace("std::__1::", "std::").replace("std::__ndk1::", "std::")
            t = re.sub(r"\b(const|volatile)\b", " ", t)
            t = re.sub(r"[&*]", " ", t)
            t = re.sub(r"\s+", " ", t).strip()
            # 保留最后一个作用域段 + 常见关键类型
            last = t.split("::")[-1].strip() if "::" in t else t
            if last:
                hints.append(last)
            for kw in ("vector", "float", "double", "int", "unsigned", "long", "shared_ptr"):
                if re.search(rf"\b{re.escape(kw)}\b", t):
                    hints.append(kw)
        # 去重保序
        seen = set()
        uniq: List[str] = []
        for h in hints:
            if h not in seen:
                seen.add(h)
                uniq.append(h)
        return uniq

    def _score_cpp_candidate_signature(
        self, line: str, owner: str, owner_tail: str, method: str, param_hints: List[str]
    ) -> int:
        """对 C++ 候选定义行打分：owner/method/参数越匹配分越高。"""
        txt = (line or "").strip()
        if not txt:
            return -1
        score = 0
        owner_macro_optional = owner
        # 宏命名空间可选：允许忽略前缀形如 _baidu_framework::
        if "::" in owner and owner.split("::", 1)[0].startswith("_"):
            owner_macro_optional = owner.split("::", 1)[1]

        if re.search(rf"\b{re.escape(owner)}\s*::\s*{re.escape(method)}\s*\(", txt):
            score += 120
        elif re.search(rf"\b{re.escape(owner_macro_optional)}\s*::\s*{re.escape(method)}\s*\(", txt):
            score += 100
        elif re.search(rf"\b{re.escape(owner_tail)}\s*::\s*{re.escape(method)}\s*\(", txt):
            score += 85
        else:
            return -1

        # 参数个数
        m = re.search(r"\((.*)\)", txt)
        cand_params = self._split_cpp_params_top_level(m.group(1)) if m else []
        if cand_params:
            score += min(20, len(cand_params) * 2)

        # 关键类型 token 命中，辅助重载判别
        low_txt = txt.lower().replace("std::__1::", "std::").replace("std::__ndk1::", "std::")
        for h in param_hints:
            if h and h.lower() in low_txt:
                score += 4
        return score

    def _find_cpp_qualified_definition_location(
        self, resolved_function: str, code_roots: List[str]
    ) -> Optional[Tuple[str, int]]:
        """
        按 C++ 限定名（owner::method）严格定位定义，避免退化到 simple_name 产生误命中。
        """
        parsed = self._extract_cpp_qualified_parts(resolved_function)
        if not parsed:
            return None
        owner, method = parsed
        owner_tail = owner.split("::")[-1].strip()
        cache_key = f"cppq:{owner}::{method}"
        if cache_key in self._function_def_cache:
            return self._function_def_cache[cache_key]

        param_hints = self._extract_cpp_param_hints(resolved_function)
        scanned = 0
        best: Optional[Tuple[int, str, int]] = None  # (score, file, line)

        impl_ext = {".cpp", ".cc", ".cxx", ".mm", ".m", ".c"}
        header_ext = {".h", ".hpp", ".hh", ".hxx", ".ipp", ".inl"}

        def _ordered_candidate_files(code_root: str) -> List[str]:
            ckey = (str(code_root), owner_tail)
            cached = self._cpp_candidate_files_cache.get(ckey)
            if cached is not None:
                return cached
            exact_impl: List[str] = []
            contain_impl: List[str] = []
            exact_header: List[str] = []
            contain_header: List[str] = []
            other_supported: List[str] = []
            owner_low = owner_tail.lower()
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if d not in self.exclude_dirs]
                for file in files:
                    file_path = os.path.join(root, file)
                    if not self._is_supported_file(file_path):
                        continue
                    ext = os.path.splitext(file)[1].lower()
                    stem = os.path.splitext(file)[0].lower()
                    is_exact = stem == owner_low
                    is_contain = owner_low in stem if owner_low else False
                    if ext in impl_ext:
                        if is_exact:
                            exact_impl.append(file_path)
                        elif is_contain:
                            contain_impl.append(file_path)
                        else:
                            other_supported.append(file_path)
                    elif ext in header_ext:
                        if is_exact:
                            exact_header.append(file_path)
                        elif is_contain:
                            contain_header.append(file_path)
                        else:
                            other_supported.append(file_path)
                    else:
                        other_supported.append(file_path)
            # 优先实现文件同名 -> 实现文件包含类名 -> 头文件同名 -> 头文件包含类名。
            # 若存在高相关候选，默认不再进入 other_supported 的长尾全仓扫描。
            ordered = exact_impl + contain_impl + exact_header + contain_header
            if not ordered:
                ordered = other_supported
            # 去重保序
            seen: set = set()
            uniq: List[str] = []
            for p in ordered:
                if p in seen:
                    continue
                seen.add(p)
                uniq.append(p)
            self._cpp_candidate_files_cache[ckey] = uniq
            return uniq

        for code_root in code_roots or []:
            for file_path in _ordered_candidate_files(code_root):
                    scanned += 1
                    if scanned % 50 == 0:
                        self._code_context_phase_check("find_cpp_qualified_definition_location")
                    if not self._is_supported_file(file_path) or not self._is_file_readable(file_path):
                        continue
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()
                        for i, line in enumerate(lines, 1):
                            probe = self._cpp_signature_probe_string(lines, i - 1)
                            if not probe:
                                continue
                            sc = self._score_cpp_candidate_signature(
                                probe, owner, owner_tail, method, param_hints
                            )
                            if sc < 0:
                                continue
                            if best is None or sc > best[0]:
                                best = (sc, file_path, i)
                                if sc >= 124:
                                    self._function_def_cache[cache_key] = (file_path, i)
                                    return (file_path, i)
                    except (IOError, OSError):
                        continue
        if best is not None:
            _sc, _fp, _ln = best
            self._function_def_cache[cache_key] = (_fp, _ln)
            return (_fp, _ln)
        return None
    
    def _is_function_call_line(self, line: str, function_name: str) -> bool:
        """检查行是否是函数调用（而不是函数定义）"""
        line = line.strip()
        
        # 排除函数定义行
        if self._is_function_definition_line(line):
            return False
        
        # 检查是否是函数调用
        # 模式1: object->function_name(...)
        if re.search(rf'\w+\s*->\s*{re.escape(function_name)}\s*\(', line):
            return True
        
        # 模式2: object.function_name(...)
        if re.search(rf'\w+\s*\.\s*{re.escape(function_name)}\s*\(', line):
            return True
        
        # 模式3: function_name(...) - 但不在函数定义中
        if re.search(rf'\b{re.escape(function_name)}\s*\(', line):
            # 若前面是类型名则视为定义；若前面是控制语句/关键字（如 return/if/while），仍视为调用。
            m = re.search(rf'\b(\w+)\s+{re.escape(function_name)}\s*\(', line)
            if m:
                keyword_like = {
                    "return", "if", "while", "for", "switch", "case",
                    "sizeof", "catch", "else"
                }
                if m.group(1) not in keyword_like:
                    # 形如 \"int foo(\" / \"MyType foo(\"，视为定义
                    return False
            return True
        
        return False

    # 与 std::vector / std::deque 等成员同名，全仓正则扫描会产生海量误报；TS 未命中时仅在「业务 bmsdk 目录」回退
    _CALL_CHAIN_COLLISION_METHOD_NAMES = frozenset(
        {
            "emplace_back",
            "push_back",
            "pop_back",
            "emplace_front",
            "push_front",
            "pop_front",
            "emplace",
            "push",
            "pop",
            "begin",
            "end",
            "size",
            "empty",
            "clear",
            "at",
            "front",
            "back",
        }
    )
    # 业务中高频且语义宽泛的方法名，易在全仓静态扫描中发生“同名误匹配”
    # （例如 m_render->UnInit() 被误连到 WalkMapControl::UnInit）。
    _GENERIC_METHOD_NAMES_FOR_CALL_CHAIN = frozenset(
        {
            "init",
            "uninit",
            "oninit",
            "onuninit",
            "create",
            "destroy",
            "release",
            "reset",
            "start",
            "stop",
            "execute",
            "update",
        }
    )

    def _merge_call_chain_function_lists(
        self, first: List[CallChainFunction], second: List[CallChainFunction]
    ) -> List[CallChainFunction]:
        """合并调用链结果，按 (name, file, snippet 首行) 去重，保留顺序。"""
        seen: set = set()
        out: List[CallChainFunction] = []
        for c in first + second:
            sig0 = (c.snippet[0].strip() if c.snippet else "")
            k = (c.name, c.file, sig0)
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
        return out

    def _regex_scan_callers_in_paths(
        self,
        simple_function_name: str,
        code_roots: List[str],
        restrict_to_files: Optional[List[str]],
        exclude_files: Optional[set],
        seen_caller_keys: set,
    ) -> List[CallChainFunction]:
        """正则扫描：在 restrict 文件集或全仓（可 exclude 栈文件）中查找调用崩溃函数的函数。

        优化：支持并行扫描
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        out: List[CallChainFunction] = []

        # 收集所有待扫描的文件
        all_files = []
        rxi = 0
        for file_path in self._iter_ts_files_for_call_chain(
            code_roots, restrict_to_files, exclude_files
        ):
            rxi += 1
            if rxi % 300 == 0:
                self._code_context_phase_check("regex_collect_file_list")
            cr = self._pick_code_root_for_file(file_path, code_roots)
            if self._should_skip_file(file_path, cr):
                continue
            all_files.append((file_path, cr))

        total_files = len(all_files)
        if total_files == 0:
            return out

        # 确定并行 worker 数量
        max_workers = self.max_workers
        if max_workers is None:
            import multiprocessing
            max_workers = min(multiprocessing.cpu_count(), 8)

        # 判断是否启用并行（文件数 >= 阈值且非禁用）
        use_parallel = total_files >= self.parallel_threshold and max_workers > 0

        if use_parallel:
            logger.info(f"正则扫描启用并行: {max_workers} workers, 扫描 {total_files} 个文件")
            return self._parallel_regex_scan(
                all_files, simple_function_name, code_roots, seen_caller_keys, max_workers
            )

        # 串行扫描
        for file_path, cr in all_files:
            self._code_context_phase_check("regex_serial_file")
            if restrict_to_files is not None:
                self.search_stats["files_scanned"] += 1
            try:
                self.search_stats["files_read"] += 1
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                lines = content.split("\n")
                for i, line in enumerate(lines, 1):
                    if self._contains_function_call(line, simple_function_name):
                        if self._is_function_call_line(line, simple_function_name):
                            caller_function = self._extract_function_name_at_line(lines, i)
                            if caller_function and caller_function != simple_function_name:
                                caller_code = self._extract_full_function_code_for_caller(
                                    lines, i - 1, caller_function
                                )
                                if caller_code:
                                    if not re.search(
                                        rf"\b{re.escape(simple_function_name)}\s*\(", caller_code
                                    ):
                                        continue
                                    ck = (caller_function, file_path)
                                    if ck in seen_caller_keys:
                                        continue
                                    seen_caller_keys.add(ck)
                                    caller_snippet = [
                                        ln.rstrip() for ln in caller_code.split("\n") if ln.strip()
                                    ]
                                    out.append(
                                        CallChainFunction(
                                            name=caller_function,
                                            file=file_path,
                                            snippet=caller_snippet,
                                        )
                                    )
                                    logger.info(
                                        f"找到直接调用崩溃函数的上层函数: {caller_function} 在 {file_path}:{i}"
                                    )
            except _CodeContextPhaseTimeout:
                raise
            except Exception as e:
                logger.debug(f"分析文件失败 {file_path}: {e}")
                continue
        return out

    def _parallel_regex_scan(
        self,
        file_infos: List[Tuple[str, str]],
        simple_function_name: str,
        code_roots: List[str],
        seen_caller_keys: set,
        max_workers: int = 4,
    ) -> List[CallChainFunction]:
        """并行正则扫描多个文件

        优化策略：分两阶段
        1. 第一阶段：快速扫描，只找 caller 函数名和行号（不提取完整函数体）
        2. 第二阶段：串行提取函数体（这部分慢，但需要访问文件）
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # 第一阶段：并行快速扫描，找出调用点
        caller_locations: List[Tuple[str, int, str]] = []  # [(file_path, line_number, caller_function), ...]

        def _quick_scan(file_info: Tuple[str, str]) -> List[Tuple[str, int, str]]:
            """快速扫描：只找调用点，不提取完整函数体"""
            file_path, cr = file_info
            local_results = []
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                lines = content.split("\n")
                for i, line in enumerate(lines, 1):
                    if self._contains_function_call(line, simple_function_name):
                        if self._is_function_call_line(line, simple_function_name):
                            caller_function = self._extract_function_name_at_line(lines, i)
                            if caller_function and caller_function != simple_function_name:
                                local_results.append((file_path, i, caller_function))
            except Exception:
                pass
            return local_results

        scanned_count = 0
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {executor.submit(_quick_scan, info): info for info in file_infos}
            for future in as_completed(futures):
                self._code_context_phase_check("parallel_regex_quick")
                scanned_count += 1
                if scanned_count % 500 == 0:
                    logger.info(f"正则并行扫描(快速阶段): {scanned_count}/{len(file_infos)}")
                try:
                    locations = future.result()
                    caller_locations.extend(locations)
                except Exception:
                    pass
        except _CodeContextPhaseTimeout:
            logger.warning("正则并行扫描(快速阶段)因整阶段超时中止")
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        logger.info(f"快速扫描完成: 找到 {len(caller_locations)} 个调用点")

        # 第二阶段：串行提取函数体（逐个提取，控制并发）
        results: List[CallChainFunction] = []
        for idx, (file_path, call_line, caller_function) in enumerate(caller_locations):
            self._code_context_phase_check("parallel_regex_extract")
            if idx % 10 == 0:
                logger.info(f"提取函数体进度: {idx}/{len(caller_locations)}")
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                lines = content.split("\n")
                caller_code = self._extract_full_function_code_for_caller(
                    lines, call_line - 1, caller_function
                )
                if caller_code:
                    if not re.search(rf"\b{re.escape(simple_function_name)}\s*\(", caller_code):
                        continue
                    ck = (caller_function, file_path)
                    if ck in seen_caller_keys:
                        continue
                    seen_caller_keys.add(ck)
                    caller_snippet = [
                        ln.rstrip() for ln in caller_code.split("\n") if ln.strip()
                    ]
                    results.append(
                        CallChainFunction(
                            name=caller_function,
                            file=file_path,
                            snippet=caller_snippet,
                        )
                    )
            except _CodeContextPhaseTimeout:
                raise
            except Exception:
                pass

        self.search_stats["files_scanned"] = len(file_infos)
        self.search_stats["files_read"] = len(file_infos)
        logger.info(f"正则并行扫描完成: 扫描 {len(file_infos)} 个文件, 找到 {len(results)} 个调用者")
        return results

    def _find_call_chain_functions(
        self,
        crash_function_name: str,
        code_roots: List[str],
        stack_priority_files: Optional[List[str]] = None,
    ) -> List[CallChainFunction]:
        """查找直接调用崩溃函数的上层函数。先扫堆栈帧对应源码，再扫全仓（去重合并，仍受 max_direct_callers 截断）。

        优化点：
        1. 调用链缓存：跨分析复用，减少重复扫描
        2. 并行扫描：大目录时自动启用多线程
        3. 超时控制：防止单次扫描过长
        """
        import time
        import signal
        start_time = time.time()
        logger.info(f"查找直接调用崩溃函数的上层函数: {crash_function_name}")
        call_chain_functions: List[CallChainFunction] = []

        # ========== 缓存检查（优化3：建立代码索引缓存）==========
        if code_roots:
            # 使用最短的 code_root 作为缓存键的一部分
            primary_code_root = min(code_roots, key=len)
            cache_key = (primary_code_root, crash_function_name)
            if cache_key in self._call_chain_cache:
                cached_result = self._call_chain_cache[cache_key]
                logger.info(f"命中调用链缓存: {crash_function_name}, 返回 {len(cached_result)} 个结果")
                return cached_result

        self.search_stats["files_scanned"] = 0
        self.search_stats["files_read"] = 0

        simple_function_name = self._extract_function_name_from_resolved(crash_function_name)
        if not simple_function_name:
            simple_function_name = self._extract_simple_function_name(crash_function_name)
        demangled = self._extract_simple_name_from_mangled(simple_function_name)
        if demangled:
            simple_function_name = demangled
        logger.info(f"简化函数名: {simple_function_name}")

        stack_norm: List[str] = []
        if stack_priority_files:
            for raw in stack_priority_files:
                if not raw:
                    continue
                try:
                    ap = os.path.abspath(raw)
                except Exception:
                    ap = raw
                if os.path.isfile(ap) and self._is_supported_file(ap):
                    stack_norm.append(ap)
        stack_set = set(stack_norm)
        nearby_module_files: List[str] = []
        is_generic_method = simple_function_name.lower() in self._GENERIC_METHOD_NAMES_FOR_CALL_CHAIN
        # 通用名（如 UnInit）在大仓库中同名噪音高：邻近补扫限制 Top-N，兼顾召回与耗时
        generic_nearby_top_n = 300
        if stack_norm:
            nearby_module_files = self._collect_nearby_module_files(stack_norm, code_roots)
            nearby_scan_files = (
                nearby_module_files[:generic_nearby_top_n]
                if is_generic_method
                else nearby_module_files
            )
            logger.info(
                f"静态调用链：堆栈优先 {len(stack_norm)} 个源文件，随后邻近模块补扫 "
                f"{len(nearby_scan_files)} 个文件（去重合并）"
            )
        else:
            nearby_scan_files = nearby_module_files

        # 根据 code_parser_backend 选择扫描方式
        use_tree_sitter = self.code_parser_backend == "tree_sitter" and self._ts_parser is not None
        use_regex = self.code_parser_backend == "regex" or not use_tree_sitter

        logger.info(f"调用链搜索模式: {'tree-sitter' if use_tree_sitter else 'regex'} (backend={self.code_parser_backend})")

        if use_tree_sitter:
            if stack_norm:
                p1 = self._find_call_chain_functions_tree_sitter(
                    crash_function_name, code_roots, restrict_to_files=stack_norm, exclude_files=None
                )
                p2 = []
                if nearby_scan_files:
                    if is_generic_method and p1:
                        logger.info(
                            "tree-sitter 调用链搜索：通用方法名在堆栈优先已命中，"
                            "继续执行有限 Top-N 邻近补扫（N=%d）",
                            generic_nearby_top_n,
                        )
                    p2 = self._find_call_chain_functions_tree_sitter(
                        crash_function_name,
                        code_roots,
                        restrict_to_files=nearby_scan_files,
                        exclude_files=stack_set,
                    )
                call_chain_functions = self._merge_call_chain_function_lists(p1, p2)
            else:
                call_chain_functions = self._find_call_chain_functions_tree_sitter(
                    crash_function_name, code_roots, restrict_to_files=None, exclude_files=None
                )
            ts_elapsed = time.time() - start_time
            logger.info(
                f"tree-sitter 调用链搜索完成: 扫描 {self.search_stats['files_scanned']} 个文件, "
                f"读取 {self.search_stats['files_read']} 个文件, 找到 {len(call_chain_functions)} 个调用者函数, "
                f"耗时 {ts_elapsed:.2f}秒"
            )
            if call_chain_functions:
                self.search_stats["search_time"] += ts_elapsed
                # 缓存结果
                if code_roots:
                    self._call_chain_cache[cache_key] = call_chain_functions
                    logger.info(f"已缓存调用链结果: {crash_function_name}")
                return call_chain_functions
            if simple_function_name in self._CALL_CHAIN_COLLISION_METHOD_NAMES:
                logger.info(
                    "tree-sitter 未命中且目标为 vector/deque 等常见成员名，跳过全仓正则（避免与 std:: 容器混淆）；"
                    "请依赖 graph.call_chain_from_add2line 与基于栈序补全的 calls_direct 边"
                )
                self.search_stats["search_time"] += ts_elapsed
                # 缓存空结果
                if code_roots:
                    self._call_chain_cache[cache_key] = []
                    logger.info(f"已缓存空调用链结果: {crash_function_name}")
                return []
            logger.info(
                "tree-sitter 未命中直接调用者，回退到正则扫描（覆盖部分成员调用/宏等 TS 未识别场景）"
            )

        # 如果 backend 是 regex，直接跳到正则扫描
        if use_regex:
            logger.info("跳过 tree-sitter，使用正则扫描")

        call_chain_functions = []

        template_info = self._parse_template_container_function(crash_function_name)
        generic_methods = {
            "RemoveAt",
            "RemoveHead",
            "RemoveTail",
            "RemoveAll",
            "AddHead",
            "AddTail",
            "InsertAfter",
            "InsertBefore",
        }
        if template_info and template_info.get("method") in generic_methods:
            call_chain_functions = self._find_call_chain_functions_for_template_container(
                crash_function_name, code_roots
            )
            elapsed_time = time.time() - start_time
            self.search_stats["search_time"] += elapsed_time
            logger.info(
                f"模板容器定向调用链搜索完成: 扫描 {self.search_stats['files_scanned']} 个文件, "
                f"读取 {self.search_stats['files_read']} 个文件, 找到 {len(call_chain_functions)} 个调用者函数, "
                f"耗时 {elapsed_time:.2f}秒"
            )
            return call_chain_functions

        seen_caller_keys: set = set()
        if stack_norm:
            call_chain_functions.extend(
                self._regex_scan_callers_in_paths(
                    simple_function_name,
                    code_roots,
                    restrict_to_files=stack_norm,
                    exclude_files=None,
                    seen_caller_keys=seen_caller_keys,
                )
            )
        if stack_norm:
            if nearby_scan_files:
                if is_generic_method and call_chain_functions:
                    logger.info(
                        "正则调用链搜索：通用方法名在堆栈优先已命中，"
                        "继续执行有限 Top-N 邻近补扫（N=%d）",
                        generic_nearby_top_n,
                    )
                call_chain_functions.extend(
                    self._regex_scan_callers_in_paths(
                        simple_function_name,
                        code_roots,
                        restrict_to_files=nearby_scan_files,
                        exclude_files=stack_set,
                        seen_caller_keys=seen_caller_keys,
                    )
                )
        else:
            call_chain_functions.extend(
                self._regex_scan_callers_in_paths(
                    simple_function_name,
                    code_roots,
                    restrict_to_files=None,
                    exclude_files=None,
                    seen_caller_keys=seen_caller_keys,
                )
            )
        

        elapsed_time = time.time() - start_time
        self.search_stats["search_time"] += elapsed_time
        logger.info(
            f"调用链搜索完成: 扫描 {self.search_stats['files_scanned']} 个文件, "
            f"读取 {self.search_stats['files_read']} 个文件, "
            f"找到 {len(call_chain_functions)} 个调用者函数, "
            f"耗时 {elapsed_time:.2f}秒"
        )
        logger.info(
            f"搜索统计: 跳过(大小)={self.search_stats['files_skipped_size']}, "
            f"跳过(排除)={self.search_stats['files_skipped_excluded']}, "
            f"跳过(扩展名)={self.search_stats['files_skipped_extension']}"
        )

        # ========== 缓存结果（优化3：建立代码索引缓存）==========
        if code_roots and cache_key:
            self._call_chain_cache[cache_key] = call_chain_functions
            logger.info(f"已缓存调用链结果: {crash_function_name}")

        return call_chain_functions

    def _line_suggests_implicit_ctor_use(self, line: str, type_name: str) -> bool:
        """
        判断一行是否可能触发「隐式默认构造」相关语义（非显式 Type(...) call_expression 也能命中）。
        排除：类型定义行、前向声明、纯引用/指针形参、typedef 等。
        """
        t = (type_name or "").strip()
        if len(t) < 2:
            return False
        s = line.strip()
        if not s or s.startswith("//"):
            return False
        if "/*" in s[:3]:
            return False
        if not re.search(rf"\b{re.escape(t)}\b", line):
            return False
        # 纯引用/指针形参：不视为在本行默认构造局部对象
        if re.search(rf"(?:const\s+)?{re.escape(t)}\s*[\*&]\s*\w+", line):
            return False
        if re.match(rf"^\s*(?:template\s*<[^>]+>\s*)?(?:struct|class)\s+{re.escape(t)}\b", s):
            return False
        if re.match(rf"^\s*(?:struct|class)\s+{re.escape(t)}\s*;\s*$", s):
            return False
        if re.match(rf"^\s*using\s+{re.escape(t)}\s*=", s):
            return False
        if re.match(rf"^\s*typedef\b", s):
            return False
        # 变量 / 成员声明：Type name ; 或 = { 或 (
        if re.search(
            rf"(?:\bconst\s+)?(?:\bvolatile\s+)?{re.escape(t)}\s+\w+\s*[=;{{\[\(]",
            line,
        ):
            return True
        if re.search(rf"\breturn\s+{re.escape(t)}\b", line):
            return True
        if re.search(rf"\b{re.escape(t)}\s*\(\s*\)", line):
            return True
        if re.search(rf"\b{re.escape(t)}\s*{{", line):
            return True
        if re.search(rf"\bnew\s+{re.escape(t)}\b", line):
            return True
        if re.search(rf"<\s*{re.escape(t)}\s*>", line) and re.search(
            r"make_unique|make_shared|emplace|emplace_back|emplace_front|try_emplace|"
            r"\bvector\b|\bdeque\b|\blist\b|\barray\b|\boptional\b",
            line,
        ):
            return True
        if re.search(
            rf"(?:static_cast|reinterpret_cast|dynamic_cast)\s*<\s*{re.escape(t)}\s*>",
            line,
        ):
            return True
        return False

    def _find_implicit_ctor_usage_callers(
        self,
        crash_function_name: str,
        code_roots: List[str],
        max_hits: int = 400,
    ) -> List[CallChainFunction]:
        """
        对「类名::类名()」隐式默认构造，在工程内扫描类型名使用点，归属到 enclosing 函数，作为调用链补充。
        """
        ctor_cls = self._ctor_or_dtor_class_name_from_resolved(crash_function_name or "")
        if not ctor_cls:
            return []
        t = ctor_cls
        results: List[CallChainFunction] = []
        seen: set = set()
        hits = 0

        for code_root in code_roots or []:
            if hits >= max_hits:
                break
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]
                for file in files:
                    if hits >= max_hits:
                        break
                    file_path = os.path.join(root, file)
                    self.search_stats["files_scanned"] += 1
                    if self._should_skip_file(file_path, code_root):
                        continue
                    try:
                        self.search_stats["files_read"] += 1
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                    except (OSError, IOError, UnicodeDecodeError):
                        continue
                    if t not in content:
                        continue
                    lines = content.split("\n")
                    for i, line in enumerate(lines, 1):
                        if hits >= max_hits:
                            break
                        if not self._line_suggests_implicit_ctor_use(line, t):
                            continue
                        caller = self._extract_function_name_at_line(lines, i)
                        if caller == t:
                            continue
                        caller_code: Optional[str] = None
                        origin_tag = "implicit_ctor_usage"
                        if caller:
                            caller_code = self._extract_full_function_code_for_caller(
                                lines, i - 1, caller
                            )
                        if not caller_code:
                            # 成员子对象：字段声明不在函数内，归属外层 struct/class（如 RenderPiplineDescriptor 内含 T 成员）
                            owner = self._enclosing_struct_name_for_field_line(lines, i)
                            if not owner or owner == t:
                                continue
                            scope = self._try_struct_or_class_scope_snippet(lines, i, owner)
                            if not scope:
                                continue
                            snip_lines, _, _ = scope
                            caller = owner
                            caller_code = "\n".join(snip_lines)
                            origin_tag = "implicit_ctor_member_subobject"
                        if not caller_code or not re.search(rf"\b{re.escape(t)}\b", caller_code):
                            continue
                        key = (caller, file_path)
                        if key in seen:
                            continue
                        snippet = [ln.rstrip() for ln in caller_code.split("\n") if ln.strip()]
                        if not snippet:
                            continue
                        if self.max_code_length > 0:
                            snippet = self._truncate_snippet(snippet)
                        seen.add(key)
                        results.append(
                            CallChainFunction(
                                name=caller,
                                file=file_path,
                                snippet=snippet,
                                chain_origin=origin_tag,
                            )
                        )
                        hits += 1
                        logger.info(
                            f"隐式构造使用点: {t} 在 {file_path}:{i} → 归属 {caller} ({origin_tag})"
                        )
        logger.info(
            f"隐式默认构造调用链补充: 类型 {t}，命中 {len(results)} 个 enclosing 函数（上限 {max_hits}）"
        )
        return results

    def _first_struct_or_class_decl_line(self, snippet: Optional[List[str]]) -> str:
        """跳过 template 行，取首条 struct/class 声明行（用于解析真实类型名）。"""
        for ln in snippet or []:
            s = (ln or "").strip()
            if s.startswith("template") and "<" in s:
                continue
            if re.match(r"^\s*(?:struct|class)\s+", s):
                return s
        return (snippet[0].strip() if snippet else "") or ""

    def _extract_class_or_struct_name_from_decl_line(self, line: str) -> Optional[str]:
        """
        从 struct/class 声明行提取真实类型名。
        跳过 class BAIDU_VI_EXPORT Foo 中的导出宏，避免误用 BAIDU_VI_EXPORT 当作类型名。
        """
        if not (line or "").strip():
            return None
        m = re.match(
            r"^\s*(?:template\s*<[^>]+>\s*)?(?:struct|class)\s+(.+)",
            line,
        )
        if not m:
            return None
        rest = m.group(1).strip()
        rest = rest.split("{")[0].strip()
        rest = rest.split(":")[0].strip()
        if not rest:
            return None
        for tok in re.split(r"\s+", rest):
            if not tok or tok in ("{", "};", "final"):
                break
            # 全大写标识符（含下划线）视为导出/平台宏，如 BAIDU_VI_EXPORT
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", tok):
                continue
            if tok.endswith("_EXPORT") or tok.upper() in ("API", "DLL"):
                continue
            if re.match(r"^\w+$", tok):
                return tok
        return None

    def _symbol_for_upstream_call_chain_search(self, cf: CallChainFunction) -> str:
        """从 CallChainFunction 构造用于「再向上一层找调用者」的符号串。"""
        if getattr(cf, "chain_origin", None) == "implicit_ctor_member_subobject":
            return f"{cf.name}::{cf.name}()"
        sig0 = self._first_struct_or_class_decl_line(cf.snippet)
        cn = self._extract_class_or_struct_name_from_decl_line(sig0)
        if cn:
            return f"{cn}::{cn}()"
        sig0 = (sig0 or (cf.snippet[0] if cf.snippet else "") or "").strip()
        if "(" in sig0:
            head = sig0.split("{")[0].strip()
            if head:
                return head
        return f"{cf.name}()" if "(" not in cf.name else cf.name

    def _upstream_candidate_trust_tier(self, file_path: str) -> int:
        """0=生产/业务源码优先，1=demo/examples，2=单测与第三方测试框架（静态搜索易误命中）。"""
        fp = (file_path or "").lower().replace("\\", "/")
        if not fp:
            return 1
        if (
            "_unittest." in fp
            or "/unittest/" in fp
            or "_test.cc" in fp
            or "_test.cpp" in fp
            or "_tests.cpp" in fp
            or "/googletest/" in fp
            or "/googlemock/" in fp
            or "gtest/" in fp
            or "gtest_" in fp
            or "gmock" in fp
        ):
            return 2
        if "/demo/" in fp or "/examples/" in fp or "renderdemo" in fp or "/sample/" in fp:
            return 1
        return 0

    def _common_path_prefix_components(self, a: str, b: str) -> int:
        """两段路径从根起连续相同的目录分量数（用于优先选与下游同模块的调用者）。"""
        if not a or not b:
            return 0
        pa = [p for p in a.replace("\\", "/").split("/") if p]
        pb = [p for p in b.replace("\\", "/").split("/") if p]
        n = 0
        for x, y in zip(pa, pb):
            if x.lower() != y.lower():
                break
            n += 1
        return n

    def _upstream_candidate_sort_key(
        self, cf: CallChainFunction, prefer_near_file: Optional[str]
    ) -> Tuple[int, int, str, str]:
        tier = self._upstream_candidate_trust_tier(cf.file or "")
        common = (
            self._common_path_prefix_components(cf.file or "", prefer_near_file or "")
            if prefer_near_file
            else 0
        )
        # tier 升序；同 tier 下 common 降序 → 用 -common
        return (tier, -common, cf.file or "", cf.name or "")

    def _merge_upstream_call_layer(
        self,
        symbol: str,
        code_roots: List[str],
        prefer_near_file: Optional[str] = None,
    ) -> List[CallChainFunction]:
        """合并 call_expression 与隐式构造一层，去重；按与下游文件路径相近度与可信度排序。"""
        if not (symbol or "").strip():
            return []
        try:
            a = self._find_call_chain_functions(symbol, code_roots)
        except Exception:
            a = []
        try:
            b = self._find_implicit_ctor_usage_callers(symbol, code_roots, max_hits=80)
        except Exception:
            b = []
        seen: set = set()
        out: List[CallChainFunction] = []
        for x in a + b:
            k = (x.name, x.file)
            if k in seen:
                continue
            seen.add(k)
            out.append(x)
        out.sort(key=lambda z: self._upstream_candidate_sort_key(z, prefer_near_file))
        return out

    def _expand_static_call_chain_prepend(
        self,
        seed: CallChainFunction,
        code_roots: List[str],
        max_nodes_including_crash: int,
        layer_cache: Dict[Tuple[str, str], List[CallChainFunction]],
    ) -> List[CallChainFunction]:
        """
        从直接调用者 seed 向上扩展，得到 [最外层, ..., seed]（不含崩溃函数）。
        含崩溃函数在内的路径长度 ≤ max_nodes_including_crash。
        """
        chain: List[CallChainFunction] = [seed]
        # 记录当前链上已有节点，避免在向上扩展时形成 A->B->A 这类伪环。
        # key 采用 (name, file) 与图节点去重维度保持一致。
        chain_seen: set[Tuple[str, str]] = {(seed.name or "", seed.file or "")}
        max_ancestors = max_nodes_including_crash - 1
        while len(chain) < max_ancestors:
            outer = chain[0]
            sym = self._symbol_for_upstream_call_chain_search(outer)
            if not sym or not sym.strip():
                break
            cache_key = (sym, outer.file or "")
            if cache_key in layer_cache:
                layer = layer_cache[cache_key]
            else:
                layer = self._merge_upstream_call_layer(
                    sym, code_roots, prefer_near_file=outer.file
                )
                layer_cache[cache_key] = layer
            if not layer:
                break
            # 选第一个“不会引入环”且“调用证据可信”的父函数候选，避免同名误匹配。
            parent: Optional[CallChainFunction] = None
            for cand in layer:
                ckey = (cand.name or "", cand.file or "")
                if ckey in chain_seen:
                    continue
                if not self._is_confident_parent_caller(cand, outer):
                    continue
                parent = cand
                break
            if parent is None:
                break
            if parent.name == outer.name and parent.file == outer.file:
                break
            chain.insert(0, parent)
            chain_seen.add((parent.name or "", parent.file or ""))
            if len(chain) >= max_ancestors:
                break
        return chain

    def _is_confident_parent_caller(
        self, parent: CallChainFunction, callee: CallChainFunction
    ) -> bool:
        """
        判断 parent 是否“可信地”调用了 callee。

        对普通方法名：只要在 parent 片段内出现调用 token 即可；
        对高碰撞方法名（Init/UnInit/Destroy...）：要求更强证据，避免同名误连边。
        """
        ps = parent.snippet or []
        if not ps:
            return True
        callee_owner, method = self._owner_and_method_from_callchain_func(callee)
        if not method:
            return True

        owner = callee_owner or ""

        call_pat = re.compile(rf"(?:->|\.|\b)\s*{re.escape(method)}\s*\(")
        has_any_call = any(call_pat.search((ln or "").strip()) for ln in ps)
        if not has_any_call:
            return False

        # 非高碰撞方法名：到这里即可视为可信
        if method.lower() not in self._GENERIC_METHOD_NAMES_FOR_CALL_CHAIN:
            return True

        # 高碰撞方法名：要求更强证据
        explicit_qualified_pat = (
            re.compile(rf"\b{re.escape(owner)}\s*::\s*{re.escape(method)}\s*\(")
            if owner
            else None
        )
        if explicit_qualified_pat and any(explicit_qualified_pat.search((ln or "")) for ln in ps):
            return True

        if not owner:
            return False

        # 先走“接收者类型约束”：obj->method()/obj.method() 中 obj 的类型需与 callee owner 兼容（含基类）。
        # 这是避免同名方法误匹配的核心校验。
        recv_vars = self._extract_receiver_vars_for_method(ps, method)
        if recv_vars:
            local_types = self._collect_local_var_type_hints(ps)
            parent_owner, _ = self._owner_and_method_from_callchain_func(parent)
            member_types, inherit_map = self._collect_member_types_for_parent(parent.file, parent_owner)
            typed_seen = False
            for rv in recv_vars:
                tset = set(local_types.get(rv, set())) | set(member_types.get(rv, set()))
                if not tset:
                    continue
                typed_seen = True
                if any(self._types_compatible_with_owner(owner, t, inherit_map) for t in tset):
                    return True
            # 若接收者类型已识别但都不兼容，直接判为不可信，避免回落到宽松规则。
            if typed_seen:
                return False

        # 允许“变量调用 + 近邻声明里出现 owner 类型名”的场景（如 shared_ptr<WalkMapControl> walkMap; walkMap->UnInit();）
        var_call_pat = re.compile(rf"\b([A-Za-z_]\w*)\s*(?:->|\.)\s*{re.escape(method)}\s*\(")
        owner_pat = re.compile(rf"\b{re.escape(owner)}\b")
        decl_pat_tpl = r"\b{owner}\b[^;\n]*\b{var}\b"

        for i, raw in enumerate(ps):
            line = (raw or "").strip()
            m = var_call_pat.search(line)
            if not m:
                continue
            var_name = m.group(1)
            # 先看当前行是否已有明确类型信息
            if owner_pat.search(line) and re.search(
                decl_pat_tpl.format(owner=re.escape(owner), var=re.escape(var_name)), line
            ):
                return True
            # 再向上看若干行声明
            lo = max(0, i - 8)
            ctx = "\n".join((x or "") for x in ps[lo : i + 1])
            if re.search(decl_pat_tpl.format(owner=re.escape(owner), var=re.escape(var_name)), ctx):
                return True

        return False

    def _owner_and_method_from_callchain_func(self, cf: CallChainFunction) -> Tuple[str, str]:
        """从 CallChainFunction 提取 (owner, method)。"""
        name = (cf.name or "").strip()
        head = name.split("(", 1)[0].strip() if name else ""
        if "::" in head:
            parts = [p.strip() for p in head.split("::") if p.strip()]
            if len(parts) >= 2:
                return parts[-2], parts[-1]
        if head:
            return "", head.split()[-1]
        # 回退：尝试从 snippet 首行提取
        sig0 = (cf.snippet[0] if cf.snippet else "").strip()
        if sig0:
            sig_head = sig0.split("(", 1)[0].strip()
            if "::" in sig_head:
                parts = [p.strip() for p in sig_head.split("::") if p.strip()]
                if len(parts) >= 2:
                    return parts[-2], parts[-1]
            toks = sig_head.split()
            if toks:
                return "", toks[-1]
        return "", ""

    def _extract_receiver_vars_for_method(self, lines: List[str], method: str) -> List[str]:
        """提取片段内对 method 的接收者变量名（obj->method / obj.method）。"""
        out: List[str] = []
        seen: set = set()
        pat = re.compile(rf"\b([A-Za-z_]\w*)\s*(?:->|\.)\s*{re.escape(method)}\s*\(")
        for raw in lines or []:
            line = (raw or "").strip()
            if not line:
                continue
            for m in pat.finditer(line):
                v = m.group(1)
                if v not in seen:
                    seen.add(v)
                    out.append(v)
        return out

    def _collect_local_var_type_hints(self, lines: List[str]) -> Dict[str, set]:
        """
        从函数片段提取局部变量类型提示：
        - std::shared_ptr<T> v / unique_ptr<T> v
        - T* v / T& v / T v
        """
        m: Dict[str, set] = {}

        def _add(var_name: str, type_name: str) -> None:
            v = (var_name or "").strip()
            t = self._normalize_type_name(type_name)
            if not v or not t:
                return
            m.setdefault(v, set()).add(t)

        sp_pat = re.compile(
            r"\b(?:std::)?(?:shared_ptr|unique_ptr|weak_ptr)\s*<\s*([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*>\s*([A-Za-z_]\w*)\b"
        )
        ptr_pat = re.compile(
            r"\b([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*[*&]\s*([A-Za-z_]\w*)\b"
        )
        obj_pat = re.compile(
            r"\b([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s+([A-Za-z_]\w*)\b"
        )
        skip_words = {
            "if", "for", "while", "switch", "return", "case", "default",
            "const", "static", "inline", "virtual", "class", "struct",
        }

        for raw in lines or []:
            line = (raw or "").strip()
            if not line or line.startswith("//") or line.startswith("/*"):
                continue
            if line.startswith(("if ", "if(", "for ", "for(", "while ", "while(", "switch ", "switch(")):
                continue
            if "(" in line and ")" in line and line.endswith("{"):
                # 函数定义行
                continue
            for mm in sp_pat.finditer(line):
                _add(mm.group(2), mm.group(1))
            for mm in ptr_pat.finditer(line):
                t0 = mm.group(1)
                v0 = mm.group(2)
                if t0 in skip_words:
                    continue
                _add(v0, t0)
            for mm in obj_pat.finditer(line):
                t0 = mm.group(1)
                v0 = mm.group(2)
                if t0 in skip_words:
                    continue
                # 避免把 "return foo" 这类误当声明
                if line.startswith("return "):
                    continue
                _add(v0, t0)
        return m

    def _normalize_type_name(self, raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return ""
        s = re.sub(r"\bconst\b", "", s)
        s = s.replace("&", "").replace("*", "").strip()
        s = re.sub(r"<.*?>", "", s).strip()
        if "::" in s:
            s = s.split("::")[-1].strip()
        return s

    def _collect_member_types_for_parent(
        self, parent_file: Optional[str], parent_owner: str
    ) -> Tuple[Dict[str, set], Dict[str, set]]:
        """
        返回：
        - parent_owner 的成员变量类型映射：member -> {Type...}
        - 当前可见类继承图：class -> {base...}
        """
        if not parent_file or not parent_owner:
            return {}, {}
        infos = self._collect_class_info_for_source(parent_file)
        cinfo = infos.get(parent_owner) or {}
        members = cinfo.get("members") or {}
        inherit_map = {k: set((v.get("bases") or set())) for k, v in infos.items()}
        return members, inherit_map

    def _collect_class_info_for_source(self, source_file: str) -> Dict[str, Dict[str, Any]]:
        src = os.path.abspath(source_file)
        if src in self._class_info_by_source_cache:
            return self._class_info_by_source_cache[src]
        infos: Dict[str, Dict[str, Any]] = {}
        paths = self._candidate_class_info_files(src)
        for p in paths:
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
            except Exception:
                continue
            self._merge_class_infos(infos, self._parse_class_infos_from_text(txt))
        self._class_info_by_source_cache[src] = infos
        return infos

    def _candidate_class_info_files(self, source_file: str) -> List[str]:
        out: List[str] = []
        seen: set = set()

        def _add(p: str) -> None:
            if not p:
                return
            ap = os.path.abspath(p)
            if ap in seen:
                return
            if os.path.isfile(ap):
                seen.add(ap)
                out.append(ap)

        _add(source_file)
        d = os.path.dirname(source_file)
        stem = os.path.splitext(os.path.basename(source_file))[0]
        for ext in (".h", ".hpp", ".hh", ".hxx"):
            _add(os.path.join(d, stem + ext))

        # 读取本源文件的本地 include，补充可见类定义
        try:
            with open(source_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = re.search(r'^\s*#\s*include\s*"([^"]+)"', line)
                    if not m:
                        continue
                    _add(os.path.join(d, m.group(1)))
        except Exception:
            pass
        return out

    def _merge_class_infos(self, base: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]]) -> None:
        for cls, info in (new or {}).items():
            if cls not in base:
                base[cls] = {"bases": set(), "members": {}}
            base[cls]["bases"].update(info.get("bases") or set())
            for var, tset in (info.get("members") or {}).items():
                base[cls]["members"].setdefault(var, set()).update(tset)

    def _parse_class_infos_from_text(self, text: str) -> Dict[str, Dict[str, Any]]:
        infos: Dict[str, Dict[str, Any]] = {}
        lines = (text or "").split("\n")
        i = 0
        n = len(lines)
        class_head = re.compile(r"^\s*(?:class|struct)\s+([A-Za-z_]\w*)(?:\s*:\s*([^{]+))?\s*\{")
        while i < n:
            line = lines[i]
            m = class_head.search(line)
            if not m:
                i += 1
                continue
            cls = m.group(1)
            bases_raw = (m.group(2) or "").strip()
            bases: set = set()
            if bases_raw:
                for part in bases_raw.split(","):
                    p = part.strip()
                    p = re.sub(r"\b(public|protected|private|virtual)\b", "", p).strip()
                    p = self._normalize_type_name(p)
                    if p:
                        bases.add(p)
            brace = line.count("{") - line.count("}")
            j = i + 1
            body_lines: List[str] = []
            while j < n:
                body_lines.append(lines[j])
                brace += lines[j].count("{") - lines[j].count("}")
                if brace <= 0:
                    break
                j += 1
            members: Dict[str, set] = {}
            for bl in body_lines:
                s = (bl or "").strip()
                if not s or s.startswith("//") or s.startswith("/*"):
                    continue
                if "(" in s and ")" in s:
                    continue
                mm_sp = re.search(
                    r"\b(?:std::)?(?:shared_ptr|unique_ptr|weak_ptr)\s*<\s*([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*>\s*([A-Za-z_]\w*)\s*;",
                    s,
                )
                if mm_sp:
                    t = self._normalize_type_name(mm_sp.group(1))
                    v = mm_sp.group(2)
                    if t and v:
                        members.setdefault(v, set()).add(t)
                    continue
                mm_ptr = re.search(
                    r"\b([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*[*&]\s*([A-Za-z_]\w*)\s*;",
                    s,
                )
                if mm_ptr:
                    t = self._normalize_type_name(mm_ptr.group(1))
                    v = mm_ptr.group(2)
                    if t and v:
                        members.setdefault(v, set()).add(t)
                    continue
                mm_obj = re.search(
                    r"\b([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s+([A-Za-z_]\w*)\s*;",
                    s,
                )
                if mm_obj:
                    t = self._normalize_type_name(mm_obj.group(1))
                    v = mm_obj.group(2)
                    if t and v:
                        members.setdefault(v, set()).add(t)
            infos[cls] = {"bases": bases, "members": members}
            i = j + 1
        return infos

    def _collect_all_ancestors(self, cls: str, inherit_map: Dict[str, set]) -> set:
        out: set = set()
        stack = [cls]
        while stack:
            cur = stack.pop()
            for b in inherit_map.get(cur, set()) or set():
                if b in out:
                    continue
                out.add(b)
                stack.append(b)
        return out

    def _types_compatible_with_owner(self, owner: str, inferred_type: str, inherit_map: Dict[str, set]) -> bool:
        o = self._normalize_type_name(owner)
        t = self._normalize_type_name(inferred_type)
        if not o or not t:
            return False
        if o == t:
            return True
        anc_t = self._collect_all_ancestors(t, inherit_map)
        anc_o = self._collect_all_ancestors(o, inherit_map)
        # 允许：inferred_type 为 owner 基类（虚分派可能落到 owner）或 owner 为 inferred_type 基类
        return (o in anc_t) or (t in anc_o)

    def _parse_template_container_function(self, crash_function_name: str) -> Optional[Dict[str, str]]:
        """
        解析模板容器成员函数签名，例如：
        _baidu_vi::CVList<_baidu_framework::CBVDEOptCacheElement, ...>::RemoveAt(void*)
        """
        if not crash_function_name or "::" not in crash_function_name or "<" not in crash_function_name:
            return None

        match = re.search(
            r"(?P<container>[A-Za-z_][\w:]*)\s*<(?P<args>.+)>\s*::\s*(?P<method>[A-Za-z_]\w*)\s*\(",
            crash_function_name,
        )
        if not match:
            return None

        container_full = match.group("container").strip()
        method = match.group("method").strip()
        args = match.group("args").strip()

        # 提取第一个模板参数（支持嵌套模板）
        depth = 0
        first_arg_chars: List[str] = []
        for ch in args:
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                break
            first_arg_chars.append(ch)

        first_arg_raw = "".join(first_arg_chars).strip()
        if not first_arg_raw:
            return None

        first_arg_simple = re.sub(r"\bconst\b", "", first_arg_raw).replace("&", "").replace("*", "").strip()
        if "::" in first_arg_simple:
            first_arg_simple = first_arg_simple.split("::")[-1]

        return {
            "container": container_full.split("::")[-1],
            "container_full": container_full,
            "method": method,
            "first_arg_raw": first_arg_raw,
            "first_arg_simple": first_arg_simple,
        }

    def _collect_template_container_member_candidates(
        self, code_roots: List[str], container_name: str, first_arg_simple: str
    ) -> Tuple[List[str], List[str]]:
        """收集模板容器实例成员变量名与其声明所在目录。"""
        member_names: set[str] = set()
        candidate_dirs: set[str] = set()

        decl_pattern = re.compile(
            rf"\b{re.escape(container_name)}\s*<[^;{{}}]*{re.escape(first_arg_simple)}[^;{{}}]*>\s*([A-Za-z_]\w*)\s*;"
        )

        for code_root in code_roots or []:
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]
                for file in files:
                    file_path = os.path.join(root, file)
                    self.search_stats['files_scanned'] += 1
                    if self._should_skip_file(file_path, code_root):
                        continue
                    try:
                        self.search_stats['files_read'] += 1
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                if container_name not in line or first_arg_simple not in line:
                                    continue
                                m = decl_pattern.search(line)
                                if not m:
                                    continue
                                member_names.add(m.group(1))
                                candidate_dirs.add(os.path.dirname(file_path))
                    except Exception:
                        continue

        return sorted(member_names), sorted(candidate_dirs)

    def _find_call_chain_functions_for_template_container(
        self, crash_function_name: str, code_roots: List[str]
    ) -> List[CallChainFunction]:
        """
        模板容器函数调用链定向搜索：
        仅使用“模板实参 + 成员变量名”去定位调用点，不做裸方法名全仓扫描。
        """
        parsed = self._parse_template_container_function(crash_function_name)
        if not parsed:
            return []

        container_name = parsed["container"]
        method = parsed["method"]
        first_arg_simple = parsed["first_arg_simple"]

        member_names, candidate_dirs = self._collect_template_container_member_candidates(
            code_roots, container_name, first_arg_simple
        )

        if not member_names:
            logger.info(
                f"模板容器定向搜索未找到实例成员，跳过调用链搜索: {container_name}<{first_arg_simple}>::{method}"
            )
            return []

        call_patterns = [
            re.compile(rf"\b{re.escape(member)}\s*\.\s*{re.escape(method)}\s*\(") for member in member_names
        ] + [
            re.compile(rf"\b{re.escape(member)}\s*->\s*{re.escape(method)}\s*\(") for member in member_names
        ]

        unique: Dict[Tuple[str, str], CallChainFunction] = {}

        for code_root in code_roots or []:
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]
                if candidate_dirs and not any(root == d or root.startswith(d + os.sep) for d in candidate_dirs):
                    continue

                for file in files:
                    file_path = os.path.join(root, file)
                    self.search_stats['files_scanned'] += 1
                    if self._should_skip_file(file_path, code_root):
                        continue
                    try:
                        self.search_stats['files_read'] += 1
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.read().split("\n")

                        for i, line in enumerate(lines, 1):
                            if not any(p.search(line) for p in call_patterns):
                                continue
                            caller_function = self._extract_function_name_at_line(lines, i)
                            if not caller_function:
                                continue
                            caller_code = self._extract_full_function_code_for_caller(lines, i - 1, caller_function)
                            if not caller_code:
                                continue
                            if not any(p.search(caller_code) for p in call_patterns):
                                continue
                            key = (caller_function, file_path)
                            if key in unique:
                                continue
                            caller_snippet = [ln.rstrip() for ln in caller_code.split('\n') if ln.strip()]
                            unique[key] = CallChainFunction(
                                name=caller_function,
                                file=file_path,
                                snippet=caller_snippet
                            )
                            logger.info(f"模板容器定向命中调用者: {caller_function} 在 {file_path}:{i}")
                    except Exception:
                        continue

        return list(unique.values())

    def _find_function_definition(
        self, function_name: str, code_roots: List[str]
    ) -> Optional[Tuple[str, List[str]]]:
        """在代码根目录中查找函数定义，并返回(文件路径, 函数完整代码片段)。

        优先使用类限定名（Class::func）做精确匹配；若不可用再回退到简单函数名匹配。
        """
        simple_name = self._extract_simple_function_name(function_name)
        if not simple_name:
            return None
        qualified_name: Optional[str] = None
        if "::" in (function_name or ""):
            # 去掉参数列表，仅保留 Class::func
            qualified_name = function_name.split("(")[0].strip()
            # 对 _baidu_vi::CVPlex::Create 这类多级命名空间，保留最后一级类 + 方法，避免过严匹配
            q_parts = qualified_name.split("::")
            if len(q_parts) >= 2:
                qualified_name = "::".join(q_parts[-2:])

        for code_root in code_roots or []:
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]

                for file in files:
                    file_path = os.path.join(root, file)

                    if self._should_skip_file(file_path, code_root):
                        continue

                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                            lines = content.split("\n")

                        # 先尝试类限定名匹配（更精确）
                        if qualified_name:
                            q_pattern = re.compile(rf"\b{re.escape(qualified_name)}\s*\(")
                            for i, line in enumerate(lines, 1):
                                if q_pattern.search(line) and self._cpp_signature_probe_string(lines, i - 1):
                                    func_code = self._extract_full_function_code(
                                        lines, i - 1, target_function_name=simple_name
                                    )
                                    if func_code:
                                        snippet = [l.rstrip() for l in func_code.split("\n") if l.strip()]
                                        return file_path, snippet

                        for i, line in enumerate(lines, 1):
                            # 查找函数定义行：返回类型 + 名称 + '('
                            if re.search(rf"\b{re.escape(simple_name)}\s*\(", line):
                                if self._cpp_signature_probe_string(lines, i - 1):
                                    func_code = self._extract_full_function_code(
                                        lines, i - 1, target_function_name=simple_name
                                    )
                                    if func_code:
                                        snippet = [
                                            l.rstrip()
                                            for l in func_code.split("\n")
                                            if l.strip()
                                        ]
                                        return file_path, snippet
                    except Exception:
                        continue

        return None
    
    def _find_variable_functions(self, crash_line_code: str, crash_function_name: str, 
                                code_roots: List[str]) -> List[VariableFunction]:
        """查找使用崩溃行涉及变量的所有函数"""
        import time
        start_time = time.time()
        logger.info(f"查找变量相关函数: {crash_line_code}")
        variable_functions = []
        
        # 从崩溃行代码中提取变量
        crash_variables = self._extract_variables_from_line(crash_line_code)
        if not crash_variables:
            logger.warning("无法从崩溃行提取变量")
            return variable_functions
        
        logger.info(f"提取到崩溃变量: {crash_variables}")
        
        # 用于去重的字典，key 为 (函数名, 文件路径, 变量名, 关系)，value 为 VariableFunction
        unique_functions = {}
        
        # 重置搜索统计（但保留之前的累计值）
        files_scanned_before = self.search_stats['files_scanned']
        files_read_before = self.search_stats['files_read']
        
        # 搜索所有源文件
        for code_root in code_roots or []:
            for root, dirs, files in os.walk(code_root):
                # 过滤掉排除的目录
                dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]
                
                for file in files:
                    file_path = os.path.join(root, file)
                    self.search_stats['files_scanned'] += 1
                    
                    # 检查是否应该跳过该文件
                    if self._should_skip_file(file_path, code_root):
                        continue
                    
                    try:
                        self.search_stats['files_read'] += 1
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            lines = content.split('\n')
                        
                        # 查找使用崩溃变量的函数
                        for i, line in enumerate(lines, 1):
                            for var in crash_variables:
                                if self._contains_variable_usage(line, var):
                                    # 检查这一行是否真的使用了变量（不是函数定义）
                                    if self._is_variable_usage_line(line, var):
                                        function_name = self._extract_function_name_at_line(lines, i)
                                        if function_name and function_name != crash_function_name:
                                            # 提取函数的完整代码
                                            function_code = self._extract_full_function_code(
                                                lines, i - 1, target_function_name=function_name
                                            )
                                            if function_code:
                                                # 确定变量使用关系
                                                relation = self._determine_variable_relation(line, var)

                                                # 格式化代码片段，确保每行独立
                                                function_snippet = [line.rstrip() for line in function_code.split('\n') if line.strip()]
                                                # 应用代码片段截断
                                                if self.max_code_length > 0:
                                                    function_snippet = self._truncate_snippet(function_snippet)

                                                # 创建VariableFunction对象
                                                var_func = VariableFunction(
                                                    variable=var,
                                                    relation=relation,
                                                    name=function_name,
                                                    file=file_path,
                                                    snippet=function_snippet
                                                )
                                                
                                                # 去重：允许同一函数/变量在不同关系（read/write/delete/unknown）下各保留一条
                                                key = (function_name, file_path, var, relation)
                                                if key not in unique_functions:
                                                    unique_functions[key] = var_func
                                                    logger.info(
                                                        f"找到变量相关函数: {function_name} 使用变量 {var} ({relation}) 在 {file_path}:{i}"
                                                    )
                    
                    except Exception as e:
                        logger.debug(f"分析文件失败 {file_path}: {e}")
                        continue
        
        # 将去重后的结果转换为列表
        variable_functions = list(unique_functions.values())
        elapsed_time = time.time() - start_time
        self.search_stats['search_time'] += elapsed_time
        
        files_scanned_this = self.search_stats['files_scanned'] - files_scanned_before
        files_read_this = self.search_stats['files_read'] - files_read_before
        
        logger.info(
            f"变量搜索完成: 扫描 {files_scanned_this} 个文件, "
            f"读取 {files_read_this} 个文件, "
            f"找到 {len(variable_functions)} 条函数-变量关系(按关系类型区分), "
            f"耗时 {elapsed_time:.2f}秒"
        )
        
        return variable_functions

    def _find_variable_functions_for_vars(
        self,
        shared_vars: List[str],
        crash_function_name: str,
        code_roots: List[str],
        stack_priority_files: Optional[List[str]] = None,
    ) -> List[VariableFunction]:
        """
        根据给定的共享变量名集合，在全项目中查找使用这些变量的所有函数（含崩溃函数本身），
        用于构建 use_shared_var 边。崩溃函数也会被包含，以便图中展示“崩溃函数使用哪些共享变量”。
        若提供 stack_priority_files，则先在这些堆栈相关源文件中扫描，再扫描全仓（去重，顺序上堆栈命中在前）。
        """
        import time
        start_time = time.time()
        if not shared_vars:
            return []
        logger.info(f"按共享变量查找相关函数: {shared_vars}")
        unique_functions: Dict[tuple, VariableFunction] = {}
        files_scanned_before = self.search_stats["files_scanned"]
        files_read_before = self.search_stats["files_read"]

        stack_norm: List[str] = []
        if stack_priority_files:
            for raw in stack_priority_files:
                if not raw:
                    continue
                try:
                    ap = os.path.abspath(raw)
                except Exception:
                    ap = raw
                if os.path.isfile(ap) and self._is_supported_file(ap):
                    stack_norm.append(ap)
        stack_set = set(stack_norm)
        if stack_norm:
            logger.info(
                f"共享变量扫描：堆栈优先 {len(stack_norm)} 个源文件，随后全仓补充（去重）"
            )

        def _scan_file_paths(file_paths: List[str]) -> None:
            for file_path in file_paths:
                cr = self._pick_code_root_for_file(file_path, code_roots)
                self.search_stats["files_scanned"] += 1
                if self._should_skip_file(file_path, cr):
                    continue
                try:
                    self.search_stats["files_read"] += 1
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    lines = content.split("\n")
                    for i, line in enumerate(lines, 1):
                        for var in shared_vars:
                            if not self._contains_variable_usage(line, var):
                                continue
                            if not self._is_variable_usage_line(line, var):
                                continue
                            function_name = self._extract_function_name_at_line(lines, i)
                            if not function_name:
                                continue
                            function_code = self._extract_full_function_code(
                                lines, i - 1, target_function_name=function_name
                            )
                            if not function_code:
                                continue
                            relation = self._determine_variable_relation(line, var)
                            function_snippet = [
                                ln.rstrip() for ln in function_code.split("\n") if ln.strip()
                            ]
                            if self.max_code_length > 0:
                                function_snippet = self._truncate_snippet(function_snippet)
                            var_func = VariableFunction(
                                variable=var,
                                relation=relation,
                                name=function_name,
                                file=file_path,
                                snippet=function_snippet,
                            )
                            key = (function_name, file_path, var, relation)
                            if key not in unique_functions:
                                unique_functions[key] = var_func
                                logger.debug(
                                    f"变量相关函数: {function_name} 使用 {var} ({relation}) @ {file_path}:{i}"
                                )
                except Exception as e:
                    logger.debug(f"分析文件失败 {file_path}: {e}")
                    continue

        if stack_norm:
            _scan_file_paths(stack_norm)

        for code_root in code_roots or []:
            for root, dirs, files in os.walk(code_root):
                dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        ap = os.path.abspath(file_path)
                    except Exception:
                        ap = file_path
                    if stack_set and ap in stack_set:
                        continue
                    self.search_stats["files_scanned"] += 1
                    if self._should_skip_file(file_path, code_root):
                        continue
                    try:
                        self.search_stats["files_read"] += 1
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        lines = content.split("\n")
                        for i, line in enumerate(lines, 1):
                            for var in shared_vars:
                                if not self._contains_variable_usage(line, var):
                                    continue
                                if not self._is_variable_usage_line(line, var):
                                    continue
                                function_name = self._extract_function_name_at_line(lines, i)
                                if not function_name:
                                    continue
                                function_code = self._extract_full_function_code(
                                    lines, i - 1, target_function_name=function_name
                                )
                                if not function_code:
                                    continue
                                relation = self._determine_variable_relation(line, var)
                                function_snippet = [
                                    ln.rstrip() for ln in function_code.split("\n") if ln.strip()
                                ]
                                if self.max_code_length > 0:
                                    function_snippet = self._truncate_snippet(function_snippet)
                                var_func = VariableFunction(
                                    variable=var,
                                    relation=relation,
                                    name=function_name,
                                    file=file_path,
                                    snippet=function_snippet,
                                )
                                key = (function_name, file_path, var, relation)
                                if key not in unique_functions:
                                    unique_functions[key] = var_func
                                    logger.debug(
                                        f"变量相关函数: {function_name} 使用 {var} ({relation}) @ {file_path}:{i}"
                                    )
                    except Exception as e:
                        logger.debug(f"分析文件失败 {file_path}: {e}")
                        continue

        variable_functions = list(unique_functions.values())
        elapsed_time = time.time() - start_time
        self.search_stats["search_time"] += elapsed_time
        files_scanned_this = self.search_stats["files_scanned"] - files_scanned_before
        files_read_this = self.search_stats["files_read"] - files_read_before
        logger.info(
            f"共享变量函数搜索完成: 扫描 {files_scanned_this} 个文件, "
            f"读取 {files_read_this} 个文件, 找到 {len(variable_functions)} 条函数-变量关系, "
            f"耗时 {elapsed_time:.2f}秒"
        )
        return variable_functions
    
    def _extract_variables_from_line(self, line: str) -> List[str]:
        """从代码行中提取变量"""
        variables = []
        
        # 针对崩溃行的特殊处理：tail->next = new_node;
        # 这里 tail 是被读取的变量，new_node 是被写入的变量
        
        # 优先提取指针解引用中的变量（如 tail->next 中的 tail）
        # 这是最常见的崩溃场景
        ptr_deref_pattern = r'\b(\w+)\s*->'
        ptr_matches = re.findall(ptr_deref_pattern, line)
        for match in ptr_matches:
            if self._is_valid_variable_name(match):
                variables.append(match)
                logger.debug(f"从指针解引用中提取变量: {match}")
        
        # 变量提取模式 - 更精确的匹配
        patterns = [
            r'\b(\w+)\s*\.\s*\w+',  # obj.member
            r'\b(\w+)\s*\[',  # array[index]
            r'\b(\w+)\s*=',  # var = value (赋值)
            r'=\s*(\w+)',  # = var (被赋值的变量)
            r'\(\s*(\w+)\s*\)',  # (variable)
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, line)
            for match in matches:
                if self._is_valid_variable_name(match) and match not in variables:
                    variables.append(match)
        
        # 过滤掉关键字
        keywords = {'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default', 
                   'return', 'break', 'continue', 'goto', 'const', 'static', 'extern',
                   'volatile', 'register', 'auto', 'typedef', 'struct', 'union', 'enum',
                   'class', 'public', 'private', 'protected', 'virtual', 'inline',
                   'new', 'delete', 'this', 'nullptr', 'NULL', 'true', 'false'}
        
        variables = [v for v in variables if v not in keywords and len(v) > 1]
        
        return list(set(variables))

    # 噪音变量名：局部临时变量等，即使出现在代码中也不作为共享变量候选
    _SHARED_VAR_BLOCKLIST = frozenset({
        "i", "j", "k", "n", "p", "x", "y", "e", "id", "size", "tmp", "temp",
        "gen", "dis", "oss", "now", "tm", "ctx", "sig", "info", "context",
        "path", "name", "file", "line", "idx", "num", "len", "val", "key",
    })

    # 典型共享语义的变量名/后缀（用于在无成员访问时仍能识别候选）
    _SHARED_LIKE_NAMES = frozenset({
        "mtx", "mutex", "head", "tail", "node_count", "is_destroying",
        "count", "lock", "ref_count", "running", "operation_count",
        "shared_data", "g_running", "g_operation_count", "g_shared_data",
        "g_crash_type", "g_log_dir",
    })

    def _collect_local_declared_vars(self, lines: List[str]) -> set:
        """
        粗提取函数体内局部变量声明名，用于从共享变量候选中排除明显局部变量。
        目标是降低同名变量跨文件误关联（如 uiObj/uiObjType）。
        """
        # tree-sitter 路径（阶段1.5）：优先基于 AST 识别局部声明变量
        if self.code_parser_backend == "tree_sitter" and self._ts_parser is not None:
            src = "\n".join(lines or [])
            ts_vars = self._ts_collect_local_declared_vars(src)
            if ts_vars:
                return ts_vars

        declared: set = set()
        decl_patterns = [
            # auto x = ...; / auto x(...);
            r"^\s*auto\s+([A-Za-z_]\w*)\b",
            # Type x = ...; / Type* x; / Type& x(...)
            r"^\s*(?:const\s+)?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*(?:\s*<[^;{}()]+>)?\s*[*&]?\s+([A-Za-z_]\w*)\b",
        ]
        for raw in lines:
            line = (raw or "").strip()
            if not line or line.startswith("//") or line.startswith("/*"):
                continue
            # 跳过明显非声明行
            if line.startswith(("if ", "if(", "for ", "for(", "while ", "while(", "switch ", "switch(", "return ")):
                continue
            for p in decl_patterns:
                m = re.search(p, line)
                if not m:
                    continue
                name = m.group(1)
                if self._is_valid_variable_name(name):
                    declared.add(name)
                break
        return declared

    def _ts_collect_identifiers_in_declarator(self, node, source_text: str) -> List[str]:
        """从 declarator 子树中提取变量名（identifier）。"""
        out: List[str] = []
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur is None:
                continue
            if cur.type == "identifier":
                name = source_text[cur.start_byte:cur.end_byte].strip()
                if name:
                    out.append(name)
                continue
            try:
                stack.extend(list(cur.children))
            except Exception:
                continue
        return out

    def _ts_collect_local_declared_vars(self, source_text: str) -> set:
        """使用 tree-sitter 从 declaration/init_declarator 中提取局部变量名。"""
        out: set = set()
        if not (self._ts_parser and source_text):
            return out
        try:
            tree = self._ts_parser.parse(source_text.encode("utf-8", errors="ignore"))
            root = tree.root_node
        except Exception:
            return out

        stack = [root]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            if node.type == "declaration":
                try:
                    for ch in node.children:
                        # init_declarator / pointer_declarator / reference_declarator 等
                        if "declarator" in ch.type or ch.type in {"identifier", "array_declarator"}:
                            for name in self._ts_collect_identifiers_in_declarator(ch, source_text):
                                if self._is_valid_variable_name(name):
                                    out.add(name)
                except Exception:
                    pass
            try:
                stack.extend(list(node.children))
            except Exception:
                continue
        return out

    def _collect_function_param_vars(self, lines: List[str]) -> set:
        """
        从函数签名提取参数变量名（排除：跨函数共享变量候选）。
        支持多行签名，直到遇到 '{' 或 ')' 完成解析。
        """
        # tree-sitter 路径（阶段1.5）：优先从 parameter_declaration 提取参数名
        if self.code_parser_backend == "tree_sitter" and self._ts_parser is not None:
            src = "\n".join(lines or [])
            ts_params = self._ts_collect_param_vars(src)
            if ts_params:
                return ts_params

        sig_text = ""
        for raw in lines:
            line = (raw or "").strip()
            if not line:
                continue
            sig_text += " " + line
            if "{" in line or ")" in line:
                break
        sig_text = sig_text.strip()
        if "(" not in sig_text or ")" not in sig_text:
            return set()
        m = re.search(r"\((.*)\)", sig_text)
        if not m:
            return set()
        params_text = m.group(1)
        params = self._split_function_params(params_text)
        out: set = set()
        for p in params:
            p = p.strip()
            if not p or p == "void":
                continue
            # 去默认值
            if "=" in p:
                p = p.split("=", 1)[0].strip()
            toks = re.findall(r"[A-Za-z_]\w*", p)
            if not toks:
                continue
            # 最后一个 token 近似视为参数名
            cand = toks[-1]
            # 排除类型关键字/命名空间 token
            if cand in {"const", "volatile", "unsigned", "signed", "long", "short"}:
                continue
            if self._is_valid_variable_name(cand):
                out.add(cand)
        return out

    def _ts_collect_param_vars(self, source_text: str) -> set:
        """使用 tree-sitter 提取函数参数变量名。"""
        out: set = set()
        if not (self._ts_parser and source_text):
            return out
        try:
            tree = self._ts_parser.parse(source_text.encode("utf-8", errors="ignore"))
            root = tree.root_node
        except Exception:
            return out

        stack = [root]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            if node.type == "parameter_declaration":
                try:
                    for ch in node.children:
                        if "declarator" in ch.type or ch.type in {"identifier", "optional_parameter_declaration"}:
                            for name in self._ts_collect_identifiers_in_declarator(ch, source_text):
                                if self._is_valid_variable_name(name):
                                    out.add(name)
                except Exception:
                    pass
            try:
                stack.extend(list(node.children))
            except Exception:
                continue
        return out

    def _split_function_params(self, params_text: str) -> List[str]:
        """按逗号拆分参数列表，忽略模板/括号嵌套。"""
        parts: List[str] = []
        cur: List[str] = []
        angle = 0
        paren = 0
        bracket = 0
        for ch in params_text or "":
            if ch == "<":
                angle += 1
            elif ch == ">":
                if angle > 0:
                    angle -= 1
            elif ch == "(":
                paren += 1
            elif ch == ")":
                if paren > 0:
                    paren -= 1
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                if bracket > 0:
                    bracket -= 1

            if ch == "," and angle == 0 and paren == 0 and bracket == 0:
                item = "".join(cur).strip()
                if item:
                    parts.append(item)
                cur = []
                continue
            cur.append(ch)
        tail = "".join(cur).strip()
        if tail:
            parts.append(tail)
        return parts

    def _collect_member_or_global_candidates(self, lines: List[str]) -> set:
        """
        只提取“可能为成员变量/全局变量”的候选：
        - this->member 取 member
        - scope::var 取 var（命名空间/全局样式）
        - 变量名前有作用域解析符 ::var
        """
        out: set = set()
        for raw in lines:
            line = (raw or "").strip()
            if not line or line.startswith("//") or line.startswith("/*"):
                continue
            for m in re.finditer(r"\bthis\s*->\s*([A-Za-z_]\w*)", line):
                name = m.group(1)
                if self._is_valid_variable_name(name):
                    out.add(name)
            for m in re.finditer(r"\b(?:[A-Za-z_]\w*::)+([A-Za-z_]\w*)\b", line):
                name = m.group(1)
                if self._is_valid_variable_name(name):
                    out.add(name)
            for m in re.finditer(r"::\s*([A-Za-z_]\w*)\b", line):
                name = m.group(1)
                if self._is_valid_variable_name(name):
                    out.add(name)
        return out

    def _extract_shared_variables_from_code(self, code_text: str) -> List[str]:
        """
        从代码片段中提取“共享变量”候选集合（仅类成员/全局变量），不包含函数参数与局部变量。
        用于精确行时输入为崩溃行；用于 from_log_deduce 时输入为整个函数片段。
        返回去重且排序的变量名列表。
        """
        if not (code_text and code_text.strip()):
            return []
        lines = code_text.split("\n")

        local_declared = self._collect_local_declared_vars(lines)
        param_vars = self._collect_function_param_vars(lines)
        member_or_global = self._collect_member_or_global_candidates(lines)

        out: List[str] = []
        seen: set = set()
        for v in sorted(member_or_global):
            if v in seen or v in self._SHARED_VAR_BLOCKLIST:
                continue
            if not self._is_valid_variable_name(v):
                continue
            # 按要求：函数参数不作为共享变量；局部声明变量不作为共享变量
            if v in param_vars:
                continue
            if v in local_declared:
                continue
            seen.add(v)
            out.append(v)
        return sorted(out)
    
    def _is_valid_variable_name(self, name: str) -> bool:
        """检查是否是有效的变量名"""
        if not name or len(name) < 2:
            return False
        
        # 排除关键字和常见非变量词
        excluded_words = {
            'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
            'return', 'break', 'continue', 'goto', 'try', 'catch', 'throw',
            'new', 'delete', 'sizeof', 'typeof', 'const', 'static', 'extern',
            'volatile', 'register', 'auto', 'inline', 'virtual', 'explicit',
            'public', 'private', 'protected', 'class', 'struct', 'union',
            'enum', 'namespace', 'using', 'template', 'typename', 'operator',
            'this', 'nullptr', 'NULL', 'true', 'false', 'and', 'or', 'not',
            'int', 'char', 'float', 'double', 'void', 'bool', 'long', 'short',
            'unsigned', 'signed', 'const', 'mutable', 'volatile', 'restrict'
        }
        
        return name not in excluded_words and name.isalnum()
    
    def _contains_variable_usage(self, line: str, variable_name: str) -> bool:
        """检查行是否包含变量使用"""
        patterns = [
            rf'\b{re.escape(variable_name)}\b',  # 变量名
            rf'->\s*{re.escape(variable_name)}\b',  # ->variable
            rf'\.\s*{re.escape(variable_name)}\b',  # .variable
        ]
        
        for pattern in patterns:
            if re.search(pattern, line):
                return True
        return False

    def _find_pre_call_fun_in_same_parent_functions(
        self,
        crash_function_name: str,
        direct_callers: List[CallChainFunction],
        code_roots: List[str],
    ) -> List[CallChainFunction]:
        """
        查找与崩溃函数同处一个入口函数体内、且在崩溃函数调用之前执行的“前置环境函数”。
        这些函数作为 pre_call_fun_in_same_parent_fun 返回，并在 parent_fun 字段中标记所属的直接调用者。
        """
        pre_call_fun_in_same_parent_functions: List[CallChainFunction] = []
        seen: set[tuple[str, str, str]] = set()  # (parent_fun, func_name, file)

        simple_function_name = self._extract_simple_function_name(crash_function_name)

        for caller in direct_callers:
            parent_name = caller.name
            snippet_lines = caller.snippet or []
            if not snippet_lines:
                continue

            # 在 caller 的代码片段中找到第一次调用崩溃函数的位置
            crash_call_index: Optional[int] = None
            for idx, line in enumerate(snippet_lines):
                if self._is_function_call_line(line, simple_function_name):
                    crash_call_index = idx
                    break

            if crash_call_index is None:
                continue

            # 在崩溃调用之前的代码中，寻找其它函数调用，作为候选 entry context
            for line in snippet_lines[:crash_call_index]:
                text = line.strip()
                if not text or text.startswith("//") or text.startswith("/*"):
                    continue

                # 提取形如 foo(...), obj.foo(...), obj->foo(...) 中的 foo 作为候选函数名
                # 先过滤掉控制结构/关键字，避免把 if/for 等误认为函数调用
                # 简单关键字列表（可按需扩展）
                keywords = {
                    "if", "for", "while", "switch", "return", "sizeof",
                    "static_cast", "dynamic_cast", "reinterpret_cast",
                    "catch", "else"
                }

                # 匹配标识符后跟 "(" 的模式
                candidates = re.findall(r"\b([A-Za-z_]\w*)\s*\(", text)
                for func_name in candidates:
                    if func_name in keywords:
                        continue
                    if func_name == simple_function_name:
                        continue

                    key = (parent_name, func_name, "")
                    if key in seen:
                        continue

                    # 在整个代码根目录中尝试找到该函数的定义位置
                    loc = self._find_function_definition_location(func_name, code_roots)
                    if not loc:
                        continue
                    file_path, line_no = loc
                    key = (parent_name, func_name, file_path)
                    if key in seen:
                        continue

                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()
                        func_code = self._extract_full_function_code(lines, line_no - 1)
                        if not func_code:
                            continue
                        func_snippet = [
                            l.rstrip() for l in func_code.split("\n") if l.strip()
                        ]
                        pre_call_fun_in_same_parent_functions.append(
                            CallChainFunction(
                                name=func_name,
                                file=file_path,
                                snippet=func_snippet,
                                parent_fun=parent_name,
                            )
                        )
                        seen.add(key)
                        logger.info(
                            f"找到 pre_call_fun_in_same_parent_fun: {func_name} (parent={parent_name}) 在 {file_path}:{line_no}"
                        )
                    except Exception as e:
                        logger.debug(f"提取 pre_call_fun_in_same_parent_fun 失败 {file_path}:{line_no}: {e}")
                        continue

        return pre_call_fun_in_same_parent_functions

    def _is_snippet_omit_placeholder_line(self, line: str) -> bool:
        """智能截断产生的省略行，不参与行号定位。"""
        s = (line or "").strip()
        if not s:
            return False
        if re.match(r"^\.\.\.\s*\[", s):
            return True
        if "lines omitted" in s.lower() or "more lines" in s.lower():
            return True
        return False

    def _snippet_lines_for_locate(self, snippet: List[str]) -> List[str]:
        return [ln for ln in snippet if not self._is_snippet_omit_placeholder_line(ln)]

    def _brace_match_end_line_index(
        self, flines: List[str], start_idx: int, n: int
    ) -> Optional[int]:
        """
        从 start_idx 起扫描，找到与首个“使深度由 0 变为正”的 { 配对的 } 所在行（0-based）。
        用于省略片段时定位函数体结束行，避免误匹配内层 } 或文件后部重复行。
        """
        depth = 0
        seen_open = False
        for j in range(start_idx, n):
            for c in flines[j]:
                if c == "{":
                    depth += 1
                    seen_open = True
                elif c == "}":
                    if not seen_open:
                        continue
                    depth -= 1
                    if depth == 0:
                        return j
                    if depth < 0:
                        return None
        return None

    def _read_file_lines_cached(
        self, file_path: str, cache: Dict[str, List[str]]
    ) -> Optional[List[str]]:
        if file_path in cache:
            return cache[file_path]
        try:
            if not os.path.isfile(file_path):
                return None
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                cache[file_path] = f.read().splitlines()
            return cache[file_path]
        except Exception:
            return None

    def _locate_snippet_lines_in_file(
        self,
        file_path: Optional[str],
        snippet: Optional[List[str]],
        file_lines_cache: Optional[Dict[str, List[str]]] = None,
    ) -> Tuple[Optional[int], Optional[int]]:
        """
        根据 snippet 在源文件中的内容反查 1-based 起止行号（含端点）。
        截断片段（含省略占位行）时：首行定起点，末条非占位行定终点。
        """
        if not isinstance(file_path, str) or not file_path.strip() or not snippet:
            return None, None
        real = self._snippet_lines_for_locate(snippet)
        if not real:
            return None, None
        cache = file_lines_cache if file_lines_cache is not None else {}
        flines = self._read_file_lines_cached(file_path, cache)
        if not flines:
            return None, None

        has_omit = any(self._is_snippet_omit_placeholder_line(ln) for ln in snippet)
        n = len(flines)
        t0 = real[0].strip()

        def lines_match_at(i: int) -> bool:
            if i < 0 or i >= n:
                return False
            if flines[i].strip() != t0:
                return False
            if len(real) < 2:
                return True
            max_k = min(len(real), 12)
            for k in range(1, max_k):
                if i + k >= n:
                    return False
                if flines[i + k].strip() != real[k].strip():
                    return False
            return True

        start_idx: Optional[int] = None
        for i in range(n):
            if lines_match_at(i):
                start_idx = i
                break
        if start_idx is None:
            # 宽松：首行是整行子串（截断或空白差异）
            for i in range(n):
                row = flines[i].strip()
                if not row:
                    continue
                if t0 in row or row in t0:
                    if len(t0) >= 12 or len(row) >= 12:
                        start_idx = i
                        break
        if start_idx is None:
            return None, None

        start_1 = start_idx + 1

        if not has_omit and len(real) >= 2:
            ok = True
            for k in range(len(real)):
                if start_idx + k >= n:
                    ok = False
                    break
                if flines[start_idx + k].strip() != real[k].strip():
                    ok = False
                    break
            if ok:
                return start_1, start_idx + len(real)

        last_t = real[-1].strip()
        end_idx = start_idx
        br_end = self._brace_match_end_line_index(flines, start_idx, n)
        if br_end is not None:
            end_idx = br_end
        else:
            # 无配对（极少）：末行重复时取首次出现，避免误用文件后部
            for j in range(start_idx, n):
                if flines[j].strip() == last_t:
                    end_idx = j
                    break
        return start_1, end_idx + 1

    def _format_graph_edges_empty_reason(
        self,
        *,
        max_n: int,
        direct_callers_count: int,
        has_code_roots: bool,
        shared_var_relations: int,
        add2line_max_nodes: int,
    ) -> str:
        """当 graph.edges 为空时，生成供下游展示的简短说明（多句拼接）。"""
        parts: List[str] = []
        if max_n == 1:
            parts.append(
                "max_static_call_chain_depth 为 1，仅保留崩溃函数节点，不生成静态调用链上的 calls_direct 边。"
            )
        if max_n > 1:
            if direct_callers_count == 0:
                parts.append(
                    "静态分析未在代码库中定位到直接调用崩溃函数的上层函数（或候选签名未通过校验、以及与 STL 容器同名成员被跳过全仓正则等）。"
                )
            elif not has_code_roots:
                parts.append("未配置有效的 code_roots，静态调用边无法生成。")
            else:
                parts.append(
                    "虽有直接调用候选，但未形成有效调用边（签名校验、链扩展或 min_static_call_chain_nodes 等限制）。"
                )
        if shared_var_relations == 0:
            parts.append(
                "未从崩溃行/函数片段提取到共享变量，或未发现其它函数使用这些变量，故无 use_shared_var 边。"
            )
        if add2line_max_nodes < 2:
            parts.append(
                "addr2line 侧在图中可解析的调用链节点少于 2 个，无法按栈序补 calls_direct 边。"
            )
        if not parts:
            return "未生成任何边；原因未单独记录。"
        return " ".join(parts)

    def _build_crash_graph(
        self,
        crash_summary: CrashSummary,
        crash_file: str,
        crash_func: CrashFunction,
        direct_call_crash_fun: List[CallChainFunction],
        pre_call_fun_in_same_parent_fun: List[CallChainFunction],
        use_same_var_related_fun: List[VariableFunction],
        sibling_member_func_in_same_class: List[RelatedFunction],
        thread_context: List[ThreadContext],
        os_type: str,
    ) -> CrashGraph:
        """
        基于已提取的函数/变量/线程信息，构建图结构视图：
        - nodes: 所有函数节点 + 变量节点（如有）
        - edges: 函数调用、变量关联、同类兄弟关系等
        - execution_paths: 以入口函数为视角的语义化执行路径
        - call_chain_from_add2line: 从 thread_context 派生的 add2line 调用链视图
        """
        nodes: Dict[str, GraphNode] = {}
        edges: List[GraphEdge] = []
        call_chain_from_code: List[ExecutionPath] = []
        call_chain_from_add2line: List[Dict[str, Any]] = []

        def _normalize_signature(sig: Optional[str], fallback_name: str) -> str:
            """
            归一化函数签名：
            - 优先使用 signature；若为空则退回到函数名
            - 去掉尾部的大括号 '{' 及其后面的空白，避免同一函数因是否带 '{' 被当成不同节点
            """
            key = sig or fallback_name or ""
            key = key.rstrip()
            if key.endswith("{"):
                key = key[:-1].rstrip()
            return key

        def func_node_id(name: str, file: Optional[str], signature: Optional[str] = None) -> str:
            """构造函数节点 ID：func|file|normalized_signature_or_name，避免重载函数仅按名称冲突。"""
            key = _normalize_signature(signature, name)
            return f"func|{(file or '')}|{key}"

        _graph_file_lines_cache: Dict[str, List[str]] = {}

        # 1. 函数节点：崩溃函数本身
        norm_cf_sig = _normalize_signature(crash_func.signature, crash_func.name)
        cf_id = func_node_id(crash_func.name, crash_file, norm_cf_sig)
        ss_cf = getattr(crash_func, "snippet_start_line", None)
        se_cf = getattr(crash_func, "snippet_end_line", None)
        if (ss_cf is None or se_cf is None) and crash_func.snippet and crash_file:
            loc_ss, loc_se = self._locate_snippet_lines_in_file(
                crash_file, crash_func.snippet, _graph_file_lines_cache
            )
            if loc_ss is not None:
                ss_cf, se_cf = loc_ss, loc_se
        nodes[cf_id] = GraphNode(
            id=cf_id,
            type="function",
            name=crash_func.name,
            file=crash_file,
            signature=norm_cf_sig,
            snippet=crash_func.snippet,
            snippet_start_line=ss_cf,
            snippet_end_line=se_cf,
        )

        def ensure_func_node(name: str, file: Optional[str], signature: Optional[str], snippet: Optional[List[str]]):
            norm_sig = _normalize_signature(signature, name)
            nid = func_node_id(name, file, norm_sig)
            ss: Optional[int] = None
            se: Optional[int] = None
            if snippet and file:
                ss, se = self._locate_snippet_lines_in_file(
                    file, snippet, _graph_file_lines_cache
                )
            if nid not in nodes:
                nodes[nid] = GraphNode(
                    id=nid,
                    type="function",
                    name=name,
                    file=file,
                    signature=norm_sig,
                    snippet=snippet,
                    snippet_start_line=ss,
                    snippet_end_line=se,
                )
            elif nodes[nid].snippet_start_line is None and ss is not None:
                nodes[nid] = replace(
                    nodes[nid],
                    snippet_start_line=ss,
                    snippet_end_line=se,
                )
            return nid

        def _is_plausible_function_signature(sig: Optional[str]) -> bool:
            s = (sig or "").strip()
            if not s:
                return False
            # 过滤控制流语句，避免把 "if (...)" 误当成函数签名
            if re.match(r"^(if|for|while|switch|catch)\b", s):
                return False
            # 隐式构造成员子对象：snippet 首行常为 struct/class 定义
            if re.match(r"^\s*(?:template\s*<[^>]+>\s*)?(?:struct|class)\s+\w+", s):
                return True
            # 过滤明显“语句级误命中”
            if ";" in s and "(" in s and ")" in s and "::" not in s:
                return False
            if "(" not in s or ")" not in s:
                return False
            return True

        code_roots_for_graph: List[str] = list(getattr(self, "current_code_roots", []) or [])
        if not code_roots_for_graph and getattr(self, "current_code_root", None):
            code_roots_for_graph = [str(getattr(self, "current_code_root"))]

        # 2. 直接调用崩溃函数的上层函数

        # 若能根据 OS / 平台信息推断本次崩溃实际走过的入口路径，则优先保留最匹配的平台入口；
        # 例如：os_type == "macos" 时，优先选择 desktop/main.cpp 而非 Harmony/iOS 入口。
        effective_direct_callers: List[CallChainFunction] = direct_call_crash_fun
        if os_type.lower() == "macos":
            mac_candidates = [
                f for f in direct_call_crash_fun
                if isinstance(f.file, str) and "/desktop/" in f.file
            ]
            if mac_candidates:
                effective_direct_callers = mac_candidates

        max_n = max(1, min(int(getattr(self, "max_static_call_chain_nodes", 5)), 128))
        min_n = max(1, min(int(getattr(self, "min_static_call_chain_nodes", 1)), max_n))
        layer_cache: Dict[Tuple[str, str], List[CallChainFunction]] = {}
        path_index = 0
        seen_path_keys: set = set()

        # 深度为 1：仅崩溃函数一层（无上游静态扩展）
        if max_n == 1:
            pk = (cf_id,)
            if pk not in seen_path_keys:
                seen_path_keys.add(pk)
                call_chain_from_code.append(
                    ExecutionPath(
                        id=f"path_{path_index}",
                        thread_id="unknown",
                        nodes=[cf_id],
                        description=None,
                    )
                )
                path_index += 1
        else:
            for f in effective_direct_callers:
                sig = f.snippet[0].strip() if f.snippet else None
                if not _is_plausible_function_signature(sig):
                    continue
                if not code_roots_for_graph:
                    nid = ensure_func_node(f.name, f.file, sig, f.snippet)
                    edges.append(GraphEdge(from_id=nid, to_id=cf_id, type="calls_direct"))
                    pk = (nid, cf_id)
                    if pk not in seen_path_keys:
                        seen_path_keys.add(pk)
                        call_chain_from_code.append(
                            ExecutionPath(
                                id=f"path_{path_index}",
                                thread_id="unknown",
                                nodes=[nid, cf_id],
                                description=None,
                            )
                        )
                        path_index += 1
                    continue

                ensure_func_node(f.name, f.file, sig, f.snippet)
                ancestors = self._expand_static_call_chain_prepend(
                    f, code_roots_for_graph, max_nodes_including_crash=max_n, layer_cache=layer_cache
                )
                path_ids: List[str] = []
                for anc in ancestors:
                    s0 = anc.snippet[0].strip() if anc.snippet else None
                    if not _is_plausible_function_signature(s0):
                        continue
                    aid = ensure_func_node(anc.name, anc.file, s0, anc.snippet)
                    path_ids.append(aid)
                if not path_ids:
                    continue
                full_chain = path_ids + [cf_id]
                if len(full_chain) < min_n:
                    continue
                path_key = tuple(full_chain)
                if path_key in seen_path_keys:
                    continue
                seen_path_keys.add(path_key)
                for i in range(len(full_chain) - 1):
                    edges.append(
                        GraphEdge(
                            from_id=full_chain[i],
                            to_id=full_chain[i + 1],
                            type="calls_direct",
                        )
                    )
                call_chain_from_code.append(
                    ExecutionPath(
                        id=f"path_{path_index}",
                        thread_id="unknown",
                        nodes=list(full_chain),
                        description=None,
                    )
                )
                path_index += 1

            if not call_chain_from_code and min_n <= 1:
                pk = (cf_id,)
                if pk not in seen_path_keys:
                    seen_path_keys.add(pk)
                    call_chain_from_code.append(
                        ExecutionPath(
                            id=f"path_{path_index}",
                            thread_id="unknown",
                            nodes=[cf_id],
                            description=None,
                        )
                    )
                    path_index += 1

        # 变量相关函数：函数节点 + 变量节点 + use_shared_var 边
        var_nodes: Dict[str, GraphNode] = {}

        # 变量声明缓存：var_name -> (file, signature_line)
        var_decl_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

        def var_node_id(var_name: str) -> str:
            return f"var|{var_name}"

        for vf in use_same_var_related_fun:
            sig = vf.snippet[0].strip() if vf.snippet else None
            fn_id = ensure_func_node(vf.name, vf.file, sig, vf.snippet)
            v_id = var_node_id(vf.variable)
            if v_id not in var_nodes:
                if vf.variable not in var_decl_cache:
                    # 优先使用 current_code_roots 全量搜索；必要时附加崩溃文件所在目录。
                    decl_roots: List[str] = list(getattr(self, "current_code_roots", []) or [])
                    try:
                        if crash_file:
                            d = os.path.dirname(crash_file)
                            if d and os.path.isdir(d) and os.path.abspath(d) not in decl_roots:
                                decl_roots.append(os.path.abspath(d))
                    except Exception:
                        pass
                    decl_file, decl_sig = self._find_variable_declaration(vf.variable, decl_roots or None)
                    var_decl_cache[vf.variable] = (decl_file, decl_sig)
                else:
                    decl_file, decl_sig = var_decl_cache[vf.variable]
                var_nodes[v_id] = GraphNode(
                    id=v_id,
                    type="variable",
                    name=vf.variable,
                    file=decl_file,
                    signature=decl_sig,
                    snippet=None,
                    role="crash_func_shared_var",
                )
            nodes.setdefault(v_id, var_nodes[v_id])
            edges.append(
                GraphEdge(
                    from_id=fn_id,
                    to_id=v_id,
                    type="use_shared_var",
                    relation=vf.relation,
                )
            )

        # 同类兄弟函数：补充生命周期/关联函数节点，供后续修复策略扩展可修改目标。
        # 注意：此前 sibling_member_func_in_same_class 已在上游提取，但未并入 graph，导致 03/05 缺少关键函数。
        for rf in sibling_member_func_in_same_class:
            if not isinstance(rf, RelatedFunction):
                continue
            sig = rf.snippet[0].strip() if rf.snippet else None
            if not _is_plausible_function_signature(sig):
                continue
            rid = ensure_func_node(rf.name, rf.file, sig, rf.snippet)
            if rid == cf_id:
                continue
            edges.append(
                GraphEdge(
                    from_id=rid,
                    to_id=cf_id,
                    type="same_class_brother",
                    relation=rf.description or rf.relation_type,
                )
            )

        # 5. 从 thread_context 构造 call_chain_from_add2line 视图（nodes 里放 GraphNode.id 序列），
        #    同时确保出现在调用链中的每个函数至少有一个对应的函数节点，尽量补充代码片段
        for tc in thread_context:
            thread_nodes: List[str] = []
            source_positions: List[int] = []
            details_list: List[Dict[str, Any]] = getattr(tc, "call_chain_frame_details", None) or []
            for idx_in_chain, fn in enumerate(tc.call_chain_from_add2line, 1):
                if not fn:
                    continue
                # 统一使用更稳健的 resolved_function 解析逻辑，避免模板/命名空间场景
                # 下 `split('::')[-1]` 把函数名误切成参数尾巴导致首帧丢失。
                simple_name = self._extract_function_name_from_resolved(fn)
                if not simple_name:
                    continue
                # 崩溃函数本身已作为节点，保持 file 一致
                if simple_name == crash_func.name:
                    nid = func_node_id(simple_name, crash_file, crash_func.signature)
                    ensure_func_node(simple_name, crash_file, crash_func.signature, crash_func.snippet)
                    thread_nodes.append(nid)
                    source_positions.append(idx_in_chain)
                else:
                    # 其他函数：仅当能在工程代码根下找到定义时，才创建函数节点并加入调用链视图，
                    # 避免像 _Z14signal_handleriP9__siginfoPv 这类仅存在于堆栈中的符号污染 graph.nodes。
                    if not code_roots_for_graph:
                        continue
                    file_path: Optional[str] = None
                    snippet: Optional[List[str]] = None
                    sig: Optional[str] = None

                    # 优先：addr2line 已给出工程内路径 + 行号时，与首帧崩溃函数相同方式抽取（与 regex 行为对齐，
                    # 避免仅靠全仓搜索 + tree-sitter/regex 差异导致漏节点）。
                    frame_detail: Optional[Dict[str, Any]] = None
                    if len(details_list) >= idx_in_chain:
                        frame_detail = details_list[idx_in_chain - 1]
                    if (
                        frame_detail
                        and (frame_detail.get("resolved_file") or "").strip()
                        and int(frame_detail.get("resolved_line") or 0) > 0
                    ):
                        cf_stack = self._extract_crash_function(
                            frame_detail.get("resolved_function", fn),
                            frame_detail["resolved_file"],
                            int(frame_detail["resolved_line"]),
                            code_roots_for_graph,
                        )
                        if cf_stack is not None and cf_stack.snippet:
                            file_path = self._find_source_file(
                                frame_detail["resolved_file"], code_roots_for_graph
                            ) or frame_detail["resolved_file"]
                            sig = _normalize_signature(cf_stack.signature, simple_name)
                            # 签名行偶发被宏/辅助调用污染时，用 snippet 首行（通常为真实函数定义行）兜底
                            cand0 = (cf_stack.snippet[0] or "").strip() if cf_stack.snippet else ""
                            if (
                                not _is_plausible_function_signature(sig)
                                or (simple_name and simple_name not in sig)
                            ) and cand0:
                                if _is_plausible_function_signature(cand0):
                                    sig = _normalize_signature(cand0, simple_name)
                                else:
                                    # cand0 若是注释/右花括号等无效行，则退回 resolved_function，
                                    # 避免在 05 中出现“/// 注释”或“}”这类伪函数签名。
                                    sig = _normalize_signature(
                                        frame_detail.get("resolved_function", fn), simple_name
                                    )
                            snippet = cf_stack.snippet

                    # 回退：全仓按符号名搜索定义（历史逻辑）
                    if not (file_path and snippet):
                        found = self._find_function_definition(fn, code_roots_for_graph)
                        if not found:
                            continue
                        file_path, snippet = found
                        if snippet:
                            sig = snippet[0].strip()
                    nid = func_node_id(simple_name, file_path, sig)
                    ensure_func_node(simple_name, file_path, sig, snippet)
                    thread_nodes.append(nid)
                    source_positions.append(idx_in_chain)

            call_chain_from_add2line.append(
                {
                    "thread_id": tc.thread_id,
                    "nodes": thread_nodes,
                    "source_positions": source_positions,
                }
            )

        # 静态 + 变量边之后是否已有 calls_direct（5b 之前快照，供补边与补全 call_chain_from_code）
        had_static_calls_direct = any(e.type == "calls_direct" for e in edges)

        # 5b. 静态分析未产生 calls_direct 时，按 add2line 栈序「候选」相邻帧，但仅在通过源码校验时连边：
        #     在外层函数中用语义分析（tree-sitter call_expression，失败则 snippet/行范围正则）确认存在对内层函数的调用。
        #     thread_nodes[0] 为崩溃帧，索引增大方向为向栈底延伸；边方向为 外层 -> 内层（tnodes[i] -> tnodes[i-1]）。
        verified_stack_pairs: set = set()
        if not had_static_calls_direct:
            for item in call_chain_from_add2line:
                tnodes = item.get("nodes") or []
                if len(tnodes) < 2:
                    continue
                tid = item.get("thread_id")
                for i in range(len(tnodes) - 1, 0, -1):
                    outer_id, inner_id = tnodes[i], tnodes[i - 1]
                    outer_node = nodes.get(outer_id)
                    inner_node = nodes.get(inner_id)
                    if not outer_node or not inner_node:
                        continue
                    if not self._verify_stack_caller_resolves_call_to_callee(outer_node, inner_node):
                        continue
                    verified_stack_pairs.add((outer_id, inner_id))
                    edges.append(
                        GraphEdge(
                            from_id=outer_id,
                            to_id=inner_id,
                            type="calls_direct",
                            thread_id=tid,
                        )
                    )

        # 5c. 静态未产出 calls_direct 时，call_chain_from_code 与 5b 一致：仅包含已通过校验的相邻 hop，
        #     从崩溃端向前取最长后缀（避免未校验的外层帧混进路径）。
        if not had_static_calls_direct:
            extended_paths: List[ExecutionPath] = []
            seen_stack_path_keys: set = set()
            stack_path_idx = 0
            for item in call_chain_from_add2line:
                tnodes = item.get("nodes") or []
                if len(tnodes) < 2:
                    continue
                tid_raw = item.get("thread_id")
                tid_str = "unknown" if tid_raw is None else str(tid_raw)
                chain_outer_to_crash = list(reversed(tnodes))
                verified_chain = self._longest_verified_suffix_chain(
                    chain_outer_to_crash, verified_stack_pairs
                )
                if len(verified_chain) < 2:
                    continue
                pk = tuple(verified_chain)
                if pk in seen_stack_path_keys:
                    continue
                seen_stack_path_keys.add(pk)
                extended_paths.append(
                    ExecutionPath(
                        id=f"path_{stack_path_idx}",
                        thread_id=tid_str,
                        nodes=verified_chain,
                        description=None,
                    )
                )
                stack_path_idx += 1
            if extended_paths:
                call_chain_from_code = extended_paths

        # 6. 基于代码的调用路径：优先第 2 步静态结果；若仅由 5c 从栈补全，则为 add2line 推导链路

        # 7. 对 edges 去重：基于 (from_id, to_id, type) 三元组
        deduped_edges: List[GraphEdge] = []
        seen_edge_keys: set = set()
        for e in edges:
            key = (e.from_id, e.to_id, e.type)
            if key in seen_edge_keys:
                continue
            seen_edge_keys.add(key)
            deduped_edges.append(e)

        add2line_max_nodes = 0
        for item in call_chain_from_add2line:
            add2line_max_nodes = max(add2line_max_nodes, len(item.get("nodes") or []))

        edges_empty_reason: Optional[str] = None
        if not deduped_edges:
            edges_empty_reason = self._format_graph_edges_empty_reason(
                max_n=max_n,
                direct_callers_count=len(direct_call_crash_fun or []),
                has_code_roots=bool(code_roots_for_graph),
                shared_var_relations=len(use_same_var_related_fun or []),
                add2line_max_nodes=add2line_max_nodes,
            )

        return CrashGraph(
            nodes=list(nodes.values()),
            edges=deduped_edges,
            call_chain_from_code=call_chain_from_code,
            call_chain_from_add2line=call_chain_from_add2line,
            edges_empty_reason=edges_empty_reason,
        )

    def _inject_flat_fields_from_graph(self, result_dict: Dict[str, Any]) -> None:
        """从 result_dict['graph'] 反推 direct_call_crash_fun、sibling_member_func_in_same_class、thread_context，
        写入 result_dict，供生成 05_ai_final_tip 的 agent 直接使用。"""
        graph = result_dict.get("graph") or {}
        nodes_list = graph.get("nodes") or []
        edges_list = graph.get("edges") or []
        call_chain_from_code = graph.get("call_chain_from_code") or []
        call_chain_from_add2line = graph.get("call_chain_from_add2line") or []
        crash_summary = result_dict.get("crash_summary") or {}
        crash_func = result_dict.get("crash_func") or {}
        crash_file = crash_summary.get("file") or "unknown"
        crash_name = crash_func.get("name") or ""
        crash_node_id = f"func|{crash_file}|{crash_name}"

        node_by_id: Dict[str, Dict[str, Any]] = {n.get("id"): n for n in nodes_list if n.get("id")}

        direct_call_crash_fun: List[Dict[str, Any]] = []
        seen_caller_ids: set = set()
        for path in call_chain_from_code:
            path_nodes = path.get("nodes") or []
            if len(path_nodes) >= 2 and path_nodes[-1] == crash_node_id:
                caller_id = path_nodes[0]
                if caller_id not in seen_caller_ids and caller_id in node_by_id:
                    seen_caller_ids.add(caller_id)
                    n = node_by_id[caller_id]
                    direct_call_crash_fun.append({
                        "name": n.get("name") or "",
                        "file": n.get("file") or "",
                        "snippet": n.get("snippet") or [],
                    })

        sibling_member_func_in_same_class: List[Dict[str, Any]] = []
        for e in edges_list:
            if e.get("type") != "same_class_brother":
                continue
            from_id = e.get("from_id")
            to_id = e.get("to_id")
            other_id = from_id if to_id == crash_node_id else (to_id if from_id == crash_node_id else None)
            if other_id and other_id in node_by_id:
                n = node_by_id[other_id]
                sibling_member_func_in_same_class.append({
                    "name": n.get("name") or "",
                    "file": n.get("file") or "",
                    "snippet": n.get("snippet") or [],
                    "relation_type": "same_class",
                    "description": "同一类中的兄弟函数",
                })

        thread_context: List[Dict[str, Any]] = []
        for item in call_chain_from_add2line:
            thread_context.append({
                "thread_id": item.get("thread_id") or "unknown",
                "call_chain_from_add2line": item.get("call_order_from_add2line") or [],
            })

        result_dict["direct_call_crash_fun"] = direct_call_crash_fun
        result_dict["sibling_member_func_in_same_class"] = sibling_member_func_in_same_class
        result_dict["thread_context"] = thread_context

    def _is_variable_usage_line(self, line: str, variable_name: str) -> bool:
        """检查行是否是变量使用（而不是函数定义）"""
        line = line.strip()
        
        # 排除函数定义行
        if self._is_function_definition_line(line):
            return False
        
        # 排除注释行
        if line.startswith('//') or line.startswith('/*') or line.startswith('*'):
            return False
        
        # 排除包含函数调用的行（如 sigaction 调用）
        if any(func in line for func in ['sigaction', 'signal', 'printf', 'cout', 'endl']):
            return False
        
        # 检查是否是变量使用
        patterns = [
            rf'\b{re.escape(variable_name)}\s*=',  # var = value
            rf'\b{re.escape(variable_name)}\s*->',  # var->member
            rf'\b{re.escape(variable_name)}\s*\.',  # var.member
            rf'\b{re.escape(variable_name)}\s*\[',  # var[index]
            rf'=\s*{re.escape(variable_name)}\b',  # = var
            rf'\(\s*{re.escape(variable_name)}\s*\)',  # (var)
        ]
        
        for pattern in patterns:
            if re.search(pattern, line):
                return True
        
        return False
    
    def _determine_variable_relation(self, line: str, variable_name: str) -> str:
        """确定变量使用关系"""
        line = line.strip()
        
        # 指针解引用赋值 - 如 tail->next = new_node
        if f'{variable_name}->' in line and '=' in line:
            return 'read'  # tail 是被读取的
        
        # 删除操作
        if any(keyword in line for keyword in ['delete', 'free']):
            return 'delete'
        
        # 赋值操作 - 变量在赋值号左边
        if '=' in line:
            # 处理多重赋值：head = tail = new_node
            assignments = line.split('=')
            for i, assignment in enumerate(assignments[:-1]):  # 除了最后一个
                if variable_name in assignment.strip():
                    return 'write'
            # 最后一个赋值右边的变量是读取
            if variable_name in assignments[-1].strip():
                return 'read'
        
        # 读取操作
        if variable_name in line:
            return 'read'
        
        return 'unknown'

    def _find_variable_declaration(self, var_name: str, code_roots: Optional[List[str]]) -> Tuple[Optional[str], Optional[str]]:
        """
        在代码根目录中尝试找到变量声明所在的文件和声明行文本，用于填充变量节点的 file/signature。
        这是一个尽量简单且稳健的启发式实现：
          - 仅遍历 code_root 下的 C/C++ 源文件和头文件（.c/.cc/.cpp/.cxx/.h/.hpp/.hh）
          - 查找同时满足以下条件的行：
              * 非空且非注释
              * 以分号结尾
              * 含有变量名 var_name
              * 不像函数定义/声明（var_name 之后紧跟 '('）
        """
        if not code_roots:
            return None, None

        def _looks_like_function_proto(line: str, name: str) -> bool:
            """
            简单判断 name 在该行是否作为函数名出现（后面紧跟 '('），避免依赖容易出错的正则。
            """
            idx = line.find(name)
            if idx == -1:
                return False
            j = idx + len(name)
            while j < len(line) and line[j].isspace():
                j += 1
            return j < len(line) and line[j] == "("

        try:
            for code_root in code_roots:
                for root, dirs, files in os.walk(code_root):
                    dirs[:] = [d for d in dirs if not self._should_skip_directory(d)]
                    for file in files:
                        # 仅考虑典型源码/头文件，避免无关文件
                        if not file.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh")):
                            continue
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                                for line in f:
                                    stripped = line.strip()
                                    if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
                                        continue
                                    if var_name not in stripped:
                                        continue
                                    # 典型函数原型：var_name 后紧跟 '('，排除
                                    if _looks_like_function_proto(stripped, var_name):
                                        continue
                                    # 仅考虑以分号结尾的行
                                    if not stripped.endswith(";"):
                                        continue
                                    return file_path, stripped
                        except Exception:
                            continue
        except Exception:
            return None, None
        return None, None
    
    def _get_variable_declaration_snippet(
        self,
        decl_file: str,
        var_name: str,
        max_context_lines: int = 3,
    ) -> Optional[List[str]]:
        """
        根据变量声明所在文件和变量名，抽取一小段带上下文的声明代码片段。
        
        设计目标：
        - 至少包含真正声明该变量的那一行；
        - 尝试带上前后若干行上下文，便于理解（例如类内成员声明块）；
        - 只在确认匹配到声明行时才返回片段，否则返回 None。
        """
        if not decl_file:
            return None
        
        try:
            with open(decl_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            return None
        
        # 在文件中查找第一个符合“声明”特征的行
        def _looks_like_function_proto(line: str, name: str) -> bool:
            idx = line.find(name)
            if idx == -1:
                return False
            j = idx + len(name)
            while j < len(line) and line[j].isspace():
                j += 1
            return j < len(line) and line[j] == "("

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
                continue
            
            # 排除明显的函数定义/声明：变量名后紧跟 '('
            if _looks_like_function_proto(stripped, var_name):
                continue
            
            # 典型的声明语句：以分号结尾且包含变量名（此处不再依赖正则的单词边界）
            if stripped.endswith(";") and var_name in stripped:
                start = max(0, idx - max_context_lines)
                end = min(len(lines), idx + max_context_lines + 1)
                return [l.rstrip("\n") for l in lines[start:end]]
        
        return None
    
    def _contains_function_call(self, line: str, function_name: str) -> bool:
        """检查行是否包含函数调用"""
        # 提取函数名（去掉类名部分和签名信息）
        if '::' in function_name:
            simple_function_name = function_name.split('::')[-1]
        else:
            simple_function_name = function_name
        
        # 进一步清理函数名，去掉签名信息
        if '(' in simple_function_name:
            simple_function_name = simple_function_name.split('(')[0]
        
        # 函数调用检测模式
        patterns = [
            rf'\b{re.escape(simple_function_name)}\s*\(',  # function_name(
            rf'->\s*{re.escape(simple_function_name)}\s*\(',  # ->function_name(
            rf'\.\s*{re.escape(simple_function_name)}\s*\(',  # .function_name(
            rf'\b{re.escape(function_name)}\s*\(',  # Class::function_name(
        ]
        
        for pattern in patterns:
            if re.search(pattern, line):
                return True
        return False
    
    def _extract_function_name_at_line(self, lines: List[str], line_number: int) -> Optional[str]:
        """提取指定行的函数名"""
        if line_number <= 0 or line_number > len(lines):
            return None

        # tree-sitter 路径：直接定位“该行所属函数定义”，再从签名里提取函数名 token。
        if self.code_parser_backend == "tree_sitter" and self._ts_parser is not None:
            src = "\n".join(lines or [])
            sig, _ = self._ts_extract_signature_and_body(src, line_number - 1, None)
            if sig:
                m = re.search(r"([~A-Za-z_]\w*)\s*\(", sig)
                if m:
                    cand = m.group(1)
                    if cand and cand not in {"if", "for", "while", "switch", "catch", "else", "return"}:
                        return cand

        # 简化策略：从目标行向上扫描一段距离，直接按常见函数定义模式匹配，
        # 不再依赖大括号计数，增强对简单 free 函数 / main 等场景的鲁棒性。
        max_lookback = 50
        start = max(0, line_number - 1)
        end = max(0, line_number - max_lookback)

        function_patterns = [
            # 普通/成员函数
            r'(\w+::\w+)\s*\([^)]*\)\s*:',                  # Class::function_name( ... ) :
            r'(\w+::\w+)\s*\([^)]*\)\s*\{',                 # Class::function_name( ... ) {
            r'(\w+::\w+)\s*\([^)]*\)\s*$',                  # Class::function_name( ... )
            r'(\w+)\s+(\w+::\w+)\s*\([^)]*\)\s*\{',         # return_type Class::function_name(
            r'(\w+)\s+(\w+::\w+)\s*\([^)]*\)\s*$',          # return_type Class::function_name(
            r'(\w+)\s+(\w+)\s*\([^)]*\)\s*\{',              # return_type function_name(
            r'(\w+)\s+(\w+)\s*\([^)]*\)\s*$',               # return_type function_name(
            r'void\s+(\w+)\s*\([^)]*\)\s*\{',               # void function_name(
            r'void\s+(\w+)\s*\([^)]*\)\s*$',                # void function_name(
            r'int\s+(\w+)\s*\([^)]*\)\s*\{',                # int function_name(
            r'int\s+(\w+)\s*\([^)]*\)\s*$',                 # int function_name(
            # C++ 运算符重载：Class::operator...(...)
            r'([A-Za-z_][A-Za-z_0-9:]*::operator[^\s(]*)\s*\(',  # CVStringHash::operator(
        ]

        for i in range(start, end - 1, -1):
            line = lines[i].strip()
            if not line or line.startswith("//") or line.startswith("/*"):
                continue
            for pattern in function_patterns:
                match = re.search(pattern, line)
                if match:
                    keywords = {"if", "for", "while", "switch", "catch", "else", "return"}
                    if len(match.groups()) >= 2:
                        candidate = match.group(2)
                        if candidate in keywords:
                            continue
                        return candidate
                    elif len(match.groups()) >= 1:
                        candidate = match.group(1)
                        if candidate in keywords:
                            continue
                        return candidate

        return None
    
    def _analyze_thread_context(self, add2line_data: Dict[str, Any]) -> List[ThreadContext]:
        """分析线程上下文"""
        logger.info("分析线程上下文")
        thread_contexts = []
        
        # 从add2line数据中提取线程信息
        resolved_frames = add2line_data.get("resolved_frames", [])
        
        # 提取崩溃线程信息（基于 add2line 符号化结果构造的简单调用链）
        crash_thread_id = "unknown"
        crash_call_chain = []
        frame_details: List[Dict[str, Any]] = []
        
        for frame in resolved_frames:
            resolved_function = frame.get("resolved_function", "")
            if resolved_function:
                crash_call_chain.append(resolved_function)
                frame_details.append(
                    {
                        "resolved_function": resolved_function,
                        "resolved_file": frame.get("resolved_file", "") or "",
                        "resolved_line": int(frame.get("resolved_line") or 0),
                    }
                )
        
        # 创建崩溃线程上下文
        if crash_call_chain:
            thread_contexts.append(ThreadContext(
                thread_id=crash_thread_id,
                call_chain_from_add2line=crash_call_chain,
                call_chain_frame_details=frame_details if frame_details else None,
            ))

        return thread_contexts
    
    def _find_thread_functions(self, resolved_frames: List[Dict[str, Any]]) -> List[str]:
        """查找线程相关函数"""
        thread_functions = []
        
        for frame in resolved_frames:
            resolved_function = frame.get("resolved_function", "")
            if resolved_function and isinstance(resolved_function, str):
                if any(pattern in resolved_function.lower() for pattern in 
                      ['thread', 'worker', 'handler', 'callback', 'corruption']):
                    thread_functions.append(resolved_function)
        
        return thread_functions
    
    def _find_related_functions_in_class(self, crash_function_name: str, crash_file: str, code_roots: List[str]) -> List[RelatedFunction]:
        """查找同一类中的相关函数"""
        logger.info(f"查找同一类中的相关函数: {crash_function_name} 在 {crash_file}")
        related_functions = []
        
        # 提取类名（支持 mangled name）
        class_name = self._extract_class_name_from_resolved(crash_function_name)
        if not class_name:
            logger.debug(f"函数非类成员或无法提取类名，跳过同类函数扩展: {crash_function_name}")
            return related_functions
        
        logger.info(f"提取类名: {class_name}")
        
        # 查找源文件
        source_file = self._find_source_file(crash_file, code_roots)
        if not source_file:
            logger.warning(f"未找到源文件: {crash_file}")
            return related_functions
        
        try:
            with open(source_file, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                content = '\n'.join(lines)
            
            # 在整个文件中查找所有该类的成员函数（不限制在类定义范围内）
            # 因为 C++ 中成员函数可以在类外定义
            logger.info(f"在整个文件中查找 {class_name} 类的成员函数")
            
            # 提取崩溃函数的简单函数名（用于排除）
            crash_func_simple = self._extract_function_name_from_resolved(crash_function_name)
            found_functions = set()
            
            # 查找所有成员函数定义
            function_patterns = [
                rf'([\w:\<\>\,\s\*&\~]+)\s+{re.escape(class_name)}::([~]?\w+)\s*\([^)]*\)\s*(?:{{|:)',  # return_type Class::function(...) { or :
                rf'void\s+{re.escape(class_name)}::([~]?\w+)\s*\([^)]*\)\s*(?:{{|:)',  # void Class::function(...) {
                rf'{re.escape(class_name)}::([~]?\w+)\s*\([^)]*\)\s*(?:{{|:)',  # Class::function(...) {
            ]

            def _snippet_matches_member_signature(
                snippet_lines: List[str], expected_class: str, expected_func: str
            ) -> bool:
                """校验提取片段首行是否真的是 expected_class::expected_func，避免跨函数误抽取。"""
                first_non_empty = ""
                for s in snippet_lines or []:
                    t = (s or "").strip()
                    if t:
                        first_non_empty = t
                        break
                if not first_non_empty:
                    return False
                sig_re = re.compile(
                    rf"\b{re.escape(expected_class)}\s*::\s*{re.escape(expected_func)}\s*\("
                )
                return bool(sig_re.search(first_non_empty))
            
            for i, line in enumerate(lines):
                for pattern in function_patterns:
                    match = re.search(pattern, line)
                    if match:
                        # 提取函数名
                        if len(match.groups()) >= 2:
                            func_name = match.group(2)
                        else:
                            func_name = match.group(1)
                        
                        # 跳过崩溃函数本身
                        if func_name == crash_func_simple or func_name in crash_function_name:
                            continue
                        
                        # 跳过已找到的函数
                        if func_name in found_functions:
                            continue
                        
                        found_functions.add(func_name)
                        
                        # 提取函数完整代码
                        func_code = self._extract_full_function_code(lines, i)
                        if func_code:
                            function_snippet = [line.rstrip() for line in func_code.split('\n') if line.strip()]
                            if not _snippet_matches_member_signature(function_snippet, class_name, func_name):
                                logger.debug(
                                    "跳过疑似误匹配的同类函数片段: expected=%s::%s first_line=%s",
                                    class_name,
                                    func_name,
                                    function_snippet[0] if function_snippet else "",
                                )
                                continue
                            # 分析函数类型和相关性
                            relation_type, description = self._analyze_function_relation(func_code, crash_function_name)
                            # 应用代码片段截断
                            if self.max_code_length > 0:
                                function_snippet = self._truncate_snippet(function_snippet)

                            related_functions.append(RelatedFunction(
                                name=f"{class_name}::{func_name}",
                                file=source_file,
                                snippet=function_snippet,
                                relation_type=relation_type,
                                description=description
                            ))
                            logger.info(f"找到相关函数: {class_name}::{func_name} ({relation_type}) 在行 {i+1}")
            
        except Exception as e:
            logger.error(f"查找相关函数失败: {e}")
        
        return related_functions
    
    def _find_class_end(self, content: str, class_start: int) -> int:
        """查找类定义的结束位置"""
        brace_count = 0
        found_opening_brace = False
        
        for i in range(class_start, len(content)):
            char = content[i]
            if char == '{':
                brace_count += 1
                found_opening_brace = True
            elif char == '}':
                brace_count -= 1
                if found_opening_brace and brace_count == 0:
                    return i + 1
        
        return -1
    
    def _analyze_function_relation(self, func_code: str, crash_function_name: str) -> Tuple[str, str]:
        """分析函数与崩溃函数的关系"""
        if not func_code or not isinstance(func_code, str):
            return "unknown", "无法分析函数关系"
        func_code_lower = func_code.lower()
        
        # 检查内存操作
        if any(keyword in func_code_lower for keyword in ['delete', 'free', 'new', 'malloc']):
            if 'delete' in func_code_lower or 'free' in func_code_lower:
                return "memory_operation", "此函数包含内存释放操作，可能与崩溃函数存在内存竞争"
            elif 'new' in func_code_lower or 'malloc' in func_code_lower:
                return "memory_operation", "此函数包含内存分配操作，可能与崩溃函数存在内存竞争"
        
        # 检查线程操作
        if any(keyword in func_code_lower for keyword in ['thread', 'detach', 'join', 'mutex', 'lock']):
            return "thread_operation", "此函数包含线程或锁操作，可能与崩溃函数存在线程安全问题"
        
        # 检查是否使用相同的变量（如 tail, head, next 等）
        common_vars = ['tail', 'head', 'next', 'prev', 'current', 'node']
        if any(var in func_code_lower for var in common_vars):
            return "same_variable", "此函数使用与崩溃函数相同的变量，可能存在数据竞争"
        
        # 默认：同一类中的函数
        return "same_class", "此函数与崩溃函数在同一类中，可能共享数据或存在关联"

    def _code_context_phase_timeout_extra_top_level(self) -> Dict[str, Any]:
        """整阶段超时时附加的顶层字段：标志 + 避免再次超时的可操作建议（供 CLI/插件展示）。"""
        return {
            "code_context_phase_timed_out": True,
            "code_context_phase_timeout_avoidance_hints": [
                "将 --code-root 收窄到与崩溃栈相关的子工程或子目录，避免在超大仓库上做全量遍历与解析。",
                "使用 --include-subdirs 仅包含业务源码目录（如 src、app），或使用 --exclude-dirs 排除 third_party、build、out、test、generated 等大目录。",
                "适当降低静态分析规模：减小 --max-static-call-chain-depth、--max-direct-callers、--max-shared-var-related-functions，以缩短调用链与图构建时间。",
                "在可接受范围内提高 --code-context-timeout-sec；CLI 下该值会同时作为源文件定位与第三步整阶段预算传入，放宽可缓解「找文件」与「静态扫描」两类耗时。",
                "若仅需崩溃点附近片段且可接受启发式解析，可尝试 --code-parser-backend regex，通常比 tree-sitter 全量解析更轻（精度与误报需自行权衡）。",
            ],
        }

    def _finalize_code_content_provider_json(
        self,
        crash_summary: CrashSummary,
        crash_func: CrashFunction,
        graph: CrashGraph,
        extraction_warnings: List[str],
        resolved_file: str,
        resolved_function: str,
        extra_top_level: Optional[Dict[str, Any]] = None,
    ) -> str:
        """将 CrashAnalysisData 规范化为与历史一致的 JSON 字符串（供正常结束与超时截断复用）。"""
        crash_analysis_data = CrashAnalysisData(
            crash_summary=crash_summary,
            crash_func=crash_func,
            graph=graph,
        )
        result_dict = asdict(crash_analysis_data)
        if extraction_warnings:
            result_dict["extraction_warnings"] = extraction_warnings
        cs = result_dict.get("crash_summary") or {}
        cf = result_dict.get("crash_func") or {}
        if isinstance(cs, dict) and isinstance(cf, dict):
            src = cf.get("crash_location_source") or cs.get("crash_location_source")
            if src is not None:
                cs["crash_location_source"] = src
            note = cf.get("crash_line_note") or cs.get("crash_line_note")
            if note is not None:
                cs["crash_line_note"] = note
            file_path = resolved_file or ""
            func_str = cf.get("signature") or resolved_function
            if file_path and func_str:
                cs["node_id"] = f"func|{file_path}|{func_str}"
                cs.pop("file", None)
                cs.pop("function", None)
            result_dict["crash_summary"] = cs
            result_dict.pop("crash_func", None)

        graph_dict = result_dict.get("graph") or {}
        nodes_list = graph_dict.get("nodes") or []
        edges_list = graph_dict.get("edges") or []
        call_chain_from_code = graph_dict.get("call_chain_from_code") or []

        for n in nodes_list:
            try:
                if n.get("type") == "variable":
                    n.pop("snippet", None)
                if n.get("type") == "function":
                    n.pop("role", None)
                    n.pop("name", None)
            except AttributeError:
                continue

        for e in edges_list:
            try:
                if e.get("type") == "calls_direct":
                    e.pop("relation", None)
            except AttributeError:
                continue

        for path in call_chain_from_code:
            try:
                path.pop("description", None)
            except AttributeError:
                continue

        call_chain_from_code_res = "ok" if call_chain_from_code else "not_found"
        graph_dict["call_chain_from_code_res"] = call_chain_from_code_res

        graph_dict["nodes"] = nodes_list
        graph_dict["edges"] = edges_list
        graph_dict["call_chain_from_code"] = call_chain_from_code
        graph_dict["stack_semantic_hints"] = getattr(self, "_stack_semantic_hints", [])
        graph_dict["stack_kept_original_indices"] = getattr(self, "_stack_kept_original_indices", [])
        graph_dict["stack_function_symbols"] = getattr(self, "_stack_function_symbols", [])
        if edges_list:
            graph_dict.pop("edges_empty_reason", None)
        elif not graph_dict.get("edges_empty_reason"):
            graph_dict["edges_empty_reason"] = "未生成任何边；原因未单独记录。"
        result_dict["graph"] = graph_dict
        result_dict["code_parser_backend"] = self.code_parser_backend
        if extra_top_level:
            result_dict.update(extra_top_level)

        json_result = json.dumps(result_dict, ensure_ascii=False, indent=2)

        logger.info("=" * 60)
        logger.info("搜索性能统计:")
        logger.info(f"  扫描文件数: {self.search_stats['files_scanned']}")
        logger.info(f"  读取文件数: {self.search_stats['files_read']}")
        logger.info(f"  跳过(文件过大): {self.search_stats['files_skipped_size']}")
        logger.info(f"  跳过(排除目录): {self.search_stats['files_skipped_excluded']}")
        logger.info(f"  跳过(扩展名): {self.search_stats['files_skipped_extension']}")
        logger.info(f"  总搜索耗时: {self.search_stats['search_time']:.2f}秒")
        logger.info("=" * 60)

        return json_result

    def _minimal_code_context_on_phase_timeout(
        self, add2line_data: Dict[str, Any], detail: str
    ) -> str:
        """超时发生在崩溃摘要/函数提取之前时的降级 JSON。"""
        frames = add2line_data.get("resolved_frames") or []
        f0 = frames[0] if frames else {}
        rf = (f0.get("resolved_function") or "unknown").strip() or "unknown"
        rfile = (f0.get("resolved_file") or "").strip()
        try:
            rline = int(f0.get("resolved_line") or 0)
        except (TypeError, ValueError):
            rline = 0
        addr = str(f0.get("address") or "")
        crash_summary = CrashSummary(
            file=rfile or "N/A",
            function=rf,
            crash_line_number=max(0, rline),
            stack_address=addr,
            error_type="SIGSEGV",
            thread_id="unknown",
            crash_location_source="from_add2line",
            crash_line_note="代码上下文整阶段超时，崩溃帧信息可能不完整。",
            crash_line_code=None,
        )
        fn_token = self._extract_function_name_from_resolved(rf) or "unknown"
        crash_func = CrashFunction(
            name=fn_token,
            signature=rf,
            snippet=["（代码上下文整阶段超时，未完成源码提取）"],
            crash_line="",
            crash_line_note="超时截断",
            snippet_scope="error",
        )
        graph = CrashGraph(
            nodes=[],
            edges=[],
            call_chain_from_code=[],
            call_chain_from_add2line=[],
            edges_empty_reason="代码上下文整阶段超时，已跳过后续静态分析。",
        )
        warn = (
            f"代码上下文整阶段超时（{getattr(self, 'code_context_timeout_sec', 0):.0f}s）: {detail}"
        )
        return self._finalize_code_content_provider_json(
            crash_summary,
            crash_func,
            graph,
            [warn],
            rfile or "",
            rf,
            extra_top_level=self._code_context_phase_timeout_extra_top_level(),
        )

    def code_content_provider(self, add2line_json: str, code_root: Union[str, List[str], None],
                             exclude_dirs: Optional[List[str]] = None,
                             include_subdirs: Optional[List[str]] = None) -> str:
        """
        代码内容提供工具 - 重构版本 V2

        Args:
            add2line_json (str): add2line_resolver工具的输出JSON内容
            code_root: 单个代码根目录路径，或多个根目录的列表（顺序 = 查找优先级）
            
        Returns:
            str: JSON格式的结构化崩溃分析数据
        """
        try:
            logger.info("开始生成结构化崩溃分析数据...")
            
            # 更新过滤设置（如果提供了参数）
            if exclude_dirs is not None:
                self.exclude_dirs.update(exclude_dirs)
            if include_subdirs is not None:
                if self.include_subdirs is None:
                    self.include_subdirs = set(include_subdirs)
                else:
                    self.include_subdirs.update(include_subdirs)
            
            # 重置搜索统计
            self.search_stats = {
                'files_scanned': 0,
                'files_skipped_size': 0,
                'files_skipped_excluded': 0,
                'files_skipped_extension': 0,
                'files_read': 0,
                'search_time': 0.0
            }
            
            logger.info(f"搜索配置: 排除目录={sorted(self.exclude_dirs)[:10]}..., 包含子目录={self.include_subdirs}")
            
            # 解析add2line输出
            add2line_data = json.loads(add2line_json)
            
            # 检查必要字段
            if "resolved_frames" not in add2line_data:
                raise ValueError("JSON中缺少resolved_frames字段")
            
            resolved_frames = add2line_data["resolved_frames"]
            os_type = add2line_data.get("os_type", "unknown")
            
            # 检查代码根目录（统一解析为绝对路径，后续所有扫描/索引统一使用绝对路径，避免相对/绝对混用导致重复节点）
            code_roots_abs_strs = _normalize_code_roots_arg(code_root)
            if not code_roots_abs_strs:
                raise ValueError("请至少指定一个有效的代码根目录（code_root）")
            for p in code_roots_abs_strs:
                if not Path(p).exists():
                    raise ValueError(f"代码根目录不存在: {p}")
                if not Path(p).is_dir():
                    raise ValueError(f"指定路径不是目录: {p}")

            # 记录当前代码根目录（绝对路径），供后续图构建/查找函数定义时使用
            self.current_code_roots = list(code_roots_abs_strs)
            self.current_code_root = code_roots_abs_strs[0]

            logger.info(f"代码根目录(共 {len(code_roots_abs_strs)} 个): {code_roots_abs_strs}")
            logger.info(f"检测到 {len(resolved_frames)} 个解析后的堆栈帧")

            self._cc_stage_crash_summary = None
            self._cc_stage_crash_func = None
            self._session_reset_timeouts_for_code_content_phase()
            self._code_context_phase_start()
            
            # 如果提供了代码根目录，过滤掉不在代码目录中的堆栈帧
            # 使用 _find_source_file 来查找实际文件路径（支持索引服务）
            original_first_frame = resolved_frames[0] if resolved_frames else None
            if code_roots_abs_strs:
                filtered_frames = []
                kept_original_indices: List[int] = []
                semantic_hints: List[Dict[str, Any]] = []
                skipped_count = 0
                
                for original_idx, frame in enumerate(resolved_frames):
                    self._code_context_phase_check("filter_stack_frames")
                    resolved_file = frame.get("resolved_file", "")
                    if not resolved_file:
                        # 如果没有文件路径，保留该帧（可能是系统库等）
                        filtered_frames.append(frame)
                        kept_original_indices.append(original_idx)
                        continue

                    # 外部路径快速短路：
                    # 例如 NDK/STL 头文件（.../Android/sdk/.../sysroot/usr/include/...），
                    # 不在当前 code_root 中查找，也不保留到 03，避免噪音污染。
                    if self._is_external_path(resolved_file, code_roots_abs_strs):
                        self._append_external_frame_semantic_hint(
                            semantic_hints, original_idx, frame
                        )
                        skipped_count += 1
                        logger.debug(f"外部路径帧已丢弃: {resolved_file}")
                        continue
                    
                    # 使用 _find_source_file 查找实际文件路径（支持索引服务）
                    actual_file_path = self._find_source_file(resolved_file, code_roots_abs_strs)
                    
                    if actual_file_path:
                        # 找到实际文件路径，检查是否在代码根目录下
                        try:
                            actual_path = Path(actual_file_path).resolve()
                            try:
                                if not _file_under_any_project_root(actual_path, code_roots_abs_strs):
                                    raise ValueError("not under project roots")
                                # 文件在代码目录中，保留（并更新 frame 中的路径）
                                frame["resolved_file"] = actual_file_path
                                filtered_frames.append(frame)
                                kept_original_indices.append(original_idx)

                                # 工程内隐式默认 special member（如默认析构）提示，不入主函数节点
                                is_implicit, decl_window, member_hints, operation_text = self._is_implicit_default_special_member(
                                    frame.get("resolved_function", ""),
                                    actual_file_path,
                                    int(frame.get("resolved_line", 0) or 0),
                                )
                                if is_implicit:
                                    semantic_hints.append(
                                        {
                                            "hint_kind": "implicit_default_special_member",
                                            "severity": "info",
                                            "source_frame_index": original_idx,
                                            "resolved_function": frame.get("resolved_function", ""),
                                            "resolved_file": actual_file_path,
                                            "resolved_line": frame.get("resolved_line", 0),
                                            "summary": "隐式默认 special member（无显式函数体），仅作为生命周期语义提示。",
                                            "details": {
                                                "declaration_window": decl_window,
                                                "member_type_hints": member_hints,
                                                "operation_kind": "default_destructor",
                                                "operation_text": operation_text,
                                            },
                                            "attach_policy": "attach_to_next_project_frame",
                                        }
                                    )
                                self._maybe_append_addr2line_line_unknown_hint(
                                    semantic_hints, original_idx, frame, actual_file_path
                                )
                            except ValueError:
                                # 文件不在代码目录中，跳过；仍写入外部语义 hint，避免 05 帧号断档
                                skipped_count += 1
                                self._append_external_frame_semantic_hint(
                                    semantic_hints, original_idx, frame
                                )
                                logger.debug(f"跳过不在代码目录中的堆栈帧: {actual_file_path}")
                        except (OSError, ValueError) as e:
                            # 路径解析失败，但找到了文件，保留该帧
                            logger.debug(f"无法解析文件路径 {actual_file_path}: {e}，保留该帧")
                            frame["resolved_file"] = actual_file_path
                            filtered_frames.append(frame)
                            kept_original_indices.append(original_idx)
                            self._maybe_append_addr2line_line_unknown_hint(
                                semantic_hints, original_idx, frame, actual_file_path
                            )
                    else:
                        # 未找到文件，跳过该帧；若有符号仍写 hint（常见于工具链路径）
                        skipped_count += 1
                        if self._is_cpp_native_symbol(str(frame.get("resolved_function") or "")):
                            self._append_external_frame_semantic_hint(
                                semantic_hints, original_idx, frame
                            )
                        logger.debug(f"未找到源文件，跳过堆栈帧: {resolved_file}")
                
                if skipped_count > 0:
                    logger.info(f"已过滤 {skipped_count} 个不在代码目录中的堆栈帧（剩余 {len(filtered_frames)} 个）")
                
                # 若真实崩溃帧（栈顶 frame 0）被过滤掉（atos 常将用户库首帧错误解析为系统符号如 sstream），
                # 则用 mangled 名在源码中按函数名定位，补回为“真实崩溃帧”，避免误用后续帧。
                if original_first_frame and (
                    not filtered_frames or filtered_frames[0].get("address") != original_first_frame.get("address")
                ):
                    mangled = original_first_frame.get("function") or ""
                    simple_name = self._extract_simple_name_from_mangled(mangled)
                    if simple_name:
                        loc = self._find_function_definition_location(simple_name, code_roots_abs_strs)
                        if loc:
                            file_path, line_no = loc
                            raw_resolved_file = original_first_frame.get("resolved_file") or ""
                            # 根据原始解析结果区分补救原因：
                            # - 若 resolved_file 为空或为占位符（如 "?", "??", "???", "??:0" 等），更可能是库缺少调试信息或符号不完整
                            # - 否则，一般视为被解析到了库/STL 等实现上
                            rescue_reason = "library_impl"
                            placeholder_candidates = {"?", "<unknown>", "<invalid>", "??"}
                            if (
                                not raw_resolved_file
                                or raw_resolved_file in placeholder_candidates
                                or re.fullmatch(r"\?+", raw_resolved_file.strip())
                            ):
                                rescue_reason = "missing_debug_info"
                            rescue_frame = {
                                "address": original_first_frame.get("address", ""),
                                "function": mangled,
                                "file": None,
                                "line": None,
                                "module": original_first_frame.get("module", ""),
                                "resolved_function": f"{simple_name}()",
                                "resolved_file": file_path,
                                "resolved_line": line_no,
                                "crash_location_source": "from_log_deduce",
                                "rescue_reason": rescue_reason,
                            }
                            filtered_frames.insert(0, rescue_frame)
                            logger.info(f"已按 mangled 名补回真实崩溃帧: {simple_name}() 在 {file_path}:{line_no}")
                
                # 更新 resolved_frames 和 add2line_data
                resolved_frames = filtered_frames
                add2line_data["resolved_frames"] = filtered_frames

                # 为语义提示计算挂载位置；render_order_pos 始终按原始堆栈索引（1-based）供 05 排序
                if semantic_hints:
                    for h in semantic_hints:
                        src = int(h.get("source_frame_index", -1))
                        if src >= 0:
                            h["render_order_pos"] = int(src) + 1
                    if kept_original_indices:
                        for h in semantic_hints:
                            src = int(h.get("source_frame_index", -1))
                            target_filtered_idx = None
                            for i, orig in enumerate(kept_original_indices):
                                if orig > src:
                                    target_filtered_idx = i
                                    break
                            if target_filtered_idx is None:
                                prev = [i for i, orig in enumerate(kept_original_indices) if orig < src]
                                target_filtered_idx = prev[-1] if prev else 0
                            h["attach_target_filtered_index"] = target_filtered_idx
                self._stack_semantic_hints = semantic_hints
                self._stack_kept_original_indices = kept_original_indices
                stack_syms: List[Dict[str, Any]] = []
                for filtered_i, frame in enumerate(filtered_frames, 1):
                    orig_pos = (
                        int(kept_original_indices[filtered_i - 1]) + 1
                        if filtered_i - 1 < len(kept_original_indices)
                        else filtered_i
                    )
                    fn = (frame.get("resolved_function") or frame.get("function") or "").strip()
                    if not fn:
                        continue
                    stack_syms.append(
                        {
                            "render_order_pos": orig_pos,
                            "function": fn,
                            "module": (frame.get("module") or "").strip(),
                        }
                    )
                self._stack_function_symbols = stack_syms
            
            # 获取崩溃帧：优先用可提取源码的帧；不因单帧失败而中断
            if not resolved_frames:
                raise ValueError("没有可用的堆栈帧（所有堆栈帧都被过滤或未解析）")

            extraction_warnings: List[str] = []
            crash_func: Optional[CrashFunction] = None
            crash_frame: Optional[Dict[str, Any]] = None
            addr2line_refined_inside_function = False

            candidate_pairs: List[Tuple[CrashFunction, Dict[str, Any], int]] = []
            symbol_only_rescue_attempts = 0
            max_symbol_only_rescues = int(getattr(self, "max_symbol_only_rescues", 5))

            for fi, candidate in enumerate(resolved_frames):
                self._code_context_phase_check("crash_func_candidates")
                address_c = candidate.get("address", "")
                rf = candidate.get("resolved_function", "") or ""
                raw_fn = candidate.get("function", "") or ""
                rf_or_raw = rf or raw_fn
                module_c = (candidate.get("module") or "").strip()
                rfile = candidate.get("resolved_file", "") or ""
                rline_raw = candidate.get("resolved_line", 0)
                objc_info = self._parse_objc_symbol_class_selector(rf_or_raw)
                # Native C++ 崩溃分析默认不提取 ObjC selector 的源码上下文，避免大量无效全仓扫描。
                if objc_info:
                    extraction_warnings.append(
                        f"帧[{fi}] ObjC selector 已按 native-only 策略跳过: {rf_or_raw}"
                    )
                    continue
                if not self._is_cpp_native_symbol(rf_or_raw):
                    extraction_warnings.append(
                        f"帧[{fi}] 非 C++ 符号已按 native-only 策略跳过: {rf_or_raw}"
                    )
                    continue
                try:
                    rline_int = int(rline_raw) if rline_raw not in (None, "", False) else 0
                except (TypeError, ValueError):
                    rline_int = 0
                if not all([address_c, rf, rfile]) or rline_int <= 0:
                    if symbol_only_rescue_attempts >= max_symbol_only_rescues:
                        extraction_warnings.append(
                            f"帧[{fi}] add2line 信息不完整，已达符号兜底上限({max_symbol_only_rescues})，跳过"
                        )
                        continue
                    if self._is_probably_system_module_name(module_c):
                        extraction_warnings.append(f"帧[{fi}] 系统库帧且缺少 file:line，跳过兜底")
                        continue
                    # 兜底：当 02 只有符号（无 file:line）时，尝试按函数名在 code_roots 中定位定义行
                    # 常见于「已符号化日志 + 未提供/不可用库目录」场景。
                    loc = None
                    # native-only 模式：禁用 ObjC 兜底定位，避免 selector 触发 code_root 大范围检索。
                    cpp_qualified = self._extract_cpp_qualified_parts(rf_or_raw)
                    if (not loc) and cpp_qualified:
                        loc = self._find_cpp_qualified_definition_location(rf_or_raw, code_roots_abs_strs)
                    simple_name = self._extract_function_name_from_resolved(rf_or_raw)
                    # ObjC 符号不再退化到通用按 simple_name 全仓扫描，避免误命中到无关组件。
                    # C++ 限定名同样不退化到 simple_name，防止把类名/构造函数误匹配到无关代码。
                    if (not objc_info) and (not cpp_qualified) and (not loc) and simple_name:
                        loc = self._find_function_definition_location(simple_name, code_roots_abs_strs)
                    if loc and simple_name:
                        symbol_only_rescue_attempts += 1
                        if loc:
                            guessed_file, guessed_line = loc
                            guessed_rf = rf_or_raw or simple_name
                            try:
                                cf_guess = self._extract_crash_function(
                                    guessed_rf,
                                    guessed_file,
                                    guessed_line,
                                    code_roots_abs_strs,
                                )
                            except Exception as e:
                                logger.warning(f"帧[{fi}] 兜底函数定位异常: {e}")
                                cf_guess = None
                            if cf_guess:
                                guessed_frame = dict(candidate)
                                guessed_frame["resolved_function"] = guessed_rf
                                guessed_frame["resolved_file"] = guessed_file
                                guessed_frame["resolved_line"] = guessed_line
                                guessed_frame["crash_location_source"] = "from_log_deduce"
                                guessed_frame["rescue_reason"] = "symbol_only_function_locate"
                                # 将兜底结果回填到当前 resolved_frames，供后续 thread_context/graph 构建复用，
                                # 避免 03 中 warning 已定位但 nodes/call_chain 仍缺失对应函数节点。
                                try:
                                    resolved_frames[fi] = guessed_frame
                                except Exception:
                                    pass
                                candidate_pairs.append((cf_guess, guessed_frame, fi))
                                extraction_warnings.append(
                                    f"帧[{fi}] add2line 信息不完整，已按函数名兜底定位源码: {simple_name} -> {guessed_file}:{guessed_line}"
                                )
                                continue
                    if cpp_qualified:
                        extraction_warnings.append(
                            f"帧[{fi}] add2line 信息不完整，未在当前 code_root 命中 C++ 限定名实现，可能因代码目录范围缩小，跳过"
                        )
                        continue
                    extraction_warnings.append(f"帧[{fi}] add2line 信息不完整，跳过")
                    continue
                try:
                    cf = self._extract_crash_function(rf, rfile, rline_int, code_roots_abs_strs)
                except Exception as e:
                    logger.warning(f"帧[{fi}] _extract_crash_function 异常: {e}")
                    cf = None
                if cf:
                    candidate_pairs.append((cf, candidate, fi))
                    continue
                cf2 = self._fallback_crash_function_line_window(rf, rfile, rline_int, code_roots_abs_strs)
                if cf2:
                    candidate_pairs.append((cf2, candidate, fi))
                    extraction_warnings.append(
                        f"帧[{fi}] 完整函数体提取失败，已降级为行窗口片段: {rf}"
                    )
                    continue
                extraction_warnings.append(f"帧[{fi}] 无法从源码提取上下文: {rf}")

            if candidate_pairs:
                # 主崩溃帧：栈顶优先——自上而下第一个能在 code_roots 中拿到源码上下文的帧；
                # 跳过系统库模块名；仅当没有非系统候选时才回退到含系统库的帧。
                by_fi: Dict[int, Tuple[CrashFunction, Dict[str, Any], int]] = {}
                for row in candidate_pairs:
                    _cf, _fr, _fi = row
                    if _fi not in by_fi:
                        by_fi[_fi] = row

                def _ordered_fi_candidates(skip_system: bool) -> List[int]:
                    out: List[int] = []
                    for fi in range(len(resolved_frames)):
                        if fi not in by_fi:
                            continue
                        mod = (resolved_frames[fi].get("module") or "").strip()
                        if skip_system and self._is_probably_system_module_name(mod):
                            continue
                        out.append(fi)
                    return out

                ordered_fi = _ordered_fi_candidates(True)
                if not ordered_fi:
                    ordered_fi = _ordered_fi_candidates(False)
                if not ordered_fi:
                    ordered_fi = sorted(by_fi.keys())
                best_fi = ordered_fi[0]
                best_cf, best_frame, _ = by_fi[best_fi]
                best_cf, refined_line = self._refine_crash_func_when_addr2line_outside_snippet(
                    best_cf, best_frame, code_roots_abs_strs
                )
                if refined_line:
                    addr2line_refined_inside_function = True
                    extraction_warnings.append(
                        "addr2line 行号落在已提取函数体范围外，已在当前崩溃函数体内重选展示行"
                    )
                crash_func, crash_frame = best_cf, best_frame
                if len(candidate_pairs) > 1:
                    logger.info(
                        "多帧可解析源码，已按栈顶优先（跳过系统库模块）选中帧索引 %s（共 %s 个候选）",
                        best_fi,
                        len(candidate_pairs),
                    )

            if not crash_frame:
                raise ValueError("没有可用的堆栈帧（所有堆栈帧都被过滤或未解析）")

            if not crash_func:
                crash_frame = resolved_frames[0]
                rf0 = crash_frame.get("resolved_function", "") or ""
                rfile0 = crash_frame.get("resolved_file", "") or ""
                try:
                    rline0 = int(crash_frame.get("resolved_line", 0) or 0)
                except (TypeError, ValueError):
                    rline0 = 0
                fn_token = self._extract_function_name_from_resolved(rf0) or "unknown"
                crash_func = CrashFunction(
                    name=fn_token,
                    signature=rf0 or "unknown",
                    snippet=["（源码提取失败，请检查 code_roots 与 addr2line 路径是否一致）"],
                    crash_line="",
                    crash_line_note="所有候选帧均未能定位源码片段",
                    snippet_scope="error",
                )
                extraction_warnings.append(
                    "所有候选帧均未能提取源码；已使用占位片段继续后续分析步骤"
                )

            address = crash_frame.get("address", "")
            resolved_function = crash_frame.get("resolved_function", "") or ""
            resolved_file = crash_frame.get("resolved_file", "") or ""
            try:
                resolved_line = int(crash_frame.get("resolved_line", 0) or 0)
            except (TypeError, ValueError):
                resolved_line = 0
            is_rescue_frame = crash_frame.get("crash_location_source") == "from_log_deduce"
            rescue_reason = crash_frame.get("rescue_reason") if is_rescue_frame else None

            if not all([address, resolved_function, resolved_file, resolved_line]):
                raise ValueError("崩溃帧信息不完整")

            logger.info(f"分析崩溃帧: {resolved_function} 在 {resolved_file}:{resolved_line}")

            # 1. 构建崩溃摘要（若源码侧重选了展示行，使行号与 crash_line 文本对齐）
            eff_crash_line_no = resolved_line
            _cln = getattr(crash_func, "crash_line_number", None)
            if _cln is not None:
                try:
                    _icln = int(_cln)
                    if _icln > 0:
                        eff_crash_line_no = _icln
                except (TypeError, ValueError):
                    pass

            try:
                _icln_display = int(_cln) if _cln is not None else 0
            except (TypeError, ValueError):
                _icln_display = 0
            display_line_differs_from_addr2line = (
                _icln_display > 0
                and resolved_line > 0
                and _icln_display != resolved_line
            )

            crash_line_note: Optional[str] = None
            _scope = getattr(crash_func, "snippet_scope", None)
            if _scope == "error":
                crash_line_note = getattr(crash_func, "crash_line_note", None)
            elif addr2line_refined_inside_function:
                crash_line_note = (
                    "已由 addr2line/add2line 提供文件与行号，但该行落在自动提取的函数体范围之外"
                    "（常见于内联、编译器优化或与 dSYM/源码映射不完全一致）。"
                    "已在当前选定的崩溃函数体内按启发式重选展示行与行号；该位置为辅助阅读用，不代表指令级精确 PC。"
                )
            elif is_rescue_frame:
                if rescue_reason == "missing_debug_info":
                    head = (
                        "无法将崩溃地址解析为带文件与行号的用户源码（当前模块可能缺少完整调试信息或符号不完整）。"
                    )
                elif rescue_reason == "symbol_only_function_locate":
                    head = (
                        "堆栈为已符号化函数名，但缺少可用的 file:line（或未与当前 code_root 对齐）。"
                    )
                else:
                    head = (
                        "栈帧符号曾被解析到系统/标准库实现，或符号链未直接落在当前工程源文件上（多与内联/链接视图有关，非工具误报）。"
                    )
                mid = (
                    "主崩溃帧已按「自栈顶向下优先、跳过系统库模块」选取，并在 code_root 中关联到本函数实现。"
                )
                if display_line_differs_from_addr2line or (
                    _icln_display <= 0 and eff_crash_line_no != resolved_line
                ):
                    tail = (
                        "其中 crash_line / crash_line_number 为在该函数范围内的启发式展示（优先选取信息量较高的语句行），"
                        "不是指令级精确崩溃位置。"
                    )
                else:
                    tail = (
                        "展示行靠近用于定位的锚点行（常为函数定义或入口附近）；精确到指令的崩溃行仍可能未知。"
                    )
                crash_line_note = head + mid + tail
            elif _scope == "line_window":
                crash_line_note = (
                    "已根据 addr2line 行号提取邻近源码窗口；未得到完整函数体时，"
                    "展示内容可能仅以解析行为中心的片段，若运行库与符号/源码构建不一致，行号可能偏移。"
                )
            elif display_line_differs_from_addr2line:
                crash_line_note = (
                    "已由 addr2line/add2line 将地址解析至参考行号；"
                    "展示行在与解析行邻近的当前函数体内按启发式重选，以突出更可能相关的语句。"
                    "若运行库与当前符号/源码构建不一致，行号仍可能偏移。"
                )
            else:
                crash_line_note = (
                    "已由 addr2line/add2line 将崩溃地址解析至本行；"
                    "若运行库与当前符号/源码构建不一致，行号可能偏移。"
                )

            crash_summary = CrashSummary(
                file=resolved_file,
                function=resolved_function,
                crash_line_number=eff_crash_line_no,
                stack_address=address,
                error_type="SIGSEGV",  # 默认值，实际应从崩溃日志获取
                thread_id="unknown",  # 默认值，实际应从崩溃日志获取
                crash_location_source="from_log_deduce" if is_rescue_frame else "from_add2line",
                crash_line_note=crash_line_note,
            )
            self._cc_stage_crash_summary = crash_summary

            # 2. 提取崩溃函数信息（已在上方完成）
            try:
                setattr(crash_func, "file", resolved_file)
            except Exception:
                pass
            if is_rescue_frame:
                crash_func = CrashFunction(
                    name=crash_func.name,
                    signature=crash_func.signature,
                    snippet=crash_func.snippet,
                    crash_line=crash_func.crash_line,
                    crash_location_source="from_log_deduce",
                    crash_line_note=crash_line_note,
                    snippet_scope=getattr(crash_func, "snippet_scope", None) or "full_function",
                    snippet_start_line=getattr(crash_func, "snippet_start_line", None),
                    snippet_end_line=getattr(crash_func, "snippet_end_line", None),
                    crash_line_number=getattr(crash_func, "crash_line_number", None),
                )

            try:
                crash_summary.crash_line_code = crash_func.crash_line
            except Exception:
                pass

            self._cc_stage_crash_func = crash_func

            # 2b. add2line 堆栈帧对应的工程内源文件（静态「直接调用者 / 共享变量」先扫这些，再全仓补充）
            stack_priority_files: List[str] = []
            direct_call_crash_fun: List[CallChainFunction] = []
            pre_call_fun_in_same_parent_fun: List[CallChainFunction] = []
            shared_vars: List[str] = []
            use_same_var_related_fun: List[VariableFunction] = []
            sibling_member_func_in_same_class: List[RelatedFunction] = []
            thread_context: List[ThreadContext] = []
            graph: Optional[CrashGraph] = None
            phase_timed_out = False

            try:
                stack_priority_files = self._collect_stack_priority_source_files(
                    add2line_data, code_roots_abs_strs
                )

                # 3. 查找直接调用崩溃函数的上层函数
                template_info = self._parse_template_container_function(resolved_function)
                template_generic_methods = {"RemoveAt", "RemoveHead", "RemoveTail", "RemoveAll", "AddHead", "AddTail", "InsertAfter", "InsertBefore"}
                is_template_generic_crash = bool(
                    template_info and template_info.get("method") in template_generic_methods
                )
                try:
                    direct_call_crash_fun = self._find_call_chain_functions(
                        resolved_function, code_roots_abs_strs, stack_priority_files=stack_priority_files
                    )
                except _CodeContextPhaseTimeout:
                    raise
                except Exception as e:
                    logger.warning(f"查找调用链失败，已跳过: {e}")
                    extraction_warnings.append(f"调用链分析失败: {e}")
                logger.info(f"找到 {len(direct_call_crash_fun)} 个直接调用崩溃函数的上层函数（call_expression 扫描）")
                try:
                    implicit_callers = self._find_implicit_ctor_usage_callers(
                        resolved_function, code_roots_abs_strs
                    )
                    if implicit_callers:
                        existing_keys = {(c.name, c.file) for c in direct_call_crash_fun}
                        for ic in implicit_callers:
                            if (ic.name, ic.file) not in existing_keys:
                                direct_call_crash_fun.append(ic)
                                existing_keys.add((ic.name, ic.file))
                        logger.info(
                            f"合并隐式构造使用点后，共 {len(direct_call_crash_fun)} 个调用链候选"
                        )
                except _CodeContextPhaseTimeout:
                    raise
                except Exception as e:
                    logger.warning(f"隐式构造调用链补充失败，已跳过: {e}")
                    extraction_warnings.append(f"隐式构造调用链补充失败: {e}")

                if len(direct_call_crash_fun) > self.max_direct_callers:
                    direct_call_crash_fun = direct_call_crash_fun[: self.max_direct_callers]
                    logger.info(
                        f"直接调用崩溃函数的静态候选已截断为最多 {self.max_direct_callers} 个（防止输出膨胀）"
                    )

                # 4. 查找与直接调用者同一入口函数内、在崩溃调用之前执行的前置环境函数
                pre_call_fun_in_same_parent_fun: List[CallChainFunction] = []
                if is_template_generic_crash:
                    pre_call_fun_in_same_parent_fun = []
                else:
                    try:
                        pre_call_fun_in_same_parent_fun = self._find_pre_call_fun_in_same_parent_functions(
                            resolved_function, direct_call_crash_fun, code_roots_abs_strs
                        )
                    except _CodeContextPhaseTimeout:
                        raise
                    except Exception as e:
                        logger.warning(f"查找前置环境函数失败，已跳过: {e}")
                        extraction_warnings.append(f"前置环境函数分析失败: {e}")
                logger.info(f"找到 {len(pre_call_fun_in_same_parent_fun)} 个 pre_call_fun_in_same_parent_fun（前置环境函数）")

                # 5. 共享变量与 use_shared_var
                crash_location_source = getattr(crash_func, "crash_location_source", None) or getattr(
                    crash_summary, "crash_location_source", None
                )
                shared_vars: List[str] = []
                use_same_var_related_fun: List[VariableFunction] = []
                try:
                    if is_template_generic_crash:
                        shared_vars = []
                    else:
                        if crash_location_source != "from_log_deduce":
                            shared_vars = self._extract_shared_variables_from_code(crash_func.crash_line or "")
                            if not shared_vars and crash_func.snippet:
                                shared_vars = self._extract_shared_variables_from_code("\n".join(crash_func.snippet))
                                logger.info("精确行未提取到共享变量，回退到整个函数片段抽取")
                        else:
                            code_for_vars = "\n".join(crash_func.snippet) if crash_func.snippet else (crash_func.crash_line or "")
                            shared_vars = self._extract_shared_variables_from_code(code_for_vars)
                    if shared_vars:
                        use_same_var_related_fun = self._find_variable_functions_for_vars(
                            shared_vars,
                            resolved_function,
                            code_roots_abs_strs,
                            stack_priority_files=stack_priority_files,
                        )
                        if len(use_same_var_related_fun) > self.max_shared_var_related_functions:
                            use_same_var_related_fun = use_same_var_related_fun[
                                : self.max_shared_var_related_functions
                            ]
                            logger.info(
                                f"共享变量相关函数已截断为最多 {self.max_shared_var_related_functions} 条（防止输出膨胀）"
                            )
                except _CodeContextPhaseTimeout:
                    raise
                except Exception as e:
                    logger.warning(f"共享变量分析失败，已跳过: {e}")
                    extraction_warnings.append(f"共享变量分析失败: {e}")
                logger.info(f"共享变量候选 {len(shared_vars)} 个，找到 {len(use_same_var_related_fun)} 条函数-变量关系")

                # 6. 查找同一类中的兄弟函数
                name_for_related = resolved_function
                if getattr(crash_func, "signature", None) and "::" in (crash_func.signature or ""):
                    name_for_related = (crash_func.signature or "").split("(")[0].strip()
                sibling_member_func_in_same_class: List[RelatedFunction] = []
                try:
                    sibling_member_func_in_same_class = self._find_related_functions_in_class(
                        name_for_related, resolved_file, getattr(self, "current_code_roots", []) or []
                    )
                except _CodeContextPhaseTimeout:
                    raise
                except Exception as e:
                    logger.warning(f"兄弟函数查找失败，已跳过: {e}")
                    extraction_warnings.append(f"兄弟函数查找失败: {e}")
                logger.info(f"找到 {len(sibling_member_func_in_same_class)} 个同一类中的兄弟函数或其他关联函数")

                # 7. 分析线程上下文
                thread_context: List[ThreadContext] = []
                try:
                    thread_context = self._analyze_thread_context(add2line_data)
                except _CodeContextPhaseTimeout:
                    raise
                except Exception as e:
                    logger.warning(f"线程上下文分析失败，已跳过: {e}")
                    extraction_warnings.append(f"线程上下文分析失败: {e}")
                logger.info(f"找到 {len(thread_context)} 个线程上下文")

                # 8. 构建图结构视图
                graph: CrashGraph
                try:
                    graph = self._build_crash_graph(
                        crash_summary=crash_summary,
                        crash_file=resolved_file,
                        crash_func=crash_func,
                        direct_call_crash_fun=direct_call_crash_fun,
                        pre_call_fun_in_same_parent_fun=pre_call_fun_in_same_parent_fun,
                        use_same_var_related_fun=use_same_var_related_fun,
                        sibling_member_func_in_same_class=sibling_member_func_in_same_class,
                        thread_context=thread_context,
                        os_type=os_type,
                    )
                except _CodeContextPhaseTimeout:
                    raise
                except Exception as e:
                    logger.warning(f"构建崩溃图失败，已降级为空图: {e}")
                    extraction_warnings.append(f"图构建失败: {e}")
                    graph = CrashGraph(
                        nodes=[],
                        edges=[],
                        call_chain_from_code=[],
                        call_chain_from_add2line=[],
                        edges_empty_reason="图构建阶段异常，已降级为空图；详见 extraction_warnings。",
                    )

            except _CodeContextPhaseTimeout as e:
                phase_timed_out = True
                capt = float(getattr(self, "code_context_timeout_sec", 0) or 0)
                msg = f"代码上下文整阶段超时（{capt:.0f}s），已截断静态分析: {e}"
                logger.warning(msg)
                extraction_warnings.append(msg)
                if graph is None:
                    graph = CrashGraph(
                        nodes=[],
                        edges=[],
                        call_chain_from_code=[],
                        call_chain_from_add2line=[],
                        edges_empty_reason=msg,
                    )

            assert graph is not None

            logger.info("结构化崩溃分析数据生成完成")
            return self._finalize_code_content_provider_json(
                crash_summary,
                crash_func,
                graph,
                extraction_warnings,
                resolved_file,
                resolved_function,
                extra_top_level=self._code_context_phase_timeout_extra_top_level()
                if phase_timed_out
                else None,
            )

        except _CodeContextPhaseTimeout as e:
            detail = str(e) or "unknown"
            logger.warning("代码上下文整阶段超时（静态分析前）: %s", detail)
            return self._minimal_code_context_on_phase_timeout(add2line_data, detail)
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析错误: {e}")
            return json.dumps({
                "error": f"JSON解析错误: {e}",
                "input": add2line_json,
                "code_parser_backend": self.code_parser_backend
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"生成崩溃分析数据时出错: {e}")
            return json.dumps({
                "error": str(e),
                "input": add2line_json,
                "code_roots": _normalize_code_roots_arg(code_root),
                "code_parser_backend": self.code_parser_backend
            }, ensure_ascii=False, indent=2)

# 测试代码
if __name__ == "__main__":
    import sys
    
    # 如果从stdin读取输入
    if not sys.stdin.isatty():
        add2line_json = sys.stdin.read()
        # 过滤掉"-"参数，它表示从stdin读取
        args = [arg for arg in sys.argv if arg != "-"]
        code_root = args[1] if len(args) > 1 else "."
        
        provider = CodeContentProvider(code_parser_backend="regex")
        result = provider.code_content_provider(add2line_json, code_root)
        print(result)
    else:
        # 示例add2line输出JSON
        sample_add2line_json = '''
        {
            "resolved_frames": [
                {
                    "address": "0x12345678",
                    "resolved_function": "ComplexDataStructure::add_node",
                    "resolved_file": "src/data/complex_data.cpp",
                    "resolved_line": 128
                },
                {
                    "address": "0x87654321",
                    "resolved_function": "worker_thread",
                    "resolved_file": "src/thread/worker.cpp",
                    "resolved_line": 87
                }
            ],
            "os_type": "macos",
            "library_path": "/usr/lib",
            "success_count": 2,
            "total_count": 2,
            "errors": []
        }
        '''
        
        # 测试代码内容提供工具
        provider = CodeContentProvider(code_parser_backend="regex")
        result = provider.code_content_provider(sample_add2line_json, "/tmp")
        print("生成的崩溃分析数据:")
        print(result)


# ==================== CodeContentProviderWithPrompts (merged from analyzers/) ====================

import os as _os
import logging as _logging_cwp
from typing import Dict as _Dict_cwp, Any as _Any_cwp
from pathlib import Path as _Path_cwp

try:
    from prompts.crash_analysis_prompt_templates import (
        generate_crash_analysis_prompt as _gen_analysis_prompt,
        generate_crash_repair_prompt as _gen_repair_prompt,
        generate_thread_safety_prompt as _gen_thread_prompt,
    )
    _PROMPTS_CWP_AVAILABLE = True
except ImportError:
    _PROMPTS_CWP_AVAILABLE = False
    _gen_analysis_prompt = None
    _gen_repair_prompt = None
    _gen_thread_prompt = None

_cwp_logger = _logging_cwp.getLogger(__name__)


class CodeContentProviderWithPrompts:
    """代码内容提供器 - 带提示词的完整版本（合并自 analyzers/code_content_provider_with_prompts.py）"""

    def __init__(self):
        _b = _os.environ.get("MAP_SDK_CRASH_CODE_PARSER_BACKEND", "regex")
        self.provider = CodeContentProvider(code_parser_backend=_b)

    def generate_complete_analysis(self, add2line_json: str, code_root: str,
                                   prompt_type: str = "full") -> _Dict_cwp[str, _Any_cwp]:
        try:
            _cwp_logger.info("开始生成完整崩溃分析内容...")
            import json as _j
            json_data = self.provider.code_content_provider(add2line_json, code_root)
            crash_data = _j.loads(json_data)
            if _PROMPTS_CWP_AVAILABLE:
                if prompt_type == "full":
                    prompt = _gen_analysis_prompt(crash_data)
                elif prompt_type == "repair":
                    prompt = _gen_repair_prompt(crash_data)
                elif prompt_type == "thread_safety":
                    prompt = _gen_thread_prompt(crash_data)
                else:
                    prompt = _gen_analysis_prompt(crash_data)
            else:
                prompt = ""
            result = {
                "json_data": crash_data,
                "prompt": prompt,
                "prompt_type": prompt_type,
                "metadata": {
                    "generated_at": str(_Path_cwp().cwd()),
                    "version": "2.0",
                    "total_functions": len(crash_data.get("call_chain_fun", [])) +
                                       len(crash_data.get("var_call_fun", [])),
                    "thread_contexts": len(crash_data.get("thread_context", [])),
                },
            }
            _cwp_logger.info("完整崩溃分析内容生成完成")
            return result
        except Exception as e:
            _cwp_logger.error(f"生成完整崩溃分析内容时出错: {e}")
            return {"error": str(e), "json_data": None, "prompt": None, "prompt_type": prompt_type}

    def generate_json_only(self, add2line_json: str, code_root: str) -> str:
        return self.provider.code_content_provider(add2line_json, code_root)

    def generate_prompt_only(self, add2line_json: str, code_root: str,
                             prompt_type: str = "full") -> str:
        try:
            import json as _j
            json_data = self.provider.code_content_provider(add2line_json, code_root)
            crash_data = _j.loads(json_data)
            if not _PROMPTS_CWP_AVAILABLE:
                return ""
            if prompt_type == "full":
                return _gen_analysis_prompt(crash_data)
            elif prompt_type == "repair":
                return _gen_repair_prompt(crash_data)
            elif prompt_type == "thread_safety":
                return _gen_thread_prompt(crash_data)
            else:
                return _gen_analysis_prompt(crash_data)
        except Exception as e:
            _cwp_logger.error(f"生成提示词时出错: {e}")
            return f"生成提示词时出错: {e}"

    def generate_combined_output(self, add2line_json: str, code_root: str) -> str:
        try:
            import json as _j
            analysis = self.generate_complete_analysis(add2line_json, code_root, "full")
            if "error" in analysis:
                return _j.dumps(analysis, ensure_ascii=False, indent=2)
            combined_output = {
                "analysis_data": analysis["json_data"],
                "prompt": analysis["prompt"],
                "metadata": analysis["metadata"],
            }
            return _j.dumps(combined_output, ensure_ascii=False, indent=2)
        except Exception as e:
            _cwp_logger.error(f"生成组合输出时出错: {e}")
            import json as _j
            return _j.dumps({"error": str(e), "analysis_data": None, "prompt": None},
                            ensure_ascii=False, indent=2)


# ==================== CodeContentProviderTool (BaseTool wrapper) ====================

import logging as _logging_tool
from typing import Any as _Any_tool, Dict as _Dict_tool, Optional as _Optional_tool

from tool_system.tool import BaseTool, ToolDefinition
from tool_system.registry import Priority

_tool_logger = _logging_tool.getLogger(__name__)


class CodeContentProviderTool(BaseTool):
    """代码内容提取工具 — 内置 Tool 实现，自包含所有代码提取逻辑。"""

    def __init__(
        self,
        include_subdirs: _Optional_tool[list] = None,
        exclude_dirs: _Optional_tool[list] = None,
        backend: str = "tree-sitter",
    ):
        self.include_subdirs = list(include_subdirs or [])
        self.exclude_dirs = list(exclude_dirs or [])
        self.backend = backend

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="code_content_provider",
            description="根据解析后的堆栈信息，提取相关源代码上下文。包括崩溃函数、调用链、相关函数等。",
            input_schema={
                "type": "object",
                "properties": {
                    "resolved_stack": {"type": "string", "description": "解析后的堆栈信息 JSON"},
                    "code_roots": {"type": "array", "description": "代码根目录列表"},
                    "backend": {"type": "string", "description": "解析后端: tree-sitter/native", "default": "tree-sitter"},
                },
                "required": ["resolved_stack", "code_roots"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "crash_contexts": {"type": "array"},
                    "call_chain_contexts": {"type": "array"},
                    "related_contexts": {"type": "array"},
                },
            },
            category="provider",
            version="1.0.0",
        )

    def execute(self, input_data: _Dict_tool[str, _Any_tool]) -> _Dict_tool[str, _Any_tool]:
        import json as _json

        resolved_stack = input_data.get("resolved_stack", "")
        code_roots = input_data.get("code_roots", [])
        backend = input_data.get("backend", self.backend)

        if isinstance(code_roots, str):
            code_roots = [code_roots]

        provider = CodeContentProvider(code_parser_backend=backend)
        result = provider.code_content_provider(
            resolved_stack,
            code_roots,
            exclude_dirs=self.exclude_dirs or None,
            include_subdirs=self.include_subdirs or None,
        )
        try:
            parsed = _json.loads(result)
        except Exception:
            parsed = {"raw_result": result}
        return parsed
