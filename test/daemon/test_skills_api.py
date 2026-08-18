#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skills Manager + daemon Skills HTTP smoke tests."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill_system.manager import SkillManager
from daemon.server import Handler, WEB_ROOT
from http.server import ThreadingHTTPServer


def _write_minimal_skill(root: Path, name: str = "web-ui-demo-skill") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: Demo skill for web UI tests
---

# {name}

Do nothing special.
""",
        encoding="utf-8",
    )
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "0.0.1",
                "type": "prompt",
                "description": "Demo skill for web UI tests",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return skill_dir


class SkillManagerLifecycleTests(unittest.TestCase):
    def test_install_lint_list_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = _write_minimal_skill(tmp_path / "src")
            home = tmp_path / "home"
            manager = SkillManager(skill_roots=[home], installed_root=home)

            issues = manager.lint(source)
            self.assertFalse(any(i.level == "error" for i in issues))

            result = manager.install_from_path(source, overwrite=False)
            self.assertTrue(Path(result.installed_path).exists())
            names = {s.command_name for s in manager.list_installed()}
            self.assertIn(result.command_name, names)

            with self.assertRaises(FileExistsError):
                manager.install_from_path(source, overwrite=False)

            result2 = manager.install_from_path(source, overwrite=True)
            self.assertEqual(result2.command_name, result.command_name)

            self.assertTrue(manager.uninstall(result.command_name))
            self.assertFalse(manager.uninstall(result.command_name))


class DaemonSkillsHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._httpd.shutdown()
        cls._httpd.server_close()

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None):
        conn = HTTPConnection("127.0.0.1", self._port, timeout=10)
        payload = None
        headers = {}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(data) if data else {}
        except json.JSONDecodeError:
            parsed = {"_raw": data}
        return resp.status, parsed

    def test_health_and_static_index(self) -> None:
        status, data = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("web_ui"))

        self.assertTrue((WEB_ROOT / "index.html").is_file())
        conn = HTTPConnection("127.0.0.1", self._port, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Stability Analysis Agent", body)

    def test_skills_list_installed_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            installed = tmp_path / "installed"
            other = tmp_path / "other"
            installed.mkdir()
            other.mkdir()
            _write_minimal_skill(installed, "installed-skill")
            _write_minimal_skill(other, "discovered-only")

            import os

            old_home = os.environ.get("STABILITY_AGENT_SKILL_HOME")
            old_dirs = os.environ.get("STABILITY_AGENT_SKILL_DIRS")
            os.environ["STABILITY_AGENT_SKILL_HOME"] = str(installed)
            os.environ["STABILITY_AGENT_SKILL_DIRS"] = f"{installed}{os.pathsep}{other}"
            try:
                status, listed = self._request("GET", "/skills")
                self.assertEqual(status, 200)
                names = {s.get("command_name") for s in listed.get("skills", [])}
                self.assertIn("installed-skill", names)
                self.assertNotIn("discovered-only", names)
            finally:
                if old_home is None:
                    os.environ.pop("STABILITY_AGENT_SKILL_HOME", None)
                else:
                    os.environ["STABILITY_AGENT_SKILL_HOME"] = old_home
                if old_dirs is None:
                    os.environ.pop("STABILITY_AGENT_SKILL_DIRS", None)
                else:
                    os.environ["STABILITY_AGENT_SKILL_DIRS"] = old_dirs

    def test_skills_http_install_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = _write_minimal_skill(tmp_path)
            # Point manager home via env for this process
            import os

            home = tmp_path / "installed"
            home.mkdir()
            old_home = os.environ.get("STABILITY_AGENT_SKILL_HOME")
            os.environ["STABILITY_AGENT_SKILL_HOME"] = str(home)
            try:
                status, data = self._request("POST", "/skills/lint", {"source": str(source)})
                self.assertEqual(status, 200)
                self.assertIn("issues", data)

                status, data = self._request(
                    "POST",
                    "/skills/install",
                    {"source": str(source), "overwrite": True},
                )
                self.assertEqual(status, 200)
                self.assertIn("command_name", data)

                status, listed = self._request("GET", "/skills")
                self.assertEqual(status, 200)
                names = {s.get("command_name") for s in listed.get("skills", [])}
                self.assertIn(data["command_name"], names)

                status, detail = self._request("GET", f"/skills/{data['command_name']}")
                self.assertEqual(status, 200)
                self.assertIn("summary", detail)

                status, un = self._request(
                    "POST",
                    "/skills/uninstall",
                    {"name": data["command_name"]},
                )
                self.assertEqual(status, 200)
                self.assertTrue(un.get("ok"))
            finally:
                if old_home is None:
                    os.environ.pop("STABILITY_AGENT_SKILL_HOME", None)
                else:
                    os.environ["STABILITY_AGENT_SKILL_HOME"] = old_home


if __name__ == "__main__":
    unittest.main()
