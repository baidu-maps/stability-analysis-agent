import unittest

from services.verification import parse_verification_diagnostics, verification_observation


class VerificationDiagnosticsTests(unittest.TestCase):
    def test_compiler_and_test_diagnostics_are_structured(self):
        output = "src/a.cpp:12:4: error: bad pointer\nlib/a.cpp:8: warning: suspicious value"
        rows = parse_verification_diagnostics(output)
        self.assertEqual(rows[0]["line"], 12)
        self.assertEqual(rows[0]["column"], 4)
        self.assertEqual(rows[1]["severity"], "warning")
        observation = verification_observation({"status": "failed", "provider": "compiler", "stderr": output})
        self.assertEqual(len(observation["diagnostics"]), 2)


if __name__ == "__main__":
    unittest.main()
