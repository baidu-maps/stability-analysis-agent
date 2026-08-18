#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cpp_crash.core import classify_stack_layers, diagnose_cpp_crash, extract_cpp_evidence, match_cpp_fault_modes
from tools.cpp_crash.hints import match_crash_hints
from tools.cpp_crash.tool import CppCrashDiagnosisTool
from tool_system.registry import ToolAndWorkflowRegistry
from tools import register_all_tools


class CppCrashDiagnosisTests(unittest.TestCase):
    def test_extracts_signal_and_fault_address_from_raw_log(self) -> None:
        evidence = extract_cpp_evidence({"raw_content": "Reason:Signal:SIGSEGV(SEGV_MAPERR)@0x0\n"})
        self.assertEqual(evidence["signal"], "SIGSEGV")
        self.assertEqual(evidence["signal_code"], "SEGV_MAPERR")
        self.assertEqual(evidence["fault_address"], "0x0")

    def test_null_address_matches_high_confidence_mode(self) -> None:
        result = diagnose_cpp_crash({"crash_info": {"signal": "SIGSEGV", "fault_addr": "0x0"}, "registers": {"pc": "0x1234"}, "raw_log_sections": {"memory_near": True}, "threads": [{"frames": [{"function": "foo", "module": "libfoo.so", "language": "cpp"}]}]})
        self.assertEqual(result["diagnosis_status"], "confirmed")
        self.assertEqual(result["fault_modes"][0]["id"], "CPP-FM-01")
        self.assertTrue(result["repair_guidance"]["direct_fix"])

    def test_poison_address_requests_asan(self) -> None:
        modes = match_cpp_fault_modes({"signal": "SIGSEGV", "fault_address": "0xdeadbeef", "native_frames": []})
        self.assertTrue(any(item["id"] == "CPP-FM-02" for item in modes))
        result = diagnose_cpp_crash({"crash_info": {"signal": "SIGSEGV", "fault_addr": "0xdeadbeef"}})
        self.assertTrue(any(item["id"] == "asan_reproduction" for item in result["follow_up_checks"]))

    def test_assert_and_napi_stack_evidence(self) -> None:
        result = diagnose_cpp_crash({"crash_info": {"signal": "SIGABRT"}, "threads": [{"frames": [{"function": "__assert_fail", "module": "libc.so"}, {"function": "napi_create_reference", "module": "libnapi.so", "language": "cpp"}]}]})
        ids = {item["id"] for item in result["fault_modes"]}
        self.assertIn("CPP-FM-08", ids)
        self.assertIn("CPP-FM-11", ids)

    def test_preliminary_result_lists_missing_evidence(self) -> None:
        result = diagnose_cpp_crash({"crash_info": {}})
        self.assertEqual(result["diagnosis_status"], "preliminary")
        self.assertIn("signal", result["missing_evidence"])
        self.assertIn("registers", result["missing_evidence"])

    def test_stack_layers_skip_runtime_frames(self) -> None:
        layers = classify_stack_layers([
            {"function": "abort", "module": "libc.so"},
            {"function": "napi_create_reference", "module": "libace_napi.z.so"},
            {"function": "App::OnClick", "module": "/data/app/libapp.so"},
        ])
        self.assertEqual(layers["crash_frame"]["function"], "abort")
        self.assertEqual(layers["first_application_frame"]["function"], "App::OnClick")
        self.assertFalse(layers["runtime_only"])

    def test_hints_detect_uncaught_exception_and_js_oom(self) -> None:
        hits = match_crash_hints(last_fatal_message="terminating due to uncaught exception of type std::runtime_error")
        self.assertTrue(any(item["id"] == "uncaught_exception" for item in hits))
        hits = match_crash_hints(stack_text="libark_jsruntime.so(ThrowOutOfMemoryError+0x10)")
        self.assertTrue(any(item["id"] == "js_oom" for item in hits))

    def test_si_code_and_gwp_asan_modes(self) -> None:
        result = diagnose_cpp_crash({"crash_info": {"signal": "SIGSEGV", "si_code": "SEGV_ACCERR", "fault_addr": "0x1000"}, "raw_content": "GWP-ASan: Use After Free"})
        ids = {item["id"] for item in result["fault_modes"]}
        self.assertIn("CPP-FM-03", ids)
        self.assertIn("CPP-FM-12", ids)
        self.assertEqual(result["evidence_grade"], "detector")
        self.assertEqual(result["signal_taxonomy"]["level_3"], "映射权限错误")

    def test_tool_registration(self) -> None:
        tool = CppCrashDiagnosisTool()
        self.assertEqual(tool.definition.name, "cpp_crash_diagnosis")
        registry = ToolAndWorkflowRegistry()
        register_all_tools(registry)
        self.assertIsNotNone(registry.get_tool("cpp_crash_diagnosis"))


if __name__ == "__main__":
    unittest.main()
