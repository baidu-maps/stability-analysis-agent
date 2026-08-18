#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.appfreeze.core import analyze_appfreeze, classify_freeze_type, cluster_stack_samples, compare_block_vs_busy, detect_dependency_cycles, parse_binder_text, parse_system_load
from tools.appfreeze.tool import AppFreezeDiagnosisTool
from tool_system.registry import ToolAndWorkflowRegistry
from tools import register_all_tools


class AppFreezeTests(unittest.TestCase):
    def test_freeze_type_and_timeout(self) -> None:
        result = classify_freeze_type({}, "name_: APP_FREEZE\nREASON: THREAD_BLOCK_6S")
        self.assertEqual(result["freeze_type"], "THREAD_BLOCK_6S")
        self.assertEqual(result["timeout_threshold_ms"], 6000)

    def test_multi_sample_stack_cluster(self) -> None:
        clusters = cluster_stack_samples([
            {"timestamp": "3s", "frames": ["main", "EventHandler::Process", "App::Run"]},
            {"timestamp": "6s", "frames": ["main", "EventHandler::Process", "App::Run"]},
            {"timestamp": "20s", "frames": ["main", "Other", "App::Run"]},
        ])
        self.assertEqual(clusters[0]["sample_count"], 2)
        self.assertEqual(clusters[0]["stable_prefix"][1], "EventHandler::Process")

    def test_ffrt_cycle_and_fault_mode(self) -> None:
        edges = [{"source": "A", "target": "B"}, {"source": "B", "target": "C"}, {"source": "C", "target": "A"}]
        self.assertEqual(detect_dependency_cycles(edges), [["A", "B", "C", "A"]])
        result = analyze_appfreeze({"freeze_reason": "FFRT_TIMEOUT", "samples": [{"frames": ["main", "Worker"]}], "ffrt_edges": edges})
        self.assertEqual(result["freeze"]["freeze_type"], "FFRT_TIMEOUT")
        self.assertTrue(any(mode["id"] == "FREEZE-FM-06" for mode in result["fault_modes"]))
        self.assertTrue(result["evidence_chain"])

    def test_application_and_system_guidance_are_separate(self) -> None:
        result = analyze_appfreeze({"freeze_type": "APPFREEZE"})
        self.assertIn("application", result["repair_guidance"])
        self.assertIn("system_observation", result["repair_guidance"])
        self.assertIn("多时间点采样栈", result["missing_evidence"])

    def test_system_stress_gate_and_binder_graph(self) -> None:
        load = parse_system_load("NOTE: low memory and thermal throttling\nCPU Usage: 91%\nMemAvailable: 512 MB")
        self.assertTrue(load["system_stressed"])
        binder = parse_binder_text("20020:20020 to 1234:5678 code 5f475352 wait:6.5 s\n1234:5678 to 20020:20020 code 5f475352 wait:1.0 s")
        self.assertTrue(binder["cycles"])
        result = analyze_appfreeze({"freeze_type": "APPFREEZE"}, raw_content="NOTE: low memory and thermal throttling\nCPU Usage: 91%")
        self.assertTrue(any(mode["id"] == "FREEZE-FM-11" for mode in result["fault_modes"]))
        self.assertTrue(result["system_load"]["early_exit"])

    def test_block_vs_busy_from_3s_6s_samples(self) -> None:
        comparison = compare_block_vs_busy([
            {"timestamp": "3s", "frames": ["main", "Lock", "App::Run"]},
            {"timestamp": "6s", "frames": ["main", "Lock", "App::Run"]},
        ])
        self.assertEqual(comparison["kind"], "BLOCKED")
        comparison = compare_block_vs_busy([
            {"timestamp": "3s", "frames": ["main", "Lock", "App::Run"]},
            {"timestamp": "6s", "frames": ["main", "Compute", "App::Run"]},
        ])
        self.assertEqual(comparison["kind"], "BUSY")

    def test_tool_registration(self) -> None:
        tool = AppFreezeDiagnosisTool()
        self.assertEqual(tool.definition.name, "appfreeze_diagnosis")
        registry = ToolAndWorkflowRegistry()
        register_all_tools(registry)
        self.assertIsNotNone(registry.get_tool("appfreeze_diagnosis"))


if __name__ == "__main__":
    unittest.main()
