from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tool_system.llm.llm_adapter import ReplayLLMAdapter


class OfflineReplayE2ETests(unittest.TestCase):
    def test_load_responses_from_report_round_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            round_dir = root / "round_0"
            round_dir.mkdir()
            (round_dir / "07_ai_gen_res.md").write_text('{"agent_can_fetch_more": false}', encoding="utf-8")
            adapter = ReplayLLMAdapter({"provider": "offline_replay", "report_dir": str(root)})
            response = adapter.chat([{"role": "user", "content": "x"}])
            self.assertIn("agent_can_fetch_more", response.content)


if __name__ == "__main__":
    unittest.main()
