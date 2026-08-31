#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""03 代码定位审计轨迹（03b）测试。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.code_locator import CodeLocatorService, LocatorConfig
from tools.code_content_provider_tool import CodeContentProviderTool


class TestCodeLocationTrace(unittest.TestCase):
    def test_locator_records_find_source_and_function_def(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            src_dir.mkdir()
            cpp = src_dir / "demo.cpp"
            cpp.write_text(
                "void Loop() {\n"
                "  int x = 0;\n"
                "}\n"
                "void ThreadStart() {\n"
                "  Loop();\n"
                "}\n",
                encoding="utf-8",
            )
            roots = [tmp]
            svc = CodeLocatorService(LocatorConfig())
            hit = svc.find_source_file("src/demo.cpp", roots)
            self.assertTrue(hit and hit.endswith("demo.cpp"))
            loc = svc.find_function_definition("Loop", roots)
            self.assertIsNotNone(loc)
            trace = svc.export_location_trace()
            self.assertEqual(trace.get("source"), "code_content_provider")
            kinds = {s.get("kind") for s in trace.get("steps") or []}
            self.assertIn("find_source_file", kinds)
            self.assertIn("find_function_definition", kinds)
            src_steps = [
                s for s in trace.get("steps") or [] if s.get("kind") == "find_source_file"
            ]
            self.assertTrue(any(s.get("success") for s in src_steps))

    def test_code_content_provider_emits_location_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "proj" / "src"
            src_dir.mkdir(parents=True)
            cpp = src_dir / "crash.cpp"
            cpp.write_text(
                "class Worker {\n"
                "public:\n"
                "  void Loop() {\n"
                "    int* p = nullptr;\n"
                "    *p = 1;\n"
                "  }\n"
                "  static void ThreadStart(Worker* pThis) {\n"
                "    pThis->Loop();\n"
                "  }\n"
                "};\n",
                encoding="utf-8",
            )
            resolved = {
                "os_type": "linux",
                "crash_thread_id": "1",
                "crash_thread_name": "main",
                "crash_thread_is_main_thread": True,
                "crash_thread_has_business_frames": True,
                "resolved_threads": [
                    {
                        "thread_id": "1",
                        "thread_name": "main",
                        "tid": "1",
                        "name": "main",
                        "is_crash_thread": True,
                        "is_main_thread": True,
                        "frames": [
                            {
                                "address": "0x1000",
                                "resolved_function": "Worker::Loop()",
                                "resolved_file": "proj/src/crash.cpp",
                                "resolved_line": 5,
                            }
                        ],
                    }
                ],
            }
            tool = CodeContentProviderTool()
            out = tool.execute(
                {
                    "resolved_stack": json.dumps(resolved, ensure_ascii=False),
                    "code_roots": [tmp],
                }
            )
            self.assertIsInstance(out, dict)
            self.assertNotIn("error", out)
            trace = out.get("location_trace")
            self.assertIsInstance(trace, dict)
            steps = trace.get("steps")
            self.assertIsInstance(steps, list)
            self.assertGreater(len(steps), 0)
            pipeline_steps = {s.get("step") for s in steps if s.get("kind") == "pipeline"}
            self.assertIn("analysis_entry", pipeline_steps)
            self.assertIn("frame_filter", pipeline_steps)

    def test_non_crash_business_frame_is_analysis_entry_not_crash_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "proj" / "src"
            src_dir.mkdir(parents=True)
            (src_dir / "queue.cpp").write_text(
                "class Worker {\n"
                "public:\n"
                "  void Loop() {\n"
                "    while (true) {}\n"
                "  }\n"
                "};\n",
                encoding="utf-8",
            )
            resolved = {
                "os_type": "harmonyos",
                "crash_thread_id": "main-tid",
                "crash_thread_name": "com.demo",
                "crash_thread_is_main_thread": True,
                "crash_thread_has_business_frames": False,
                "resolved_threads": [
                    {
                        "thread_id": "worker-tid",
                        "thread_name": "worker",
                        "tid": "worker-tid",
                        "name": "worker",
                        "is_crash_thread": False,
                        "is_main_thread": False,
                        "frames": [
                            {
                                "address": "0x2000",
                                "resolved_function": "Worker::Loop()",
                                "resolved_file": "proj/src/queue.cpp",
                                "resolved_line": 4,
                            }
                        ],
                    }
                ],
            }
            out = CodeContentProviderTool().execute(
                {
                    "resolved_stack": json.dumps(resolved, ensure_ascii=False),
                    "code_roots": [tmp],
                }
            )
            self.assertNotIn("error", out)
            # 解耦重构后：crash_summary 不再写入 03 输出
            # 验证 03 输出中确实不含 crash_summary
            self.assertNotIn("crash_summary", out)
            # 验证 graph 和 location_trace 仍存在
            self.assertIn("graph", out)
            self.assertIn("location_trace", out)

    def test_same_method_name_on_caller_class_stays_separate_node(self):
        """栈上 WalkMapControl::UpdateFoo 不得折叠成崩溃点 NaviMapRender::UpdateFoo。"""
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "proj" / "src"
            src_dir.mkdir(parents=True)
            (src_dir / "navi_map_render.cpp").write_text(
                "class NaviMapRender {\n"
                "public:\n"
                "  void UpdateMapRenderCustomDrawOption() {\n"
                "    m_config->x = 1;\n"
                "  }\n"
                "};\n"
                "void NaviMapRender::OnCreate() {\n"
                "    m_config = 0;\n"
                "}\n",
                encoding="utf-8",
            )
            (src_dir / "walk_map_control.cpp").write_text(
                "class WalkMapControl {\n"
                "public:\n"
                "  void UpdateMapRenderCustomDrawOption() {\n"
                "    if (!m_render) return;\n"
                "    m_render->UpdateMapRenderCustomDrawOption();\n"
                "  }\n"
                "};\n"
                "void WalkMapControl::OnInit() {\n"
                "    m_render = 0;\n"
                "}\n"
                "void WalkMapControl::Init() {\n"
                "    if (Hop(WalkMapControl::Init)) return;\n"
                "    OnInit();\n"
                "}\n"
                "void WalkMapControl::SetOverlookMode() {\n"
                "    if (Hop(WalkMapControl::SetOverlookMode)) return;\n"
                "    m_render->SetOverlookMode();\n"
                "}\n",
                encoding="utf-8",
            )
            resolved = {
                "os_type": "android",
                "crash_thread_id": "1242",
                "crash_thread_name": "ninebot.ninebot",
                "crash_thread_is_main_thread": True,
                "crash_thread_has_business_frames": True,
                "resolved_threads": [
                    {
                        "thread_id": "1242",
                        "thread_name": "ninebot.ninebot",
                        "tid": "1242",
                        "name": "ninebot.ninebot",
                        "is_crash_thread": True,
                        "is_main_thread": True,
                        "frames": [
                            {
                                "address": "0x1000",
                                "resolved_function": (
                                    "walk_navi::NaviMapRender::UpdateMapRenderCustomDrawOption("
                                    "walk_navi::NE_MapRenderCustomDrawOption&)"
                                ),
                                "resolved_file": "proj/src/navi_map_render.cpp",
                                "resolved_line": 4,
                            },
                            {
                                "address": "0x2000",
                                "resolved_function": (
                                    "walk_navi::WalkMapControl::UpdateMapRenderCustomDrawOption("
                                    "walk_navi::NE_MapRenderCustomDrawOption&)"
                                ),
                                "resolved_file": "proj/src/walk_map_control.cpp",
                                "resolved_line": 5,
                            },
                        ],
                    }
                ],
            }
            out = CodeContentProviderTool().execute(
                {
                    "resolved_stack": json.dumps(resolved, ensure_ascii=False),
                    "code_roots": [tmp],
                }
            )
            self.assertNotIn("error", out)
            graph = out.get("graph") or {}
            nodes = graph.get("nodes") or []
            files = " ".join(str(n.get("file") or n.get("id") or "") for n in nodes)
            self.assertIn("walk_map_control.cpp", files)
            chains = graph.get("call_chain_from_add2line") or []
            self.assertTrue(chains)
            node_ids = chains[0].get("nodes") or []
            self.assertGreaterEqual(len(set(node_ids)), 2)
            node_blob = " ".join(
                str(n.get("name") or "") + " " + str(n.get("signature") or "")
                for n in nodes
            )
            self.assertIn("Init", node_blob)
            self.assertIn("OnInit", node_blob)
            self.assertIn("OnCreate", node_blob)
            self.assertNotIn("SetOverlookMode", node_blob)
            edges = graph.get("edges") or []
            self.assertTrue(
                any(
                    isinstance(e, dict)
                    and e.get("type") == "same_class_brother"
                    and "thread_affinity" in str(e.get("relation") or "")
                    for e in edges
                ),
                edges,
            )


if __name__ == "__main__":
    unittest.main()

