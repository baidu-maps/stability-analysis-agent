#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""粘连格式 iOS 栈行解析与 ObjC 源码定位回归。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.add2line_resolver_tool import add2line_resolver
from tools.code_content_provider_tool import CodeContentProvider
from tools.crash_log_parser_tool import crash_log_parser
from tools.crash_parser.stack_lines import (
    _try_parse_ios_glued_stack_line,
    _try_parse_ios_macos_stack_line,
    _try_parse_ios_symbol_only_stack_line,
)
from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

GLUED_SAMPLE = """\
Exception Type: EXC_BAD_ACCESS (SIGBUS)
Exception Subtype: EXC_ARM_DA_ALIGN at 0x0000000000000001

pc: 0x0000000000000001
lr: 0x00000001049dbd94
sp: 0x000000012bd67b20

Thread 0 Crashed:
0 ???0x0000000000000001 0 + 1
1 libsystem_platform.dylib 0x1ec151290_platform_handle_signal
5 libdyld.dylib0x1a05ce5f4
7 ZiroomerProject0x104b30a90-[BMKAnnotationContainer initializeData] + 400
8 ZiroomerProject0x104b30864-[BMKAnnotationContainer initWithFrame:] + 72
9 ZiroomerProject0x104b3a62c-[BMKMapView initSubView] + 1180
"""


class TestIosGluedStackLines(unittest.TestCase):
    """粘连 Apple 栈行应拆出正确 module / address / ObjC selector。"""

    def test_glued_objc_selector_line(self) -> None:
        """模块与地址粘连、紧跟 ObjC selector。"""
        parsed = _try_parse_ios_glued_stack_line(
            "7 ZiroomerProject0x104b30a90-[BMKAnnotationContainer initializeData] + 400"
        )
        self.assertIsNotNone(parsed)
        module, addr, func, offset = parsed  # type: ignore[misc]
        self.assertEqual(module, "ZiroomerProject")
        self.assertEqual(addr, "0x104b30a90")
        self.assertEqual(func, "-[BMKAnnotationContainer initializeData]")
        self.assertEqual(offset, "400")

    def test_glued_address_to_symbol(self) -> None:
        """地址与符号以下划线粘连。"""
        parsed = _try_parse_ios_glued_stack_line(
            "1 libsystem_platform.dylib 0x1ec151290_platform_handle_signal"
        )
        self.assertIsNotNone(parsed)
        module, addr, func, offset = parsed  # type: ignore[misc]
        self.assertEqual(module, "libsystem_platform.dylib")
        self.assertEqual(addr, "0x1ec151290")
        self.assertEqual(func, "_platform_handle_signal")
        self.assertEqual(offset, "0")

    def test_glued_module_to_address_only(self) -> None:
        """仅有模块粘连地址、无符号。"""
        parsed = _try_parse_ios_glued_stack_line("5 libdyld.dylib0x1a05ce5f4")
        self.assertIsNotNone(parsed)
        module, addr, func, offset = parsed  # type: ignore[misc]
        self.assertEqual(module, "libdyld.dylib")
        self.assertEqual(addr, "0x1a05ce5f4")
        self.assertEqual(func, "")
        self.assertEqual(offset, "0")

    def test_standard_line_not_claimed_by_glued(self) -> None:
        """标准空格分隔栈行仍由 macos 解析器处理，glued 返回 None。"""
        line = (
            "7   ZiroomerProject                  0x0000000104b30a90 "
            "-[BMKAnnotationContainer initializeData] + 400"
        )
        self.assertIsNone(_try_parse_ios_glued_stack_line(line))
        std = _try_parse_ios_macos_stack_line(line)
        self.assertIsNotNone(std)
        self.assertEqual(std[0], "ZiroomerProject")  # type: ignore[index]
        self.assertEqual(std[2], "-[BMKAnnotationContainer initializeData]")  # type: ignore[index]

    def test_symbol_only_no_longer_wins_on_glued_objc(self) -> None:
        """回归：粘连 ObjC 行不应再被 symbol-only 拆成脏 module。"""
        line = "7 ZiroomerProject0x104b30a90-[BMKAnnotationContainer initializeData] + 400"
        glued = _try_parse_ios_glued_stack_line(line)
        dirty = _try_parse_ios_symbol_only_stack_line(line)
        self.assertIsNotNone(glued)
        self.assertEqual(glued[0], "ZiroomerProject")  # type: ignore[index]
        # symbol-only 仍可能误匹配，但提取链路应优先 glued。
        self.assertIsNotNone(dirty)
        self.assertIn("0x", dirty[0])  # type: ignore[index]

    def test_full_parse_glued_sample(self) -> None:
        """整段粘连日志应解析出正确 ObjC 帧。"""
        result = json.loads(crash_log_parser(GLUED_SAMPLE))
        self.assertEqual(result["meta_info"]["os_type"], "ios")
        frames = result["threads"][0]["frames"]
        hit = next(
            f
            for f in frames
            if (f.get("function") or "") == "-[BMKAnnotationContainer initializeData]"
        )
        self.assertEqual(hit.get("module"), "ZiroomerProject")
        self.assertEqual(hit.get("address"), "0x104b30a90")
        self.assertEqual(hit.get("offset"), "400")
        self.assertEqual(hit.get("language"), "objc")

    def test_ios_objc_symbol_locates_mm(self) -> None:
        """iOS + 粘连 ObjC 符号应能定位到 .mm 方法实现。"""
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = os.path.join(tmp, "MapComponent")
            os.makedirs(src_dir)
            src_file = os.path.join(src_dir, "BMKAnnotationContainer.mm")
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(
                    "@implementation BMKAnnotationContainer\n"
                    "- (instancetype)initWithFrame:(CGRect)frame {\n"
                    "  self = [super initWithFrame:frame];\n"
                    "  if (self) {\n"
                    "    [self initializeData];\n"
                    "  }\n"
                    "  return self;\n"
                    "}\n"
                    "- (void)initializeData {\n"
                    "  _annotations = [NSMutableArray array];\n"
                    "  _displayLink.preferredFrameRateRange = CAFrameRateRangeMake(60, 60, 60);\n"
                    "}\n"
                    "@end\n"
                )

            parsed = json.loads(crash_log_parser(GLUED_SAMPLE))
            resolved = json.loads(add2line_resolver(json.dumps(parsed), library_dir=None))
            self.assertEqual(resolved.get("os_type"), "ios")
            frames = flatten_resolved_frames_from_stack(resolved)
            hit = next(
                f
                for f in frames
                if (f.get("resolved_function") or f.get("function") or "")
                == "-[BMKAnnotationContainer initializeData]"
            )
            self.assertEqual(hit.get("module"), "ZiroomerProject")

            provider = CodeContentProvider(max_symbol_only_rescues=5)
            out = provider.code_content_provider(json.dumps(resolved), code_root=tmp)
            data = json.loads(out)
            self.assertNotEqual(data.get("error_code"), "NO_EXTRACTABLE_CONTEXT", out[:800])
            blob = json.dumps(data, ensure_ascii=False)
            self.assertIn("BMKAnnotationContainer.mm", blob)
            self.assertIn("initializeData", blob)
            self.assertIn("preferredFrameRateRange", blob)
            # 崩溃函数节点必须是 initializeData 本体，而不是误抽成 initWithFrame。
            crash_node = next(
                n
                for n in ((data.get("graph") or {}).get("nodes") or [])
                if isinstance(n, dict) and "initializeData" in str(n.get("signature") or "")
            )
            crash_blob = "\n".join(crash_node.get("snippet") or [])
            self.assertIn("preferredFrameRateRange", crash_blob)
            self.assertNotIn("[self initializeData]", crash_blob)
            entry = next(
                (
                    step
                    for step in ((data.get("location_trace") or {}).get("steps") or [])
                    if isinstance(step, dict) and step.get("step") == "analysis_entry"
                ),
                {},
            )
            self.assertIn(
                "BMKAnnotationContainer.mm",
                str(entry.get("selected_frame_file") or ""),
            )
            self.assertIn(
                "initializeData",
                str(entry.get("selected_frame_function") or ""),
            )

    def test_objc_method_block_ignores_call_sites(self) -> None:
        """抽取 initializeData 时不能误命中 initWithFrame 里的同名调用。"""
        from tools._stack_symbol_utils import (
            objc_definition_line_matches_selector,
            parse_objc_stack_symbol,
            sanitize_stack_symbol,
        )

        self.assertEqual(
            sanitize_stack_symbol("-[BMKAnnotationContainer initializeData] + 400"),
            "-[BMKAnnotationContainer initializeData]",
        )
        self.assertEqual(
            parse_objc_stack_symbol("-[BMKAnnotationContainer initializeData] + 400"),
            ("BMKAnnotationContainer", "initializeData"),
        )
        self.assertTrue(
            objc_definition_line_matches_selector("- (void)initializeData {", "initializeData")
        )
        self.assertFalse(
            objc_definition_line_matches_selector(
                "- (instancetype)initWithFrame:(CGRect)frame {",
                "initializeData",
            )
        )

        provider = CodeContentProvider()
        lines = [
            "- (instancetype)initWithFrame:(CGRect)frame {\n",
            "    [self initializeData];\n",
            "    return self;\n",
            "}\n",
            "- (void)initializeData {\n",
            "    _displayLink.preferredFrameRateRange = CAFrameRateRangeMake(60, 60, 60);\n",
            "}\n",
        ]
        block = provider._extract_objc_method_block(lines, "initializeData")
        self.assertIsNotNone(block)
        snippet, start, end, signature = block  # type: ignore[misc]
        self.assertEqual(start, 5)
        self.assertEqual(end, 7)
        self.assertIn("preferredFrameRateRange", "\n".join(snippet))
        self.assertIn("initializeData", signature)

    def test_extract_name_from_objc_source_signature(self) -> None:
        """源码签名 `- (void)initializeData` 不得把返回类型 void 当成函数名。"""
        provider = CodeContentProvider()
        self.assertEqual(
            provider._extract_function_name_from_resolved("- (void)initializeData {"),
            "initializeData",
        )
        self.assertEqual(
            provider._extract_function_name_from_resolved(
                "- (instancetype)initWithFrame:(CGRect)frame"
            ),
            "initWithFrame",
        )


if __name__ == "__main__":
    unittest.main()
