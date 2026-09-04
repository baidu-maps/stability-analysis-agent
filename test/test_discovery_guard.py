import importlib
import unittest
from pathlib import Path


class DiscoveryGuardTests(unittest.TestCase):
    def test_daemon_package_does_not_hide_root_daemon_module(self):
        module = importlib.import_module("daemon")
        self.assertTrue(Path(getattr(module, "__file__", "")).name in {"__init__.py", "server.py"})
        importlib.import_module("daemon.server")

    def test_expected_test_packages_are_present(self):
        root = Path(__file__).resolve().parent
        for package in ("harness", "daemon", "tools", "agent_py_tool", "skill_system"):
            self.assertTrue((root / package).is_dir(), package)


if __name__ == "__main__":
    unittest.main()
