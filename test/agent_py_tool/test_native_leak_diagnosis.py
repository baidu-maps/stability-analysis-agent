#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Native leak deterministic parser and workflow tests."""

from __future__ import annotations

import json
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tool_system import (
    ConfigDrivenExecutor,
    SystemConfig,
    ToolAndWorkflowRegistry,
    ToolConfig,
    WorkflowConfig,
    register_all_tools_and_workflows,
)
from tools.memory_diagnosis.core import run_memory_pressure_diagnosis
from tools.native_leak_diagnosis import (
    analyze_native_hook_database,
    analyze_native_leak_bundle,
    discover_native_leak_bundle,
    parse_kernel_dma_file,
    parse_kernel_memory_file,
    parse_sample_file,
    parse_smaps_file,
)
from cli.native_leak import handle_native_leak_command


SAMPLE = """processName: com.example.app
pid: 123
SoftThreshold: 512(MB)
Index  RSS(KB)  PSS(KB)  SwapPSS(KB)  TotalPSS(KB)  ION(KB)  GPU(KB)  TotalMem(KB)  Running Time(s)  Realtime
0  100  90  10  100  20  10  130  0  2026-08-10 10:00:00
1  180  160  20  180  25  10  215  60  2026-08-10 10:01:00
2  280  250  30  280  30  10  320  120  *2026-08-10 10:02:00
"""

SMAPS = """LOGGER_MEMCHECK_SMAPS_INFO
Size  Rss  Pss  Clean  Swap  SwapPss  Counts  Name
1000  900  700  0  100  100  1  [anon:native_heap:jemalloc]
500  400  100  0  20  20  1  anon:ArkTS Heap
100  80  40  0  0  0  1  /dev/ashmem/Image
********
LOGGER_MEMCHECK_SAMPLE_NMD_INFO
Size  Allocated  Count
64  1000  10
128  2000  10
***** endl *****
LOGGER_MEMCHECK_SAMPLE_NMD_INFO
Size  Allocated  Count
64  1800  15
128  5000  25
***** endl *****
"""

KERNEL = """memoryName:ion
Process  pid  fd  size_bytes  ino  buf_name  buf_type  leak_type  is_reclaim
com.example.app  123  7  4096  100  image-a  pixelmap  pixelmap  0
render_service  10  8  4096  100  image-a  pixelmap  pixelmap  0
com.example.app  123  9  8192  101  surface-a  external  external  0
*****
"""


def _create_trace_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE data_dict(id INTEGER PRIMARY KEY, data TEXT);
        CREATE TABLE native_hook(
          callchain_id INTEGER, ipid INTEGER, itid INTEGER, addr INTEGER,
          heap_size INTEGER, start_ts INTEGER, end_ts INTEGER,
          event_type TEXT, sub_type_id INTEGER
        );
        CREATE TABLE native_hook_frame(
          callchain_id INTEGER, depth INTEGER, ip INTEGER, symbol_id INTEGER,
          file_id INTEGER, offset INTEGER, symbol_offset INTEGER
        );
        """
    )
    conn.executemany("INSERT INTO data_dict(id, data) VALUES (?, ?)", [
        (1, "malloc"), (2, "libc.so"), (3, "App::Allocate"), (4, "libapp.so"),
    ])
    conn.executemany(
        "INSERT INTO native_hook VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 123, 7, 1000, 128, 1, 0, "AllocEvent", 0),
            (1, 123, 7, 2000, 128, 2, 0, "AllocEvent", 0),
            (2, 123, 8, 3000, 64, 3, 9, "AllocEvent", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO native_hook_frame VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(1, 0, 0x10, 1, 2, 0, 0), (1, 1, 0x20, 3, 4, 0, 16)],
    )
    conn.commit()
    conn.close()


class TestNativeLeakDiagnosis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sample = self.root / "memleak-native-com.example.app-123-sample.txt"
        self.smaps = self.root / "memleak-native-com.example.app-123-smaps.txt"
        self.kernel = self.root / "memleak-kernel-com.example.app-0-20260810100200.txt"
        self.db = self.root / "trace.db"
        self.sample.write_text(SAMPLE, encoding="utf-8")
        self.smaps.write_text(SMAPS, encoding="utf-8")
        self.kernel.write_text(KERNEL, encoding="utf-8")
        _create_trace_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovery_keeps_all_related_files(self):
        found = discover_native_leak_bundle(str(self.root))
        self.assertEqual(found["scenario"], "managed")
        self.assertEqual(found["selected"]["pid"], "123")
        self.assertEqual(len(found["selected"]["sample"]), 1)
        self.assertEqual(len(found["selected"]["smaps"]), 1)
        self.assertEqual(len(found["selected"]["kernel"]), 1)

    def test_sample_trend_uses_net_growth(self):
        parsed = parse_sample_file(str(self.sample))
        trend = parsed["trends"]["total_pss_kb"]
        self.assertEqual(trend["start_kb"], 100.0)
        self.assertEqual(trend["end_kb"], 280.0)
        self.assertEqual(trend["growth_kb"], 180.0)
        self.assertEqual(parsed["leak_types"][0]["type"], "pss")
        self.assertEqual(parsed["trigger_indices"], [2])

    def test_smaps_and_nmd_breakdown(self):
        parsed = parse_smaps_file(str(self.smaps))
        self.assertEqual(parsed["dominant_types"][0]["type"], "jemalloc")
        self.assertEqual(parsed["dominant_types"][0]["pss_kb"], 800)
        self.assertEqual(parsed["nmd"]["top_growth"][0]["size_bytes"], 128)
        self.assertEqual(parsed["nmd"]["top_growth"][0]["growth_bytes"], 3000)

    def test_trace_database_only_counts_outstanding(self):
        parsed = analyze_native_hook_database(str(self.db), sizes=[128], leak_type="malloc")
        self.assertEqual(parsed["source_table"], "native_hook")
        self.assertEqual(parsed["total_outstanding_bytes"], 256)
        self.assertEqual(parsed["total_outstanding_allocations"], 2)
        self.assertEqual(parsed["results"][0]["allocation_count"], 2)
        self.assertEqual(parsed["results"][0]["suspected_frame"]["symbol"], "App::Allocate")

    def test_statistic_trace_fallback_uses_outstanding_delta(self):
        path = self.root / "stat.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE data_dict(id INTEGER PRIMARY KEY, data TEXT);
            CREATE TABLE native_hook_statistic(
              callchain_id INTEGER, ipid INTEGER, apply_size INTEGER,
              release_size INTEGER, type INTEGER, sub_type_id INTEGER
            );
            CREATE TABLE native_hook_frame(
              callchain_id INTEGER, depth INTEGER, ip INTEGER, symbol_id INTEGER,
              file_id INTEGER, offset INTEGER, symbol_offset INTEGER
            );
            INSERT INTO native_hook_statistic VALUES (9, 123, 4096, 1024, 0, 0);
            """
        )
        conn.commit()
        conn.close()
        parsed = analyze_native_hook_database(str(path), leak_type="malloc")
        self.assertEqual(parsed["source_table"], "native_hook_statistic")
        self.assertEqual(parsed["total_outstanding_bytes"], 3072)

    def test_dma_deduplicates_shared_buffer(self):
        parsed = parse_kernel_dma_file(str(self.kernel))
        self.assertEqual(parsed["record_count"], 3)
        self.assertEqual(parsed["unique_buffer_count"], 2)
        self.assertEqual(parsed["total_unique_dma_bytes"], 12288)
        self.assertEqual(parsed["top_buffers"][0]["knowledge"]["rule_id"], "native_surface")

    def test_gpu_and_slab_kernel_summaries(self):
        gpu_path = self.root / "gpu.txt"
        gpu_path.write_text(
            "memoryName:gpu\nTotal U(device): 3000\nC:Texture: 2000\nC:RenderBuffer: 1000\n",
            encoding="utf-8",
        )
        gpu = parse_kernel_memory_file(str(gpu_path))
        self.assertEqual(gpu["kind"], "gpu")
        self.assertEqual(gpu["gpu"]["top_categories"][0]["name"], "Texture")

        slab_path = self.root / "slab.txt"
        slab_path.write_text(
            "memoryName:slab\nslabinfo - version: 2.1\n# name active_objs num_objs objsize\n"
            "dma_buf 10 20 128\nkmalloc-64 30 40 64\n",
            encoding="utf-8",
        )
        slab = parse_kernel_memory_file(str(slab_path))
        self.assertEqual(slab["kind"], "slab")
        self.assertEqual(slab["slab"]["top_caches"][0]["name"], "kmalloc-64")

    def test_bundle_builds_evidence_and_prompt(self):
        result = analyze_native_leak_bundle(str(self.root), trace_db=str(self.db))
        self.assertTrue(result["analyzed"])
        self.assertTrue(result["evidence_chain"])
        self.assertTrue(result["fault_mode_matches"])
        self.assertIn("Native 内存泄漏确定性诊断", result["prompt_section_zh"])
        json.dumps(result, ensure_ascii=False)

    def test_existing_04d_can_merge_native_leak_evidence(self):
        out = run_memory_pressure_diagnosis(
            {"meta_info": {"log_kind": "oom_kill", "oom_suspected": True}},
            {},
            "OutOfMemoryError",
            native_leak_path=str(self.root),
            native_leak_trace_db=str(self.db),
        )
        self.assertIsNotNone(out)
        self.assertTrue(out["native_leak_diagnosis"]["analyzed"])
        self.assertIn("Native 内存泄漏确定性诊断", out["prompt_section_zh"])

    def test_tool_and_workflow_are_registered(self):
        registry = ToolAndWorkflowRegistry()
        register_all_tools_and_workflows(registry)
        self.assertIsNotNone(registry.get_tool("native_leak_analyzer"))
        self.assertIsNotNone(registry.get_workflow("native_leak_analysis"))

    def test_workflow_runs_without_llm(self):
        registry = ToolAndWorkflowRegistry()
        register_all_tools_and_workflows(registry)
        executor = ConfigDrivenExecutor(
            registry,
            SystemConfig(
                tools=[ToolConfig(name="native_leak_analyzer", enabled=True)],
                workflows=[WorkflowConfig(name="native_leak_analysis", enabled=True)],
            ),
            None,
        )
        result = executor.execute_workflow("native_leak_analysis", {
            "native_leak_path": str(self.root),
            "native_leak_trace_db": str(self.db),
            "scope": "gen_prompt_only",
        })
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["native_leak_diagnosis"]["analyzed"])
        self.assertIn("分析与修复要求", result["final_tip"])

    def test_standalone_cli_writes_json_and_markdown(self):
        output = self.root / "reports"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = handle_native_leak_command([
                "--input", str(self.root),
                "--trace-db", str(self.db),
                "--output-dir", str(output),
            ])
        self.assertEqual(rc, 0)
        self.assertTrue((output / "04d_native_leak_diagnosis.json").is_file())
        self.assertTrue((output / "native_leak_report.md").is_file())
        self.assertIn("Native 内存泄漏分析报告", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
