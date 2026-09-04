import json
import unittest
from pathlib import Path

from services.verification_profile import VerificationProfile


ROOT = Path(__file__).resolve().parents[2] / "examples" / "crash_cases"


class ExampleVerificationProfileTests(unittest.TestCase):
    def test_profile_commands_are_explicit_argv(self):
        for path in ROOT.glob("*/.crash-agent/verification.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            profile = VerificationProfile.from_mapping(payload)
            self.assertTrue(profile.checks)
            for check in profile.checks:
                self.assertIsInstance(check.command, list)
                self.assertTrue(all(isinstance(item, str) for item in check.command))

    def test_static_only_case_has_no_profile(self):
        payload = json.loads((ROOT / "agent_static_only_native" / "case.json").read_text(encoding="utf-8"))
        self.assertIsNone(payload["verification_profile"])
        self.assertFalse(payload["binary_required"])


if __name__ == "__main__":
    unittest.main()
