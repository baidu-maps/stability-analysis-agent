from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.repair_actions import RepairActionDeps, _post_fix_diagnosis_handler


class PostFixDiagnosisInputTests(unittest.TestCase):
    def test_reads_current_nested_request_schema_and_uses_active_code_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            deps = RepairActionDeps(
                result={},
                code_roots=["/tmp/isolated-code"],
                report_dir=Path(tmp),
                run_id="run-1",
                request_record={
                    "crash_input": {"source": "content", "content": "SIGSEGV\nframe"},
                    "paths": {
                        "library_dir": "/tmp/symbols",
                        "code_roots": ["/tmp/original-code"],
                    },
                },
            )
            with patch(
                "services.repair_actions.run_post_fix_diagnosis",
                return_value={"status": "passed"},
            ) as diagnose:
                result = _post_fix_diagnosis_handler(deps, {})

        self.assertEqual(result["status"], "passed")
        diagnose.assert_called_once_with(
            crash_log="",
            crash_log_content="SIGSEGV\nframe",
            library_dir="/tmp/symbols",
            code_roots=["/tmp/isolated-code"],
        )


if __name__ == "__main__":
    unittest.main()
