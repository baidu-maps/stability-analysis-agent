#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.api_fault.core import diagnose_api_fault, normalize_api_error
from tools.api_fault.tool import ApiFaultDiagnosisTool
from tool_system.registry import ToolAndWorkflowRegistry
from tools import register_all_tools


class ApiFaultTests(unittest.TestCase):
    def test_normalizes_hex_and_decimal_codes(self) -> None:
        self.assertEqual(normalize_api_error({"error_code": "0x10"})["normalized_code"], "16")
        self.assertEqual(normalize_api_error({"error_code": 5400105})["code_format"], "decimal")

    def test_media_service_died_matches_knowledge(self) -> None:
        result = diagnose_api_fault({"error_code": "5400105", "error_name": "BusinessError", "message": "media service died", "api": "AVPlayer"})
        self.assertEqual(result["diagnosis_status"], "confirmed")
        self.assertEqual(result["module_classification"]["selected"]["module"], "multimedia")
        self.assertEqual(result["knowledge_matches"][0]["id"], "multimedia.service_died")
        self.assertTrue(result["repair_guidance"]["defensive_fix"])

    def test_missing_evidence_is_explicit(self) -> None:
        result = diagnose_api_fault({"error_name": "BusinessError"})
        self.assertEqual(result["diagnosis_status"], "preliminary")
        self.assertTrue(any(item["id"] == "api_name" for item in result["missing_evidence"]))
        self.assertTrue(result["next_questions"])

    def test_project_api_usage_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.ets").write_text("const player = AVPlayer.create();", encoding="utf-8")
            result = diagnose_api_fault({"message": "media service died", "api": "AVPlayer"}, project_root=str(root))
            self.assertEqual(result["project_context"]["status"], "success")
            self.assertEqual(result["project_context"]["api_usage_sites"], ["player.ets"])

    def test_io_error_code_and_repeated_reset_timeline(self) -> None:
        result = diagnose_api_fault({"error_code": "5400103", "message": "IO Error", "api": "AVPlayer"})
        self.assertEqual(result["knowledge_matches"][0]["id"], "multimedia.io_error")
        result = diagnose_api_fault({"error_name": "BusinessError", "message": "invalid state", "api": "AVPlayer", "raw_log": "reset reset reset prepare"})
        self.assertTrue(result["state_timeline"]["escalate_to_app"])
        self.assertTrue(any(item["id"] == "common.repeated_reset" for item in result["knowledge_matches"]))

    def test_tool_registration(self) -> None:
        tool = ApiFaultDiagnosisTool()
        self.assertEqual(tool.definition.name, "api_fault_diagnosis")
        registry = ToolAndWorkflowRegistry()
        register_all_tools(registry)
        self.assertIsNotNone(registry.get_tool("api_fault_diagnosis"))


if __name__ == "__main__":
    unittest.main()
