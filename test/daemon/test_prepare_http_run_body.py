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
    _prepare_http_run_body,
    _run_fingerprint,
    set_deny_local_path_fields,
)


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


if __name__ == "__main__":
    unittest.main()
