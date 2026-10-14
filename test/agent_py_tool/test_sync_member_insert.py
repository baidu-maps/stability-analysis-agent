#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新增同步成员声明的提取、头文件插入和构造初始化。"""

import tempfile
import unittest
from pathlib import Path

from services.code_fixer import (
    CodeFixer,
    _extract_member_declaration_edits,
    _members_introduced_by_declaration_edits,
)


class SyncMemberInsertTests(unittest.TestCase):
    """两行声明块应变成头文件成员，并在同类 Create 旁补初始化。"""

    def test_extract_and_apply_adds_mutex_and_create(self):
        """声明块插在已有成员后，函数体可以引用这把新锁。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            header = root / "GridLayer.h"
            source = root / "GridLayer.cpp"
            header.write_text(
                "class CGridLayer {\n"
                "    std::list<int> m_loadingVMPBlockTask;\n"
                "    CVMutex                                 m_poolMutex;\n"
                "};\n",
                encoding="utf-8",
            )
            source.write_text(
                "CGridLayer::CGridLayer()\n"
                "{\n"
                "    m_poolMutex.Create(V_NULL);\n"
                "}\n"
                "\n"
                "void CGridLayer::CancelTask(bool wait)\n"
                "{\n"
                "    m_loadingVMPBlockTask.clear();\n"
                "}\n",
                encoding="utf-8",
            )
            code_context = {
                "crash_summary": {
                    "owner_class_context": {
                        "class_name": "CGridLayer",
                        "definition_file": str(header),
                    }
                }
            }
            analysis = """
## 修复代码
```cpp
    std::list<int> m_loadingVMPBlockTask;
    CVMutex m_loadingTaskMutex;
```
```cpp
void CGridLayer::CancelTask(bool wait)
{
    CVMutex::ScopedLock lock(m_loadingTaskMutex);
    m_loadingVMPBlockTask.clear();
}
```
"""
            decl_edits = _extract_member_declaration_edits(analysis, code_context)
            self.assertEqual(len(decl_edits), 1)
            self.assertEqual(decl_edits[0]["sync_init_name"], "m_loadingTaskMutex")
            self.assertIn(
                "m_loadingTaskMutex",
                _members_introduced_by_declaration_edits(decl_edits),
            )
            fix_plan = {
                "summary": "lock loading list",
                "edits": [
                    {
                        "file": str(source),
                        "function_signature": "void CGridLayer::CancelTask(bool wait)",
                        "replacement_code": (
                            "void CGridLayer::CancelTask(bool wait)\n"
                            "{\n"
                            "    CVMutex::ScopedLock lock(m_loadingTaskMutex);\n"
                            "    m_loadingVMPBlockTask.clear();\n"
                            "}\n"
                        ),
                    },
                    *decl_edits,
                ],
            }
            result = CodeFixer(llm_adapter=None).apply_fix_plan(
                fix_plan,
                [],
                [str(root)],
                backup_original_sources=False,
                code_context=code_context,
            )
            self.assertTrue(result.success, result.to_dict())
            header_text = header.read_text(encoding="utf-8")
            source_text = source.read_text(encoding="utf-8")
            self.assertIn("CVMutex                                 m_loadingTaskMutex;", header_text)
            self.assertLess(
                header_text.index("m_loadingVMPBlockTask"),
                header_text.index("m_loadingTaskMutex"),
            )
            self.assertIn("m_loadingTaskMutex.Create(V_NULL);", source_text)
            self.assertIn("ScopedLock lock(m_loadingTaskMutex)", source_text)

    def test_class_skeleton_header_is_enough_without_owner_summary(self):
        """崩溃摘要没有 owner 时，仍从头文件类骨架提取新增锁。"""
        with tempfile.TemporaryDirectory() as temp:
            header = Path(temp) / "Layer.h"
            header.write_text(
                "class Layer {\n"
                "    std::list<int> m_items;\n"
                "    CVMutex                                 m_poolMutex;\n"
                "};\n",
                encoding="utf-8",
            )
            analysis = """
```cpp
    std::list<int> m_items;
    CVMutex m_itemsMutex;
```
"""
            code_context = {
                "crash_summary": {},
                "graph": {
                    "nodes": [
                        {
                            "type": "class_skeleton",
                            "class_name": "Layer",
                            "file": str(header),
                        }
                    ]
                },
            }
            edits = _extract_member_declaration_edits(analysis, code_context)
            self.assertEqual(len(edits), 1)
            self.assertEqual(edits[0]["sync_init_name"], "m_itemsMutex")
            self.assertEqual(edits[0]["file"], str(header))

    def test_scoped_lock_unlock_call_is_rejected(self):
        """区间锁对象上的 Unlock() 不能当作解锁。"""
        from services.code_fixer import _validate_sync_api_consistency

        replacement = (
            "void Layer::Cancel()\n"
            "{\n"
            "    CVMutex::ScopedLock lock(m_itemsMutex);\n"
            "    lock.Unlock();\n"
            "}\n"
        )
        error = _validate_sync_api_consistency("void Layer::Cancel() {}", replacement)
        self.assertIsNotNone(error)
        self.assertIn("Unlock()", error or "")

    def test_create_init_skips_preprocessor_branch(self):
        """同类 Create 在 #if 里时，初始化插到无条件的那一处旁边。"""
        from services.code_fixer import _insert_mutex_create_beside_sibling

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            header = root / "Layer.h"
            source = root / "Layer.cpp"
            header.write_text(
                "class Layer {\n"
                "    CVMutex m_gridIdMutex;\n"
                "    CVMutex m_poolMutex;\n"
                "    CVMutex m_itemsMutex;\n"
                "};\n",
                encoding="utf-8",
            )
            source.write_text(
                "Layer::Layer()\n"
                "{\n"
                "#if TEST_ONLY\n"
                "    m_gridIdMutex.Create(V_NULL);\n"
                "#endif\n"
                "    m_poolMutex.Create(V_NULL);\n"
                "}\n",
                encoding="utf-8",
            )
            note = _insert_mutex_create_beside_sibling(
                header, "m_itemsMutex", "CVMutex", [str(root.resolve())]
            )
            text = source.read_text(encoding="utf-8")
            self.assertIsNone(note)
            self.assertIn("    m_poolMutex.Create(V_NULL);\n    m_itemsMutex.Create(V_NULL);", text)
            self.assertNotIn("m_gridIdMutex.Create(V_NULL);\n    m_itemsMutex", text)

    def test_create_init_refuses_when_every_sibling_is_conditional(self):
        """同类 Create 全在预处理条件里时，不挑其中一支插入。"""
        from services.code_fixer import _insert_mutex_create_beside_sibling

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            header = root / "Layer.h"
            source = root / "Layer.cpp"
            header.write_text(
                "class Layer {\n"
                "    CVMutex m_poolMutex;\n"
                "    CVMutex m_itemsMutex;\n"
                "};\n",
                encoding="utf-8",
            )
            source.write_text(
                "Layer::Layer()\n"
                "{\n"
                "#ifdef TEST_ONLY\n"
                "    m_poolMutex.Create(V_NULL);\n"
                "#endif\n"
                "}\n",
                encoding="utf-8",
            )
            note = _insert_mutex_create_beside_sibling(
                header, "m_itemsMutex", "CVMutex", [str(root.resolve())]
            )
            self.assertIn("预处理条件", note or "")
            self.assertNotIn("m_itemsMutex.Create", source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
