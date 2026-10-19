"""类声明请求，以及函数未命中时按检索提示回填。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.agent_output_parser import parse_agent_decision
from services.context_request_contract import format_context_resolution
from services.context_source_resolver import CodeContextRequestResolver


class ClassContextRequestTests(unittest.TestCase):
    """class 请求和函数检索回退。"""

    def setUp(self) -> None:
        """准备带导出宏的类声明和一份没有 Release 的实现。"""
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        header = root / "inc" / "BmShell.h"
        header.parent.mkdir(parents=True)
        header.write_text(
            "template< class TYPE >\n"
            "class EXPORT_MACRO BmShell : public BmBaseShell\n"
            "{\n"
            "public:\n"
            "    std::shared_ptr<TYPE> m_spBmInstance;\n"
            "};\n",
            encoding="utf-8",
        )
        source = root / "napi" / "NapiBmGround.cpp"
        source.parent.mkdir(parents=True)
        source.write_text(
            "napi_value NapiBmGround::SetDrawableResource(napi_env env, napi_callback_info info)\n"
            "{\n"
            "    // Java GC 只会 delete BmShell，不会删 Engine 对象。\n"
            "    return nullptr;\n"
            "}\n",
            encoding="utf-8",
        )
        self.root = str(root)

    def tearDown(self) -> None:
        """删除临时源码树。"""
        self.tmp.cleanup()

    def test_class_request_is_accepted_and_returns_declaration(self) -> None:
        """type=class 不再被丢弃，并能跨过导出宏定位声明。"""
        parsed = parse_agent_decision(
            '{"agent_can_fetch_more": true, "context_requests": ['
            '{"type": "class", "symbol": "BmShell", "reason": "确认所有权", "priority": "high"}]}'
        )
        self.assertEqual(parsed["invalid_context_requests"], [])
        self.assertEqual(parsed["context_requests"][0]["type"], "class")
        struct_req = parse_agent_decision(
            '{"agent_can_fetch_more": true, "context_requests": ['
            '{"type": "struct", "symbol": "BmShell", "reason": "确认所有权", "priority": "high"}]}'
        )
        self.assertEqual(struct_req["context_requests"][0]["type"], "class")

        resolved = CodeContextRequestResolver.resolve_requests(
            [{"type": "class", "symbol": "BmShell", "reason": "确认所有权", "priority": "high"}],
            [self.root],
        )
        self.assertTrue(resolved[0]["success"])
        text = "\n".join(resolved[0]["matches"][0]["context"])
        self.assertIn("EXPORT_MACRO BmShell", text)
        self.assertIn("m_spBmInstance", text)
        rendered = format_context_resolution(resolved[0])
        self.assertIn("类或结构体声明", rendered)
        self.assertIn("m_spBmInstance", rendered)

    def test_missing_function_falls_back_to_reason_search(self) -> None:
        """符号不存在时，按 reason 里的检索提示回填相关行。"""
        resolved = CodeContextRequestResolver.resolve_requests(
            [{
                "type": "function",
                "symbol": "NapiBmGround::Release",
                "reason": "若符号名不存在，请检索 delete BmShell<BmGround> 的调用点",
                "priority": "high",
            }],
            [self.root],
        )
        item = resolved[0]
        self.assertTrue(item["success"])
        self.assertEqual(item["context_type"], "grep")
        blob = "\n".join(str(match.get("line_text") or "") for match in item["matches"])
        self.assertIn("delete BmShell", blob)
        rendered = format_context_resolution(item)
        self.assertIn("已改为检索", rendered)
        self.assertIn("delete BmShell", rendered)
