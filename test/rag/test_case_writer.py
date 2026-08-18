#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rag.case_writer import (
    build_case_record_from_report,
    commit_case_record,
    commit_from_report_dir,
    write_commit_audit,
)
from rag.vector_store_config import VectorStoreHandle, VectorStoreNotImplementedError, get_vector_store, normalize_vector_db_config


def _write_eligible_report(report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "01_crash_log_parser.json").write_text(
        json.dumps(
            {
                "crash_info": {"signal": "SIGSEGV", "crash_reason": "segmentation fault"},
                "meta_info": {"os_type": "macos", "platform": "arm64"},
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "02_add2line_resolver.json").write_text(
        json.dumps(
            {
                "resolved_threads": [
                    {
                        "is_crash_thread": True,
                        "frames": [
                            {
                                "resolved_function": "crash_here",
                                "module": "libmylib.dylib",
                                "resolved_file": "foo.cpp",
                                "resolved_line": 10,
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "08_apply_ai_fixes.json").write_text(
        json.dumps(
            {
                "success": True,
                "applied": [{"status": "applied", "file": "foo.cpp"}],
            }
        ),
        encoding="utf-8",
    )


class CaseWriterTests(unittest.TestCase):
    def test_build_returns_none_without_successful_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            _write_eligible_report(report_dir)
            (report_dir / "08_apply_ai_fixes.json").write_text(
                json.dumps({"success": False, "applied": []}),
                encoding="utf-8",
            )
            self.assertIsNone(build_case_record_from_report(report_dir))

    def test_build_record_from_eligible_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            _write_eligible_report(report_dir)
            record = build_case_record_from_report(report_dir)
            self.assertIsNotNone(record)
            assert record is not None
            pattern = record["pattern"]
            self.assertTrue(str(pattern.get("pattern_id", "")).startswith("pattern_"))
            self.assertEqual(pattern.get("validation_state"), "draft")
            self.assertEqual(pattern.get("source_type"), "internal_case")
            self.assertEqual(record["summary"]["signal"], "SIGSEGV")
            self.assertEqual(record["summary"]["fix_files"], ["foo.cpp"])

    def test_commit_case_record_uses_store(self) -> None:
        analyzer = mock.Mock()
        analyzer.add_pattern.return_value = True
        store = VectorStoreHandle(mode="local", local_path="/tmp/vdb", analyzer=analyzer)
        record = {
            "pattern": {"pattern_id": "pattern_test"},
            "evidence": [{"evidence_id": "evidence_1", "pattern_id": "pattern_test"}],
        }
        result = commit_case_record(record, store)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pattern_id"], "pattern_test")
        analyzer.add_pattern.assert_called_once()
        analyzer.add_evidence.assert_called_once()

    def test_commit_from_report_dir_skips_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            result = commit_from_report_dir(report_dir)
            self.assertFalse(result["ok"])
            self.assertTrue(result.get("skipped"))

    def test_commit_from_report_dir_with_mock_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            _write_eligible_report(report_dir)
            fake_store = VectorStoreHandle(
                mode="local",
                local_path=str(Path(tmp) / "vector_db"),
                analyzer=mock.Mock(add_pattern=mock.Mock(return_value=True), add_evidence=mock.Mock()),
            )
            with mock.patch("rag.case_writer.get_vector_store", return_value=fake_store):
                result = commit_from_report_dir(report_dir, vector_db_path=str(Path(tmp) / "vector_db"))
            self.assertTrue(result["ok"])
            self.assertIn("pattern_id", result)

    def test_write_commit_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            out = write_commit_audit(report_dir, {"status": "committed", "pattern_id": "p1"})
            self.assertEqual(out.name, "09_vector_db_commit.json")
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "committed")

    def test_remote_vector_store_raises(self) -> None:
        cfg = normalize_vector_db_config({"mode": "remote", "remote_url": "https://example.com"})
        with self.assertRaises(VectorStoreNotImplementedError):
            get_vector_store(cfg)


if __name__ == "__main__":
    unittest.main()
