#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三方崩溃平台 JSON 导出解析测试。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.crash_log_parser_tool import crash_log_parser
from tools.crash_parser.platform_json_exports import is_platform_json_export
from tools.parse_crash_errors import parse_result_has_usable_crash_data


class TestPlatformJsonExports(unittest.TestCase):
    def _parse(self, payload):
        result = json.loads(crash_log_parser(json.dumps(payload, ensure_ascii=False)))
        self.assertEqual(result["parse_status"], "ok")
        self.assertTrue(parse_result_has_usable_crash_data(result))
        return result

    def test_sentry_event_json(self):
        payload = {
            "event_id": "abc",
            "platform": "javascript",
            "release": "1.2.3",
            "transaction": "HomePage",
            "exception": {
                "values": [
                    {
                        "type": "TypeError",
                        "value": "Cannot read properties of undefined",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "/app/bootstrap.js",
                                    "function": "boot",
                                    "lineno": 1,
                                    "in_app": True,
                                },
                                {
                                    "filename": "/app/main.js",
                                    "function": "doCrash",
                                    "lineno": 42,
                                    "in_app": True,
                                },
                            ]
                        },
                    }
                ]
            },
        }
        self.assertTrue(is_platform_json_export(json.dumps(payload)))
        result = self._parse(payload)
        self.assertEqual(result["meta_info"]["log_format"], "sentry_event_json")
        self.assertEqual(result["meta_info"]["app_version"], "1.2.3")
        self.assertEqual(result["crash_info"]["exception_type"], "TypeError")
        self.assertEqual(result["crash_info"]["category"], "js_exception")
        frames = result["threads"][0]["frames"]
        self.assertEqual(frames[0]["function"], "doCrash")
        self.assertEqual(frames[0]["line"], 42)

    def test_crashlytics_event_json(self):
        payload = {
            "eventId": "evt-1",
            "platform": "android",
            "bundleOrPackage": "com.example.app",
            "appVersion": "5.0",
            "exceptions": [
                {
                    "type": "SIGSEGV",
                    "message": "segmentation fault",
                    "frames": [
                        {
                            "symbol": "demo::CrashHere()",
                            "library": "/data/app/libdemo.so",
                            "address": "0000000000012340",
                            "line": 7,
                            "inApp": True,
                        }
                    ],
                }
            ],
        }
        result = self._parse(payload)
        self.assertEqual(result["meta_info"]["log_format"], "firebase_crashlytics_json")
        self.assertEqual(result["meta_info"]["os_type"], "android")
        self.assertEqual(result["meta_info"]["process_name"], "com.example.app")
        frame = result["threads"][0]["frames"][0]
        self.assertEqual(frame["address"], "0x0000000000012340")
        self.assertEqual(frame["module"], "libdemo.so")
        self.assertEqual(frame["function"], "demo::CrashHere()")

    def test_bugsnag_event_json(self):
        payload = {
            "notifier": {"name": "Bugsnag"},
            "events": [
                {
                    "app": {"type": "android", "version": "9.1"},
                    "context": "MainActivity",
                    "exceptions": [
                        {
                            "errorClass": "java.lang.IllegalStateException",
                            "message": "bad state",
                            "stacktrace": [
                                {
                                    "method": "com.example.MainActivity.onCreate",
                                    "file": "MainActivity.kt",
                                    "lineNumber": 12,
                                    "inProject": True,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        result = self._parse(payload)
        self.assertEqual(result["meta_info"]["log_format"], "bugsnag_event_json")
        self.assertEqual(result["meta_info"]["app_version"], "9.1")
        self.assertEqual(result["crash_info"]["exception_type"], "java.lang.IllegalStateException")
        self.assertEqual(result["threads"][0]["frames"][0]["function"], "com.example.MainActivity.onCreate")

    def test_generic_json_stack_export(self):
        payload = {
            "platform": "harmonyos",
            "app_version": "1.0",
            "process_name": "com.example.generic",
            "stack_frames": [
                {
                    "frame_addr": "0000000000255294",
                    "image": "/data/storage/el1/bundle/libs/arm64/libapp_Generic.so",
                    "local_symbol": "generic::Crash()",
                    "line": 100,
                },
                {
                    "frame_addr": "00000000001dca54",
                    "image": "/system/lib/ld-musl-aarch64.so.1",
                    "local_symbol": "start+240",
                },
            ],
        }
        result = self._parse(payload)
        self.assertEqual(result["meta_info"]["log_format"], "generic_json_stack_export")
        self.assertEqual(result["meta_info"]["os_type"], "harmonyos")
        frame = result["threads"][0]["frames"][0]
        self.assertEqual(frame["address"], "0x0000000000255294")
        self.assertEqual(frame["module"], "libapp_Generic.so")


if __name__ == "__main__":
    unittest.main()
