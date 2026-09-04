from __future__ import annotations

import json
import unittest

from services.evidence_ingest import ingest_diagnosis, normalize_diagnosis_for_evaluation
from services.evidence_store import EvidenceStore
from services.run_snapshot import HarnessRunSnapshot


class EvidenceIngestTests(unittest.TestCase):
    def test_ingest_diagnosis_expands_evidence_compass(self):
        store = EvidenceStore()
        diagnosis = {
            "crash_classification": {"primary_pattern": "null_pointer"},
            "evidence_compass": {
                "confidence_ceiling": 0.82,
                "layers": {"stack": {"available": True}, "source": {"available": False}},
                "missing_evidence": [{"kind": "logcat"}],
            },
        }
        ingest_diagnosis(store, diagnosis)
        kinds = {item.get("kind") for item in store.items()}
        self.assertIn("crash_diagnosis", kinds)
        self.assertIn("missing_evidence", kinds)
        self.assertIn("evidence_layer:stack", kinds)

    def test_ingest_diagnosis_includes_compass_notes(self):
        store = EvidenceStore()
        ingest_diagnosis(store, {
            "evidence_compass": {
                "confidence_note_zh": "寄存器不足",
                "analysis_order_zh": "栈优先",
                "layers": {
                    "pc_vs_fault": {"available": True, "summary_zh": "PC 与 fault 不一致"},
                },
            },
        })
        kinds = {item.get("kind") for item in store.items()}
        self.assertIn("confidence_note", kinds)
        self.assertIn("analysis_order", kinds)
        self.assertIn("pc_vs_fault", kinds)

    def test_normalize_diagnosis_for_evaluation(self):
        normalized = normalize_diagnosis_for_evaluation({
            "crash_classification": {"primary_pattern": "SIGSEGV"},
            "stack_summary": {"crash_file": "foo.cpp", "crash_function": "bar"},
            "evidence_compass": {"missing_evidence": [{"x": 1}], "layers": {"a": {"available": True}}},
        })
        self.assertEqual(normalized.get("category"), "SIGSEGV")
        self.assertEqual(normalized.get("file"), "foo.cpp")
        self.assertEqual(len(normalized.get("missing_evidence") or []), 1)


class RunSnapshotTimelineTests(unittest.TestCase):
    def test_unified_timeline_merges_sources(self):
        snap = HarnessRunSnapshot(
            run_id="r1",
            transport_status="done",
            runtime_trace={"events": [{"event": "tool.success", "seq": 2}]},
            events=[{"type": "stdout", "seq": 1, "data": {"line": "ok"}}],
        )
        timeline = snap.unified_timeline()
        sources = {item.get("source") for item in timeline}
        self.assertIn("harness", sources)
        self.assertIn("transport", sources)
        self.assertEqual(snap.timeline_summary()["total"], 2)


if __name__ == "__main__":
    unittest.main()
