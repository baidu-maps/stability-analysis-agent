#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP POST /runs body guards."""

from __future__ import annotations

import sys
import unittest
from http import HTTPStatus
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from daemon.server import (
    DaemonHttpError,
    RunState,
    _emit_trace_handoff,
    _prepare_http_run_body,
    _run_fingerprint,
    set_deny_local_path_fields,
)
from protocol.models import RunEvent
import time


class PrepareHttpRunBodyTests(unittest.TestCase):
    """Remote-mode field guards and HTTP defaults."""

    def tearDown(self) -> None:
        set_deny_local_path_fields(False)

    def test_missing_apply_ai_fixes_defaults_false(self) -> None:
        """Omitted apply_ai_fixes is false on the HTTP path."""
        body = _prepare_http_run_body({"crash_log_content": "tombstone"})
        self.assertFalse(body["apply_ai_fixes"])

    def test_external_agent_evaluation_is_opt_in(self) -> None:
        omitted = _prepare_http_run_body({"crash_log_content": "tombstone"})
        enabled = _prepare_http_run_body(
            {
                "crash_log_content": "tombstone",
                "external_agent_evaluation": True,
            }
        )
        self.assertNotIn("external_agent_evaluation", omitted)
        self.assertIs(enabled["external_agent_evaluation"], True)

    def test_external_agent_evaluation_rejects_non_boolean(self) -> None:
        with self.assertRaises(DaemonHttpError) as ctx:
            _prepare_http_run_body(
                {
                    "crash_log_content": "tombstone",
                    "external_agent_evaluation": "true",
                }
            )
        self.assertEqual(ctx.exception.status, int(HTTPStatus.BAD_REQUEST))
        self.assertEqual(
            ctx.exception.payload["error"],
            "invalid_external_agent_evaluation",
        )

    def test_runtime_verification_switch_rejects_string_boolean(self) -> None:
        with self.assertRaises(DaemonHttpError) as ctx:
            _prepare_http_run_body(
                {
                    "crash_log_content": "tombstone",
                    "enable_runtime_verification": "false",
                }
            )
        self.assertEqual(ctx.exception.status, int(HTTPStatus.BAD_REQUEST))
        self.assertEqual(ctx.exception.payload["error"], "invalid_boolean")
        self.assertEqual(ctx.exception.payload["field"], "enable_runtime_verification")

    def test_runtime_verification_flags_are_mutually_exclusive(self) -> None:
        with self.assertRaises(DaemonHttpError) as ctx:
            _prepare_http_run_body(
                {
                    "crash_log_content": "tombstone",
                    "enable_runtime_verification": True,
                    "skip_runtime_verification": True,
                }
            )
        self.assertEqual(
            ctx.exception.payload["error"],
            "conflicting_runtime_verification_flags",
        )

    def test_http_cannot_submit_verification_script_class(self) -> None:
        with self.assertRaises(DaemonHttpError) as ctx:
            _prepare_http_run_body(
                {
                    "crash_log_content": "tombstone",
                    "verification": {
                        "effectiveness": {
                            "issue": {"class": "untrusted.module:IssueScript"},
                        }
                    },
                }
            )
        self.assertEqual(
            ctx.exception.payload["error"],
            "dynamic_verification_class_forbidden",
        )

    def test_external_agent_evaluation_changes_run_fingerprint(self) -> None:
        base = {"crash_log_content": "tombstone"}
        self.assertNotEqual(
            _run_fingerprint(base),
            _run_fingerprint({**base, "external_agent_evaluation": True}),
        )

    def test_empty_content_rejected(self) -> None:
        """Blank crash_log_content is 400."""
        with self.assertRaises(DaemonHttpError) as ctx:
            _prepare_http_run_body({"crash_log_content": "  "})
        self.assertEqual(ctx.exception.status, int(HTTPStatus.BAD_REQUEST))
        self.assertEqual(ctx.exception.payload["error"], "crash_log_content_required")

    def test_icafe_card_id_allows_omitting_crash_log_content(self) -> None:
        """Enterprise digital-employee path may send only icafe_card_id."""
        set_deny_local_path_fields(True)
        body = _prepare_http_run_body(
            {
                "icafe_card_id": (
                    "https://console.cloud.baidu-int.com/devops/icafe/"
                    "issue/openmap-client-3456/show"
                ),
            }
        )
        self.assertFalse(body["apply_ai_fixes"])
        self.assertIn("icafe_card_id", body)

    def test_icafe_card_id_changes_run_fingerprint(self) -> None:
        """Different cards must not share the same idempotency fingerprint."""
        base = {"apply_ai_fixes": False, "output_format": "markdown"}
        self.assertNotEqual(
            _run_fingerprint({**base, "icafe_card_id": "openmap-client-1"}),
            _run_fingerprint({**base, "icafe_card_id": "openmap-client-2"}),
        )

    def test_forbidden_local_path_when_denied(self) -> None:
        """code_roots is rejected in remote mode."""
        set_deny_local_path_fields(True)
        with self.assertRaises(DaemonHttpError) as ctx:
            _prepare_http_run_body(
                {"crash_log_content": "x", "code_roots": ["/etc/passwd"]}
            )
        self.assertEqual(ctx.exception.payload["error"], "forbidden_field")
        self.assertEqual(ctx.exception.payload["field"], "code_roots")

    def test_invalid_output_format(self) -> None:
        """Unknown output_format is 400."""
        with self.assertRaises(DaemonHttpError) as ctx:
            _prepare_http_run_body({"crash_log_content": "x", "output_format": "pdf"})
        self.assertEqual(ctx.exception.payload["error"], "invalid_output_format")


class EmitTraceHandoffTests(unittest.TestCase):
    """Successful CLI runs must hand off runtime_trace without raising."""

    def test_emit_trace_handoff_includes_run_id(self) -> None:
        """Regression: keyword-only RunEvent missed required run_id."""
        run = RunState(run_id="run-trace-1", transport_status="running", created_at=time.time())
        _emit_trace_handoff(
            run,
            {"events": [{"type": "x"}], "run_id": "cli-trace"},
        )
        event = run.events.get_nowait()
        self.assertIsInstance(event, RunEvent)
        self.assertEqual(event.run_id, "run-trace-1")
        self.assertEqual(event.type, "trace_loaded")
        self.assertEqual(event.data.get("event_count"), 1)
        self.assertEqual(event.data.get("run_id"), "cli-trace")


if __name__ == "__main__":
    unittest.main()
