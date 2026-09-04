import tempfile
import unittest
from pathlib import Path

from services.file_context_tracker import FileContextTracker
from services.workspace_revision import workspace_revisions


class WorkspaceRevisionGuardTests(unittest.TestCase):
    def test_external_file_change_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.cpp"
            path.write_text("one", encoding="utf-8")
            tracker = FileContextTracker()
            tracker.record_read(str(path), "one", workspace_revision="r1")
            path.write_text("two", encoding="utf-8")
            result = tracker.check_stale(str(path), workspace_revision="r1")
            self.assertTrue(result["stale"])

    def test_non_git_revision_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            path.write_text("x", encoding="utf-8")
            self.assertEqual(workspace_revisions([tmp]), workspace_revisions([tmp]))


if __name__ == "__main__":
    unittest.main()
