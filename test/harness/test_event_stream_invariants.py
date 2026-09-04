import unittest

from tool_system.runtime import RunTrace


class EventStreamInvariantTests(unittest.TestCase):
    def test_sequence_parent_and_large_payload_contract(self):
        trace = RunTrace(run_id="run_test")
        first = trace.emit("action.started", kind="action", name="repo_search", round=1)
        trace.emit("tool.result", kind="tool", name="repo_search", status="success",
                    parent_event_id=first["event_id"], output="x" * 10000, round=1)
        events = trace.snapshot()["events"]
        self.assertEqual([e["seq"] for e in events], [1, 2])
        self.assertEqual(len({e["event_id"] for e in events}), len(events))
        self.assertEqual(events[1]["parent_event_id"], events[0]["event_id"])
        self.assertNotIn("output", events[1])
        self.assertTrue(events[1]["output_hash"])

    def test_snapshot_round_trip_preserves_canonical_fields(self):
        trace = RunTrace()
        trace.emit("verification.completed", kind="verification", status="success", round=2)
        restored = RunTrace.from_dict(trace.snapshot())
        self.assertEqual(restored.events[0]["canonical_event"], "verification")
        self.assertEqual(restored.events[0]["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
