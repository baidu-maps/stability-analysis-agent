#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/ — 内置 Tool 实现（替换原 analyzers/ + tool_system/builtins.py）

公开 API：
  - CrashLogParserTool, crash_log_parser, CrashParseOptions, crash_parse_options_from_cli_args
  - Add2LineResolverTool, add2line_resolver
  - CodeContentProviderTool, CodeContentProvider, CodeContentProviderWithPrompts
  - register_all_tools()
"""

from .crash_log_parser_tool import (
    CrashLogParserTool,
    crash_log_parser,
    CrashParseOptions,
    crash_parse_options_from_cli_args,
)
from .add2line_resolver_tool import Add2LineResolverTool, add2line_resolver
from .code_content_provider_tool import (
    CodeContentProviderTool,
    CodeContentProvider,
    CodeContentProviderWithPrompts,
)
from .symbol_callsite_finder_tool import SymbolCallsiteFinderTool
from .snippet_extractor_tool import SnippetExtractorTool
from .fix_code_extractor_tool import FixCodeExtractorTool
from .fix_code_applier_tool import FixCodeApplierTool
from .vector_memory_retriever_tool import VectorMemoryRetrieverTool
from .repo_search_tool import RepoSearchTool
from .native_leak_diagnosis import NativeLeakAnalyzerTool
from .js_heap import JsHeapAnalyzerTool
from .js_crash import JsCrashDiagnosisTool
from .jank_analysis import JankAnalyzerTool
from .cpp_crash import CppCrashDiagnosisTool
from .appfreeze import AppFreezeDiagnosisTool
from .api_fault import ApiFaultDiagnosisTool

__all__ = [
    # Tool classes
    "CrashLogParserTool",
    "Add2LineResolverTool",
    "CodeContentProviderTool",
    # Bare functions / classes (for direct use by agent/ and cli/)
    "crash_log_parser",
    "CrashParseOptions",
    "crash_parse_options_from_cli_args",
    "add2line_resolver",
    "CodeContentProvider",
    "CodeContentProviderWithPrompts",
    "SymbolCallsiteFinderTool",
    "SnippetExtractorTool",
    "FixCodeExtractorTool",
    "FixCodeApplierTool",
    "VectorMemoryRetrieverTool",
    "RepoSearchTool",
    "NativeLeakAnalyzerTool",
    # Registration helper
    "register_all_tools",
]


def register_all_tools(registry=None):
    """注册所有内置工具到注册表（延迟导入避免循环依赖）。"""
    from tool_system import get_registry, Priority

    if registry is None:
        registry = get_registry()

    registry.register(
        "crash_log_parser",
        CrashLogParserTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "add2line_resolver",
        Add2LineResolverTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "code_content_provider",
        CodeContentProviderTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "symbol_callsite_finder",
        SymbolCallsiteFinderTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "snippet_extractor",
        SnippetExtractorTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "fix_code_extractor",
        FixCodeExtractorTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "fix_code_applier",
        FixCodeApplierTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "vector_memory_retriever",
        VectorMemoryRetrieverTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "repo_search",
        RepoSearchTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "native_leak_analyzer",
        NativeLeakAnalyzerTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "js_heap_analyzer",
        JsHeapAnalyzerTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "js_crash_diagnosis",
        JsCrashDiagnosisTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "jank_analyzer",
        JankAnalyzerTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "cpp_crash_diagnosis",
        CppCrashDiagnosisTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "appfreeze_diagnosis",
        AppFreezeDiagnosisTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    registry.register(
        "api_fault_diagnosis",
        ApiFaultDiagnosisTool(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=True,
        module="tools",
    )
    from tools.diagnosis.knowledge import register_builtin_knowledge

    register_builtin_knowledge()
