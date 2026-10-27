#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领域知识点名的栈函数缺失时，首轮必须补齐函数体。"""

from __future__ import annotations

import unittest

from services.knowledge_required_context import (
    build_prefetch_requests,
    missing_knowledge_stack_functions,
    prefetch_missing_knowledge_functions,
)


class KnowledgeRequiredContextTests(unittest.TestCase):
    """检测 playbook 点名但函数源码缺失的栈函数。"""

    def test_missing_destructor_is_requested(self) -> None:
        """Unload 已有函数体时，仍要补 ~CVStyle。"""
        prompt = """
## 按线程符号化结果（02）
- CVStyleScene::Unload() +648
- CVStyle::~CVStyle() +344

## 函数源码
#### 函数源码: VVoid CVStyleScene::Unload()
VVoid CVStyleScene::Unload()
{
    V_free(it->second);
}

## 代码库领域知识
只改这两个函数。
1. `CVStyleScene::Unload`
2. `CVStyle::~CVStyle`：RLock 改 WLock
"""
        missing = missing_knowledge_stack_functions(prompt)
        self.assertEqual(missing, ["CVStyle::~CVStyle"])
        requests = build_prefetch_requests(missing)
        self.assertEqual(requests[0]["type"], "function")
        self.assertEqual(requests[0]["symbol"], "CVStyle::~CVStyle")

    def test_skips_when_both_bodies_present(self) -> None:
        """两处函数体都在「函数源码」里时不再预取。"""
        prompt = """
## 函数源码
#### 函数源码: VVoid CVStyleScene::Unload()
VVoid CVStyleScene::Unload() {}
#### 函数源码: CVStyle::~CVStyle()
CVStyle::~CVStyle() { m_themeLock->RLock(); }

## 代码库领域知识
1. `CVStyleScene::Unload`
2. `CVStyle::~CVStyle`
"""
        self.assertEqual(missing_knowledge_stack_functions(prompt), [])

    def test_prefetch_inserts_destructor_into_function_source(self) -> None:
        """预取必须写入「函数源码」，不能再走会被 24k 预算裁掉的 evidence_delta。"""
        prompt = """
## 按线程符号化结果（02）
- CVStyleScene::Unload() +648
- CVStyle::~CVStyle() +344

## 函数源码
#### 函数源码: VVoid CVStyleScene::Unload()
VVoid CVStyleScene::Unload()
{
    V_free(it->second);
}

## 代码库领域知识
只改这两个函数。
1. `CVStyleScene::Unload`
2. `CVStyle::~CVStyle`：RLock 改 WLock
"""

        class _Engine:
            """只提供 resolve；若走预算路径则测试失败。"""

            def resolve_requests(self, requests, round_index=0):
                """返回仅含 snippet 的析构函数体。"""
                del round_index
                return [
                    {
                        "success": True,
                        "request": requests[0],
                        "file": "/tmp/Style.cpp",
                        "function_signature": "CVStyle::~CVStyle()",
                        "snippet": [
                            "CVStyle::~CVStyle()",
                            "{",
                            "    m_themeLock->RLock();",
                            "}",
                        ],
                    }
                ]

            def evidence_delta(self, *args, **kwargs):
                """预取不得依赖 24k evidence 打包。"""
                raise AssertionError("prefetch must not use evidence_delta")

            def build_prompt(self, *args, **kwargs):
                """预取不得重新走 build_prompt 预算。"""
                raise AssertionError("prefetch must not rebuild against char budget")

        out = prefetch_missing_knowledge_functions(
            _Engine(),
            stable_prompt=prompt,
            assembled_prompt=prompt,
        )
        self.assertIn("m_themeLock->RLock()", out)
        self.assertIn("#### 函数源码: CVStyle::~CVStyle()", out)
        self.assertEqual(missing_knowledge_stack_functions(out), [])

    def test_skips_std_and_allocator_symbols(self) -> None:
        """标准库与 allocator 符号不预取。"""
        prompt = """
## 函数源码
empty

## 代码库领域知识
栈含 `_xzm_free` 与 `std::__1::shared_ptr<CVStyleScene>::operator=`。
"""
        self.assertEqual(missing_knowledge_stack_functions(prompt), [])


if __name__ == "__main__":
    unittest.main()
