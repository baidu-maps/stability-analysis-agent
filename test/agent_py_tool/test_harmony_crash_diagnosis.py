#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harmony crashDiagnosis 单行 JSON 崩溃日志解析测试。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.crash_log_parser_tool import crash_log_parser
from tools.crash_parser.harmony_crash_diagnosis import (
    is_harmony_crash_diagnosis_json,
    try_load_crash_diagnosis_document,
)
from tools.parse_crash_errors import parse_result_has_usable_crash_data

SAMPLE_DOC = {
    "base_type": "crash",
    "sub_type": "basic_info",
    "app_version": "17.37",
    "bundle_id": "com.example.app",
    "platform": "Harmony",
    "crashTime": 1780367951,
    "attributes": {
        "exp_info": {"type": "3", "name": "SIGSEGV(11,1)", "message": ""},
    },
    "body": {
        "attributed_stack": {
            "thread_name": "com.example.app",
            "thread_id": 21840,
            "stack_frames": [
                {
                    "index": 0,
                    "type": "native",
                    "image": "/system/lib64/platformsdk/libmmi-client.z.so",
                    "frame_addr": "00000000001076a8",
                    "local_symbol": "OHOS::RefBase::~RefBase()",
                    "offset": 64,
                    "has_offset": True,
                    "register": "pc",
                },
                {
                    "index": 1,
                    "type": "native",
                    "image": "/data/storage/el1/bundle/libs/arm64/libdemo.so",
                    "frame_addr": "0000000000b6f3c0",
                    "local_symbol": "demo::CrashHere()",
                    "offset": 12,
                    "has_offset": True,
                    "register": "pc",
                },
            ],
        },
        "stacks": [{"thread_id": 1}, {"thread_id": 2}],
    },
}

HARMONY_PC_CALL_STACK = (
    "#00 pc 00000000001d8250 /system/lib/ld-musl-aarch64.so.1 (__timedwait_cp+156) [::35422f66114500c7d794bf84b3fd302b]\n"
    "#01 pc 00000000001da328 /system/lib/ld-musl-aarch64.so.1 (pthread_cond_timedwait+172) [::35422f66114500c7d794bf84b3fd302b]\n"
    "#02 pc 0000000000255f20 /data/storage/el1/bundle/libs/arm64/libapp_BaiduVIlib.so [::4bbc97464fe8933c04dffdf61f1d3e7c]\n"
    "#03 pc 0000000000255294 /data/storage/el1/bundle/libs/arm64/libapp_BaiduVIlib.so [::4bbc97464fe8933c04dffdf61f1d3e7c]\n"
    "#04 pc 00000000001dca54 /system/lib/ld-musl-aarch64.so.1 (start+240) [::35422f66114500c7d794bf84b3fd302b]\n"
)

SAMPLE_DOC_WITH_PC_STACK = {
    **SAMPLE_DOC,
    "body": {
        **SAMPLE_DOC["body"],
        "stacks": [
            {
                "thread_name": "worker",
                "thread_id": "worker-1",
                "call_stack": HARMONY_PC_CALL_STACK,
            }
        ],
    },
}

SAMPLE = "crashDiagnsis: " + json.dumps(SAMPLE_DOC, ensure_ascii=False)
SAMPLE_WITH_PC = "crashDiagnsis: " + json.dumps(SAMPLE_DOC_WITH_PC_STACK, ensure_ascii=False)


class TestHarmonyCrashDiagnosis(unittest.TestCase):
    def test_detect_and_load(self):
        self.assertTrue(is_harmony_crash_diagnosis_json(SAMPLE))
        doc = try_load_crash_diagnosis_document(SAMPLE)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["bundle_id"], "com.example.app")

    def test_full_parse(self):
        result = json.loads(crash_log_parser(SAMPLE))
        self.assertEqual(result["parse_status"], "ok")
        self.assertEqual(result["meta_info"]["log_format"], "harmony_crash_diagnosis_json")
        self.assertEqual(result["meta_info"]["os_type"], "harmonyos")
        self.assertEqual(result["meta_info"]["app_version"], "17.37")
        self.assertEqual(result["meta_info"]["process_name"], "com.example.app")
        self.assertEqual(result["crash_info"]["signal"], "11 (SIGSEGV)")
        self.assertEqual(result["crash_info"]["crash_reason"], "segmentation fault")
        self.assertTrue(parse_result_has_usable_crash_data(result))

        frames = result["threads"][0]["frames"]
        self.assertEqual(len(frames), 2)
        self.assertTrue(result["threads"][0]["is_crash_thread"])
        self.assertTrue(result["threads"][0]["is_main_thread"])
        self.assertEqual(frames[0]["address"], "0x00000000001076a8")
        self.assertEqual(frames[0]["function"], "OHOS::RefBase::~RefBase()")
        self.assertEqual(frames[0]["module"], "libmmi-client.z.so")
        self.assertEqual(frames[0]["library_type"], "system")
        self.assertEqual(frames[1]["module"], "libdemo.so")
        self.assertEqual(frames[1]["library_type"], "app")

    def test_typo_prefix_crash_diagnsis(self):
        """设备导出常见拼写 crashDiagnsis（少 i）。"""
        self.assertTrue(is_harmony_crash_diagnosis_json(SAMPLE))

    def test_normalizes_swapped_stack_thread_fields(self):
        """Harmony stacks[] 常将 thread_id 填线程名、thread_name 填数字 tid。"""
        doc = {
            **SAMPLE_DOC,
            "body": {
                "attributed_stack": SAMPLE_DOC["body"]["attributed_stack"],
                "stacks": [
                    {
                        "thread_id": "aime-decisdata8",
                        "thread_name": "22985",
                        "call_stack": HARMONY_PC_CALL_STACK,
                    }
                ],
            },
        }
        sample = "crashDiagnosis: " + json.dumps(doc, ensure_ascii=False)
        result = json.loads(crash_log_parser(sample))
        worker = next(
            t
            for t in result["threads"]
            if t.get("tid") == "22985" and t.get("name") == "aime-decisdata8"
        )
        self.assertEqual(len(worker["frames"]), 5)

    def test_prefers_body_stacks_pc_call_stack(self):
        """stack_frames 无 bundle 库时，应提取 body.stacks 内 #NN pc 应用库栈。"""
        result = json.loads(crash_log_parser(SAMPLE_WITH_PC))
        primary = next(t for t in result["threads"] if t.get("tid") == "worker-1")
        frames = primary["frames"]
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[3]["address"], "0x0000000000255294")
        self.assertEqual(frames[3]["module"], "libapp_BaiduVIlib.so")
        self.assertEqual(frames[3]["library_type"], "app")
        self.assertEqual(primary["name"], "worker")
        self.assertEqual(primary["tid"], "worker-1")
        crash = next(t for t in result["threads"] if t.get("is_crash_thread"))
        self.assertEqual(crash["tid"], "21840")
        baidu_frames = [f for f in frames if f.get("module") == "libapp_BaiduVIlib.so"]
        self.assertEqual(len(baidu_frames), 2)
        self.assertEqual(baidu_frames[0]["address"], "0x0000000000255f20")

    def test_aggregates_unique_app_frames_across_stacks(self):
        second_stack = (
            "#00 pc 00000000001d8250 /system/lib/ld-musl-aarch64.so.1\n"
            "#01 pc 0000000000425e80 /data/storage/el1/bundle/libs/arm64/libapp_BaiduVIlib.so\n"
            "#02 pc 0000000000422eb0 /data/storage/el1/bundle/libs/arm64/libapp_OtherApp.so\n"
        )
        doc = {
            **SAMPLE_DOC_WITH_PC_STACK,
            "body": {
                "attributed_stack": {"thread_name": "main", "thread_id": 1},
                "stacks": [
                    {
                        "thread_name": "worker-a",
                        "thread_id": "a",
                        "call_stack": HARMONY_PC_CALL_STACK,
                    },
                    {
                        "thread_name": "worker-b",
                        "thread_id": "b",
                        "call_stack": second_stack,
                    },
                ],
            },
        }
        sample = "crashDiagnosis: " + json.dumps(doc, ensure_ascii=False)
        result = json.loads(crash_log_parser(sample))
        agg = next((t for t in result["threads"] if t.get("name") == "aggregated_app_libs"), None)
        self.assertIsNotNone(agg)
        modules = {f.get("module") for f in agg["frames"]}
        self.assertIn("libapp_BaiduVIlib.so", modules)
        self.assertIn("libapp_OtherApp.so", modules)
        addrs = {f.get("address") for f in agg["frames"] if f.get("module") == "libapp_BaiduVIlib.so"}
        self.assertIn("0x0000000000255294", addrs)
        self.assertIn("0x0000000000425e80", addrs)

    def test_detects_pc_call_stack_without_attributed_frames(self):
        doc = {
            **SAMPLE_DOC_WITH_PC_STACK,
            "body": {
                "attributed_stack": {"thread_name": "main", "thread_id": 1},
                "stacks": SAMPLE_DOC_WITH_PC_STACK["body"]["stacks"],
            },
        }
        sample = "crashDiagnosis: " + json.dumps(doc, ensure_ascii=False)
        self.assertTrue(is_harmony_crash_diagnosis_json(sample))
        result = json.loads(crash_log_parser(sample))
        self.assertEqual(result["parse_status"], "ok")
        primary = next(t for t in result["threads"] if t.get("tid") == "worker-1")
        self.assertEqual(primary["frames"][3]["module"], "libapp_BaiduVIlib.so")

    def test_full_by_threads_when_crash_stack_misses_library_dir(self):
        """崩溃线程栈未命中 library_dir 时，全量输出 body.stacks 各线程。"""
        doc = {
            **SAMPLE_DOC_WITH_PC_STACK,
            "body": {
                "attributed_stack": SAMPLE_DOC["body"]["attributed_stack"],
                "stacks": [
                    {
                        "thread_name": "22985",
                        "thread_id": "worker-a",
                        "call_stack": HARMONY_PC_CALL_STACK,
                    },
                    {
                        "thread_name": "22102",
                        "thread_id": "OS_VSyncThread",
                        "call_stack": (
                            "#00 pc 0000000000175ad4 /system/lib/ld-musl-aarch64.so.1\n"
                            "#01 pc 000000000002ea8c /system/lib64/chipset-sdk-sp/libeventhandler.z.so\n"
                        ),
                    },
                ],
            },
        }
        sample = "crashDiagnosis: " + json.dumps(doc, ensure_ascii=False)
        lib_dir = "/tmp/harmony_test_libs_full_extract"
        os.makedirs(lib_dir, exist_ok=True)
        lib_path = os.path.join(lib_dir, "libapp_BaiduVIlib.so")
        with open(lib_path, "wb"):
            pass
        try:
            from tools.crash_parser.types import CrashParseOptions

            opts = CrashParseOptions(library_dir=lib_dir)
            result = json.loads(crash_log_parser(sample, options=opts))
            self.assertEqual(result["meta_info"]["harmony_extraction_mode"], "full_by_threads")
            self.assertEqual(result["meta_info"]["crash_thread_id"], "21840")
            self.assertTrue(result["threads"][0]["is_crash_thread"])
            self.assertEqual(result["meta_info"]["thread_count_total"], 2)
            self.assertEqual(result["meta_info"]["thread_count_extracted"], 3)
            self.assertNotIn("aggregated_app_libs", [t.get("name") for t in result["threads"]])
            bg = next(t for t in result["threads"] if t.get("tid") == "OS_VSyncThread")
            self.assertFalse(bg["is_crash_thread"])
            self.assertFalse(bg["is_main_thread"])
            self.assertGreaterEqual(len(bg["frames"]), 2)
        finally:
            if os.path.exists(lib_path):
                os.remove(lib_path)
            if os.path.isdir(lib_dir):
                os.rmdir(lib_dir)


if __name__ == "__main__":
    unittest.main()
