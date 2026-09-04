from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PATHS = (
    REPO_ROOT / "cli" / "native_leak.py",
    REPO_ROOT / "services" / "repair_actions.py",
    REPO_ROOT / "tools" / "crash_diagnosis" / "disassembly_gate.py",
)


class GatewayCoverageTests(unittest.TestCase):
    def test_key_modules_reference_gateway_or_invoke_tool(self):
        for path in SCAN_PATHS:
            text = path.read_text(encoding="utf-8")
            self.assertTrue(
                "invoke_tool" in text or "ToolExecutionGateway" in text,
                f"{path} should route tools through gateway or invoke_tool",
            )

    def test_disassembly_gate_uses_invoke_tool(self):
        text = (REPO_ROOT / "tools" / "crash_diagnosis" / "disassembly_gate.py").read_text(encoding="utf-8")
        self.assertIn("invoke_tool", text)
        self.assertNotIn("emit_traced_operation", text)


if __name__ == "__main__":
    unittest.main()
