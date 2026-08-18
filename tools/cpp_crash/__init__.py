"""Evidence-first C/C++ crash diagnosis."""

from .core import classify_stack_layers, diagnose_cpp_crash, extract_cpp_evidence, match_cpp_fault_modes
from .hints import match_crash_hints
from .tool import CppCrashDiagnosisTool

__all__ = ["classify_stack_layers", "diagnose_cpp_crash", "extract_cpp_evidence", "match_cpp_fault_modes", "match_crash_hints", "CppCrashDiagnosisTool"]
