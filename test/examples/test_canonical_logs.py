import unittest
from pathlib import Path

from tools.crash_log_parser_tool import crash_log_parser


ROOT = Path(__file__).resolve().parents[2] / "examples" / "crash_cases"


class CanonicalCrashLogTests(unittest.TestCase):
    def test_canonical_logs_are_readable_without_binaries(self):
        logs = list(ROOT.glob("*/logs/canonical/*.log"))
        self.assertGreaterEqual(len(logs), 6)
        for path in logs:
            # The parser accepts vendor-specific text and must never require a
            # local dylib/so merely to ingest a canonical fixture.
            result = crash_log_parser(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(result, path)


if __name__ == "__main__":
    unittest.main()
