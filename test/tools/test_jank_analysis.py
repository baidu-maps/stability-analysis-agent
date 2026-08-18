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

from tools.jank_analysis.core import analyze_jank_artifact, classify_trace_artifact
from tools.jank_analysis.tool import JankAnalyzerTool


class JankAnalysisTests(unittest.TestCase):
    def test_classifies_trace_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "capture.htrace").write_bytes(b"trace")
            self.assertEqual(classify_trace_artifact(str(root / "capture.htrace"))["artifact_type"], "trace")
            (root / "frames.json").write_text("[]", encoding="utf-8")
            self.assertEqual(classify_trace_artifact(str(root / "frames.json"))["artifact_type"], "analysis_report")

    def test_normalizes_frames_and_detects_jank_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frames.json"
            path.write_text(json.dumps({"frames": [
                {"frame_id": "1", "duration_ms": 8, "thread": "UIThread", "stage": "Build"},
                {"frame_id": "2", "duration_ms": 42, "thread": "UIThread", "stage": "Layout"},
                {"frame_id": "3", "duration_ms": 30, "thread": "RenderThread", "stage": "Fence"},
                {"thread": "UIThread", "state": "running", "duration_ms": 4},
                {"thread": "UIThread", "state": "blocked", "duration_ms": 3},
            ]}), encoding="utf-8")
            result = analyze_jank_artifact(str(path), top_n=5)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["summary"]["frame_count"], 3)
            self.assertEqual(result["summary"]["jank_count"], 2)
            self.assertTrue(any(item["id"] == "JANK-FM-03" for item in result["fault_modes"]))
            self.assertTrue(any(item["id"] == "JANK-FM-05" for item in result["fault_modes"]))
            self.assertEqual(result["cpu_state_stats"]["UIThread"]["blocked"], 3.0)
            self.assertTrue(result["joint_root_causes"])
            self.assertIn("level_2", result["joint_root_causes"][0])
            self.assertIn("level_3", result["joint_root_causes"][0])

    def test_false_jank_value_is_not_treated_as_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frames.json"
            path.write_text(json.dumps({"frames": [{"duration_ms": 8, "jank": "false"}]}), encoding="utf-8")
            result = analyze_jank_artifact(str(path))
            self.assertEqual(result["summary"]["jank_count"], 0)

    def test_completion_latency_reports_missing_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            path.write_text(json.dumps({"events": [{"name": "other", "ts": 100}]}), encoding="utf-8")
            result = analyze_jank_artifact(str(path), mode="completion_latency")
            self.assertEqual(result["completion_latency"]["status"], "insufficient_evidence")
            self.assertIn("completion-latency-tags", result["completion_latency"]["suggestion"])

    def test_completion_latency_calculates_ms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            path.write_text(json.dumps({"events": [{"name": "touch", "ts": 1_000_000_000}, {"name": "complete", "ts": 1_005_200_000}]}), encoding="utf-8")
            result = analyze_jank_artifact(str(path), mode="completion_latency")
            self.assertEqual(result["completion_latency"]["completion_latency_ms"], 5.2)

    def test_binary_trace_requires_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.htrace"
            path.write_bytes(b"trace")
            result = analyze_jank_artifact(str(path))
            self.assertEqual(result["status"], "unsupported")
            self.assertIn("external trace analyzer", result["message"])

    def test_tool_contract(self) -> None:
        tool = JankAnalyzerTool()
        self.assertEqual(tool.definition.name, "jank_analyzer")
        self.assertEqual(tool.validate_input({})[0], False)


if __name__ == "__main__":
    unittest.main()
