#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import ssl
import unittest

from tool_system.llm.http_ssl import (
    classify_connectivity_failure,
    is_ssl_certificate_error,
    uses_urllib_transport,
)


class TestHttpSsl(unittest.TestCase):
    def test_uses_urllib_transport(self):
        self.assertTrue(uses_urllib_transport("anthropic_messages_compatible"))
        self.assertTrue(uses_urllib_transport("openai_responses_compatible"))
        self.assertFalse(uses_urllib_transport("openai_chat_completions_compatible"))
        self.assertFalse(uses_urllib_transport(""))

    def test_is_ssl_certificate_error_from_type(self):
        self.assertTrue(is_ssl_certificate_error(ssl.SSLCertVerificationError("verify failed")))

    def test_is_ssl_certificate_error_from_message(self):
        err = RuntimeError(
            "<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate (_ssl.c:997)>"
        )
        self.assertTrue(is_ssl_certificate_error(err))

    def test_is_ssl_certificate_error_negative(self):
        self.assertFalse(is_ssl_certificate_error(RuntimeError("401 Unauthorized")))

    def test_classify_ssl_failure_hides_raw(self):
        err = RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        info = classify_connectivity_failure(err)
        self.assertEqual(info.category, "ssl_environment")
        self.assertFalse(info.show_raw_by_default)
        self.assertIn("Install Certificates.command", info.fix_steps[0])

    def test_classify_auth_failure(self):
        info = classify_connectivity_failure(RuntimeError("Error code: 401 - invalid key"))
        self.assertEqual(info.category, "auth")
        self.assertTrue(info.show_raw_by_default)


if __name__ == "__main__":
    unittest.main()
