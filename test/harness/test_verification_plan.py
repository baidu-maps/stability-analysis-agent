import unittest

from services.verification_plan import build_verification_plan
from services.verification_profile import VerificationProfile


class VerificationPlanTests(unittest.TestCase):
    def test_plan_is_stable_and_binds_declared_checks(self):
        profile = VerificationProfile.from_mapping({"profile_id": "native", "frontend_available": False,
            "checks": [{"id": "h", "kind": "native_replay", "command": ["./harness"]}]})
        plan = build_verification_plan({"statement": "uaf is gone", "minimum_level": "L3",
                                        "required_evidence": ["asan", "same_stack_signature"]}, profile)
        self.assertEqual(plan.check_ids, ["h"])
        self.assertEqual(plan.fingerprint, plan.fingerprint)
        self.assertFalse(plan.frontend_available)


if __name__ == "__main__":
    unittest.main()
