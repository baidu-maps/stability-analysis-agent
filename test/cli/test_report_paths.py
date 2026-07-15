#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``cli/report_paths.py`` 与 ``extensions/`` 自动发现的单元测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestFormatBytes(unittest.TestCase):
    def test_units(self):
        from cli.report_paths import format_bytes
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(900), "900 B")
        self.assertEqual(format_bytes(1024), "1.00 KiB")
        self.assertEqual(format_bytes(5 * 1024 * 1024), "5.00 MiB")
        self.assertEqual(format_bytes(2 * 1024 ** 3), "2.00 GiB")
        self.assertEqual(format_bytes(None), "-")
        self.assertEqual(format_bytes(-5), "-")


class TestCliReports(unittest.TestCase):
    def setUp(self):
        from cli.report_paths import summarize_cli_reports, clear_cli_reports
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cli_reports"
        self.root.mkdir(parents=True)
        for idx in range(3):
            s = self.root / f"2026010{idx + 1}_000000_demo"
            s.mkdir(parents=True)
            (s / "01.json").write_text("x" * 1024, encoding="utf-8")
        self.summarize = summarize_cli_reports
        self.clear = clear_cli_reports

    def tearDown(self):
        self.tmp.cleanup()

    def test_summarize_reports(self):
        stats = self.summarize(root=self.root)
        self.assertTrue(stats["exists"])
        self.assertEqual(stats["report_count"], 3)
        self.assertEqual(stats["total_bytes"], 3 * 1024)
        self.assertGreaterEqual(len(stats["preview"]), 1)

    def test_clear_all(self):
        result = self.clear(root=self.root, only_preview=False)
        self.assertEqual(result["removed"], 3)
        self.assertEqual(result["freed_bytes"], 3 * 1024)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_clear_preview(self):
        # Force preview to a single session so only that one is removed.
        stats = self.summarize(root=self.root, preview_limit=1)
        self.assertEqual(len(stats["preview"]), 1)
        kept = sorted(p.name for p in self.root.iterdir())
        kept_target = kept[-1]  # newest
        result = self.clear(root=self.root, only_preview=True, preview_limit=1)
        self.assertEqual(result["removed"], 1)
        remaining = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(remaining, sorted(n for n in kept if n != kept_target))

    def test_clear_missing_dir_is_safe(self):
        gone = self.root / "no-such-place"
        result = self.clear(root=gone)
        self.assertEqual(result["removed"], 0)
        self.assertIn("目录不存在", result.get("skipped", ""))


class TestExtensions(unittest.TestCase):
    def test_example_templates_register_when_imported(self):
        # reset registry
        from tool_system import get_registry

        reg = get_registry()
        reg.clear()

        from extensions.tools.example_tool import ExampleCustomTool
        from extensions.workflows.example_workflow import ExampleCustomWorkflow

        # explicit import is needed (template is opt-in to keep the
        # empty project noise-free; comment in __init__ explains)
        tool_names = [r.cls_or_instance.__name__ for r in reg._tools.values()]
        workflow_names = [r.cls_or_instance.__name__ for r in reg._workflows.values()]
        # Just confirm the symbols are importable + instantiable; final auto-load is verified below.
        tool = ExampleCustomTool()
        wf = ExampleCustomWorkflow()
        self.assertEqual(tool.definition.name, "example_custom_tool")
        self.assertEqual(wf.definition.name, "example_custom_workflow")
        self.assertIn("ExampleCustomTool", tool_names)
        self.assertIn("ExampleCustomWorkflow", workflow_names)

    def test_register_all_discovers_user_dir(self):
        # set up an isolated user extensions dir with a custom tool
        from tool_system import get_registry

        reg = get_registry()
        before_tools = set(reg._tools.keys())

        with tempfile.TemporaryDirectory() as td:
            ext_root = Path(td)
            (ext_root / "my_tool.py").write_text(
                "from tool_system import BaseTool, ToolDefinition, register_tool, Priority\n"
                "\n"
                "@register_tool(priority=Priority.EXTENSION)\n"
                "class TempPluginTool(BaseTool):\n"
                "    @property\n"
                "    def definition(self):\n"
                "        return ToolDefinition(\n"
                "            name='temp_plugin_tool',\n"
                "            description='temporarily installed via test',\n"
                "            input_schema={'type': 'object', 'properties': {}},\n"
                "            output_schema={'type': 'object', 'properties': {}},\n"
                "            category='custom',\n"
                "            version='0.1.0',\n"
                "        )\n"
                "    def execute(self, input_data):\n"
                "        return {'status': 'ok'}\n",
                encoding="utf-8",
            )

            import os
            os.environ["STABILITY_AGENT_USER_EXT_DIR"] = str(ext_root)
            try:
                from extensions import register_all
                register_all()
                after_tools = set(reg._tools.keys())
                added = after_tools - before_tools
                self.assertIn("TempPluginTool", added,
                              f"TempPluginTool not registered; tools after: {sorted(after_tools)}")
            finally:
                os.environ.pop("STABILITY_AGENT_USER_EXT_DIR", None)


if __name__ == "__main__":
    unittest.main()
