"""Analysis acceleration services (code file index, ctags symbol index, code locator, code fixer)."""

from services.code_index_service import CodeIndexMultiRoot, get_code_index_for_roots
from services.ctags_function_index import CtagsFunctionIndex, get_ctags_index_for_roots, warm_ctags_index_for_roots
from services.code_locator import (
    CodeLocatorService,
    LocatorConfig,
    LocatorContext,
    FileLocator,
    SymbolLocator,
    CallerLocator,
    VariableLocator,
    CallChainFunction,
    VariableFunction,
    FindSourceFileTimeout,
    CodeContextPhaseTimeout,
)
from services.code_fixer import (
    CodeFixer,
    FixResult,
    FixPlanGenerator,
    extract_candidate_nodes,
    graph_auto_fix_allowed,
    signatures_match,
    is_forbidden_patch,
    evaluate_fix_apply_success,
    parse_json_payload,
)

__all__ = [
    "CodeIndexMultiRoot",
    "get_code_index_for_roots",
    "CtagsFunctionIndex",
    "get_ctags_index_for_roots",
    "warm_ctags_index_for_roots",
    "CodeLocatorService",
    "LocatorConfig",
    "LocatorContext",
    "FileLocator",
    "SymbolLocator",
    "CallerLocator",
    "VariableLocator",
    "CallChainFunction",
    "VariableFunction",
    "FindSourceFileTimeout",
    "CodeContextPhaseTimeout",
    "CodeFixer",
    "FixResult",
    "FixPlanGenerator",
    "extract_candidate_nodes",
    "graph_auto_fix_allowed",
    "signatures_match",
    "is_forbidden_patch",
    "evaluate_fix_apply_success",
    "parse_json_payload",
]
