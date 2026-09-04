from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill_system.models import SkillBundle, SkillFrontmatter, SkillPackageManifest
from skill_system.runtime import SkillRuntime


class ToolSkillGatewayTests(unittest.TestCase):
    @patch("skill_system.runtime.register_skill_exports")
    @patch("tools.register_all_tools")
    @patch("tool_system.ToolAndWorkflowRegistry")
    def test_tool_skill_routes_through_gateway(self, mock_registry_cls, mock_register_tools, mock_register_exports):
        registry = MagicMock()
        mock_registry_cls.return_value = registry
        calls = {"execute": 0}

        class _FakeTool:
            definition = SimpleNamespace(risk="read_only", side_effect=False, requires_approval=False)

            def runtime_policy(self):
                return {}

            def execute(self, input_data):
                calls["execute"] += 1
                return {"success": True, "value": 42}

        tool = _FakeTool()
        registry.get_tool.return_value = tool

        bundle = SkillBundle(
            path=Path("/tmp/demo"),
            frontmatter=SkillFrontmatter(name="demo-tool-skill"),
            body="# demo",
            package=SkillPackageManifest(name="demo-tool-skill", type="tool", command_name="demo-tool-skill"),
        )
        runtime = SkillRuntime(manager=MagicMock())
        result = runtime._execute_tool_skill(bundle, "demo_tool", {"x": 1})

        self.assertEqual(calls["execute"], 1)
        self.assertEqual(result.metadata.get("tool_name"), "demo_tool")
        self.assertTrue(result.result.get("success"))
        trace = result.metadata.get("runtime_trace") or {}
        events = [item.get("event") for item in trace.get("events", [])]
        self.assertIn("tool.success", events)


if __name__ == "__main__":
    unittest.main()
