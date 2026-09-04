import unittest

from services.memory_feedback import record_verified_feedback


class MemoryAdmissionTests(unittest.TestCase):
    def test_pending_and_skipped_verification_are_not_recorded(self):
        for status in ("pending", "skipped", "unavailable"):
            result = record_verified_feedback({"pattern_hits": ["p1"],
                                               "verification": {"status": status}})
            self.assertFalse(result["recorded"])
            self.assertEqual(result["reason"], "verification_not_terminal")


if __name__ == "__main__":
    unittest.main()
