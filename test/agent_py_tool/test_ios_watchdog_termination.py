#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apple 前台退出看门狗：终止原因、线程名、镜像基址和系统库分类。"""

import json
import unittest

from tools.crash_log_parser_tool import crash_log_parser
from tools.cpp_crash.core import diagnose_cpp_crash


_SAMPLE = """\
Process:             EDJDriver [12691]
Version:             5.5.2 (123)
Code Type:           ARM-64 (Native)
Date/Time:           2026-10-23 13:40:18.5384 +0800
OS Version:          iPhone OS 27.0.1 (24A446)
Hardware Model:      iPhone18,1

Triggered by Thread: 0  Tmcom-MapDRender, Dispatch Queue: com.apple.main-thread

Exception Type:    EXC_CRASH (SIGKILL)
Exception Codes:   0x0000000000000000, 0x0000000000000000
Termination Reason:  Namespace FRONTBOARD, Code 2343432205,

:12691] Failed to terminate gracefully after 5.0s

WatchdogEvent: process-exit

Thread 0 name:  Tmcom-MapDRender Dispatch queue: com.apple.main-thread
Thread 0 Crashed:
0   libsystem_kernel.dylib                 0x254644ae8 __ulock_wait2 + 8
1   libsystem_platform.dylib               0x1a8ac5058 _os_unfair_lock_lock_slow + 192
2   libobjc.A.dylib                        0x19cb54344 objc_lookUpImpOrForward + 152
3   libobjcMsgSend.dylib                   0x1a474cce4 _objc_msgSend_uncached + 68
4   EDJDriver                              0x104a62924 0x10444c000 + 6383908
5   CoreFoundation                         0x19cdc6f84 __CFRunLoopDoTimers + 132
6   GraphicsServices                       0x247444f24 GSEventRunModal + 120
7   UIKitCore                              0x1a10601cc UIApplicationMain + 336
8   dyld                                   0x19cbd35b8 start + 6616

Thread 0 crashed with ARM Thread State (64-bit):
    x0: 0xfffffffffffffffc   x1: 0x0000000000000000   x2: 0x000000000001f7ea   x3: 0x0000000000000000
    pc: 0x0000000254644ae8   lr: 0x00000001a8ac5058   sp: 0x000000016b9b1df0   fp: 0x000000016b9b1e20

Binary Images:
       0x10444c000 -        0x108b7bfff EDJDriver arm64   /private/var/EDJDriver.app/EDJDriver
       0x254644000 -        0x25468121b libsystem_kernel.dylib arm64e   /usr/lib/system/libsystem_kernel.dylib
       0x1a8ac3000 -        0x1a8acb67f libsystem_platform.dylib arm64e   /usr/lib/system/libsystem_platform.dylib
       0x19cb50000 -        0x19cb9da57 libobjc.A.dylib arm64e   /usr/lib/libobjc.A.dylib
       0x1a474c000 -        0x1a474d0cf libobjcMsgSend.dylib arm64e   /usr/lib/objc/libobjcMsgSend.dylib
       0x19cd47000 -        0x19d229777 CoreFoundation arm64e   /System/Library/Frameworks/CoreFoundation.framework/CoreFoundation
       0x1a0fa6000 -        0x1a2fdc937 UIKitCore arm64e   /System/Library/PrivateFrameworks/UIKitCore.framework/UIKitCore
       0x19cbce000 -        0x19cc85737 dyld arm64e   /usr/lib/dyld
               0x0 - 0xffffffffffffffff ??? unknown-arch   ???
"""


class TestIosWatchdogTermination(unittest.TestCase):
    """openmap-client-3704 这类 FRONTBOARD 看门狗报告。"""

    def test_extracts_termination_thread_and_image_base(self):
        """终止原因、线程名、版本和 EDJDriver 基址都要留下。"""
        out = json.loads(crash_log_parser(_SAMPLE))
        self.assertEqual(out.get("parse_status"), "ok")
        info = out["crash_info"]
        meta = out["meta_info"]
        self.assertEqual(info.get("signal"), "SIGKILL")
        self.assertEqual(info.get("crash_reason"), "watchdog termination")
        self.assertEqual(info.get("category"), "watchdog")
        self.assertIn("Failed to terminate gracefully after 5.0s", info.get("abort_message") or "")
        self.assertIn("WatchdogEvent: process-exit", info.get("abort_message") or "")
        self.assertEqual(info.get("thread_type"), "main")
        self.assertEqual(meta.get("log_kind"), "watchdog")
        self.assertTrue(meta.get("anr_suspected"))
        self.assertEqual(meta.get("process_name"), "EDJDriver")
        self.assertEqual(meta.get("process_id"), "12691")
        self.assertEqual(meta.get("app_version"), "5.5.2 (123)")
        self.assertEqual(meta.get("timestamp"), "2026-10-23 13:40:18.5384 +0800")
        self.assertEqual(meta.get("arch"), "arm64")
        self.assertEqual(meta.get("crash_thread_name"), "Tmcom-MapDRender")
        self.assertEqual(meta.get("crash_thread_id"), "0")
        bases = meta.get("module_base_addresses") or {}
        self.assertEqual(bases.get("EDJDriver"), "0x10444c000")
        self.assertNotIn("unknown-arch   ???", bases)
        self.assertTrue(any("termination" == item for item in (out.get("raw_log_sections") or [])))

        thread = out["threads"][0]
        self.assertEqual(thread.get("name"), "Tmcom-MapDRender")
        self.assertTrue(thread.get("is_main_thread"))
        frames = thread["frames"]
        by_module = {frame["module"]: frame for frame in frames}
        self.assertEqual(by_module["libobjc.A.dylib"]["library_type"], "system")
        self.assertEqual(by_module["libobjc.A.dylib"]["language"], "objc")
        self.assertEqual(by_module["CoreFoundation"]["library_type"], "system")
        self.assertEqual(by_module["CoreFoundation"]["language"], "cpp")
        self.assertEqual(by_module["GraphicsServices"]["library_type"], "system")
        self.assertEqual(by_module["UIKitCore"]["library_type"], "system")
        self.assertEqual(by_module["UIKitCore"]["language"], "objc")
        self.assertEqual(by_module["dyld"]["library_type"], "system")
        self.assertEqual(by_module["EDJDriver"]["library_type"], "app")
        self.assertEqual(by_module["EDJDriver"]["offset"], "6383908")

        notes = ((out.get("registers") or {}).get("analysis") or {}).get("evidence_notes") or []
        self.assertFalse(any("空指针" in note for note in notes))
        address_map = (out.get("registers") or {}).get("address_map") or {}
        self.assertEqual(address_map["sp"]["kind"], "stack")
        self.assertEqual(address_map["fp"]["kind"], "stack")
        self.assertNotEqual(address_map.get("sp", {}).get("module"), "unknown-arch   ???")

    def test_diagnosis_does_not_ask_for_fault_address(self):
        """看门狗 SIGKILL 没有 fault address，不应再要求补地址。"""
        parsed = json.loads(crash_log_parser(_SAMPLE))
        diagnosis = diagnose_cpp_crash(parsed)
        follow_up = [item.get("id") for item in diagnosis.get("follow_up_checks") or []]
        self.assertNotIn("extract_fault_address", follow_up)
        mode_ids = [item.get("id") for item in diagnosis.get("fault_modes") or []]
        self.assertIn("CPP-FM-16", mode_ids)
        self.assertEqual(diagnosis["evidence"].get("thread_name"), "Tmcom-MapDRender")


if __name__ == "__main__":
    unittest.main()
