#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EventHandler / Binder 解析单测（含鸿蒙 AppFreeze dump 格式）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.event_handler_analyzer import BinderChainTracer, EventHandlerAnalyzer


_OHOS_SNIPPET = """
Reason:THREAD_BLOCK_6S
App main thread is not response!
mainHandler dump is:
 EventHandler dump begin curTime: 2025-11-04 02:43:11.722
 Event runner (Thread name = , Thread ID = 57003) is running
 Current Running: start at 2025-11-04 02:43:03.790, Event { send thread = 62828, send time = 2025-11-04 02:43:03.790, handle time = 2025-11-04 02:43:03.790, trigger time = 2025-11-04 02:43:03.790, task name = uv_io_cb, caller = [ohos_js_environment_impl.cpp(PostTaskToHandler:64)] }
 History event queue information:
 No. 0 : Event { send thread = 57003, send time = 2025-11-04 02:43:03.384, handle time = 2025-11-04 02:43:03.408, trigger time = 2025-11-04 02:43:03.409, completeTime time = 2025-11-04 02:43:03.411, priority = Low, task name = uv_timer_task }
 No. 15 : Event { send thread = 57003, send time = 2025-11-04 02:43:02.872, handle time = 2025-11-04 02:43:03.672, trigger time = 2025-11-04 02:43:03.673, completeTime time = 2025-11-04 02:43:03.678, priority = Low, task name = NotifyResponseRegionChanged }
 High priority event queue information:
 No.1 : Event { send thread = 62805, send time = 2025-11-04 02:43:03.976, handle time = 2025-11-04 02:43:03.976, task name = uv_io_cb, caller = [ohos_js_environment_impl.cpp(PostTaskToHandler:64)] }
 Total size of High events : 7
 Total size of Low events : 3
Tid:62757, Name:OS_IPC_0_62757
#02 pc libipc_core.z.so(OHOS::BinderInvoker::TransactWithDriver(bool)+300)
Tid:62758, Name:OS_IPC_1_62758
""".strip()


class TestEventHandlerAnalyzer(unittest.TestCase):
    def test_ohos_current_running_and_duration(self):
        eh = EventHandlerAnalyzer().parse_from_log(_OHOS_SNIPPET)
        self.assertIsNotNone(eh.current_running)
        assert eh.current_running is not None
        self.assertEqual(eh.current_running.name, "uv_io_cb")
        self.assertIn("ohos_js_environment_impl.cpp", eh.current_running.caller)
        self.assertEqual(eh.current_running.send_thread, "62828")
        # 02:43:03.790 → 02:43:11.722 ≈ 7932ms
        self.assertGreaterEqual(eh.running_duration_ms, 7900)
        self.assertLessEqual(eh.running_duration_ms, 8100)
        self.assertTrue(eh.is_blocked)
        self.assertIn("uv_io_cb", eh.blocking_cause)
        self.assertEqual(eh.pending_by_priority.get("High"), 7)
        self.assertEqual(eh.queue_depth, 10)  # 7+3
        md = eh.render_markdown()
        self.assertIn("uv_io_cb", md)
        self.assertIn("EventHandler", md)

    def test_legacy_current_running_still_works(self):
        text = "Current Running: DoFrame, start := 1200\ntask: SlowTask, trigger := 0, complete := 5000\n"
        eh = EventHandlerAnalyzer().parse_from_log(text, freeze_threshold_ms=1000)
        self.assertIsNotNone(eh.current_running)
        assert eh.current_running is not None
        self.assertEqual(eh.current_running.name, "DoFrame")
        self.assertTrue(eh.long_events)
        self.assertEqual(eh.long_events[0].name, "SlowTask")

    def test_binder_ipc_thread_note_without_chain(self):
        chain = BinderChainTracer().trace_from_log(_OHOS_SNIPPET)
        self.assertEqual(len(chain.chain), 0)
        self.assertEqual(chain.ipc_thread_count, 2)
        self.assertTrue(chain.note_zh)
        md = chain.render_markdown()
        self.assertIn("OS_IPC", md)


if __name__ == "__main__":
    unittest.main()
