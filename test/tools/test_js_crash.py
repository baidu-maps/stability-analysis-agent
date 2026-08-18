#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.js_crash.core import diagnose_js_crash, extract_js_error, first_application_frame, looks_like_js_crash, match_js_fault_mode
from tools.js_crash.tool import JsCrashDiagnosisTool
from tool_system.registry import ToolAndWorkflowRegistry
from tools import register_all_tools


class JsCrashDiagnosisTests(unittest.TestCase):
    def test_extracts_faultlogger_fields_from_raw_log(self) -> None:
        error = extract_js_error({"raw_content": "Reason: TypeError\nError message: Cannot read properties of undefined\nError code: 401\n"})
        self.assertEqual(error["reason"], "TypeError")
        self.assertEqual(error["message"], "Cannot read properties of undefined")
        self.assertEqual(error["code"], "401")

    def test_type_error_matches_precise_fault_mode(self) -> None:
        result = diagnose_js_crash({"crash_info": {"crash_reason": "TypeError", "error_message": "Cannot read properties of undefined"}, "threads": [{"frames": [{"function": "HomePage.render", "layer": "arkts", "file": "Home.ets", "line": 42}]}]})
        self.assertEqual(result["fault_mode"]["id"], "JSC-FM-01")
        self.assertEqual(result["diagnosis_status"], "confirmed")
        self.assertEqual(result["stack"]["js_frames"][0]["file"], "Home.ets")

    def test_error_name_only_produces_probable_secondary_match(self) -> None:
        mode = match_js_fault_mode({"name": "ReferenceError", "message": ""})
        self.assertEqual(mode["id"], "JSC-FM-03")
        self.assertEqual(mode["confidence"], 0.55)
        self.assertIn("未收录子类", mode["level_3"])

    def test_hybrid_stack_detects_napi_boundary(self) -> None:
        result = diagnose_js_crash({"crash_info": {"error_name": "BusinessError", "error_message": "permission denied"}, "threads": [{"frames": [{"function": "request", "language": "arkts"}, {"function": "napi_call_function", "module": "libace_napi.z.so", "language": "cpp"}]}]})
        self.assertEqual(result["fault_mode"]["id"], "JSC-FM-08")
        self.assertTrue(result["stack"]["has_hybrid_stack"])
        self.assertEqual(len(result["stack"]["hybrid_frames"]), 1)

    def test_missing_evidence_is_explicit(self) -> None:
        result = diagnose_js_crash({"crash_info": {"crash_reason": "Error"}})
        self.assertEqual(result["diagnosis_status"], "preliminary")
        self.assertIn("Error message", result["missing_evidence"])
        self.assertIn("JS/ArkTS 应用栈或 source map", result["missing_evidence"])

    def test_tool_accepts_wrapped_parse_result(self) -> None:
        tool = JsCrashDiagnosisTool()
        valid, error = tool.validate_input({"parse_result": {"crash_info": {}}})
        self.assertTrue(valid)
        self.assertIsNone(error)
        result = tool.execute({"parse_result": {"crash_info": {"error_name": "URIError", "error_message": "URI malformed"}}})
        self.assertEqual(result["fault_mode"]["id"], "JSC-FM-06")

    def test_first_app_frame_skips_framework(self) -> None:
        frame = first_application_frame([
            {"function": "stateMgmt.js", "file": "stateMgmt.js"},
            {"function": "HomePage.render", "file": "Home.ets"},
        ])
        self.assertEqual(frame["file"], "Home.ets")
        self.assertTrue(looks_like_js_crash({"crash_info": {"error_name": "TypeError"}}))
        self.assertFalse(looks_like_js_crash({"crash_info": {"signal": "SIGSEGV"}}))

    def test_provide_consume_and_arraybuffer_patterns(self) -> None:
        self.assertEqual(match_js_fault_mode({"name": "ReferenceError", "message": "missing @Provide property foo"})["id"], "JSC-FM-09")
        self.assertEqual(match_js_fault_mode({"name": "Error", "message": "The underlying ArrayBuffer is null or detached."})["id"], "JSC-FM-10")

    def test_tool_is_registered_as_builtin(self) -> None:
        registry = ToolAndWorkflowRegistry()
        register_all_tools(registry)
        self.assertIsNotNone(registry.get_tool("js_crash_diagnosis"))


if __name__ == "__main__":
    unittest.main()
