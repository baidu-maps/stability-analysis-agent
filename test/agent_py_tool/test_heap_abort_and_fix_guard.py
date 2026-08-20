#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""堆 abort 解析、系统库透传、改码签名守卫与崩溃行 callee 展开。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag.deterministic_analyzer import DeterministicAnalyzer
from services.code_fixer import (
    _extracted_block_matches_signature,
    _replacement_signature_compatible,
    edit_allowed_by_prompt_complete_body,
    signatures_match,
)
from tools._library_frame_whitelist import is_system_native_module, match_libraries_for_module
from tools.code_content_provider_tool import CodeContentProvider
from tools.cpp_crash.core import match_cpp_fault_modes
from tools.crash_diagnosis.classifier import classify_crash
from tools.crash_diagnosis.types import FaultAnalysis, FpAnalysis, PcLrAnalysis, RegisterCorrelation, SpAnalysis
from tools.crash_parser.abort_message import (
    extract_abort_message,
    is_heap_allocator_abort,
    thread_type_from_name,
)
from tools.crash_parser.meta import extract_crash_info
from workflows.crash_analysis_workflow import _truncate_analysis_prompt


class TestAbortMessageAndHeapAbort(unittest.TestCase):
    """Abort message / Scudo 分类。"""

    SAMPLE = (
        "pid: 1, tid: 30445, name: Tmcom-MapDRende  >>> /system/bin/app\n"
        "signal 6 (SIGABRT), code -1 (SI_QUEUE)\n"
        "Abort message: 'Scudo ERROR: invalid chunk state when deallocating address'\n"
        "backtrace:\n"
        "    #00 pc 000000000005bdc0  /apex/com.android.runtime/lib64/bionic/libc.so (abort+164)\n"
    )

    def test_extract_abort_message_and_thread_type(self) -> None:
        info = extract_crash_info(self.SAMPLE)
        self.assertIn("Scudo ERROR", info.abort_message or "")
        self.assertEqual(info.thread_type, "background")
        self.assertEqual(info.category, "native_crash")
        self.assertEqual(info.crash_reason, "heap allocator abort")
        self.assertTrue(is_heap_allocator_abort(info.abort_message))

    def test_thread_type_from_name(self) -> None:
        self.assertEqual(thread_type_from_name("main"), "main")
        self.assertEqual(thread_type_from_name("Tmcom-MapDRende"), "background")

    def test_gles_substring_is_not_gpu_crash_for_sigabrt_heap(self) -> None:
        text = self.SAMPLE + "libGLESv2.so mapped\n"
        info = extract_crash_info(text)
        self.assertNotEqual(info.category, "gpu_crash")

    def test_cpp_fault_mode_scudo(self) -> None:
        modes = match_cpp_fault_modes(
            {
                "signal": "SIGABRT",
                "last_fatal_message": extract_abort_message(self.SAMPLE),
                "native_frames": [],
                "raw_content": self.SAMPLE,
            }
        )
        self.assertTrue(any(item["id"] == "CPP-FM-15" for item in modes))

    def test_deterministic_heap_abort_not_assert_story(self) -> None:
        parse = {"crash_info": {"signal": "SIGABRT", "abort_message": extract_abort_message(self.SAMPLE)}}
        out = DeterministicAnalyzer().analyze(parse, {}, self.SAMPLE)
        types = [fact.fact_type for fact in out.facts]
        self.assertIn("heap_abort", types)
        self.assertNotIn("abort", types)
        heap = next(fact for fact in out.facts if fact.fact_type == "heap_abort")
        self.assertIn("越界", heap.implication)

    def test_classifier_heap_corruption(self) -> None:
        result = classify_crash(
            SpAnalysis(),
            FpAnalysis(),
            PcLrAnalysis(),
            FaultAnalysis(),
            RegisterCorrelation(),
            {"signal": "SIGABRT", "abort_message": "Scudo ERROR: invalid chunk state", "crash_reason": "abort"},
        )
        self.assertEqual(result.primary_pattern, "heap_corruption")


class TestSystemLibMatch(unittest.TestCase):
    """系统库匹配与短名误伤。"""

    def test_libc_is_system(self) -> None:
        self.assertTrue(is_system_native_module("libc.so"))
        self.assertTrue(is_system_native_module("/apex/com.android.runtime/lib64/bionic/libc.so"))
        self.assertFalse(is_system_native_module("libBaiduMapSDK_map_for_navi_v9_0_0.so"))

    def test_short_name_does_not_match_libcrypto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            crypto = Path(tmp) / "libcrypto.so"
            crypto.write_text("x", encoding="utf-8")
            hits = match_libraries_for_module("libc.so", [crypto])
            self.assertEqual(hits, [])


class TestCodeFixerClassGuard(unittest.TestCase):
    """禁止跨类同名函数替换。"""

    def test_signatures_match_rejects_different_class(self) -> None:
        self.assertFalse(
            signatures_match("void GLRenderShader::apply()", "void GLPiplineState::apply()")
        )
        self.assertTrue(
            signatures_match("void GLRenderShader::apply()", "void GLRenderShader::apply() noexcept")
        )

    def test_replacement_must_keep_class_name(self) -> None:
        target = "void GLRenderShader::apply()"
        wrong = "void GLPiplineState::apply() noexcept\n{\n    return;\n}\n"
        right = "void GLRenderShader::apply() noexcept\n{\n    return;\n}\n"
        self.assertFalse(_replacement_signature_compatible(target, wrong))
        self.assertTrue(_replacement_signature_compatible(target, right))
        self.assertFalse(_extracted_block_matches_signature(wrong, target))
        self.assertTrue(_extracted_block_matches_signature(right, target))

    def test_skip_edit_without_complete_prompt_body(self) -> None:
        ctx = {
            "diagnostics": {
                "prompt_context_meta": {
                    "included_complete_signatures": ["void GLRenderShader::getAttributeInfo()"],
                }
            }
        }
        self.assertIsNone(
            edit_allowed_by_prompt_complete_body("void GLRenderShader::getAttributeInfo()", ctx)
        )
        err = edit_allowed_by_prompt_complete_body("void GLRenderShader::apply()", ctx)
        self.assertIn("完整函数体", err or "")
        self.assertIsNone(edit_allowed_by_prompt_complete_body("void Foo::bar()", {}))


class TestCrashLineCalleeNames(unittest.TestCase):
    """compile() 外壳应抽出 initWithShaderSources。"""

    def test_extracts_callee_from_thin_wrapper(self) -> None:
        provider = CodeContentProvider.__new__(CodeContentProvider)
        names = provider._callee_names_from_crash_snippet(
            "initWithShaderSources(vs, fs);",
            ["void compile() {", "    initWithShaderSources(vs, fs);", "}"],
            "compile",
        )
        self.assertIn("initWithShaderSources", names)
        self.assertNotIn("compile", names)

    def test_extracts_nested_callee_from_init_snippet(self) -> None:
        provider = CodeContentProvider.__new__(CodeContentProvider)
        names = provider._callee_names_from_crash_snippet(
            "",
            ["    getAttributeInfo();", "    getUniformInfo();", "    return true;"],
            "initWithShaderSources",
        )
        self.assertIn("getAttributeInfo", names)
        self.assertIn("getUniformInfo", names)


class TestPromptTruncateKeepsDiagnosis(unittest.TestCase):
    """截断时应保住诊断与输出约束。"""

    def test_keeps_diagnosis_and_tail(self) -> None:
        prompt = (
            "## 崩溃分析任务\nhead\n\n"
            "## 崩溃证据诊断\nAbort message: Scudo ERROR\n\n"
            "## 变量相关函数\n" + ("noise\n" * 4000) + "\n"
            "## 输出要求\nmust keep tail\n"
        )
        out = _truncate_analysis_prompt(prompt, 1800)
        self.assertIn("崩溃证据诊断", out)
        self.assertIn("输出要求", out)
        self.assertNotIn("PROMPT TRUNCATED — 已优先保留诊断与输出约束", out)
        self.assertLessEqual(len(out), 1800)

    def test_keeps_middle_crash_function_section(self) -> None:
        prompt = (
            "## 崩溃分析任务\nhead\n\n"
            "## 崩溃证据诊断\nAbort message: Scudo ERROR\n\n"
            "## 函数源码\nvoid GLRenderShader::getAttributeInfo() { body; }\n\n"
            "## 变量相关函数\n" + ("noise\n" * 5000) + "\n"
            "## 输出要求\nmust keep tail\n"
        )
        out = _truncate_analysis_prompt(prompt, 2500)
        self.assertIn("getAttributeInfo", out)
        self.assertIn("输出要求", out)
        self.assertNotIn("## 变量相关函数", out)


if __name__ == "__main__":
    unittest.main()
