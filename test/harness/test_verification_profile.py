import tempfile
import unittest

from services.verification import VerificationRequest, create_verification_provider
from services.verification_baseline import compare_verification_runs
from services.verification_profile import VerificationProfile, normalize_verification_config
from services.verification_plan import build_reproduction_plan, capabilities_from_profile


class VerificationProfileTests(unittest.TestCase):
    def _profile(self):
        return {"profile_id": "native", "checks": [{"id": "compile", "kind": "target_compile",
                "command": ["python3", "-c", "pass"], "requires_approval": False}]}

    def test_profile_requires_explicit_argv_and_selects_declared_check(self):
        profile = VerificationProfile.from_mapping(self._profile())
        self.assertEqual(profile.check("compile").command[0], "python3")
        self.assertEqual(normalize_verification_config(None)["status"], "not_configured")
        provider = create_verification_provider(self._profile(), approved=True)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(provider.verify(VerificationRequest(workspace=tmp, mode="target_compile")).status, "passed")

    def test_legacy_command_remains_supported(self):
        normalized = normalize_verification_config({"command": ["true"]})
        self.assertEqual(normalized["status"], "configured")
        self.assertEqual(normalized["profile"]["checks"][0]["command"], ["true"])

    def test_capability_and_reproduction_plan_are_profile_bound(self):
        profile = VerificationProfile.from_mapping({"verification": {
            "id": "replay", "kind": "replay", "provider": "test_runner",
            "description": "fixed fixture", "command": ["runner"],
            "verification_level": "L3", "evidence_types": ["stack_signature"]}})
        self.assertEqual(capabilities_from_profile(profile)[0].check_id, "replay")
        plan = build_reproduction_plan(
            {"statement": "target crash reproduces", "minimum_level": "L3"}, profile,
            {"check_id": "replay", "purpose": "pre_fix_reproduce"})
        self.assertTrue(plan.plan_fingerprint)
        with self.assertRaises(ValueError):
            build_reproduction_plan({"statement": "x"}, profile,
                                    {"check_id": "replay", "purpose": "pre_fix_reproduce", "command": ["bad"]})

    def test_baseline_comparison_requires_same_plan_and_shows_rate_delta(self):
        result = compare_verification_runs(
            {"status": "reproduced", "iterations": 100, "crash_count": 20, "plan_fingerprint": "p"},
            {"status": "not_triggered", "iterations": 100, "crash_count": 0, "plan_fingerprint": "p"},
        )
        self.assertEqual(result["status"], "native_verified")
        self.assertEqual(result["crash_rate_delta"], -0.2)

    def test_baseline_status_respects_level_and_missing_trigger(self):
        compile_result = compare_verification_runs(
            {"status": "reproduced", "crash_count": 1, "plan_fingerprint": "p", "verification_level": "L2"},
            {"status": "passed", "crash_count": 0, "plan_fingerprint": "p", "verification_level": "L2"},
        )
        self.assertEqual(compile_result["status"], "compile_verified")
        missing = compare_verification_runs(
            {"status": "passed", "crash_count": 0, "plan_fingerprint": "p"},
            {"status": "passed", "crash_count": 0, "plan_fingerprint": "p"},
        )
        self.assertEqual(missing["status"], "not_triggered")


if __name__ == "__main__":
    unittest.main()
