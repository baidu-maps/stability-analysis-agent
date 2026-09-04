#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.diagnosis.external import run_external_tool
from tools.diagnosis.knowledge import KnowledgeRegistry, default_registry, register_builtin_knowledge
from tools.diagnosis.models import DiagnosisResult, KnowledgeEntry, normalize_diagnosis_result
from tools.diagnosis.project_context import discover_project_context
from tools.diagnosis.repair_gate import evaluate_repair_gate
from tools.diagnosis.report import build_report_manifest, write_report_manifest


class DiagnosisInfrastructureTests(unittest.TestCase):
    def test_normalizes_legacy_specialist_result(self) -> None:
        result = normalize_diagnosis_result({"status": "success", "diagnosis_status": "confirmed", "fault_modes": [{"id": "X"}], "confidence": 0.9}, "cpp_crash")
        self.assertEqual(result["domain"], "cpp_crash")
        self.assertEqual(result["confidence"], 0.9)
        self.assertNotIn("legacy_result", result)

    def test_knowledge_registry_search(self) -> None:
        registry = KnowledgeRegistry([KnowledgeEntry("x", "api_fault", "media", "service died", ["service died"])])
        self.assertEqual(registry.search(domain="api_fault", text="media service died")[0].id, "x")

    def test_builtin_knowledge_registers_api_fault_entries(self) -> None:
        count = register_builtin_knowledge()
        self.assertGreaterEqual(count, 6)
        self.assertIsNotNone(default_registry.get("multimedia.service_died"))

    def test_external_executor_reports_success_and_unavailable(self) -> None:
        self.assertEqual(run_external_tool(["/bin/echo", "ok"]).status, "success")
        self.assertEqual(run_external_tool(["/path/does/not/exist"]).status, "unavailable")

    def test_project_context_is_bounded_and_finds_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.ets").write_text("AVPlayer.create()", encoding="utf-8")
            context = discover_project_context(str(root), ["AVPlayer"])
            self.assertEqual(context["api_usage_sites"], ["main.ets"])

    def test_repair_gate_blocks_preliminary(self) -> None:
        self.assertFalse(evaluate_repair_gate({"diagnosis_status": "preliminary", "confidence": 0.99}).allowed)
        self.assertTrue(evaluate_repair_gate({"diagnosis_status": "confirmed", "confidence": 0.9}).allowed)
        self.assertFalse(evaluate_repair_gate({"diagnosis_status": "probable", "confidence": 0.9}).allowed)
        self.assertTrue(evaluate_repair_gate({"diagnosis_status": "probable", "confidence": 0.9}, allow_probable=True).allowed)

    def test_report_manifest_indexes_json_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01_input.json").write_text("{}", encoding="utf-8")
            (root / "04c_diagnosis.json").write_text("{}", encoding="utf-8")
            path = write_report_manifest(root, request={"scope": "full"}, result=DiagnosisResult("cpp_crash", diagnosis_status="confirmed", confidence=0.9).to_dict())
            manifest = build_report_manifest(root)
            self.assertEqual(path.name, "report_manifest.json")
            self.assertIn("01_input", manifest["artifacts"])


if __name__ == "__main__":
    unittest.main()
