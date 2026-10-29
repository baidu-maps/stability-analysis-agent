#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动改码候选回退与超时降级图的单测。"""

import re
import unittest
import tempfile
from pathlib import Path

from services.code_fixer import (
    CodeFixer,
    _complete_missing_repair_code,
    edit_allowed_by_prompt_complete_body,
    extract_candidate_nodes,
    graph_auto_fix_allowed,
    promote_read_file_function_candidates,
    _attach_missing_symbols,
    _collect_allowed_member_names,
    _contains_placeholder_code,
    _normalize_code_for_equivalence,
    _extract_include_directive_edits,
    _extract_function_block_from_code,
    _extract_replacement_from_analysis,
    _extract_required_function_names_from_analysis,
    _replacement_signature_compatible,
    _ensure_owner_class_methods_in_targets,
    _select_required_targets,
    _validate_and_fix_member_references,
    _validate_method_calls_in_replacement,
    is_forbidden_patch,
)
from tools.code_content_provider_tool import CodeContentProvider, CrashFunction
from tools.fix_code_applier_tool import FixCodeApplierTool
from tools.fix_code_extractor_tool import FixCodeExtractorTool


class TestApplyAiFixesFallback(unittest.TestCase):
    def test_extract_candidate_nodes_from_empty_graph_and_node_id(self):
        code_context = {
            "crash_summary": {
                "node_id": (
                    "func|/tmp/demo-app/"
                    "src/app/map/layers/DemoMapControl.cpp|void DemoMapControl::ReleaseAllLayers() {"
                ),
                "crash_line_number": 850,
            },
            "graph": {"nodes": [], "edges": []},
        }
        nodes = extract_candidate_nodes(code_context)
        self.assertEqual(len(nodes), 1)
        self.assertIn("ReleaseAllLayers", nodes[0]["signature"])

    def test_graph_auto_fix_allowed_with_calls_to_crash_site(self):
        code_context = {
            "graph": {
                "evidence_summary": {
                    "auto_fix_allowed": True,
                    "auto_fix_block_reason": None,
                },
                "edges": [{"type": "calls_to_crash_site", "from_id": "a", "to_id": "b"}],
            }
        }
        allowed, reason = graph_auto_fix_allowed(code_context)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_complete_crash_snippet_overrides_cached_auto_fix_block(self):
        """定位缓存里的 auto_fix_allowed=false 不能挡住已有完整崩溃函数体的改码。"""
        code_context = {
            "crash_summary": {"node_id": "func|/tmp/style_theme.cpp|VVoid CVStyleScene::Unload()"},
            "graph": {
                "nodes": [
                    {
                        "id": "func|/tmp/style_theme.cpp|VVoid CVStyleScene::Unload()",
                        "type": "function",
                        "signature": "VVoid CVStyleScene::Unload()",
                        "file": "/tmp/style_theme.cpp",
                        "snippet": [
                            "VVoid CVStyleScene::Unload()",
                            "{",
                            "    V_free(it->second);",
                            "}",
                        ],
                    }
                ],
                "edges": [],
                "evidence_summary": {
                    "auto_fix_allowed": False,
                    "auto_fix_block_reason": "无 calls_direct；无崩溃点外共享变量写路径。仅允许输出调查假设，禁止自动改码。",
                },
            },
        }
        allowed, reason = graph_auto_fix_allowed(code_context)
        self.assertTrue(allowed, reason)
        self.assertEqual(reason, "")

    def test_stack_symbol_complete_snippet_allows_auto_fix(self):
        """超时图只有崩溃函数节点时，用栈符号认定可改码。"""
        code_context = {
            "graph": {
                "nodes": [
                    {
                        "id": "func|/tmp/style_theme.cpp|VVoid CVStyleScene::Unload()",
                        "type": "function",
                        "signature": "VVoid CVStyleScene::Unload()",
                        "snippet": ["VVoid CVStyleScene::Unload()", "{", "    V_free(p);", "}"],
                    }
                ],
                "stack_function_symbols": [
                    {"function": "_baidu_framework::CVStyleScene::Unload() +648"},
                ],
                "evidence_summary": {
                    "auto_fix_allowed": False,
                    "auto_fix_block_reason": "无 calls_direct / calls_stack_verified / calls_to_crash_site；无崩溃点外共享变量写路径。仅允许输出调查假设，禁止自动改码。",
                },
            }
        }
        allowed, reason = graph_auto_fix_allowed(code_context)
        self.assertTrue(allowed, reason)

    def test_crash_func_snippet_usable_rejects_timeout_placeholder(self):
        provider = CodeContentProvider()
        if not hasattr(provider, "_crash_func_snippet_usable"):
            self.skipTest("CodeContentProvider implementation does not expose _crash_func_snippet_usable")
        bad = CrashFunction(
            name="foo",
            signature="foo()",
            snippet=["（代码上下文整阶段超时，未完成源码提取）"],
            crash_line="",
            snippet_scope="error",
        )
        self.assertFalse(provider._crash_func_snippet_usable(bad))
        good = CrashFunction(
            name="foo",
            signature="void foo() {",
            snippet=["void foo() {", "  return;", "}"],
            crash_line="return;",
        )
        self.assertTrue(provider._crash_func_snippet_usable(good))

    def test_equivalence_ignores_format_and_comments(self):
        old = """
        void Foo::Bar()
        {
            LockData();
            DoWork( 1 , 2 );
            UnlockData();
        }
        """
        replacement = """
        // 修复函数: Foo::Bar
        void Foo::Bar(){
            // 加锁保护
            LockData();
            DoWork(1,2);
            UnlockData();
        }
        """
        self.assertEqual(
            _normalize_code_for_equivalence(old),
            _normalize_code_for_equivalence(replacement),
        )

    def test_placeholder_comments_are_rejected_as_incomplete_code(self):
        replacement = """
        void Foo::Bar()
        {
            DoBefore();
            // ... [其他清理代码] ...
            DoAfter();
        }
        """
        self.assertTrue(_contains_placeholder_code(replacement))

    def test_placeholder_ellipsis_comment_alone_is_rejected(self):
        """`// ... 处理...` 截断注释须判为占位，避免半截函数落盘。"""
        from services.code_fixer import _contains_placeholder_code

        replacement = (
            "void DemoRoute::Build()\n"
            "{\n"
            "    if (m_trafficLightGPBuilder) {\n"
            "        // ... 处理交通灯数据 ...\n"
            "    }\n"
            "}\n"
        )
        self.assertTrue(_contains_placeholder_code(replacement))

    def test_destructive_body_shrink_rejects_truncated_long_function(self):
        """长函数被大幅缩水的 replacement 必须拒绝。"""
        from services.code_fixer import _check_destructive_body_shrink

        old_lines = ["void DemoRoute::Build()", "{"]
        for i in range(80):
            old_lines.append(f"    m_renderSteps.Add(item{i});")
        old_lines.append("}")
        old_block = "\n".join(old_lines)
        replacement = (
            "void DemoRoute::Build()\n"
            "{\n"
            "    m_renderSteps.Add(item0);\n"
            "}\n"
        )
        err = _check_destructive_body_shrink(old_block, replacement)
        self.assertIsNotNone(err)
        self.assertIn("大幅缩水", str(err))

    def test_function_block_extractor_rejects_call_statement_before_brace(self):
        text = """
ReleaseAllLayers();

if (m_pTaskGrop) {
    DemoDelete<DemoTaskGroup>(m_pTaskGrop);
}
"""
        idx = text.find("ReleaseAllLayers")
        self.assertIsNone(_extract_function_block_from_code(text, idx))

    def test_replacement_signature_rejects_destructor_return_type(self):
        replacement = """
bool DemoMapControl::~DemoMapControl()
{
    ReleaseAllLayers();
}
"""
        self.assertFalse(
            _replacement_signature_compatible(
                "demo_fw::DemoMapControl::~DemoMapControl()",
                replacement,
            )
        )

    def test_replacement_signature_rejects_call_statement_fragment(self):
        replacement = """
// ReleaseAllLayers(); // 已在析构函数末尾统一调用

if (m_pTaskGrop) {
    DemoDelete<DemoTaskGroup>(m_pTaskGrop);
}
"""
        self.assertFalse(
            _replacement_signature_compatible(
                "void DemoMapControl::ReleaseAllLayers()",
                replacement,
            )
        )

    def test_apply_relocates_current_function_when_snippet_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "demo.cpp"
            src.write_text(
                "int Foo::Bar()\n"
                "{\n"
                "    return 1;\n"
                "}\n",
                encoding="utf-8",
            )
            stale_snippet = ["int Foo::Bar()", "{", "    return 0;", "}"]
            candidate_nodes = [
                {
                    "type": "function",
                    "file": str(src),
                    "signature": "int Foo::Bar()",
                    "snippet": stale_snippet,
                    "snippet_start_line": 1,
                    "snippet_end_line": 4,
                }
            ]
            fix_plan = {
                "summary": "test",
                "edits": [
                    {
                        "file": str(src),
                        "function_signature": "int Foo::Bar()",
                        "replacement_code": "int Foo::Bar()\n{\n    return 2;\n}",
                    }
                ],
            }

            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes,
                [str(root)],
                backup_original_sources=False,
                code_context={"graph": {}},
            )

            self.assertTrue(result.success, result.to_dict())
            self.assertIn("return 2;", src.read_text(encoding="utf-8"))

    def test_select_required_targets_prefers_class_scope_over_same_simple_name(self):
        candidate_nodes = [
            {
                "type": "function",
                "file": "/tmp/a.cpp",
                "signature": "bool CBVMDOfflinePCDN::Cancel(void)",
                "snippet": ["bool CBVMDOfflinePCDN::Cancel(void) { return V_TRUE; }"],
            },
            {
                "type": "function",
                "file": "/tmp/DemoTaskQueue.h",
                "signature": "void Cancel()",
                "snippet": ["void  Cancel(){ m_cancel = true; }"],
            },
        ]
        targets = _select_required_targets(candidate_nodes, ["DemoTask::Cancel()"])
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["file"], "/tmp/DemoTaskQueue.h")
        self.assertEqual(targets[0]["function_signature"], "void Cancel()")

    def test_extract_replacement_from_class_block_inline_method(self):
        analysis_text = """
```cpp
class DemoTask {
public:
    void Cancel() {
        m_cancel.store(true, std::memory_order_release);
    }
    bool IsDone() const {
        State state = m_state.load(std::memory_order_acquire);
        return state == Complete || state == Canceled;
    }
};
```
"""
        repl = _extract_replacement_from_analysis(analysis_text, "bool IsDone()const")
        self.assertIsNotNone(repl)
        self.assertIn("bool IsDone()", repl)
        self.assertIn("return state == Complete || state == Canceled;", repl)

    def test_ensure_owner_class_methods_in_targets_adds_missing_inline_methods(self):
        code_context = {
            "crash_summary": {
                "owner_class_context": {
                    "class_name": "DemoTask",
                    "definition_file": "/tmp/DemoTaskQueue.h",
                    "class_body_excerpt": [
                        "void  Cancel(){ m_cancel = true; }",
                        "bool  IsCancel()const { return m_cancel; }",
                        "void  SetState(State state) { m_state = state; }",
                        "bool  IsDone()const { return m_state == Complete || m_state == Canceled; }",
                    ],
                }
            }
        }
        required_names = ["DemoTask::Cancel()", "DemoTask::SetState(State state)"]
        required_targets = [{"file": "/tmp/DemoTaskQueue.h", "function_signature": "bool IsDone()const"}]
        out = _ensure_owner_class_methods_in_targets(code_context, required_names, required_targets)
        sigs = {x["function_signature"] for x in out}
        self.assertIn("Cancel()", sigs)
        self.assertIn("SetState(State state)", sigs)

    def test_ensure_owner_class_methods_rewrites_owner_method_file(self):
        code_context = {
            "crash_summary": {
                "owner_class_context": {
                    "class_name": "DemoTask",
                    "definition_file": "/tmp/demo-app/include/util/DemoTaskQueue.h",
                    "class_body_excerpt": [
                        "bool  IsDone()const { return m_state == Complete || m_state == Canceled; }",
                    ],
                }
            }
        }
        out = _ensure_owner_class_methods_in_targets(
            code_context,
            ["DemoTask::IsDone()"],
            [
                {
                    "file": "/tmp/demo-app/demo-lib/include/util/DemoTaskQueue.h",
                    "function_signature": "bool IsDone()const",
                }
            ],
        )
        self.assertEqual(out[0]["file"], "/tmp/demo-app/include/util/DemoTaskQueue.h")

    def test_ensure_owner_class_methods_discards_other_class_same_name(self):
        code_context = {
            "crash_summary": {
                "owner_class_context": {
                    "class_name": "DemoTask",
                    "definition_file": "/tmp/DemoTaskQueue.h",
                    "class_body_excerpt": [
                        "void  Cancel(){ m_cancel = true; }",
                    ],
                }
            }
        }
        out = _ensure_owner_class_methods_in_targets(
            code_context,
            ["DemoTask::Cancel()"],
            [
                {
                    "file": "/tmp/Other.cpp",
                    "function_signature": "bool CBVMDOfflinePCDN::Cancel(void)",
                }
            ],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["file"], "/tmp/DemoTaskQueue.h")
        self.assertEqual(out[0]["function_signature"], "Cancel()")

    def test_extract_include_directive_edits_from_analysis(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "DemoTaskQueue.h"
            src.write_text("class DemoTask {};\n", encoding="utf-8")
            code_context = {
                "crash_summary": {
                    "owner_class_context": {
                        "class_name": "DemoTask",
                        "definition_file": str(src),
                    }
                }
            }
            analysis_text = """
```cpp
#include <atomic>

class DemoTask {};
```
"""
            edits = _extract_include_directive_edits(analysis_text, code_context)
            self.assertEqual(len(edits), 1)
            self.assertEqual(edits[0]["edit_type"], "include_directive")
            self.assertEqual(edits[0]["include"], "#include <atomic>")

    def test_apply_include_directive_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "DemoTaskQueue.h"
            src.write_text("#include <string>\n\nclass DemoTask {};\n", encoding="utf-8")
            candidate_nodes = [
                {
                    "type": "function",
                    "file": str(src),
                    "signature": "bool IsDone()const",
                    "snippet": ["bool IsDone()const { return false; }"],
                    "snippet_start_line": 3,
                    "snippet_end_line": 3,
                }
            ]
            fix_plan = {
                "summary": "test",
                "edits": [
                    {
                        "file": str(src),
                        "edit_type": "include_directive",
                        "include": "#include <atomic>",
                    }
                ],
            }

            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes,
                [str(root)],
                backup_original_sources=False,
                code_context={"graph": {}},
            )

            self.assertTrue(result.success, result.to_dict())
            self.assertIn("#include <string>\n#include <atomic>", src.read_text(encoding="utf-8"))

    def test_atomic_load_store_methods_are_not_rejected_as_unknown(self):
        original = """
class DemoTask {
private:
    volatile State m_state;
    volatile bool m_cancel;
};
"""
        replacement = """
bool IsDone() const {
    State state = m_state.load(std::memory_order_acquire);
    return state == Complete || state == Canceled;
}
void Cancel() {
    m_cancel.store(true, std::memory_order_release);
}
"""
        self.assertIsNone(_validate_method_calls_in_replacement(original, replacement))

    def test_apply_does_not_block_this_nullptr_guard(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "demo.cpp"
            src.write_text(
                "bool DemoTask::IsDone() const\n"
                "{\n"
                "    return m_state == Complete || m_state == Canceled;\n"
                "}\n",
                encoding="utf-8",
            )
            candidate_nodes = [
                {
                    "type": "function",
                    "file": str(src),
                    "signature": "bool DemoTask::IsDone() const",
                    "snippet": [
                        "bool DemoTask::IsDone() const",
                        "{",
                        "    return m_state == Complete || m_state == Canceled;",
                        "}",
                    ],
                    "snippet_start_line": 1,
                    "snippet_end_line": 4,
                }
            ]
            fix_plan = {
                "summary": "test",
                "edits": [
                    {
                        "file": str(src),
                        "function_signature": "bool DemoTask::IsDone() const",
                        "replacement_code": (
                            "bool DemoTask::IsDone() const\n"
                            "{\n"
                            "    if (this == nullptr) {\n"
                            "        return false;\n"
                            "    }\n"
                            "    return m_state == Complete || m_state == Canceled;\n"
                            "}"
                        ),
                    }
                ],
            }

            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes,
                [str(root)],
                backup_original_sources=False,
                code_context={"graph": {}},
            )

            self.assertTrue(result.success, result.to_dict())
            self.assertIn("this == nullptr", src.read_text(encoding="utf-8"))

    def test_nullptr_guard_only_body_is_forbidden_but_real_logic_is_not(self):
        """仅 this==nullptr 早返回的假修复仍拦截；后面还有真实逻辑则放行。"""
        only = (
            "bool DemoTask::IsDone() const {\n"
            "    if (this == nullptr) { return false; }\n"
            "    return false;\n"
            "}\n"
        )
        real = (
            "bool DemoTask::IsDone() const {\n"
            "    if (this == nullptr) { return false; }\n"
            "    return m_state == Complete || m_state == Canceled;\n"
            "}\n"
        )
        self.assertIsNotNone(is_forbidden_patch("bool DemoTask::IsDone() const", only))
        self.assertIsNone(is_forbidden_patch("bool DemoTask::IsDone() const", real))

    def test_extractor_keeps_model_code_without_semantic_filtering(self):
        analysis_text = """
#### 需要修改的函数
- `DemoTask::IsDone()` - 添加对象有效性检查

```cpp
bool DemoTask::IsDone() const {
    if (this == nullptr) {
        return false;
    }
    return m_state == Complete || m_state == Canceled;
}
```
"""
        code_context = {
            "crash_summary": {
                "owner_class_context": {
                    "class_name": "DemoTask",
                    "definition_file": "/tmp/DemoTaskQueue.h",
                    "class_body_excerpt": [
                        "bool  IsDone()const { return m_state == Complete || m_state == Canceled; }",
                    ],
                }
            },
            "graph": {
                "nodes": [
                    {
                        "type": "function",
                        "file": "/tmp/DemoTaskQueue.h",
                        "signature": "bool IsDone()const",
                        "snippet": ["bool  IsDone()const { return m_state == Complete || m_state == Canceled; }"],
                    }
                ]
            },
        }

        out = FixCodeExtractorTool().execute(
            {"analysis_text": analysis_text, "code_context": code_context}
        )

        self.assertTrue(out.get("success"), out)
        edits = out.get("fix_plan", {}).get("edits", [])
        self.assertEqual(len(edits), 1)
        self.assertIn("this == nullptr", edits[0]["replacement_code"])

    def test_extractor_keeps_invalid_destructor_signature_for_debugging(self):
        analysis_text = """
#### 需要修改的函数
- `DemoMapControl::~DemoMapControl()` - 调整析构顺序

```cpp
bool DemoMapControl::~DemoMapControl()
{
    ReleaseAllLayers();
}
```
"""
        code_context = {
            "graph": {
                "nodes": [
                    {
                        "type": "function",
                        "file": "/tmp/DemoMapControl.cpp",
                        "signature": "demo_fw::DemoMapControl::~DemoMapControl()",
                        "snippet": ["DemoMapControl::~DemoMapControl()", "{", "}"],
                    }
                ]
            }
        }

        out = FixCodeExtractorTool().execute(
            {"analysis_text": analysis_text, "code_context": code_context}
        )

        self.assertTrue(out.get("success"), out)
        edits = out.get("fix_plan", {}).get("edits", [])
        self.assertEqual(len(edits), 1)
        self.assertIn("bool DemoMapControl::~DemoMapControl()", edits[0]["replacement_code"])

    def test_extract_replacement_keeps_template_prefix(self):
        analysis_text = """
```cpp
template< class TYPE >
inline void DemoDelete(TYPE* pObjects)
{
    if (pObjects == nullptr)
    {
        return;
    }
    pObjects = nullptr;
}
```
"""
        replacement = _extract_replacement_from_analysis(
            analysis_text,
            "void demo_util::DemoDelete<demo_fw::DemoMapControl>(demo_fw::DemoMapControl*)",
        )
        self.assertIsNotNone(replacement)
        self.assertTrue(replacement.startswith("template< class TYPE >"), replacement)

    def test_apply_relocates_template_function_missing_from_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "templ.h"
            src.write_text(
                "template< class TYPE >\n"
                "inline void DemoDelete(TYPE* pObjects)\n"
                "{\n"
                "    if (pObjects == nullptr)\n"
                "    {\n"
                "        return;\n"
                "    }\n"
                "    CVMem::Deallocate(pObjects);\n"
                "}\n",
                encoding="utf-8",
            )
            fix_plan = {
                "summary": "test",
                "edits": [
                    {
                        "file": str(src),
                        "function_signature": (
                            "void demo_util::DemoDelete<demo_fw::DemoMapControl>"
                            "(demo_fw::DemoMapControl*)"
                        ),
                        "replacement_code": (
                            "template< class TYPE >\n"
                            "inline void DemoDelete(TYPE* pObjects)\n"
                            "{\n"
                            "    if (pObjects == nullptr)\n"
                            "    {\n"
                            "        return;\n"
                            "    }\n"
                            "    CVMem::Deallocate(pObjects);\n"
                            "    pObjects = nullptr;\n"
                            "}"
                        ),
                    }
                ],
            }

            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes=[],
                code_roots=[str(root)],
                backup_original_sources=False,
                code_context={"graph": {}},
            )

            self.assertTrue(result.success, result.to_dict())
            text = src.read_text(encoding="utf-8")
            self.assertIn("template< class TYPE >\ninline void DemoDelete", text)
            self.assertIn("pObjects = nullptr;", text)

    def test_reject_overlapping_snippet_start_line_for_objc_autoreleasepool(self):
        """错误 snippet_start_line（落在 NAMESPACE 行）不应跨函数替换。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "mtl_render_vertex_buffer.mm"
            src.write_text(
                '#include "mtl_render_vertex_buffer.h"\n\n'
                "NAMESPACE_BAIDU_VI_BEGIN\n\n"
                "void DemoVertexBuffer::createPrivateBuffer(id<MTLDevice> device, id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)\n"
                "{\n"
                "    @autoreleasepool\n"
                "    {\n"
                "        id<MTLCommandBuffer> cmd_buffer = [commandQueue commandBuffer];\n"
                "    }\n"
                "}\n\n"
                "DemoVertexBuffer::DemoVertexBuffer(id<MTLDevice> device, size_t len, StorageMode type)\n"
                "{\n"
                "    if (device) {\n"
                "        _buffer = nil;\n"
                "    }\n"
                "}\n\n"
                "NAMESPACE_BAIDU_VI_END\n",
                encoding="utf-8",
            )
            sig = (
                "void DemoVertexBuffer::createPrivateBuffer(id<MTLDevice> device, "
                "id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)"
            )
            bad_node = {
                "snippet_start_line": 3,
                "snippet": src.read_text(encoding="utf-8").splitlines()[2:],
            }
            replacement = (
                "void DemoVertexBuffer::createPrivateBuffer(id<MTLDevice> device, "
                "id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)\n"
                "{\n"
                "    @autoreleasepool { if (device) { _buffer = nil; } }\n"
                "}"
            )
            fix_plan = {
                "summary": "test",
                "edits": [
                    {
                        "file": str(src),
                        "function_signature": sig,
                        "replacement_code": replacement,
                    }
                ],
            }
            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes=[{"file": str(src), "signature": sig, **bad_node}],
                code_roots=[str(root)],
                backup_original_sources=False,
            )
            text = src.read_text(encoding="utf-8")
            self.assertTrue(result.success, result.to_dict())
            self.assertIn("NAMESPACE_BAIDU_VI_BEGIN", text)
            self.assertIn("size_t len", text)
            self.assertNotIn("    }\n}\n    }", text)


    def test_apply_rejects_namespace_polluted_signature_and_leading_brace(self):
        """LLM 提取的 NAMESPACE 污染签名与 leading '}' 不应写入源码。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "mtl_render_vertex_buffer.mm"
            src.write_text(
                '#include "mtl_render_vertex_buffer.h"\n\n'
                "NAMESPACE_BAIDU_VI_BEGIN\n\n"
                "void DemoVertexBuffer::createPrivateBuffer(id<MTLDevice> device, id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)\n"
                "{\n"
                "    @autoreleasepool { id<MTLCommandBuffer> cmd_buffer = nil; }\n"
                "}\n\n"
                "NAMESPACE_BAIDU_VI_END\n",
                encoding="utf-8",
            )
            bad_sig = (
                "NAMESPACE_BAIDU_VI_BEGIN void DemoVertexBuffer::createPrivateBuffer(id<MTLDevice> device, "
                "id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)"
            )
            bad_replacement = (
                "}\n\n"
                "void DemoVertexBuffer::createPrivateBuffer(id<MTLDevice> device, "
                "id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)\n"
                "{\n"
                "    @autoreleasepool { if (device) { return; } }\n"
                "}"
            )
            fix_plan = {
                "summary": "test",
                "edits": [
                    {
                        "file": str(src),
                        "function_signature": bad_sig,
                        "replacement_code": bad_replacement,
                    }
                ],
            }
            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes=[],
                code_roots=[str(root)],
                backup_original_sources=False,
            )
            text = src.read_text(encoding="utf-8")
            self.assertFalse(
                re.search(r"NAMESPACE_BAIDU_VI_BEGIN\n\n}", text),
                "不应在 NAMESPACE 后插入孤立 '}'",
            )
            applied = result.applied[0]
            self.assertEqual(applied["status"], "applied")
            self.assertNotIn("NAMESPACE_BAIDU_VI_BEGIN void", applied["function_signature"])
            self.assertTrue(text.index("NAMESPACE_BAIDU_VI_BEGIN") < text.index("void DemoVertexBuffer::createPrivateBuffer"))

    def test_validate_rejects_hallucinated_member_without_unique_match(self):
        original = (
            "class Demo {\n"
            "  CNMutex m_clInfoMutex;\n"
            "  CNMutex m_clOuterDataMutex;\n"
            "};\n"
            "int Demo::get() {\n"
            "  m_clInfoMutex.Lock(-1);\n"
            "  m_clInfoMutex.Unlock();\n"
            "  return 0;\n"
            "}\n"
        )
        replacement = (
            "int Demo::get()\n"
            "{\n"
            "    CVMutex::ScopedLock lock(m_clDataMutex);\n"
            "    return 0;\n"
            "}\n"
        )
        allowed = {"m_clInfoMutex", "m_clOuterDataMutex"}
        _, err = _validate_and_fix_member_references(
            original, replacement, allowed_members=allowed, allow_fuzzy_rewrite=False
        )
        self.assertIsNotNone(err)
        self.assertIn("m_clDataMutex", err or "")

    def test_validate_rewrites_unique_near_member_when_enabled(self):
        original = "void Foo::bar() { m_clInfoMutex.Lock(-1); m_clInfoMutex.Unlock(); }\n"
        replacement = (
            "void Foo::bar()\n{\n"
            "    m_clInfMutex.Lock(-1);\n"
            "    m_clInfMutex.Unlock();\n"
            "}\n"
        )
        fixed, err = _validate_and_fix_member_references(
            original,
            replacement,
            allowed_members={"m_clInfoMutex"},
            allow_fuzzy_rewrite=True,
        )
        self.assertIsNone(err)
        self.assertIn("m_clInfoMutex", fixed)
        self.assertNotIn("m_clInfMutex", fixed)

    def test_apply_rejects_hallucinated_mutex_member(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hdr = root / "engine.h"
            src = root / "engine.cpp"
            hdr.write_text(
                "class Engine {\n"
                "public:\n"
                "  Array<int> GetSteps();\n"
                "private:\n"
                "  CNMutex m_clRouteInfoMutex;\n"
                "  DemoRoute* m_pCurrentItem;\n"
                "};\n",
                encoding="utf-8",
            )
            src.write_text(
                '#include "engine.h"\n'
                "Array<int> Engine::GetSteps()\n"
                "{\n"
                "    Array<int> routes;\n"
                "    if (m_pCurrentItem) {\n"
                "        routes = m_pCurrentItem->GetDrawSteps();\n"
                "    }\n"
                "    return routes;\n"
                "}\n"
                "void Engine::Release()\n"
                "{\n"
                "    m_clRouteInfoMutex.Lock(-1);\n"
                "    m_clRouteInfoMutex.Unlock();\n"
                "}\n",
                encoding="utf-8",
            )
            skeleton_node = {
                "id": f"class_skeleton|{hdr}|Engine",
                "type": "class_skeleton",
                "class_name": "Engine",
                "file": str(hdr),
                "member_fields": ["m_clRouteInfoMutex", "m_pCurrentItem"],
                "skeleton": (
                    "class Engine {\n"
                    "  CNMutex m_clRouteInfoMutex;\n"
                    "  DemoRoute* m_pCurrentItem;\n"
                    "};\n"
                ),
            }
            candidate_nodes = [
                {
                    "type": "function",
                    "file": str(src),
                    "signature": "Array<int> Engine::GetSteps()",
                    "snippet": [
                        "Array<int> Engine::GetSteps()",
                        "{",
                        "    Array<int> routes;",
                        "    if (m_pCurrentItem) {",
                        "        routes = m_pCurrentItem->GetDrawSteps();",
                        "    }",
                        "    return routes;",
                        "}",
                    ],
                    "snippet_start_line": 2,
                }
            ]
            fix_plan = {
                "summary": "bad mutex",
                "edits": [
                    {
                        "file": str(src),
                        "function_signature": "Array<int> Engine::GetSteps()",
                        "replacement_code": (
                            "Array<int> Engine::GetSteps()\n"
                            "{\n"
                            "    Array<int> routes;\n"
                            "    CVMutex::ScopedLock lock(m_clRouteDataMutex);\n"
                            "    if (m_pCurrentItem) {\n"
                            "        routes = m_pCurrentItem->GetDrawSteps();\n"
                            "    }\n"
                            "    return routes;\n"
                            "}\n"
                        ),
                    }
                ],
            }
            code_context = {
                "crash_summary": {"owner_class_node_id": skeleton_node["id"]},
                "graph": {"nodes": [skeleton_node]},
            }
            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes,
                [str(root)],
                backup_original_sources=False,
                code_context=code_context,
            )
            self.assertFalse(result.success, result.to_dict())
            err = str((result.applied or [{}])[0].get("error") or "")
            self.assertIn("m_clRouteDataMutex", err)
            self.assertNotIn("m_clRouteDataMutex", src.read_text(encoding="utf-8"))
            allowed = _collect_allowed_member_names(
                src.read_text(encoding="utf-8"), code_context
            )
            self.assertIn("m_clRouteInfoMutex", allowed)

    def test_apply_rejects_bare_lock_unlock_when_member_lock_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "engine.cpp"
            src.write_text(
                "Array<int> Engine::GetSteps()\n"
                "{\n"
                "    Array<int> routes;\n"
                "    if (m_pCurrentItem) {\n"
                "        routes = m_pCurrentItem->GetDrawSteps();\n"
                "    }\n"
                "    return routes;\n"
                "}\n"
                "void Engine::Release()\n"
                "{\n"
                "    m_clRouteInfoMutex.Lock(-1);\n"
                "    m_clRouteInfoMutex.Unlock();\n"
                "}\n",
                encoding="utf-8",
            )
            candidate_nodes = [
                {
                    "type": "function",
                    "file": str(src),
                    "signature": "Array<int> Engine::GetSteps()",
                    "snippet": src.read_text(encoding="utf-8").splitlines()[:8],
                    "snippet_start_line": 1,
                }
            ]
            fix_plan = {
                "summary": "bare lock",
                "edits": [
                    {
                        "file": str(src),
                        "function_signature": "Array<int> Engine::GetSteps()",
                        "replacement_code": (
                            "Array<int> Engine::GetSteps()\n"
                            "{\n"
                            "    Array<int> routes;\n"
                            "    Lock();\n"
                            "    if (m_pCurrentItem) {\n"
                            "        routes = m_pCurrentItem->GetDrawSteps();\n"
                            "    }\n"
                            "    Unlock();\n"
                            "    return routes;\n"
                            "}\n"
                        ),
                    }
                ],
            }
            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes,
                [str(root)],
                backup_original_sources=False,
                code_context={"graph": {"nodes": []}},
            )
            self.assertFalse(result.success, result.to_dict())
            err = str((result.applied or [{}])[0].get("error") or "")
            self.assertIn("Lock()", err)
            self.assertNotIn("\n    Lock();\n", src.read_text(encoding="utf-8"))

    def test_apply_rejects_scoped_lock_on_lock_unlock_member(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "engine.cpp"
            src.write_text(
                "Array<int> Engine::GetSteps()\n"
                "{\n"
                "    Array<int> routes;\n"
                "    if (m_pCurrentItem) {\n"
                "        routes = m_pCurrentItem->GetDrawSteps();\n"
                "    }\n"
                "    return routes;\n"
                "}\n"
                "void Engine::Release()\n"
                "{\n"
                "    m_clRouteInfoMutex.Lock(-1);\n"
                "    m_clRouteInfoMutex.Unlock();\n"
                "}\n",
                encoding="utf-8",
            )
            candidate_nodes = [
                {
                    "type": "function",
                    "file": str(src),
                    "signature": "Array<int> Engine::GetSteps()",
                    "snippet": src.read_text(encoding="utf-8").splitlines()[:8],
                    "snippet_start_line": 1,
                }
            ]
            fix_plan = {
                "summary": "wrong lock api",
                "edits": [
                    {
                        "file": str(src),
                        "function_signature": "Array<int> Engine::GetSteps()",
                        "replacement_code": (
                            "Array<int> Engine::GetSteps()\n"
                            "{\n"
                            "    Array<int> routes;\n"
                            "    CVMutex::ScopedLock lock(m_clRouteInfoMutex);\n"
                            "    if (m_pCurrentItem) {\n"
                            "        routes = m_pCurrentItem->GetDrawSteps();\n"
                            "    }\n"
                            "    return routes;\n"
                            "}\n"
                        ),
                    }
                ],
            }
            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                candidate_nodes,
                [str(root)],
                backup_original_sources=False,
                code_context={"graph": {"nodes": []}},
            )
            self.assertFalse(result.success, result.to_dict())
            err = str((result.applied or [{}])[0].get("error") or "")
            self.assertIn("ScopedLock", err)
            self.assertNotIn("ScopedLock", src.read_text(encoding="utf-8"))

    def test_extractor_skips_class_skeleton_and_keeps_following_functions(self):
        """class 骨架 + 后续完整 Class::method 围栏时仍应提取 edits（3525 回归）。"""
        analysis_text = """
## 6. 修复建议
### 6.1 直接修复（代码级）
在类中添加互斥锁：

```cpp
class DemoMapOperator : public DemoSafeCallback
{
private:
    mutable std::mutex m_pBaseMapControlMutex; // 新增互斥锁
    // ... 其他成员 ...
};
```

修改GetMapStatus()函数：
```cpp
DemoMapStatus DemoMapOperator::GetMapStatus()
{
    std::lock_guard<std::mutex> lock(m_pBaseMapControlMutex);
    if (m_pBaseMapControl == nullptr) {
        return DemoMapStatus();
    }
    return m_pBaseMapControl->GetMapStatus();
}
```

修改析构函数：
```cpp
DemoMapOperator::~DemoMapOperator()
{
    std::lock_guard<std::mutex> lock(m_pBaseMapControlMutex);
    m_pBaseMapControl = nullptr;
}
```
"""
        code_context = {
            "graph": {
                "nodes": [
                    {
                        "type": "function",
                        "file": "/tmp/walk_map_operator.cpp",
                        "signature": "DemoMapStatus DemoMapOperator::GetMapStatus()",
                        "snippet": [
                            "DemoMapStatus DemoMapOperator::GetMapStatus()",
                            "{",
                            "    return m_pBaseMapControl->GetMapStatus();",
                            "}",
                        ],
                    },
                    {
                        "type": "function",
                        "file": "/tmp/walk_map_operator.cpp",
                        "signature": "DemoMapOperator::~DemoMapOperator()",
                        "snippet": [
                            "DemoMapOperator::~DemoMapOperator()",
                            "{",
                            "    m_pBaseMapControl = nullptr;",
                            "}",
                        ],
                    },
                ]
            }
        }
        names = _extract_required_function_names_from_analysis(analysis_text)
        self.assertTrue(
            any("GetMapStatus" in n for n in names),
            names,
        )
        self.assertTrue(
            any("~DemoMapOperator" in n or "析构" in n for n in names)
            or any("~" in n for n in names),
            names,
        )

        out = FixCodeExtractorTool().execute(
            {"analysis_text": analysis_text, "code_context": code_context}
        )
        self.assertTrue(out.get("success"), out)
        edits = out.get("fix_plan", {}).get("edits", [])
        self.assertGreaterEqual(len(edits), 2, edits)
        joined = "\n".join(str(e.get("replacement_code") or "") for e in edits)
        self.assertIn("GetMapStatus", joined)
        self.assertIn("~DemoMapOperator", joined)

    def test_objc_fence_names_c_function_and_read_file_candidate(self):
        """未符号化的 iOS 帧：ObjC 围栏里的 C 函数可从已读源码变成候选。"""
        source = (
            "__attribute__((visibility(\"hidden\"))) "
            "void bmk_signal_exception_handler(int signal, siginfo_t *info, void *context) {\n"
            "    suspend_all();\n"
            "    look_up();\n"
            "}\n"
            "void other(void) {\n"
            "    bmk_signal_exception_handler(1, 0, 0);\n"
            "}\n"
        )
        analysis = (
            "根因在信号处理器。\n"
            "```objc\n"
            "static void bmk_signal_exception_handler(int sig, siginfo_t *info, void *context) {\n"
            "    struct sigaction previous;\n"
            "    record(sig);\n"
            "}\n"
            "```\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk = root / "sdk"
            sdk.mkdir()
            header = sdk / "BMKSignalHandler.m"
            header.write_text(source, encoding="utf-8")
            decoy = root / "Other.m"
            decoy.write_text(source, encoding="utf-8")
            code_context = {"graph": {"nodes": [], "edges": []}}
            session = {
                "request_ledger": [
                    {"type": "read_file", "status": "success", "file": str(header)},
                    {"type": "read_file", "status": "success", "file": str(decoy)},
                    {"type": "grep", "status": "success", "file": "", "symbol": "sigaction"},
                ]
            }
            nodes = promote_read_file_function_candidates(
                code_context,
                analysis_text=analysis,
                context_session=session,
                code_roots=[str(sdk)],
            )
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]["origin"], "read_file")
            self.assertIn("bmk_signal_exception_handler", nodes[0]["signature"])
            self.assertTrue(str(nodes[0]["file"]).endswith("BMKSignalHandler.m"))
            self.assertIn("suspend_all", "\n".join(nodes[0]["snippet"]))
            summary = code_context["graph"]["evidence_summary"]
            self.assertTrue(summary["auto_fix_allowed"])
            self.assertEqual(summary["allow_reason"], "read_file_cited_function")
            allowed, reason = graph_auto_fix_allowed(code_context)
            self.assertTrue(allowed)
            self.assertEqual(reason, "")
            extracted = extract_candidate_nodes(code_context)
            self.assertEqual(len(extracted), 1)

    def test_function_request_promotes_existing_function(self):
        """账本 type=function 且带路径时，也能补出可替换候选。"""
        source = (
            "__attribute__((visibility(\"hidden\"))) "
            "void bmk_signal_exception_handler(int signal, siginfo_t *info, void *context) {\n"
            "    suspend_all();\n"
            "}\n"
        )
        analysis = (
            "```objc\n"
            "static void bmk_signal_exception_handler(int sig, siginfo_t *info, void *context) {\n"
            "    if (bmk_previousSignalHandler) {\n"
            "        bmk_previousSignalHandler(sig, info, context);\n"
            "    }\n"
            "}\n"
            "```\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BMKSignalHandler.m"
            path.write_text(source, encoding="utf-8")
            code_context = {"graph": {"nodes": [], "edges": []}}
            nodes = promote_read_file_function_candidates(
                code_context,
                analysis_text=analysis,
                context_session={
                    "request_ledger": [{
                        "type": "function",
                        "status": "success",
                        "symbol": "bmk_signal_exception_handler",
                        "file": str(path),
                    }],
                },
                code_roots=[tmp],
            )
            self.assertEqual(len(nodes), 1)
            self.assertIn("bmk_signal_exception_handler", nodes[0]["signature"])
            self.assertIn("suspend_all", "\n".join(nodes[0]["snippet"]))

    def test_function_resolved_context_promotes_without_ledger_file(self):
        """function 账本没写路径时，从 resolved_context 取到的文件也能补候选。"""
        source = (
            "void bmk_signal_exception_handler(int signal, siginfo_t *info, void *context) {\n"
            "    suspend_all();\n"
            "}\n"
        )
        analysis = (
            "```objc\n"
            "void bmk_signal_exception_handler(int signal, siginfo_t *info, void *context) {\n"
            "    if (bmk_previousSignalHandler) {\n"
            "        bmk_previousSignalHandler(signal, info, context);\n"
            "    }\n"
            "}\n"
            "```\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BMKSignalHandler.m"
            path.write_text(source, encoding="utf-8")
            code_context = {"graph": {"nodes": [], "edges": []}}
            session = {
                "request_ledger": [{
                    "type": "function",
                    "status": "success",
                    "symbol": "bmk_signal_exception_handler",
                    "file": "",
                }],
                "rounds": [{
                    "round_index": 0,
                    "resolved_context": [{
                        "success": True,
                        "file": str(path),
                        "request": {
                            "type": "function",
                            "symbol": "bmk_signal_exception_handler",
                        },
                    }],
                }],
            }
            nodes = promote_read_file_function_candidates(
                code_context,
                analysis_text=analysis,
                context_session=session,
                code_roots=[tmp],
            )
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]["file"], str(path.resolve()))

            class _NoLLM:
                def chat(self, messages, tools=None, **kwargs):
                    raise RuntimeError("must not call the LLM")

            def _execute(name, data):
                if name == "fix_code_extractor":
                    return FixCodeExtractorTool().execute(data)
                if name == "fix_code_applier":
                    return FixCodeApplierTool().execute(data)
                raise AssertionError(name)

            result = CodeFixer(llm_adapter=_NoLLM()).generate_and_apply(
                {
                    "analysis": analysis,
                    "code_context": {},
                    "parse_result": {},
                    "context_session": session,
                },
                [tmp],
                backup_original_sources=False,
                tool_executor=_execute,
            )
            self.assertTrue(result.success, result.error)
            written = path.read_text(encoding="utf-8")
            self.assertNotIn("suspend_all", written)
            self.assertIn("bmk_previousSignalHandler", written)

    def test_promote_adds_destructor_when_crash_candidate_exists(self):
        """图里已有 Unload 时，仍要把分析必改的 ~CVStyle 从缓存路径映射后落盘。"""
        analysis = (
            "**需要修改的函数（必改，解释本次崩溃）**\n"
            "- `VVoid CVStyleScene::Unload()`：\n"
            "- `CVStyle::~CVStyle()`：RLock 改 WLock\n"
            "  - `default` 分支改为 `VDelete((MapDisStyle*)val)`。\n\n"
            "### `style_theme.cpp`\n"
            "```cpp\n"
            "VVoid CVStyleScene::Unload()\n"
            "{\n"
            "    CVArray<StyleMapType*, StyleMapType*> levelParam;\n"
            "    V_free(p);\n"
            "}\n"
            "```\n\n"
            "### `Style.cpp`\n"
            "```cpp\n"
            "CVStyle::~CVStyle()\n"
            "{\n"
            "    m_themeLock->WLock();\n"
            "}\n"
            "```\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            theme = root / "engine-dev" / "src" / "style_theme.cpp"
            style = root / "engine-dev" / "src" / "Style.cpp"
            theme.parent.mkdir(parents=True)
            theme.write_text(
                "VVoid CVStyleScene::Unload()\n{\n    V_free(p);\n}\n",
                encoding="utf-8",
            )
            style.write_text(
                "CVStyle::~CVStyle()\n{\n    m_themeLock->RLock();\n}\n",
                encoding="utf-8",
            )
            cache_style = (
                "/Users/cache/workspaces/ios/7.1.0/baidu__mapclient__mapsdk-vector/"
                "engine-dev/src/Style.cpp"
            )
            code_context = {
                "graph": {
                    "nodes": [
                        {
                            "id": f"func|{theme}|VVoid CVStyleScene::Unload()",
                            "type": "function",
                            "signature": "VVoid CVStyleScene::Unload()",
                            "file": str(theme),
                            "snippet": [
                                "VVoid CVStyleScene::Unload()",
                                "{",
                                "    V_free(p);",
                                "}",
                            ],
                        }
                    ],
                    "edges": [],
                    "evidence_summary": {"auto_fix_allowed": True},
                }
            }
            session = {
                "request_ledger": [
                    {
                        "type": "function",
                        "status": "success",
                        "symbol": "CVStyle::~CVStyle",
                        "file": cache_style,
                    }
                ]
            }
            names = _extract_required_function_names_from_analysis(analysis)
            self.assertIn("CVStyle::~CVStyle()", names)
            self.assertNotIn("default", names)
            promoted = promote_read_file_function_candidates(
                code_context,
                analysis_text=analysis,
                context_session=session,
                code_roots=[str(root)],
            )
            self.assertEqual(len(promoted), 1)
            self.assertIn("~CVStyle", promoted[0]["signature"])
            self.assertEqual(promoted[0]["file"], str(style.resolve()))

            class _NoLLM:
                def chat(self, messages, tools=None, **kwargs):
                    raise RuntimeError("must not call the LLM")

            def _execute(name, data):
                if name == "fix_code_extractor":
                    return FixCodeExtractorTool().execute(data)
                if name == "fix_code_applier":
                    return FixCodeApplierTool().execute(data)
                raise AssertionError(name)

            result = CodeFixer(llm_adapter=_NoLLM()).generate_and_apply(
                {
                    "analysis": analysis,
                    "code_context": code_context,
                    "parse_result": {},
                    "context_session": session,
                },
                [str(root)],
                backup_original_sources=False,
                tool_executor=_execute,
            )
            self.assertTrue(result.success, result.error)
            applied_sigs = [item.get("function_signature") for item in result.applied]
            self.assertTrue(any("Unload" in str(sig) for sig in applied_sigs), applied_sigs)
            self.assertTrue(any("~CVStyle" in str(sig) for sig in applied_sigs), applied_sigs)
            self.assertIn("WLock", style.read_text(encoding="utf-8"))
            self.assertNotIn("RLock", style.read_text(encoding="utf-8"))
            self.assertIn("CVArray<StyleMapType*, StyleMapType*>", theme.read_text(encoding="utf-8"))

    def test_promoted_complete_body_is_allowed_despite_prompt_meta(self):
        """首轮提示词只含 Unload 时，后补的 ~CVStyle 完整候选仍允许写入。"""
        code_context = {
            "diagnostics": {
                "prompt_context_meta": {
                    "included_complete_signatures": ["VVoid CVStyleScene::Unload()"],
                }
            },
            "graph": {
                "nodes": [
                    {
                        "type": "function",
                        "signature": "CVStyle::~CVStyle()",
                        "file": "/tmp/Style.cpp",
                        "snippet": [
                            "CVStyle::~CVStyle()",
                            "{",
                            "    m_themeLock->RLock();",
                            "}",
                        ],
                    }
                ]
            },
        }
        self.assertIsNone(
            edit_allowed_by_prompt_complete_body("CVStyle::~CVStyle()", code_context)
        )
        self.assertEqual(
            edit_allowed_by_prompt_complete_body("CVStyleScene::~CVStyleScene()", code_context),
            "目标函数未在提示词中给出完整函数体，已拒绝写入",
        )

    def test_read_file_candidates_do_not_override_explicit_block(self):
        """证据摘要已经禁止改码时，补候选不能把结论改成允许。"""
        source = (
            "void bmk_signal_exception_handler(int signal) {\n"
            "    return;\n"
            "}\n"
        )
        analysis = (
            "```objc\n"
            "void bmk_signal_exception_handler(int signal) {\n"
            "    return;\n"
            "}\n"
            "```\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Handler.m"
            path.write_text(source, encoding="utf-8")
            code_context = {
                "graph": {
                    "nodes": [],
                    "evidence_summary": {
                        "auto_fix_allowed": False,
                        "auto_fix_block_reason": "图证据不足，已禁止自动改码",
                    },
                }
            }
            nodes = promote_read_file_function_candidates(
                code_context,
                analysis_text=analysis,
                context_session={
                    "request_ledger": [
                        {"type": "read_file", "status": "success", "file": str(path)},
                    ]
                },
                code_roots=[tmp],
            )
            self.assertEqual(len(nodes), 1)
            self.assertFalse(code_context["graph"]["evidence_summary"]["auto_fix_allowed"])
            allowed, reason = graph_auto_fix_allowed(code_context)
            self.assertFalse(allowed)
            self.assertIn("禁止", reason)

    def test_relative_read_file_path_promotes_existing_function(self):
        """账本里的仓库相对路径也要能落到 code root 下的源文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ios" / "BMKSignalHandler.m"
            path.parent.mkdir(parents=True)
            path.write_text(
                "void bmk_signal_exception_handler(int signal) {\n"
                "    old();\n"
                "}\n",
                encoding="utf-8",
            )
            analysis = (
                "```objc\n"
                "static void bmk_signal_exception_handler(int signal) {\n"
                "    record(signal);\n"
                "}\n"
                "```\n"
            )
            code_context = {}
            nodes = promote_read_file_function_candidates(
                code_context,
                analysis_text=analysis,
                context_session={
                    "request_ledger": [{
                        "type": "read_file",
                        "status": "success",
                        "file": "ios/BMKSignalHandler.m",
                    }],
                },
                code_roots=[str(root)],
            )
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]["file"], str(path.resolve()))

    def test_prose_analysis_is_completed_into_a_function_replacement(self):
        """只有步骤说明时，补一次完整函数后再落盘，并且不拼出 void static void。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BMKSignalHandler.m"
            path.write_text(
                "__attribute__((visibility(\"hidden\"))) "
                "void bmk_signal_exception_handler(int signal, siginfo_t *info, void *context) {\n"
                "    old();\n"
                "}\n",
                encoding="utf-8",
            )
            analysis = (
                "根本修复：信号 handler 不要挂起线程，也不要发 ObjC 消息。\n"
                "目标文件 ios/BMKCrashLibrary/BMKSignalHandler.m。\n"
                '{"next_action": {"kind": "propose_fix"}}\n'
            )

            class _Response:
                def __init__(self, content):
                    self.content = content

            class _LLM:
                def __init__(self):
                    self.calls = 0

                def chat(self, messages, tools=None, **kwargs):
                    self.calls += 1
                    self.assert_prompt = messages[0]["content"]
                    return _Response(
                        "```objc\n"
                        "static void bmk_signal_exception_handler"
                        "(int sig, siginfo_t *info, void *ctx) {\n"
                        "    record(sig);\n"
                        "}\n"
                        "```\n"
                    )

            llm = _LLM()
            session = {
                "request_ledger": [{
                    "type": "read_file",
                    "status": "success",
                    "file": str(path),
                }],
            }
            completed = _complete_missing_repair_code(
                llm,
                analysis,
                context_session=session,
                code_roots=[str(root)],
            )
            self.assertEqual(llm.calls, 1)
            self.assertIn("static void bmk_signal_exception_handler", completed)
            def _execute(name, data):
                if name == "fix_code_extractor":
                    return FixCodeExtractorTool().execute(data)
                if name == "fix_code_applier":
                    return FixCodeApplierTool().execute(data)
                raise AssertionError(name)

            fixer = CodeFixer(llm_adapter=llm)
            result = fixer.generate_and_apply(
                {
                    "analysis": analysis,
                    "code_context": {},
                    "parse_result": {},
                    "context_session": session,
                },
                [str(root)],
                backup_original_sources=False,
                tool_executor=_execute,
            )
            self.assertTrue(result.success, result.error)
            written = path.read_text(encoding="utf-8")
            self.assertNotIn("void static void", written)
            self.assertNotIn("__attribute__", written)
            self.assertIn("static void bmk_signal_exception_handler", written)
            self.assertIn("record(sig)", written)

    def test_attribute_prefix_is_replaced_with_new_signature(self):
        """完整新签名要盖掉原来的属性宏和返回类型，不能拼成 void static void。"""
        source = (
            "/// crash回调\n"
            "__attribute__((visibility(\"hidden\"))) "
            "void bmk_signal_exception_handler(int signal, siginfo_t *info, void *context) {\n"
            "    old();\n"
            "}\n"
        )
        replacement = (
            "static void bmk_signal_exception_handler(int sig, siginfo_t *info, void *ctx) {\n"
            "    record(sig);\n"
            "}\n"
        )
        updated, _old = CodeFixer.replace_function_block(
            source,
            "bmk_signal_exception_handler()",
            replacement,
        )
        self.assertNotIn("void static void", updated)
        self.assertNotIn("__attribute__", updated)
        self.assertIn("static void bmk_signal_exception_handler", updated)
        self.assertEqual(updated.count("bmk_signal_exception_handler"), 1)
        self.assertIn("/// crash回调", updated)

    def test_new_symbols_are_inserted_before_first_replaced_function(self):
        """围栏里的新静态变量和新函数独立插到最先被替换函数的文档注释之前。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BMKSignalHandler.m"
            path.write_text(
                "/// crash回调\n"
                "__attribute__((visibility(\"hidden\"))) "
                "void bmk_signal_exception_handler(int signal, siginfo_t *info, void *context) {\n"
                "    old();\n"
                "}\n"
                "\n"
                "__attribute__((visibility(\"hidden\"))) void bmk_signal_register(int signal) {\n"
                "    old_reg();\n"
                "}\n",
                encoding="utf-8",
            )
            analysis = (
                "```objc\n"
                "static int bmk_crash_fd = -1;\n"
                "static void bmk_chain_previous(int sig) {\n"
                "    return;\n"
                "}\n"
                "static void bmk_signal_exception_handler(int sig, siginfo_t *info, void *ctx) {\n"
                "    bmk_chain_previous(sig);\n"
                "}\n"
                "static void bmk_signal_register(int sig) {\n"
                "    return;\n"
                "}\n"
                "```\n"
            )
            handler_snippet = [
                "void bmk_signal_exception_handler(int signal, siginfo_t *info, void *context) {",
                "    old();",
                "}",
            ]
            register_snippet = [
                "void bmk_signal_register(int signal) {",
                "    old_reg();",
                "}",
            ]
            fix_plan = {
                "summary": "test",
                "edits": [
                    {
                        "file": str(path),
                        "function_signature": "bmk_signal_register()",
                        "replacement_code": (
                            "static void bmk_signal_register(int sig) {\n"
                            "    return;\n"
                            "}\n"
                        ),
                    },
                    {
                        "file": str(path),
                        "function_signature": "bmk_signal_exception_handler()",
                        "replacement_code": (
                            "static void bmk_signal_exception_handler"
                            "(int sig, siginfo_t *info, void *ctx) {\n"
                            "    bmk_chain_previous(sig);\n"
                            "}\n"
                        ),
                    },
                ],
            }
            _attach_missing_symbols(fix_plan, analysis)
            handler = fix_plan["edits"][1]["replacement_code"]
            self.assertNotIn("bmk_crash_fd", handler)
            self.assertNotIn("static void bmk_chain_previous", handler)
            self.assertNotIn("bmk_crash_fd", fix_plan["edits"][0]["replacement_code"])
            insertions = [item for item in fix_plan["edits"] if item.get("edit_type") == "symbol_insertion"]
            self.assertEqual(len(insertions), 1)
            self.assertEqual(insertions[0]["placement"], "before_function")
            self.assertEqual(insertions[0]["anchor_function"], "bmk_signal_exception_handler")
            self.assertIn("static int bmk_crash_fd = -1;", insertions[0]["code"])
            self.assertIn("bmk_chain_previous", insertions[0]["code"])
            nodes = [
                {
                    "type": "function",
                    "file": str(path),
                    "signature": "bmk_signal_exception_handler()",
                    "snippet": handler_snippet,
                },
                {
                    "type": "function",
                    "file": str(path),
                    "signature": "bmk_signal_register()",
                    "snippet": register_snippet,
                },
            ]
            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                nodes,
                [str(root)],
                backup_original_sources=False,
                code_context={"graph": {"nodes": nodes}},
            )
            self.assertTrue(result.success, result.to_dict())
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("void static void", text)
            self.assertNotIn("__attribute__", text)
            self.assertEqual(text.count("bmk_crash_fd"), 1)
            self.assertEqual(text.count("static void bmk_chain_previous"), 1)
            self.assertLess(text.find("bmk_crash_fd"), text.find("/// crash回调"))
            self.assertLess(text.find("bmk_chain_previous"), text.find("/// crash回调"))
            self.assertLess(text.find("/// crash回调"), text.find("static void bmk_signal_exception_handler"))

    def test_attribute_prefixed_function_replacement_is_extracted(self):
        """__attribute__ 前缀不能把函数名识别成属性宏。"""
        analysis = (
            "```objc\n"
            "__attribute__((visibility(\"hidden\"))) void bmk_signal_register(int sig) {\n"
            "    install(sig);\n"
            "}\n"
            "```\n"
        )
        replacement = _extract_replacement_from_analysis(analysis, "bmk_signal_register()")
        self.assertIn("install(sig)", replacement or "")

    def test_read_file_origin_allows_fix_without_call_edge(self):
        """没有崩溃行调用边时，已读函数节点本身可以放行。"""
        code_context = {
            "graph": {
                "nodes": [
                    {
                        "origin": "read_file",
                        "file": "/tmp/BMKSignalHandler.m",
                        "signature": "bmk_signal_exception_handler()",
                        "snippet": [
                            "void bmk_signal_exception_handler(int signal) {",
                            "    return;",
                            "}",
                        ],
                    }
                ],
                "edges": [],
            }
        }
        allowed, reason = graph_auto_fix_allowed(code_context)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_empty_paren_signature_replaces_definition_not_call_site(self):
        """签名写 foo()、定义写 foo(void) 时，替换落在定义上而不是函数体里的调用语句。"""
        source = (
            "void bmk_remove(void) {\n"
            "    old_remove();\n"
            "}\n"
            "\n"
            "void bmk_handler(int sig) {\n"
            "    bmk_remove();\n"
            "    tail();\n"
            "}\n"
        )
        patched, old_block = CodeFixer.replace_function_block(
            source,
            "bmk_remove()",
            "void bmk_remove(void) {\n    new_remove();\n}",
        )
        self.assertIn("old_remove();", old_block or "")
        self.assertIn("new_remove();", patched)
        self.assertIn("    bmk_remove();\n    tail();", patched)

    def test_call_statement_before_if_block_is_not_extracted_as_definition(self):
        """后一个围栏里 foo(x); 紧跟 if {...} 时，仍取前一个围栏里 foo 的真实定义。"""
        analysis = (
            "```objc\n"
            "void bmk_unregister(int signal) {\n"
            "    restore(signal);\n"
            "}\n"
            "```\n"
            "```objc\n"
            "void bmk_handler(int signal) {\n"
            "    bmk_unregister(signal);\n"
            "    if (previous) {\n"
            "        forward(signal);\n"
            "    }\n"
            "}\n"
            "```\n"
        )
        replacement = _extract_replacement_from_analysis(analysis, "bmk_unregister()")
        self.assertIn("restore(signal);", replacement or "")
        self.assertNotIn("forward(signal);", replacement or "")

    def test_missing_symbols_follow_their_file_heading(self):
        """前置宏、typedef 和静态变量只进标题所属的文件，另一文件的函数不当作新符号。"""
        analysis = (
            "## 修复代码\n"
            "### `BMKSignalHandler.m`\n"
            "```objc\n"
            "#include <fcntl.h>\n"
            "#define BMK_MAX 64\n"
            "typedef struct {\n"
            "    int frames[BMK_MAX];\n"
            "} bmk_record_t;\n"
            "static bmk_record_t bmk_g_record; // 静态缓冲\n"
            "static uintptr_t bmk_g_ranges[3][2];\n"
            "void bmk_handler(int sig) {\n"
            "    bmk_g_record.frames[0] = sig;\n"
            "}\n"
            "```\n"
            "### `BMKUncaughtExceptionHandler.m`\n"
            "```objc\n"
            "void bmk_remove(void) {\n"
            "    NSSetUncaughtExceptionHandler(NULL);\n"
            "}\n"
            "```\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            signal_file = root / "BMKSignalHandler.m"
            signal_file.write_text("void bmk_handler(int sig) {\n    old();\n}\n", encoding="utf-8")
            oc_file = root / "BMKUncaughtExceptionHandler.m"
            oc_file.write_text("void bmk_remove(void) {\n    old();\n}\n", encoding="utf-8")
            fix_plan = {
                "edits": [
                    {
                        "file": str(signal_file),
                        "function_signature": "bmk_handler()",
                        "replacement_code": "void bmk_handler(int sig) {\n    bmk_g_record.frames[0] = sig;\n}\n",
                    },
                    {
                        "file": str(oc_file),
                        "function_signature": "bmk_remove()",
                        "replacement_code": "void bmk_remove(void) {\n    NSSetUncaughtExceptionHandler(NULL);\n}\n",
                    },
                ],
            }
            _attach_missing_symbols(fix_plan, analysis)
            handler = fix_plan["edits"][0]["replacement_code"]
            self.assertNotIn("#include <fcntl.h>", handler)
            self.assertNotIn("bmk_record_t", handler)
            self.assertNotIn("bmk_remove", handler)
            self.assertNotIn("bmk_record_t", fix_plan["edits"][1]["replacement_code"])
            insertions = [item for item in fix_plan["edits"] if item.get("edit_type") == "symbol_insertion"]
            self.assertEqual({item["placement"] for item in insertions}, {"head", "before_function"})
            head = next(item["code"] for item in insertions if item["placement"] == "head")
            body = next(item["code"] for item in insertions if item["placement"] == "before_function")
            self.assertIn("#include <fcntl.h>", head)
            self.assertIn("#define BMK_MAX 64", head)
            self.assertIn("} bmk_record_t;", body)
            self.assertIn("bmk_g_record; //", body)
            self.assertIn("[3][2];", body)
            self.assertNotIn("bmk_remove", body)

            plain_plan = {
                "edits": [
                    dict(fix_plan["edits"][0]),
                    dict(fix_plan["edits"][1]),
                ],
            }
            plain_lines = analysis.replace("### `BMKSignalHandler.m`", "BMKSignalHandler.m").replace(
                "### `BMKUncaughtExceptionHandler.m`", "**BMKUncaughtExceptionHandler.m**："
            )
            _attach_missing_symbols(plain_plan, plain_lines)
            plain_inserts = [item for item in plain_plan["edits"] if item.get("edit_type") == "symbol_insertion"]
            self.assertEqual({item["placement"] for item in plain_inserts}, {"head", "before_function"})
            body_plain = next(
                item["code"] for item in plain_inserts if item["placement"] == "before_function"
            )
            self.assertIn("} bmk_record_t;", body_plain)

            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                [
                    {"type": "function", "file": str(signal_file), "signature": "bmk_handler()",
                     "snippet": ["void bmk_handler(int sig) {", "    old();", "}"]},
                    {"type": "function", "file": str(oc_file), "signature": "bmk_remove()",
                     "snippet": ["void bmk_remove(void) {", "    old();", "}"]},
                ],
                [str(root)],
                backup_original_sources=False,
            )
            self.assertTrue(result.success, result.to_dict())
            signal_text = signal_file.read_text(encoding="utf-8")
            self.assertLess(signal_text.find("#include <fcntl.h>"), signal_text.find("void bmk_handler"))
            self.assertLess(signal_text.find("bmk_g_ranges[3][2]"), signal_text.find("void bmk_handler"))
            self.assertNotIn("bmk_record_t", oc_file.read_text(encoding="utf-8"))

            plain_lines = analysis.replace("### `BMKSignalHandler.m`", "BMKSignalHandler.m").replace(
                "### `BMKUncaughtExceptionHandler.m`", "**BMKUncaughtExceptionHandler.m**："
            )
            again = {
                "edits": [
                    {
                        "file": str(signal_file),
                        "function_signature": "bmk_handler()",
                        "replacement_code": "void bmk_handler(int sig) {\n    bmk_g_record.frames[0] = sig;\n}\n",
                    },
                    {
                        "file": str(oc_file),
                        "function_signature": "bmk_remove()",
                        "replacement_code": (
                            "void bmk_remove(void) {\n    NSSetUncaughtExceptionHandler(NULL);\n}\n"
                        ),
                    },
                ],
            }
            _attach_missing_symbols(again, plain_lines)
            self.assertEqual(
                [item.get("edit_type") for item in again["edits"] if item.get("edit_type") == "symbol_insertion"],
                [],
            )

    def test_header_symbols_go_to_import_area(self):
        """#include/#define 插到已有 import 之后，静态变量仍停在函数文档注释前。"""
        analysis = (
            "## 修复代码\n"
            "BMKSignalHandler.m\n"
            "```objc\n"
            "#include <fcntl.h>\n"
            "#define BMK_ALT 8\n"
            "#define BMK_FD_COUNT ((int)(sizeof(bmk_g_fd) / sizeof(int)))\n"
            "static int bmk_g_fd = -1;\n"
            "void bmk_handler(int sig) {\n"
            "    (void)sig;\n"
            "}\n"
            "```\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BMKSignalHandler.m"
            path.write_text(
                "#import \"BMKSignalHandler.h\"\n"
                "#include <signal.h>\n"
                "\n"
                "/// 处理信号\n"
                "void bmk_handler(int sig) {\n"
                "    old();\n"
                "}\n",
                encoding="utf-8",
            )
            fix_plan = {
                "edits": [{
                    "file": str(path),
                    "function_signature": "bmk_handler()",
                    "replacement_code": "void bmk_handler(int sig) {\n    (void)sig;\n}\n",
                }],
            }
            _attach_missing_symbols(fix_plan, analysis)
            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                [{"type": "function", "file": str(path), "signature": "bmk_handler()",
                  "snippet": ["void bmk_handler(int sig) {", "    old();", "}"]}],
                [str(root)],
                backup_original_sources=False,
            )
            self.assertTrue(result.success, result.to_dict())
            text = path.read_text(encoding="utf-8")
            self.assertLess(text.find("#include <signal.h>"), text.find("#include <fcntl.h>"))
            self.assertLess(text.find("#include <fcntl.h>"), text.find("#define BMK_ALT 8"))
            self.assertLess(text.find("#define BMK_ALT 8"), text.find("#define BMK_FD_COUNT"))
            self.assertLess(text.find("#define BMK_FD_COUNT"), text.find("static int bmk_g_fd"))
            self.assertLess(text.find("static int bmk_g_fd"), text.find("/// 处理信号"))
            self.assertLess(text.find("/// 处理信号"), text.find("void bmk_handler"))


    def test_extract_drops_call_fragment_without_target_definition(self) -> None:
        """调用片段不能变成目标函数的替换；完整函数定义仍然保留。"""
        compile_sig = (
            "bool GLRenderShader::CompileShader"
            "(GLuint * shader, GLenum type, const GLchar* source)"
        )
        init_sig = (
            "bool GLRenderShader::initWithShaderSources"
            "(const GLchar* vShaderSrc, const GLchar* fShaderSrc, ShaderType shaderType)"
        )
        analysis = (
            "```cpp\n"
            "if (!CompileShader(&vertShader, GL_VERTEX_SHADER, vShaderSrc))\n"
            "{\n"
            "    glDeleteProgram(m_ProgramID);\n"
            "    return false;\n"
            "}\n"
            "```\n"
            "```cpp\n"
            f"{init_sig}\n"
            "{\n"
            "    return true;\n"
            "}\n"
            "```\n"
        )
        plan = CodeFixer()._try_extract_fix_plan_from_analysis(
            analysis,
            [
                {"file": "gl_render_shader.cpp", "signature": compile_sig},
                {"file": "gl_render_shader.cpp", "signature": init_sig},
            ],
            [
                {"file": "gl_render_shader.cpp", "function_signature": compile_sig},
                {"file": "gl_render_shader.cpp", "function_signature": init_sig},
            ],
        )
        self.assertIsNotNone(plan)
        signatures = [item["function_signature"] for item in plan["edits"]]
        self.assertNotIn(compile_sig, signatures)
        self.assertIn(init_sig, signatures)


if __name__ == "__main__":
    unittest.main()
