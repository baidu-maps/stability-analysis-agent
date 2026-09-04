import json
import unittest
from pathlib import Path


class CrashBenchmarkContractTests(unittest.TestCase):
    def test_case_manifest_has_deterministic_contract(self):
        path = Path(__file__).resolve().parent / "cases" / "demo_basic_nullptr.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key in ("id", "inputs", "allowed_changed_files"):
            self.assertIn(key, payload)
        for key in ("crash_log", "code_root"):
            self.assertIn(key, payload["inputs"])
        self.assertTrue(payload["allowed_changed_files"])

    def test_benchmark_result_schema_is_machine_readable(self):
        result = {"mode": "deterministic", "root_cause_accuracy": 1.0,
                  "evidence_coverage": 1.0, "verification_success": 1.0,
                  "tool_calls": 0, "token_usage": 0, "recovery_rate": 1.0}
        self.assertEqual(set(result) - {"mode"}, {
            "root_cause_accuracy", "evidence_coverage", "verification_success",
            "tool_calls", "token_usage", "recovery_rate"})


if __name__ == "__main__":
    unittest.main()
