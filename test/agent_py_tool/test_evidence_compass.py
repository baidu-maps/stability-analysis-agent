#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""04a 证据罗盘 / 确定性对齐 / prompt 顺序冒烟测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.crash_diagnosis.core import run_crash_diagnosis
from tools.crash_diagnosis.evidence_compass import build_evidence_compass
from tools.disassembly_tool import DisassemblyContext, DisassemblyInstruction, DisassemblyTool
from rag.deterministic_analyzer import DeterministicAnalyzer


class TestEvidenceCompass(unittest.TestCase):
    def test_missing_registers_lowers_ceiling(self):
        compass = build_evidence_compass(
            crash_info={"signal": "SIGSEGV"},
            stack_summary={
                "top_frames": [{"function": "crash_nullptr", "module": "lib.so", "address": "0x1000"}],
                "crash_function": "crash_nullptr",
            },
            data_availability={
                "has_registers": False,
                "has_memory_maps": False,
                "has_resolved_stack": True,
            },
            fault_address="0x100546ffc",
            fault_notes=["崩溃地址与 #00 帧地址一致，更可能是 PC/指令地址而非 fault address"],
            classification={"confidence": 0.9, "primary_pattern": "null_pointer_dereference"},
            deterministic_facts=[],
            disassembly={"triggered": False, "skip_reason": "stack_null_symbol"},
        )
        self.assertIn("寄存器转储", compass["missing_evidence"])
        self.assertLessEqual(compass["confidence_ceiling"], 0.70)
        self.assertTrue(compass["layers"]["location_pc"]["available"])
        self.assertEqual(
            compass["layers"]["location_pc"]["pc_vs_fault"],
            "crash_address_looks_like_pc",
        )


class TestDeterministicSymbolNull(unittest.TestCase):
    def test_nullptr_symbol_without_near_null_fault(self):
        parse = {
            "crash_info": {
                "signal": "SIGSEGV",
                "crash_reason": "NullPtr_SIGSEGV",
                "crash_address": "0x100546ffc",
            }
        }
        resolved = {
            "resolved_threads": [
                {
                    "is_crash_thread": True,
                    "frames": [
                        {"resolved_function": "crash_nullptr", "function": "crash_nullptr"},
                    ],
                }
            ]
        }
        out = DeterministicAnalyzer().analyze(parse, resolved, "")
        types = [f.fact_type for f in out.facts]
        self.assertIn("null_pointer", types)
        null_fact = next(f for f in out.facts if f.fact_type == "null_pointer")
        self.assertGreaterEqual(null_fact.confidence, 0.85)
        self.assertLess(null_fact.confidence, 1.0)


class TestCrashDiagnosisCompassWired(unittest.TestCase):
    def test_04a_contains_compass_and_aligned_facts(self):
        parse = {
            "meta_info": {"os_type": "mac"},
            "crash_info": {
                "signal": "SIGSEGV",
                "crash_reason": "NullPtr_SIGSEGV",
                "crash_address": "0x100546ffc",
                "category": "native",
            },
            "registers": {},
            "threads": [
                {
                    "tid": "1",
                    "is_crash_thread": True,
                    "frames": [
                        {
                            "function": "crash_nullptr",
                            "module": "libmylib.dylib",
                            "address": "0x100546ffc",
                        }
                    ],
                }
            ],
        }
        resolved = {
            "resolved_threads": [
                {
                    "tid": "1",
                    "is_crash_thread": True,
                    "frames": [
                        {
                            "resolved_function": "crash_nullptr",
                            "function": "crash_nullptr",
                            "module": "libmylib.dylib",
                            "address": "0x100546ffc",
                        }
                    ],
                }
            ]
        }
        out = run_crash_diagnosis(parse, {}, resolved, crash_log_content="NullPtr_SIGSEGV")
        self.assertIn("evidence_compass", out)
        self.assertTrue(out["evidence_compass"].get("missing_evidence"))
        facts = out.get("deterministic_facts") or []
        self.assertTrue(any(f.get("fact_type") == "null_pointer" for f in facts))
        prompt = out.get("prompt_section_zh") or ""
        self.assertIn("证据罗盘", prompt)
        self.assertIn("1) 位置", prompt)
        self.assertIn("2) 符号/源码", prompt)
        self.assertIn("3) 指令", prompt)
        self.assertIn("4) 数据", prompt)
        self.assertEqual(
            (out.get("crash_classification") or {}).get("primary_pattern"),
            "null_pointer_dereference",
        )


class TestDisassemblyHeuristic(unittest.TestCase):
    def test_ldr_infers_read_zh(self):
        tool = DisassemblyTool.__new__(DisassemblyTool)
        ctx = DisassemblyContext(binary_path="/tmp/a.so", crash_pc="0x10")
        inst = DisassemblyInstruction(
            address="0x10", opcode="ldr", operands="x0, [x1]", is_crash_pc=True
        )
        tool._analyze_crash_instruction(inst, ctx)
        self.assertEqual(ctx.access_direction, "read")
        self.assertIn("读", ctx.access_direction_zh)
        self.assertTrue(ctx.instruction_summary_zh)


if __name__ == "__main__":
    unittest.main()
