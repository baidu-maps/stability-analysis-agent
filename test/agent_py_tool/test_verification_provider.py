import tempfile
import unittest
from pathlib import Path

from services.verification import (
    CommandVerificationProvider,
    NoopVerificationProvider,
    VerificationRequest,
    create_verification_provider,
    discover_verification_candidates,
    approval_is_valid,
    consume_approval,
    validate_approval,
    make_approval,
)


class VerificationProviderTest(unittest.TestCase):
    def test_noop_provider_is_unavailable_not_failed(self):
        result = NoopVerificationProvider().verify(VerificationRequest(workspace="/missing"))
        self.assertEqual(result.status, "unavailable")
        self.assertNotEqual(result.status, "failed")
        self.assertFalse(result.capabilities["available"])

    def test_command_provider_runs_without_shell_and_exposes_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = CommandVerificationProvider([
                "python3", "-c",
                "import os,sys; assert os.getcwd()==sys.argv[1]; print(sys.argv[2])",
                "{workspace}", "{changed_files}",
            ], approved=True)
            result = provider.verify(VerificationRequest(
                workspace=tmp,
                changed_files=["src/a.cpp"],
                mode="build",
            ))
        self.assertEqual(result.status, "passed")
        self.assertIn("src/a.cpp", result.output)
        self.assertEqual(result.checks[0]["returncode"], 0)

    def test_command_failure_is_reported_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = CommandVerificationProvider(["python3", "-c", "raise SystemExit(3)"], approved=True).verify(
                VerificationRequest(workspace=tmp)
            )
        self.assertEqual(result.status, "failed")
        self.assertIn("退出码", result.error)

    def test_factory_keeps_default_noop_and_accepts_string_command(self):
        self.assertIsInstance(create_verification_provider(None), NoopVerificationProvider)
        provider = create_verification_provider({"command": "python3 -c pass"})
        self.assertIsInstance(provider, CommandVerificationProvider)
        self.assertIsInstance(
            create_verification_provider({"provider": "unsupported", "command": ["true"]}),
            NoopVerificationProvider,
        )

    def test_provider_exposes_validate_execute_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = create_verification_provider({"command": "python3 -c pass"}, approved=True)
            request = VerificationRequest(workspace=tmp, mode="syntax")
            pending = provider.validate(request)
            self.assertEqual(pending.status, "passed")
            self.assertTrue(pending.command_fingerprint)
            result = provider.execute(request)
            self.assertEqual(result.status, "passed")
            self.assertEqual(provider.summarize(result)["status"], "passed")

    def test_unapproved_command_is_pending_and_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "executed"
            provider = create_verification_provider({
                "command": ["python3", "-c", "from pathlib import Path; Path('executed').touch()"],
                "approved": True,
            })
            result = provider.execute(VerificationRequest(workspace=tmp))
            self.assertEqual(result.status, "pending")
            self.assertFalse(marker.exists())

    def test_discovery_is_read_only_and_returns_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
            candidates = discover_verification_candidates(tmp)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].mode, "build")
        self.assertIn("cmake", candidates[0].command)

    def test_discovery_includes_makefile_and_presets(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
            candidates = discover_verification_candidates(tmp)
            modes = {item.mode for item in candidates}
        self.assertIn("build", modes)
        from services.verification import merge_preset_candidates

        merged = merge_preset_candidates(tmp)
        self.assertGreaterEqual(len(merged), len(candidates))

    def test_approval_is_bound_to_fingerprint_and_expiry(self):
        approval = make_approval(run_id="run", tool_call_id="verify", command_fingerprint="abc", expires_at=20)
        approval.update(status="granted")
        self.assertTrue(approval_is_valid(approval, fingerprint="abc", now=10))
        self.assertFalse(approval_is_valid(approval, fingerprint="other", now=10))
        self.assertFalse(approval_is_valid(approval, fingerprint="abc", now=21))

    def test_approval_rejects_every_binding_mismatch_and_reuse(self):
        approval = make_approval(
            run_id="run", tool_call_id="verify", command_fingerprint="abc",
            scope="single_command", expires_at=100,
        )
        approval.update(status="granted", granted_by="user")
        for kwargs, expected in (
            ({"fingerprint": "bad", "run_id": "run", "tool_call_id": "verify"}, "command_fingerprint_mismatch"),
            ({"fingerprint": "abc", "run_id": "other", "tool_call_id": "verify"}, "run_id_mismatch"),
            ({"fingerprint": "abc", "run_id": "run", "tool_call_id": "other"}, "tool_call_id_mismatch"),
            ({"fingerprint": "abc", "run_id": "run", "tool_call_id": "verify", "scope": "other"}, "scope_mismatch"),
        ):
            checked = validate_approval(approval, now=10, **kwargs)
            self.assertEqual(checked["status"], "invalid")
            self.assertEqual(checked["validation_error"], expected)
        consumed = consume_approval(
            approval, fingerprint="abc", run_id="run", tool_call_id="verify", now=10,
        )
        self.assertEqual(consumed["status"], "consumed")
        with self.assertRaises(PermissionError):
            consume_approval(consumed, fingerprint="abc", run_id="run", tool_call_id="verify", now=11)


if __name__ == "__main__":
    unittest.main()
