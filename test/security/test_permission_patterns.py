import unittest

from services.policy import PolicyEngine


class PermissionPatternTests(unittest.TestCase):
    def test_allow_ask_deny_patterns(self):
        policy = PolicyEngine(permission_rules=[
            {"permission": "read", "patterns": ["src/*"], "decision": "allow"},
            {"permission": "write", "patterns": ["src/*"], "decision": "ask"},
            {"permission": "write", "patterns": ["vendor/*"], "decision": "deny"},
        ])
        self.assertTrue(policy.check_permission("read", "src/a.cpp").allowed)
        self.assertEqual(policy.check_permission("write", "src/a.cpp").decision, "approval_required")
        self.assertFalse(policy.check_permission("write", "vendor/a.cpp").allowed)


if __name__ == "__main__":
    unittest.main()
