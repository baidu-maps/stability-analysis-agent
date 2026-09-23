#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动改码候选回退与超时降级图的单测。"""

import re
import unittest
import tempfile
from pathlib import Path

from services.code_fixer import (
    CodeFixer,
    extract_candidate_nodes,
    graph_auto_fix_allowed,
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
)
from tools.code_content_provider_tool import CodeContentProvider, CrashFunction
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


if __name__ == "__main__":
    unittest.main()
