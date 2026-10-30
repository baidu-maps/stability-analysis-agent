"""未改码原因：区分模型没提修复与修复无法落盘，并汇总进 00_run_summary。"""

import tempfile
import unittest
from unittest import mock

from cli.main import _fix_outcome_summary
from services.code_fixer import CodeFixer, analysis_proposed_fix

_PROPOSED = """### 修复方案
#### 需要修改的函数（必改，解释本次崩溃）
- `Foo::Bar()` - 释放后仍被访问
"""

_NOT_PROPOSED = """### 结论（崩溃定位与根因）
- 主根因：疑似多线程释放
### 修复方案
#### 需要修改的函数（必改，解释本次崩溃）
- 无
相关代码：
```cpp
void Foo::Bar() { delete p_; }
```
"""


class AnalysisProposedFixTests(unittest.TestCase):
    def test_propose_fix_json_counts(self) -> None:
        text = '{"agent_can_fetch_more": false, "next_action": {"kind": "propose_fix"}}'
        self.assertTrue(analysis_proposed_fix(text))

    def test_fix_edits_json_counts(self) -> None:
        self.assertTrue(analysis_proposed_fix('```json\n{"fix_edits": []}\n```'))

    def test_required_function_list_counts(self) -> None:
        self.assertTrue(analysis_proposed_fix(_PROPOSED))

    def test_empty_required_list_and_quoted_source_do_not_count(self) -> None:
        self.assertFalse(analysis_proposed_fix(_NOT_PROPOSED))


class GenerateAndApplyReasonTests(unittest.TestCase):
    def _run(self, analysis: str) -> str:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "services.code_fixer._complete_missing_repair_code", return_value=""
        ):
            result = CodeFixer(llm_adapter=object()).generate_and_apply(
                {"analysis": analysis, "code_context": {}}, [tmp]
            )
        self.assertFalse(result.success)
        return str(result.no_fix_reason)

    def test_no_candidate_after_proposal_is_target_not_in_source(self) -> None:
        self.assertEqual(self._run(_PROPOSED), "target_not_in_source")

    def test_no_candidate_without_proposal_is_no_propose_fix(self) -> None:
        self.assertEqual(self._run(_NOT_PROPOSED), "no_propose_fix")


class FixOutcomeSummaryTests(unittest.TestCase):
    def test_disabled(self) -> None:
        out = _fix_outcome_summary({}, apply_enabled=False, applied_fix_result=None)
        self.assertEqual(out, {"apply_enabled": False, "code_changed": False, "no_fix_reason": None})

    def test_applied(self) -> None:
        out = _fix_outcome_summary({}, apply_enabled=True, applied_fix_result={"success": True})
        self.assertTrue(out["code_changed"])
        self.assertIsNone(out["no_fix_reason"])

    def test_reason_from_apply_result(self) -> None:
        out = _fix_outcome_summary(
            {}, apply_enabled=True,
            applied_fix_result={"success": False, "no_fix_reason": "schema_violation"},
        )
        self.assertEqual(out["no_fix_reason"], "schema_violation")

    def test_post_apply_failure(self) -> None:
        out = _fix_outcome_summary(
            {}, apply_enabled=True,
            applied_fix_result={"success": False, "skipped_reason": "patch static check failed"},
        )
        self.assertEqual(out["no_fix_reason"], "post_apply_check_failed")

    def test_reason_from_metadata_when_pipeline_not_entered(self) -> None:
        out = _fix_outcome_summary(
            {"metadata": {"no_fix_reason": "gate_blocked"}},
            apply_enabled=True,
            applied_fix_result=None,
        )
        self.assertEqual(out["no_fix_reason"], "gate_blocked")

    def test_unknown_when_nothing_recorded(self) -> None:
        out = _fix_outcome_summary({}, apply_enabled=True, applied_fix_result=None)
        self.assertEqual(out["no_fix_reason"], "unknown")


if __name__ == "__main__":
    unittest.main()
