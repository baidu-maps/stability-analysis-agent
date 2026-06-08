#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
code_content_provider（当前版本）单元测试（精简版）

目标：验证“add2line 解析结果 + code_root”能够产出当前结构化数据与 analysis_guidance。
说明：历史旧版接口（CodeContext/CrashContext/PromptContent 等）已不再维护，
这里不再用旧接口来约束当前实现，避免为了兼容测试而改动核心逻辑。
"""

import unittest
import json
import sys
import tempfile
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

try:
    from tools.code_content_provider_tool import CodeContentProvider
except ImportError:  # 兼容部分环境仍使用旧包名
    from stability_analyzer_agent.tools import CodeContentProvider  # type: ignore

try:
    from workflows.crash_analysis_workflow import iOSCrashAnalyzeWorkflow
except ImportError:
    iOSCrashAnalyzeWorkflow = None  # type: ignore


@unittest.skipUnless(iOSCrashAnalyzeWorkflow is not None, "workflow import failed")
class TestBuildPromptFinalTip(unittest.TestCase):
    def test_shared_var_sections_in_final_tip(self):
        crash_id = "func|/tmp/x.cpp|void Foo::bar()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 10,
                "crash_line_code": "x = y;",
                "error_type": "SIGSEGV",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Foo::bar()",
                        "file": "/tmp/x.cpp",
                        "snippet": ["void Foo::bar() {", "  head = 0;", "}"],
                    },
                    {
                        "id": "func|/tmp/x.cpp|void Foo::baz()",
                        "type": "function",
                        "signature": "void Foo::baz()",
                        "file": "/tmp/x.cpp",
                        "snippet": ["void Foo::baz() {", "  tail = 0;", "}"],
                    },
                    {
                        "id": "var|head",
                        "type": "variable",
                        "name": "head",
                        "signature": "int* head;",
                    },
                ],
                "edges": [
                    {
                        "type": "use_shared_var",
                        "from_id": crash_id,
                        "to_id": "var|head",
                        "relation": "write",
                    },
                    {
                        "type": "use_shared_var",
                        "from_id": "func|/tmp/x.cpp|void Foo::baz()",
                        "to_id": "var|head",
                        "relation": "write",
                    },
                ],
                "call_chain_from_code": [{"nodes": [crash_id]}],
            },
            "code_context_options": {"max_shared_var_related_functions": 10},
        }
        resolved = {
            "resolved_frames": [
                {
                    "resolved_function": "bar()",
                    "resolved_file": "x.cpp",
                    "resolved_line": 10,
                }
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertIn("### 共享成员与写路径交叉（崩溃点关联）", text)
        self.assertIn("head", text)
        self.assertIn("## 函数源码", text)
        self.assertNotIn("改码依据:", text)
        self.assertNotIn("本轮改码与引用范围", text)
        self.assertIn("- 文件: /tmp/x.cpp", text)
        self.assertIn("void Foo::baz()", text)

    def test_add2line_stack_frames_included_in_final_tip(self):
        crash_id = "func|/tmp/Crash.h|bool Crash::hit() const"
        stack_b = "func|/tmp/Caller.cpp|void Caller::run()"
        stack_c = "func|/tmp/Util.h|void Util::cleanup()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 10,
                "crash_line_code": "return flag;",
                "error_type": "SIGSEGV",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "bool Crash::hit() const",
                        "file": "/tmp/Crash.h",
                        "snippet": ["bool Crash::hit() const { return flag; }"],
                    },
                    {
                        "id": stack_b,
                        "type": "function",
                        "signature": "void Caller::run()",
                        "file": "/tmp/Caller.cpp",
                        "snippet": ["void Caller::run() {", "  hit();", "}"],
                    },
                    {
                        "id": stack_c,
                        "type": "function",
                        "signature": "void Util::cleanup()",
                        "file": "/tmp/Util.h",
                        "snippet": ["void Util::cleanup() {", "  run();", "}"],
                    },
                ],
                "edges": [],
                "call_chain_from_code": [
                    {
                        "id": "path_0",
                        "nodes": [crash_id],
                        "description": "inferred_from_add2line_stack_order",
                    }
                ],
                "call_chain_from_add2line": [
                    {
                        "thread_id": "t1",
                        "nodes": [crash_id, stack_b, stack_c],
                    }
                ],
                "stack_kept_original_indices": [0, 1, 2],
            },
        }
        resolved = {
            "resolved_frames": [
                {"resolved_function": "Crash::hit()", "resolved_file": "Crash.h", "resolved_line": 10},
                {"resolved_function": "Caller::run()", "resolved_file": "Caller.cpp", "resolved_line": 2},
                {"resolved_function": "Util::cleanup()", "resolved_file": "Util.h", "resolved_line": 1},
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertIn("void Caller::run()", text)
        self.assertIn("void Util::cleanup()", text)
        self.assertIn("- 文件: /tmp/Caller.cpp", text)

    def test_evidence_gate_message_in_final_tip(self):
        crash_id = "func|/tmp/Crash.h|void Crash::hit()"
        stack_b = "func|/tmp/Caller.cpp|void Caller::run()"
        code_ctx = {
            "crash_summary": {"node_id": crash_id + " {", "crash_line_number": 10},
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Crash::hit()",
                        "file": "/tmp/Crash.h",
                        "snippet": ["void Crash::hit() { x=1; }"],
                    },
                    {
                        "id": stack_b,
                        "type": "function",
                        "signature": "void Caller::run()",
                        "file": "/tmp/Caller.cpp",
                        "snippet": ["void Caller::run() { hit(); }"],
                    },
                ],
                "edges": [
                    {"type": "calls_stack_order", "from_id": stack_b, "to_id": crash_id},
                ],
                "evidence_summary": {
                    "auto_fix_allowed": False,
                    "has_calls_stack_order": True,
                    "auto_fix_block_reason": "test block",
                },
                "call_chain_from_code": [
                    {
                        "nodes": [crash_id, stack_b],
                        "inference": "inferred_from_add2line_stack_order",
                    }
                ],
                "call_chain_from_add2line": [{"thread_id": "t", "nodes": [crash_id, stack_b]}],
            },
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, {"resolved_frames": []}, code_ctx)
        self.assertIn("auto_fix_allowed", text)
        self.assertIn("auto_fix_allowed=false", text)
        self.assertIn("calls_stack_order", text)

    def test_symbol_only_prompt_omits_crash_line_number_and_suspicious_snippet(self):
        crash_id = "func|/tmp/mtl.mm|void MTLVertexBuffer::createPrivateBuffer()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 9,
                "crash_line_code": '#include "mtl.h"',
                "error_type": "SIGSEGV",
                "crash_location_source": "from_log_deduce",
                "crash_line_note": "启发式展示，不是指令级精确崩溃位置。",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void MTLVertexBuffer::createPrivateBuffer()",
                        "file": "/tmp/mtl.mm",
                        "snippet": ["void MTLVertexBuffer::createPrivateBuffer() {", "}"],
                    }
                ],
                "edges": [],
            },
        }
        resolved = {
            "resolved_frames": [
                {
                    "resolved_function": "MTLVertexBuffer::createPrivateBuffer()",
                    "resolved_file": None,
                    "resolved_line": None,
                }
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertNotIn("crash_line_number:", text)
        self.assertNotIn("crash_location_source:", text)
        self.assertNotIn("crash_line_note:", text)
        self.assertNotIn("## 可疑代码片段", text)
        self.assertNotIn("可疑崩溃代码行", text)
        self.assertNotIn("#include", text)
        self.assertIn("## 崩溃点定位", text)
        self.assertIn("结论：未精确定位到 file:line 级崩溃行", text)
        self.assertNotIn("启发式展示，不是指令级精确崩溃位置。", text)
        self.assertNotIn("自栈顶向下优先", text)

    def test_add2line_prompt_includes_suspicious_snippet_without_summary_line_number(self):
        crash_id = "func|/tmp/Crash.cpp|void Crash::hit()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 42,
                "crash_line_code": "*ptr = 0;",
                "error_type": "SIGSEGV",
                "crash_location_source": "from_add2line",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Crash::hit()",
                        "file": "/tmp/Crash.cpp",
                        "snippet": ["void Crash::hit() {", "  *ptr = 0;", "}"],
                    }
                ],
                "edges": [],
            },
        }
        resolved = {
            "resolved_frames": [
                {
                    "resolved_function": "Crash::hit()",
                    "resolved_file": "/tmp/Crash.cpp",
                    "resolved_line": 42,
                }
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertNotIn("- crash_line_number:", text)
        self.assertNotIn("crash_location_source:", text)
        self.assertIn("## 崩溃点定位", text)
        self.assertIn("/tmp/Crash.cpp:42", text)
        self.assertIn("*ptr = 0;", text)
        self.assertIn("结论：崩溃点已通过符号化堆栈关联到具体源码行", text)
        self.assertIn("日志崩溃线程上的崩溃位置:", text)
        self.assertIn("void Crash::hit()", text)
        self.assertNotIn("## 可疑代码片段", text)
        self.assertNotIn("日志崩溃线程栈业务库帧", text)

    def test_structured_crash_point_merges_location_section_into_summary(self):
        crash_id = "func|/tmp/Crash.cpp|void Crash::hit()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 42,
                "crash_line_code": "*ptr = 0;",
                "error_type": "SIGSEGV",
                "crash_location_source": "from_add2line",
                "analysis_entry_file": "/tmp/Crash.cpp",
                "analysis_entry_function": "void Crash::hit()",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Crash::hit()",
                        "file": "/tmp/Crash.cpp",
                        "snippet": ["void Crash::hit() {", "  *ptr = 0;", "}"],
                    }
                ],
                "edges": [],
            },
        }
        resolved = {
            "resolved_frames": [
                {
                    "resolved_function": "Crash::hit()",
                    "resolved_file": "/tmp/Crash.cpp",
                    "resolved_line": 42,
                }
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertIn("- 崩溃点信息：", text)
        self.assertIn("  - 崩溃函数: void Crash::hit()", text)
        self.assertIn("  - 崩溃位置: /tmp/Crash.cpp:42", text)
        self.assertIn("  - 崩溃位置对应代码: `*ptr = 0;`", text)
        self.assertIn("  - 定位说明:", text)
        self.assertIn("  - 定位置信度: 低（勿单独作为改码依据）", text)
        self.assertNotIn("## 崩溃点定位", text)
        self.assertNotIn("可疑单行", text)

    def test_non_crash_thread_analysis_entry_is_not_rendered_as_crash_line(self):
        crash_id = "func|/tmp/VTaskQueue.cpp|void Loop(void)"
        code_ctx = {
            "crash_summary": {
                "error_type": "SIGSEGV",
                "crash_thread_id": "21840",
                "crash_thread_name": "main",
                "is_main_thread_crash": True,
                "crash_thread_has_business_frames": False,
                "selected_analysis_thread_id": "worker",
                "selected_analysis_thread_name": "22985",
                "selected_analysis_is_crash_thread": False,
                "selected_analysis_is_main_thread": False,
                "selected_analysis_confidence": "investigation_hint",
                "selected_analysis_note": "当前源码上下文来自其它业务线程。",
                "attributed_crash_location_status": "unresolved_crash_thread_no_business_frame",
                "analysis_entry_file": "/tmp/VTaskQueue.cpp",
                "analysis_entry_function": "void Loop(void)",
                "analysis_entry_line_number": 294,
                "analysis_entry_line_code": "while (!m_stop && m_tasks.empty())",
                "crash_line_number": None,
                "crash_line_code": None,
                "crash_location_source": "unresolved_crash_thread_no_business_frame",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Loop(void)",
                        "file": "/tmp/VTaskQueue.cpp",
                        "snippet": ["void Loop(void) {", "  while (!m_stop && m_tasks.empty()) {", "  }", "}"],
                    }
                ],
                "edges": [],
                "call_chain_from_code": [{"nodes": [crash_id]}],
                "call_chain_from_add2line": [
                    {
                        "thread_id": "22985",
                        "thread_name": "worker",
                        "is_crash_thread": False,
                        "is_main_thread": False,
                        "nodes": [crash_id],
                    }
                ],
            },
        }
        resolved = {
            "resolved_threads": [
                {
                    "tid": "21840",
                    "is_crash_thread": True,
                    "is_main_thread": True,
                    "frames": [{"module": "libace_compatible.z.so"}],
                },
                {
                    "tid": "22985",
                    "name": "worker",
                    "is_crash_thread": False,
                    "is_main_thread": False,
                    "frames": [
                        {
                            "resolved_function": "void Loop(void)",
                            "resolved_file": "/tmp/VTaskQueue.cpp",
                            "resolved_line": 298,
                            "module": "libapp.so",
                        }
                    ],
                },
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertIn("结论：无法在日志崩溃线程上确定崩溃源码行", text)
        self.assertNotIn("根据日志中堆栈顺序解析的函数/帧语义列表", text)
        self.assertNotIn("[第1帧][源码函数]", text)
        self.assertNotIn("## 按线程符号化结果（02）", text)
        self.assertIn("崩溃承载线程/现象线程", text)
        self.assertIn("优先分析下文其它包含业务帧的线程", text)
        self.assertNotIn("日志崩溃线程栈业务库帧", text)
        self.assertNotIn("日志标记的崩溃线程栈帧未命中你提供的库目录", text)
        self.assertIn("## 代码结构线索", text)
        self.assertIn("线程ID=22985 线程名=worker", text)
        self.assertIn("非崩溃线程，非主线程", text)
        self.assertNotIn("崩溃线程=否", text)
        self.assertNotIn("crash=no", text)
        self.assertNotIn("tid=", text)
        self.assertNotIn("## 崩溃点定位", text)
        self.assertNotIn("当前业务分析入口", text)
        self.assertNotIn("业务排查入口上下文", text)
        self.assertNotIn("入口说明:", text)
        self.assertNotIn("可疑单行", text)
        self.assertNotIn("崩溃所属类骨架", text)

    def test_owner_class_context_not_in_prompt_summary(self):
        crash_id = "func|/tmp/mtl.mm|void MTLVertexBuffer::createPrivateBuffer()"
        skel_id = "class_skeleton|/tmp/mtl_render_vertex_buffer.mm|MTLVertexBuffer"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "error_type": "SIGSEGV",
                "selected_analysis_is_crash_thread": True,
                "owner_class_node_id": skel_id,
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void MTLVertexBuffer::createPrivateBuffer()",
                        "file": "/tmp/mtl.mm",
                        "snippet": ["void MTLVertexBuffer::createPrivateBuffer() {", "}"],
                    },
                    {
                        "id": skel_id,
                        "type": "class_skeleton",
                        "class_name": "MTLVertexBuffer",
                        "file": "/tmp/mtl_render_vertex_buffer.mm",
                        "skeleton": "class MTLVertexBuffer {\n};",
                        "member_fields": ["VertexFormatInvalid", "_buffer"],
                    },
                ],
                "edges": [],
            },
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, {"resolved_frames": []}, code_ctx, problem={})
        self.assertNotIn("### 崩溃所属类型/类上下文", text)
        self.assertNotIn("definition_file:", text)
        self.assertNotIn("member_fields:", text)
        self.assertNotIn("- class_name:", text)
        self.assertNotIn("owner_class_context", text)
        self.assertIn("### 崩溃所属类骨架（MTLVertexBuffer）", text)

    def test_fix_output_prompt_omits_no_change_optional_sections(self):
        crash_id = "func|/tmp/Crash.cpp|void Crash::hit()"
        code_ctx = {
            "crash_summary": {"node_id": crash_id + " {", "error_type": "SIGSEGV"},
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Crash::hit()",
                        "file": "/tmp/Crash.cpp",
                        "snippet": ["void Crash::hit() {", "}"],
                    }
                ],
                "edges": [],
            },
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, {"resolved_frames": []}, code_ctx, problem={})
        self.assertNotIn("**可选提供**", text)
        self.assertNotIn("无需修改但与根因相关", text)
        self.assertNotIn("#### 无需修改但关键相关的函数", text)
        self.assertIn("「需要修改的函数」与「修复代码」必须一一对应", text)
        self.assertIn("**禁止**单独列出「无需修改的函数」章节", text)


class TestCodeContentProviderV2(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.code_root = Path(self.test_dir)

        (self.code_root / "main.cpp").write_text(
            (
                '#include <iostream>\n'
                '#include "my_lib.h"\n'
                "\n"
                "int main() {\n"
                "    call_example_function();\n"
                "    return 0;\n"
                "}\n"
            ),
            encoding="utf-8",
        )

        (self.code_root / "my_lib.h").write_text(
            (
                "#pragma once\n"
                'extern "C" { void call_example_function(); }\n'
            ),
            encoding="utf-8",
        )

        self.my_lib_cpp = self.code_root / "my_lib.cpp"
        self.my_lib_cpp.write_text(
            (
                '#include "my_lib.h"\n'
                "void call_example_function() {\n"
                "    int* ptr = nullptr;\n"
                "    *ptr = 42;  // crash\n"
                "}\n"
            ),
            encoding="utf-8",
        )

        crash_line = 1
        for i, line in enumerate(self.my_lib_cpp.read_text(encoding="utf-8").splitlines(), start=1):
            if "*ptr = 42" in line:
                crash_line = i
                break

        self.sample_add2line_json = json.dumps(
            {
                "resolved_frames": [
                    {
                        "address": "0x0000000100001234",
                        "resolved_function": "call_example_function()",
                        "resolved_file": "my_lib.cpp",
                        "resolved_line": crash_line,
                    },
                    {
                        "address": "0x0000000100001567",
                        "resolved_function": "main()",
                        "resolved_file": "main.cpp",
                        "resolved_line": 1,
                    },
                ],
                "os_type": "macos",
                "success_count": 2,
                "total_count": 2,
                "library_path": "/path/to/library",
            },
            ensure_ascii=False,
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_is_supported_file(self):
        p = CodeContentProvider(code_parser_backend="regex")
        self.assertTrue(p._is_supported_file("a.cpp"))
        self.assertTrue(p._is_supported_file("a.h"))
        self.assertFalse(p._is_supported_file("a.zip"))

    def test_code_content_provider_v2_output(self):
        p = CodeContentProvider(code_parser_backend="regex")
        out = json.loads(p.code_content_provider(self.sample_add2line_json, str(self.code_root)))

        for key in [
            "crash_summary",
            "graph",
            "diagnostics",
        ]:
            self.assertIn(key, out)

        self.assertIn("nodes", out["graph"])
        self.assertEqual(out["diagnostics"]["code_parser_backend"], "regex")
        cs = out["crash_summary"]
        self.assertIn("crash_location", cs)
        self.assertNotIn("analysis_entry", cs)
        self.assertNotIn("crash_line_code", cs)
        self.assertNotIn("node_id", cs)
        loc = cs["crash_location"]
        self.assertIn("location_type", loc)
        # 无崩溃线程元数据时为弱归因，03 仅保留 location_type
        self.assertNotIn("node_id", loc)
        self.assertNotIn("line", loc)

    def test_extract_shared_vars_skips_cpp_type_names(self):
        """std::string 等类型名不得作为共享变量候选。"""
        p = CodeContentProvider(code_parser_backend="regex")
        code = (
            "static napi_value Foo(napi_env env) {\n"
            "    std::string strJson = \"{}\";\n"
            "    napi_value item = nullptr;\n"
            "    return item;\n"
            "}\n"
        )
        shared = p._extract_shared_variables_from_code(code)
        self.assertNotIn("string", shared)
        self.assertNotIn("std", shared)

    def test_extract_class_name_rejects_namespace_only_prefix(self):
        """addr2line 的 ns::free_func 不得把命名空间当成 owner 类。"""
        p = CodeContentProvider(code_parser_backend="regex")
        root = Path(self.test_dir) / "ns_only"
        root.mkdir(parents=True, exist_ok=True)
        cpp = root / "napi.cpp"
        cpp.write_text(
            "namespace _baidu_framework {\n"
            "static int NapiMapIF_GetNearlyObj() { return 0; }\n"
            "}\n",
            encoding="utf-8",
        )
        cls = p._extract_class_name_from_resolved(
            "_baidu_framework::NapiMapIF_GetNearlyObj(napi_env env)",
            crash_file=str(cpp),
            code_roots=[str(root.resolve())],
        )
        self.assertIsNone(cls)

    def test_extract_class_name_accepts_real_class_member(self):
        p = CodeContentProvider(code_parser_backend="regex")
        root = Path(self.test_dir) / "cls_member"
        root.mkdir(parents=True, exist_ok=True)
        (root / "Worker.h").write_text(
            "class Worker {\npublic:\n    void Loop();\n};\n",
            encoding="utf-8",
        )
        cpp = root / "Worker.cpp"
        cpp.write_text("void Worker::Loop() { while (true) {} }\n", encoding="utf-8")
        cls = p._extract_class_name_from_resolved(
            "Worker::Loop()",
            crash_file=str(cpp),
            code_roots=[str(root.resolve())],
        )
        self.assertEqual(cls, "Worker")

    def test_merge_class_field_usage_into_shared_vars(self):
        """成员函数内未写 this-> 的成员名，应能从类定义合并进共享变量候选。"""
        root = Path(self.test_dir) / "cls_merge"
        root.mkdir(parents=True, exist_ok=True)
        (root / "Foo.h").write_text(
            "class Foo {\npublic:\n    int head;\n    int tail;\n};\n",
            encoding="utf-8",
        )
        cpp = root / "Foo.cpp"
        cpp.write_text(
            "void Foo::bar() {\n    head = tail;\n}\n",
            encoding="utf-8",
        )
        p = CodeContentProvider(code_parser_backend="regex")
        merged = p._merge_class_field_usage_into_shared_vars(
            [],
            "head = tail;",
            "Foo::bar",
            str(cpp),
            [str(root.resolve())],
        )
        self.assertIn("head", merged)
        self.assertIn("tail", merged)

    def test_objc_mm_function_snippet_excludes_namespace_macro(self):
        """readlines + 函数体提取不应产生双换行，snippet 不得从 NAMESPACE 宏起跨函数。"""
        mm = self.code_root / "mtl_render_vertex_buffer.mm"
        mm.write_text(
            '#include "mtl_render_vertex_buffer.h"\n\n'
            "NAMESPACE_BAIDU_VI_BEGIN\n\n"
            "void MTLVertexBuffer::createPrivateBuffer(id<MTLDevice> device, id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)\n"
            "{\n"
            "    @autoreleasepool\n"
            "    {\n"
            "        id<MTLCommandBuffer> cmd_buffer = [commandQueue commandBuffer];\n"
            "    }\n"
            "}\n\n"
            "MTLVertexBuffer::MTLVertexBuffer(id<MTLDevice> device, size_t len, StorageMode type)\n"
            "{\n"
            "    if (device) {\n"
            "        _buffer = nil;\n"
            "    }\n"
            "}\n\n"
            "NAMESPACE_BAIDU_VI_END\n",
            encoding="utf-8",
        )
        sig = (
            "void MTLVertexBuffer::createPrivateBuffer(id<MTLDevice> device, "
            "id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)"
        )
        p = CodeContentProvider(code_parser_backend="regex")
        p.current_code_roots = [str(self.code_root.resolve())]
        cf = p._extract_crash_function(sig, str(mm.resolve()), 5, [str(self.code_root.resolve())])
        self.assertIsNotNone(cf)
        self.assertEqual(cf.snippet_start_line, 5)
        self.assertEqual(cf.snippet_end_line, 11)
        self.assertTrue(cf.snippet[0].startswith("void MTLVertexBuffer::createPrivateBuffer"))
        self.assertFalse(any("NAMESPACE_BAIDU_VI_BEGIN" in ln for ln in cf.snippet))
        self.assertFalse(any("size_t len" in ln for ln in cf.snippet))
        self.assertEqual(len(cf.snippet), cf.snippet_end_line - cf.snippet_start_line + 1)

        refined_snippet, ss, se = p._refine_function_snippet_bounds(
            str(mm.resolve()), cf.snippet, sig, cf.snippet_start_line, cf.snippet_end_line
        )
        self.assertEqual(ss, 5)
        self.assertEqual(se, 11)
        self.assertEqual(refined_snippet[0], cf.snippet[0])


if __name__ == "__main__":
    unittest.main()


