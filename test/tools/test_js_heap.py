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

from tools.js_heap.core import analyze_js_heap, classify_heap_artifact
from tools.js_heap.tool import JsHeapAnalyzerTool


def snapshot(extra: int = 0) -> dict:
    # Nodes: GlobalHandler -> retained object -> string.
    meta = {
        "node_fields": ["type", "name", "id", "self_size", "edge_count"],
        "node_types": [["hidden", "object", "string"]],
        "edge_fields": ["type", "name_or_index", "to_node"],
        "edge_types": [["property"]],
    }
    width = len(meta["node_fields"])
    nodes = [1, 0, 1, 8, 1, 1, 1, 2, 20 + extra, 1, 2, 2, 3, 10, 0]
    edges = [0, 0, width, 0, 0, width * 2]
    return {"snapshot": {"meta": meta}, "nodes": nodes, "edges": edges}


class JsHeapTests(unittest.TestCase):
    def test_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.heapsnapshot").write_text("{}", encoding="utf-8")
            (root / "b.rawheap").write_bytes(b"raw")
            result = classify_heap_artifact(str(root))
            self.assertEqual(result["artifact_type"], "heapsnapshot")
            self.assertEqual(result["snapshot_count"], 1)

    def test_retained_size_and_fault_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.heapsnapshot"
            data = snapshot()
            data["snapshot"]["meta"]["node_types"][0] = ["hidden", "object", "string"]
            data["nodes"][1] = 0
            data["nodes"][2] = 1
            data["strings"] = ["GlobalHandler", "retained", "value"]
            path.write_text(json.dumps(data), encoding="utf-8")
            result = analyze_js_heap(str(path), top_n=5)
            self.assertEqual(result["status"], "success")
            self.assertGreaterEqual(result["clusters"][0]["retained_size"], 20)
            self.assertTrue(any(item["id"] == "JS-FM-01" for item in result["fault_modes"]))

    def test_baseline_compare_detects_growth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current.heapsnapshot"
            baseline = Path(tmp) / "baseline.heapsnapshot"
            now = snapshot(extra=40)
            before = snapshot(extra=0)
            now["strings"] = before["strings"] = ["GlobalHandler", "retained", "value"]
            now["nodes"][1] = before["nodes"][1] = 0
            current.write_text(json.dumps(now), encoding="utf-8")
            baseline.write_text(json.dumps(before), encoding="utf-8")
            result = analyze_js_heap(str(current), top_n=5, baseline=str(baseline))
            self.assertEqual(result["comparison"]["status"], "success")
            self.assertGreaterEqual(result["comparison"]["grown_count"], 1)
            self.assertIn(result["clusters"][0]["root_kind"], {"ROOT_GLOBAL_HANDLE", "ROOT_VM", "UNKNOWN"})

    def test_rawheap_requires_external_converter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.rawheap"
            path.write_bytes(b"raw")
            result = analyze_js_heap(str(path))
            self.assertEqual(result["status"], "unsupported")
            self.assertIn("translator", result["message"])

    def test_tool_contract(self) -> None:
        tool = JsHeapAnalyzerTool()
        valid, error = tool.validate_input({"path": "/tmp/x"})
        self.assertTrue(valid)
        self.assertIsNone(error)
        valid, error = tool.validate_input({})
        self.assertFalse(valid)
        self.assertEqual(error, "path is required")


if __name__ == "__main__":
    unittest.main()
