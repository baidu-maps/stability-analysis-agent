#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from daemon.web_preferences import load_web_preferences, save_web_preferences, toggle_skill


class WebPreferencesTests(unittest.TestCase):
    def test_save_and_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "web_preferences.json"
            import os

            old = os.environ.get("STABILITY_AGENT_WEB_PREFS_FILE")
            os.environ["STABILITY_AGENT_WEB_PREFS_FILE"] = str(prefs_file)
            try:
                saved = save_web_preferences(
                    {
                        "workspace": {
                            "library_dir": "/tmp/lib",
                            "code_roots": ["/tmp/code"],
                        }
                    }
                )
                self.assertEqual(saved["workspace"]["library_dir"], "/tmp/lib")
                toggled = toggle_skill("demo-skill", enabled=False)
                self.assertIn("demo-skill", toggled["disabled_skills"])
                toggled2 = toggle_skill("demo-skill", enabled=True)
                self.assertNotIn("demo-skill", toggled2["disabled_skills"])
                loaded = load_web_preferences()
                self.assertEqual(loaded["workspace"]["code_roots"], ["/tmp/code"])
                self.assertEqual(loaded["vector_db"]["mode"], "local")
                self.assertTrue(str(loaded["vector_db"].get("local_path") or "").strip())
            finally:
                if old is None:
                    os.environ.pop("STABILITY_AGENT_WEB_PREFS_FILE", None)
                else:
                    os.environ["STABILITY_AGENT_WEB_PREFS_FILE"] = old


    def test_vector_db_section_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "web_preferences.json"
            import os

            old = os.environ.get("STABILITY_AGENT_WEB_PREFS_FILE")
            os.environ["STABILITY_AGENT_WEB_PREFS_FILE"] = str(prefs_file)
            try:
                saved = save_web_preferences(
                    {
                        "vector_db": {
                            "mode": "local",
                            "local_path": "/tmp/custom_vector_db",
                        }
                    }
                )
                self.assertEqual(
                    saved["vector_db"]["local_path"],
                    str(Path("/tmp/custom_vector_db").expanduser().resolve()),
                )
            finally:
                if old is None:
                    os.environ.pop("STABILITY_AGENT_WEB_PREFS_FILE", None)
                else:
                    os.environ["STABILITY_AGENT_WEB_PREFS_FILE"] = old


if __name__ == "__main__":
    unittest.main()
