#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_maps / registers.address_map 基础测试。"""

from __future__ import annotations

import unittest

from tools.crash_parser.memory_maps import parse_memory_maps, lookup_va
from tools.crash_parser.register_analyzer import (
    build_registers_section,
    enrich_registers_from_backtrace,
)
from tools.crash_parser.types import StackFrame, ThreadStack


_SAMPLE = """\
Reason:Signal:SIGSEGV(SEGV_MAPERR)@0x0055555555555555
Fault thread info:
Tid:1, Name:test
#00 pc 000000000038620c /data/storage/el1/bundle/libs/arm64/libapp_BaiduMapApplib.so
Registers:
x0:0000000000000000 x1:0000007f3da2a944 x20:5555555555555555
x29:0000007f3da2a9a0 lr:0000006590d8620c sp:0000007f3da2a970 pc:0000006590d8620c
Maps:
6590d64000-659183d000 r-xp 00363000 /data/storage/el1/bundle/libs/arm64/libapp_BaiduMapApplib.so
7f3da00000-7f3db00000 rw-p 00000000 [stack]
OpenFiles:
"""

# Android tombstone 风格：有寄存器绝对 VA + 相对 #00，无 Maps
_ANDROID_NO_MAPS = """\
pid: 1, tid: 2, name: map-loaddata  >>> com.example <<<
signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0
    x0  0000000000000000  x1  0000006e2f7f84b0  x2  000000725c0f0f04  x3  0000000000000010
    x4  0000000000000000  x5  000000725c0f1108  x6  00000000ffffffff  x7  0000000000000000
    x29 0000006e2f7f8480
    sp  0000006e2f7f8450  lr  0000006d3ab41cd8  pc  0000006d3ab41cdc  pst 0000000060001000

backtrace:
    #00 pc 000000000073bcdc  /data/app/app/lib/arm64/libBaiduMapSDK_map_v7_6_7.so
    #01 pc 000000000073a000  /data/app/app/lib/arm64/libBaiduMapSDK_map_v7_6_7.so
"""


class MemoryMapsAddressMapTest(unittest.TestCase):
    def test_parse_and_lookup_pc(self):
        entries = parse_memory_maps(_SAMPLE)
        self.assertGreaterEqual(len(entries), 2)
        hit = lookup_va(entries, 0x6590D8620C)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.basename, "libapp_BaiduMapApplib.so")
        self.assertIn("x", hit.perms)
        self.assertEqual(hit.file_offset_for_va(0x6590D8620C), 0x38520C)
        from tools.crash_parser.memory_maps import so_relative_offset
        self.assertEqual(so_relative_offset(entries, hit, 0x6590D8620C), 0x38520C)

    def test_address_map_kinds(self):
        regs, bases = build_registers_section(_SAMPLE, "0x0055555555555555")
        self.assertIsNotNone(regs)
        assert regs is not None
        amap = regs["address_map"]
        self.assertEqual(amap["pc"]["kind"], "code")
        self.assertEqual(amap["pc"]["module"], "libapp_BaiduMapApplib.so")
        self.assertEqual(amap["pc"]["offset"], "0x38520c")
        self.assertEqual(amap["x20"]["kind"], "poison_or_fill")
        self.assertEqual(amap["x0"]["kind"], "null")
        self.assertEqual(amap["sp"]["kind"], "stack")
        self.assertIn("libapp_BaiduMapApplib.so", bases)

    def test_backtrace_fallback_without_maps(self):
        regs, _ = build_registers_section(_ANDROID_NO_MAPS, "0x0")
        self.assertIsNotNone(regs)
        assert regs is not None
        self.assertEqual(regs["address_map"]["pc"]["kind"], "unknown")

        threads = [
            ThreadStack(
                tid="2",
                name="map-loaddata",
                thread_index=None,
                is_crash_thread=True,
                frames=[
                    StackFrame(
                        frame_number=0,
                        address="000000000073bcdc",
                        module="libBaiduMapSDK_map_v7_6_7.so",
                    ),
                    StackFrame(
                        frame_number=1,
                        address="000000000073a000",
                        module="libBaiduMapSDK_map_v7_6_7.so",
                    ),
                ],
                stack_domains=["native"],
                has_native_frames=True,
                has_arkts_frames=False,
                has_java_frames=False,
                has_objc_frames=False,
                has_swift_frames=False,
            )
        ]
        regs2, bases = enrich_registers_from_backtrace(regs, threads)
        assert regs2 is not None
        amap = regs2["address_map"]
        self.assertEqual(amap["pc"]["kind"], "code")
        self.assertEqual(amap["pc"]["module"], "libBaiduMapSDK_map_v7_6_7.so")
        self.assertEqual(amap["pc"]["offset"], "0x73bcdc")
        self.assertEqual(amap["pc"]["source"], "backtrace_frame0")
        # lr != #01；应为 pc-4 对应偏移
        self.assertEqual(amap["lr"]["kind"], "code")
        self.assertEqual(amap["lr"]["offset"], "0x73bcd8")
        self.assertEqual(amap["lr"]["source"], "backtrace_load_base")
        self.assertNotEqual(amap["lr"]["offset"], "0x73a000")
        self.assertEqual(amap["sp"]["kind"], "stack")
        self.assertIn("libBaiduMapSDK_map_v7_6_7.so", bases)


if __name__ == "__main__":
    unittest.main()
