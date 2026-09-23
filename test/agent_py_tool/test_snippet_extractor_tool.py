#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import tempfile
import unittest
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from tools.snippet_extractor_tool import SnippetExtractorTool
from workflows.crash_analysis_workflow import iOSCrashAnalyzeWorkflow


class TestSnippetExtractorTool(unittest.TestCase):
    def test_extract_template_function_from_instantiated_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "templ.h"
            fp.write_text(
                "template< class TYPE >\n"
                "inline void DemoDelete(TYPE* pObjects)\n"
                "{\n"
                "    if (pObjects == nullptr)\n"
                "    {\n"
                "        return;\n"
                "    }\n"
                "    DemoDestructElements< TYPE >(pObjects, 1);\n"
                "}\n",
                encoding="utf-8",
            )

            out = SnippetExtractorTool().execute(
                {
                    "file_path": str(fp),
                    "line_number": 5,
                    "function_name": "void demo_util::DemoDelete<demo_fw::DemoMapControl>(demo_fw::DemoMapControl*)",
                    "max_code_length": 0,
                }
            )

            self.assertNotIn("error", out, out.get("error"))
            self.assertTrue(out.get("is_complete_function"), out.get("incomplete_reason"))
            snippet = out.get("snippet") or []
            self.assertEqual(snippet[0], "template< class TYPE >")
            self.assertIn("DemoDelete(TYPE* pObjects)", "\n".join(snippet))

    def test_extract_get_leg_size_full_function(self):
        target_file = Path(
            "/tmp/demo-app/src/app/walk/guidance/route_plan/src/demo_route_result.cpp"
        )
        if not target_file.exists():
            self.skipTest(f"external source not found: {target_file}")

        tool = SnippetExtractorTool()
        out = tool.execute(
            {
                "file_path": str(target_file),
                "line_number": 2155,
                "function_name": "GetSegmentCount",
            }
        )

        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(out.get("strategy"), "function_body")
        self.assertIn(out.get("backend"), {"token_regex", "tree_sitter", "brace_counting"})
        self.assertTrue(out.get("is_complete_function"), out.get("incomplete_reason"))
        snippet = out.get("snippet") or []
        self.assertGreaterEqual(len(snippet), 3)
        self.assertIn("GetSegmentCount", snippet[0])
        snippet_text = "\n".join(snippet)
        self.assertIn("return m_clLegs.GetSize();", snippet_text)
        self.assertNotIn("GetPassTime", snippet_text)
        self.assertNotIn("RouteLinkIDAdd1", snippet_text)


class TestPromptSnippetCompletion(unittest.TestCase):
    def test_workflow_extracts_template_instantiation_simple_name(self):
        workflow = iOSCrashAnalyzeWorkflow()
        name = workflow._extract_simple_name_from_signature(
            "void demo_util::DemoDelete<demo_fw::DemoMapControl>(demo_fw::DemoMapControl*)"
        )
        self.assertEqual(name, "DemoDelete")

    def test_workflow_reextracts_incomplete_prompt_snippet(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "sample.cpp"
            fp.write_text(
                "int Foo::Bar(int x)\n"
                "{\n"
                "    if (x > 0)\n"
                "    {\n"
                "        return x;\n"
                "    }\n"
                "    return 0;\n"
                "}\n",
                encoding="utf-8",
            )

            workflow = iOSCrashAnalyzeWorkflow()
            snippet, complete, reason = workflow._prepare_prompt_function_snippet(
                {
                    "signature": "int Foo::Bar(int x)",
                    "file": str(fp),
                    "snippet_start_line": 1,
                },
                [
                    "int Foo::Bar(int x)",
                    "{",
                    "    if (x > 0)",
                ],
            )

            self.assertTrue(complete, reason)
            self.assertEqual(snippet[-1], "}")
            self.assertIn("return 0;", "\n".join(snippet))


if __name__ == "__main__":
    unittest.main()

