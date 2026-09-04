import unittest

from services.context_compactor import ContextCompactor
from services.context_parts import ContextPart


class ContextCompactionAtomicityTests(unittest.TestCase):
    def test_atomic_group_is_kept_or_dropped_as_a_unit(self):
        parts = [
            ContextPart(kind="tool_call", content="CALL", priority="recent_observation", atomic_group="a"),
            ContextPart(kind="tool_result", content="RESULT", priority="recent_observation", atomic_group="a"),
            ContextPart(kind="observation", content="old" * 100, priority="history"),
        ]
        compacted = ContextCompactor().compact(parts, max_chars=20)
        self.assertIn("CALL", compacted.text)
        self.assertIn("RESULT", compacted.text)
        self.assertGreaterEqual(compacted.metadata["atomic_groups"], 2)

    def test_summary_and_token_counter_failures_fallback(self):
        result = ContextCompactor().compact(
            [{"content": "history " * 200, "priority": "history"},
             {"content": "FINAL JSON CONTRACT", "priority": "control"}],
            max_chars=100,
            max_tokens=25,
            summary_provider=lambda _: (_ for _ in ()).throw(RuntimeError("down")),
            token_counter=lambda _: (_ for _ in ()).throw(RuntimeError("counter")),
        )
        self.assertIn("FINAL JSON CONTRACT", result.text)
        self.assertEqual(result.metadata["summary_provider_used"], 0)


if __name__ == "__main__":
    unittest.main()
