import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "examples" / "crash_cases"
CASES = [p for p in ROOT.iterdir() if p.is_dir() and (p / "case.json").is_file()]


class ExampleCaseSchemaTests(unittest.TestCase):
    def test_first_capability_cases_have_required_contract(self):
        self.assertGreaterEqual(len(CASES), 6)
        for case_dir in CASES:
            data = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
            for key in ("schema_version", "case_id", "problem_type", "crash_log", "code_roots",
                        "frontend_available", "runtime_available", "binary_required",
                        "allowed_changed_files", "expected_root_cause", "expected_evidence"):
                self.assertIn(key, data, case_dir.name)
            self.assertEqual(data["case_id"], case_dir.name)
            self.assertTrue((case_dir / data["crash_log"]).is_file(), case_dir.name)
            for root in data["code_roots"]:
                self.assertTrue((case_dir / root).is_dir(), case_dir.name)
            for item in data["allowed_changed_files"]:
                self.assertTrue(any((case_dir / item).exists() or (case_dir / root / item).exists()
                                    for root in data["code_roots"]), item)

    def test_cases_do_not_commit_platform_binaries(self):
        suffixes = {".dylib", ".so", ".dSYM", ".exe"}
        for case_dir in CASES:
            files = [p for p in case_dir.rglob("*") if p.is_file() and p.suffix in suffixes]
            self.assertEqual(files, [], case_dir.name)


if __name__ == "__main__":
    unittest.main()
