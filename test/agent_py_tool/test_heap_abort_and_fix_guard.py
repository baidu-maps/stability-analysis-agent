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
from services.code_locator import CodeLocatorService, LocatorConfig
from services.code_fixer import (
    _check_null_guard_only_patch,
    _extracted_block_matches_signature,
    _replacement_signature_compatible,
    edit_allowed_by_prompt_complete_body,
    signatures_match,
)
from tools._library_frame_whitelist import is_system_native_module, match_libraries_for_module
from tools._stack_symbol_utils import (
    caller_is_same_cpp_method,
    stack_has_same_named_trampoline,
    stack_symbol_aliases_crash_function,
)
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
from services.analyze_llm import truncate_analysis_prompt


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

    def test_android_pid_equals_tid_is_main_thread(self) -> None:
        text = (
            "pid: 1242, tid: 1242, name: ninebot.ninebot  >>> /data/app/ninebot.ninebot <<<\n"
            "signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x408\n"
        )
        info = extract_crash_info(text)
        self.assertEqual(info.thread_type, "main")

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
        out = truncate_analysis_prompt(prompt, 1800)
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
        out = truncate_analysis_prompt(prompt, 2500)
        self.assertIn("getAttributeInfo", out)
        self.assertIn("输出要求", out)
        self.assertNotIn("## 变量相关函数", out)


class TestSameNameMethodNotCollapsed(unittest.TestCase):
    """同名成员函数不得被当成崩溃点自身。"""

    def test_stack_symbol_aliases_requires_same_class(self) -> None:
        crash_sig = "VVoid NaviMapRender::UpdateMapRenderCustomDrawOption(NE_MapRenderCustomDrawOption &option)"
        self.assertTrue(
            stack_symbol_aliases_crash_function(
                "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption(walk_navi::NE_MapRenderCustomDrawOption&)",
                "UpdateMapRenderCustomDrawOption",
                crash_sig,
            )
        )
        self.assertFalse(
            stack_symbol_aliases_crash_function(
                "walk_navi::WalkMapControl::UpdateMapRenderCustomDrawOption(walk_navi::NE_MapRenderCustomDrawOption&)",
                "UpdateMapRenderCustomDrawOption",
                crash_sig,
            )
        )
        self.assertFalse(
            stack_symbol_aliases_crash_function(
                "walk_navi::WalkMapControl::UpdateMapRenderCustomDrawOption(walk_navi::NE_MapRenderCustomDrawOption&)",
                "UpdateMapRenderCustomDrawOption",
                "void UpdateMapRenderCustomDrawOption()",
            )
        )

    def test_caller_scan_keeps_same_named_trampoline(self) -> None:
        self.assertFalse(
            caller_is_same_cpp_method(
                "WalkMapControl::UpdateMapRenderCustomDrawOption",
                "UpdateMapRenderCustomDrawOption",
                "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption(walk_navi::NE_MapRenderCustomDrawOption&)",
            )
        )
        self.assertTrue(
            caller_is_same_cpp_method(
                "NaviMapRender::UpdateMapRenderCustomDrawOption",
                "UpdateMapRenderCustomDrawOption",
                "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption(walk_navi::NE_MapRenderCustomDrawOption&)",
            )
        )

    def test_find_callers_includes_same_named_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            crash_file = src / "navi.cpp"
            caller_file = src / "control.cpp"
            crash_file.write_text(
                "void NaviMapRender::UpdateMapRenderCustomDrawOption() {\n"
                "  m_config->x = 1;\n"
                "}\n",
                encoding="utf-8",
            )
            caller_file.write_text(
                "void WalkMapControl::UpdateMapRenderCustomDrawOption() {\n"
                "  if (!m_render) return;\n"
                "  m_render->UpdateMapRenderCustomDrawOption();\n"
                "}\n",
                encoding="utf-8",
            )
            svc = CodeLocatorService(LocatorConfig())
            callers = svc.find_callers(
                "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption()",
                [tmp],
                max_search_files=32,
                stack_priority_files=[str(crash_file), str(caller_file)],
            )
            names = [c.name for c in callers]
            self.assertTrue(any("WalkMapControl" in n for n in names), names)

    def test_stack_has_same_named_trampoline(self) -> None:
        self.assertTrue(
            stack_has_same_named_trampoline(
                [
                    "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption()",
                    "walk_navi::WalkMapControl::UpdateMapRenderCustomDrawOption()",
                ]
            )
        )
        self.assertFalse(
            stack_has_same_named_trampoline(
                [
                    "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption()",
                    "walk_navi::WalkMapControl::Init()",
                ]
            )
        )
        self.assertFalse(
            stack_has_same_named_trampoline(
                ["walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption()"]
            )
        )


class TestNullGuardOnlyPatchRejectsEntryReturn(unittest.TestCase):
    """同名转发存在时拒绝崩溃点整函数入口判空；按分支判空仍允许。"""

    _OLD = (
        "void NaviMapRender::UpdateMapRenderCustomDrawOption() {\n"
        "    m_config->x = 1;\n"
        "}\n"
    )
    _ENTRY_GUARD = (
        "void NaviMapRender::UpdateMapRenderCustomDrawOption() {\n"
        "    if (!m_config) {\n"
        "        return;\n"
        "    }\n"
        "    m_config->x = 1;\n"
        "}\n"
    )
    _BRANCH_GUARD = (
        "void NaviMapRender::UpdateMapRenderCustomDrawOption() {\n"
        "    if (car_bundle) {\n"
        "        foo();\n"
        "    } else if (m_config) {\n"
        "        m_config->x = 1;\n"
        "    }\n"
        "}\n"
    )
    _TRAMPOLINE_CTX = {
        "graph": {
            "call_chain_from_add2line": [
                {
                    "call_order_from_add2line": [
                        "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption()",
                        "walk_navi::WalkMapControl::UpdateMapRenderCustomDrawOption()",
                    ]
                }
            ]
        }
    }

    def test_rejects_entry_guard_when_same_named_trampoline_exists(self) -> None:
        err = _check_null_guard_only_patch(
            self._OLD, self._ENTRY_GUARD, code_context=self._TRAMPOLINE_CTX
        )
        self.assertIsNotNone(err)
        self.assertIn("同名转发", err or "")

    def test_allows_entry_guard_without_trampoline(self) -> None:
        self.assertIsNone(
            _check_null_guard_only_patch(self._OLD, self._ENTRY_GUARD, code_context={})
        )

    def test_allows_branch_guard_even_with_trampoline(self) -> None:
        self.assertIsNone(
            _check_null_guard_only_patch(
                self._OLD, self._BRANCH_GUARD, code_context=self._TRAMPOLINE_CTX
            )
        )


class TestLifecycleContrastHelpers(unittest.TestCase):
    """赋值 / 自投递序言为结构判断，不绑函数名词表。"""

    def test_forwarding_and_assign_and_repost(self) -> None:
        from tools._lifecycle_contrast import (
            deref_members_from_text,
            forwarding_members,
            has_self_repost_prologue,
            member_is_assigned,
            pick_lifecycle_contrast,
        )

        self.assertEqual(deref_members_from_text("m_config->x = 1;"), ["m_config"])
        self.assertEqual(
            forwarding_members(
                "void WalkMapControl::Update() {\n    m_render->Update();\n}\n",
                "Update",
            ),
            ["m_render"],
        )
        self.assertTrue(member_is_assigned("void OnInit() { m_render = 0; }", "m_render"))
        self.assertFalse(member_is_assigned("void Update() { if (!m_render) return; }", "m_render"))
        self.assertFalse(member_is_assigned("void Update() { m_render->Foo(); }", "m_render"))
        hop = (
            "void Init() {\n"
            "    if (Post(WalkMapControl::Init, arg)) return;\n"
            "    OnInit();\n"
            "}\n"
        )
        self.assertTrue(has_self_repost_prologue(hop, "WalkMapControl"))
        self.assertFalse(
            has_self_repost_prologue("void Update() { if (!m_render) return; }", "WalkMapControl")
        )
        picked = pick_lifecycle_contrast(
            caller_has_repost=False,
            assign_names=["WalkMapControl::OnInit"],
            callers_of_assign=["WalkMapControl::Init"],
            hop_exemplars=["WalkMapControl::SetOverlookMode"],
            crash_writers=["NaviMapRender::OnCreate"],
            names_with_repost=["WalkMapControl::Init"],
            cap=3,
        )
        self.assertEqual(
            picked,
            [
                "WalkMapControl::OnInit",
                "WalkMapControl::Init",
                "NaviMapRender::OnCreate",
            ],
        )
        self.assertNotIn("WalkMapControl::SetOverlookMode", picked)


class TestStackCallerThreadAffinitySiblings(unittest.TestCase):
    """调用方赋值切片 + 一层入口；不灌入其它同成员转发 API。"""

    def test_picks_assign_chain_not_bulk_wrappers(self) -> None:
        from tools.code_content_provider_tool import CodeContentProvider, CrashFunction

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            navi = src / "navi.cpp"
            navi.write_text(
                "void NaviMapRender::OnCreate() {\n"
                "    m_config = 0;\n"
                "}\n"
                "void NaviMapRender::UpdateMapRenderCustomDrawOption() {\n"
                "    m_config->x = 1;\n"
                "}\n",
                encoding="utf-8",
            )
            cpp = src / "walk_map_control.cpp"
            cpp.write_text(
                "void WalkMapControl::OnInit() {\n"
                "    m_render = 0;\n"
                "}\n"
                "void WalkMapControl::Init() {\n"
                "    if (Hop(WalkMapControl::Init)) return;\n"
                "    OnInit();\n"
                "}\n"
                "void WalkMapControl::SetOverlookMode() {\n"
                "    if (Hop(WalkMapControl::SetOverlookMode)) return;\n"
                "    m_render->SetOverlookMode();\n"
                "}\n"
                "void WalkMapControl::UpdateMapRenderCustomDrawOption() {\n"
                "    if (!m_render) return;\n"
                "    m_render->UpdateMapRenderCustomDrawOption();\n"
                "}\n",
                encoding="utf-8",
            )
            crash = CrashFunction(
                name="UpdateMapRenderCustomDrawOption",
                signature="void NaviMapRender::UpdateMapRenderCustomDrawOption()",
                snippet=["void NaviMapRender::UpdateMapRenderCustomDrawOption() {"],
                crash_line="m_config->x = 1;",
            )
            frames = [
                {
                    "resolved_function": (
                        "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption()"
                    ),
                    "resolved_file": str(navi),
                },
                {
                    "resolved_function": (
                        "walk_navi::WalkMapControl::UpdateMapRenderCustomDrawOption()"
                    ),
                    "resolved_file": str(cpp),
                },
            ]
            hits = CodeContentProvider()._find_stack_caller_thread_affinity_siblings(
                frames,
                crash,
                frames[0]["resolved_function"],
                str(navi),
                [tmp],
                max_total=3,
            )
            names = [h.name for h in hits]
            self.assertIn("WalkMapControl::OnInit", names)
            self.assertIn("WalkMapControl::Init", names)
            self.assertIn("NaviMapRender::OnCreate", names)
            self.assertFalse(any("SetOverlookMode" in n for n in names), names)
            self.assertTrue(all(h.relation_type == "thread_affinity" for h in hits))

    def test_hop_exemplar_when_assign_chain_lacks_repost(self) -> None:
        from tools.code_content_provider_tool import CodeContentProvider, CrashFunction

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            cpp = src / "control.cpp"
            cpp.write_text(
                "void FooControl::OnInit() {\n"
                "    m_render = 0;\n"
                "}\n"
                "void FooControl::Setup() {\n"
                "    OnInit();\n"
                "}\n"
                "void FooControl::OtherApi() {\n"
                "    if (Hop(FooControl::OtherApi)) return;\n"
                "    m_render->OtherApi();\n"
                "}\n"
                "void FooControl::UpdateMapRenderCustomDrawOption() {\n"
                "    m_render->UpdateMapRenderCustomDrawOption();\n"
                "}\n",
                encoding="utf-8",
            )
            crash = CrashFunction(
                name="UpdateMapRenderCustomDrawOption",
                signature="void NaviMapRender::UpdateMapRenderCustomDrawOption()",
                snippet=[],
                crash_line="m_config->x = 1;",
            )
            frames = [
                {
                    "resolved_function": "NaviMapRender::UpdateMapRenderCustomDrawOption()",
                    "resolved_file": "navi.cpp",
                },
                {
                    "resolved_function": "FooControl::UpdateMapRenderCustomDrawOption()",
                    "resolved_file": str(cpp),
                },
            ]
            hits = CodeContentProvider()._find_stack_caller_thread_affinity_siblings(
                frames,
                crash,
                frames[0]["resolved_function"],
                "navi.cpp",
                [tmp],
                max_total=3,
            )
            names = [h.name for h in hits]
            self.assertIn("FooControl::OnInit", names)
            self.assertIn("FooControl::Setup", names)
            self.assertIn("FooControl::OtherApi", names)


class TestNullPointerImplication(unittest.TestCase):
    def test_null_pointer_implication_does_not_default_to_race(self) -> None:
        parse = {
            "crash_info": {
                "signal": "SIGSEGV (SEGV_MAPERR)",
                "crash_address": "0x408",
            }
        }
        out = DeterministicAnalyzer().analyze(parse, {}, "fault addr 0x408")
        nulls = [f for f in out.facts if f.fact_type == "null_pointer"]
        self.assertTrue(nulls)
        self.assertIn("不自动等于多线程竞态", nulls[0].implication)


if __name__ == "__main__":
    unittest.main()
