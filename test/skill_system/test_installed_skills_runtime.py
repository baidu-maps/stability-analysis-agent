#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill_system.manager import SkillManager
from tool_system import ToolAndWorkflowRegistry, register_all_tools_and_workflows


def _write_prompt_skill(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: Demo prompt skill
---

# {name}
""",
        encoding="utf-8",
    )
    (skill_dir / "skill.json").write_text(
        '{"name": "%s", "version": "0.0.1", "type": "prompt", "description": "Demo"}' % name,
        encoding="utf-8",
    )
    return skill_dir


class InstalledSkillsRuntimeTests(unittest.TestCase):
    def test_list_installed_excludes_discovery_only_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            installed = tmp_path / "installed"
            other = tmp_path / "other"
            installed.mkdir()
            other.mkdir()
            _write_prompt_skill(installed, "installed-one")
            _write_prompt_skill(other, "other-only")

            manager = SkillManager(skill_roots=[installed, other], installed_root=installed)
            names = {s.command_name for s in manager.list_installed()}
            self.assertIn("installed-one", names)
            self.assertNotIn("other-only", names)

    @patch.object(SkillManager, "register_into_registry", return_value=[])
    def test_register_installed_skips_disabled(self, mock_register) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            source = _write_prompt_skill(tmp_path / "src", "toggle-demo")
            old_home = os.environ.get("STABILITY_AGENT_SKILL_HOME")
            old_disabled = os.environ.get("STABILITY_AGENT_DISABLED_SKILLS")
            os.environ["STABILITY_AGENT_SKILL_HOME"] = str(home)
            os.environ["STABILITY_AGENT_DISABLED_SKILLS"] = "toggle-demo"
            try:
                manager = SkillManager()
                manager.install_from_path(source, overwrite=True)

                from cli.main import _register_installed_skills

                registry = ToolAndWorkflowRegistry()
                register_all_tools_and_workflows(registry)
                _register_installed_skills(registry)

                mock_register.assert_called_once()
                bundles = list(mock_register.call_args.kwargs["bundles"])
                self.assertEqual(bundles, [])
            finally:
                if old_home is None:
                    os.environ.pop("STABILITY_AGENT_SKILL_HOME", None)
                else:
                    os.environ["STABILITY_AGENT_SKILL_HOME"] = old_home
                if old_disabled is None:
                    os.environ.pop("STABILITY_AGENT_DISABLED_SKILLS", None)
                else:
                    os.environ["STABILITY_AGENT_DISABLED_SKILLS"] = old_disabled

    @patch.object(SkillManager, "register_into_registry", return_value=[])
    def test_register_installed_includes_enabled(self, mock_register) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            source = _write_prompt_skill(tmp_path / "src", "enabled-demo")
            old_home = os.environ.get("STABILITY_AGENT_SKILL_HOME")
            old_disabled = os.environ.pop("STABILITY_AGENT_DISABLED_SKILLS", None)
            os.environ["STABILITY_AGENT_SKILL_HOME"] = str(home)
            try:
                manager = SkillManager()
                manager.install_from_path(source, overwrite=True)

                from cli.main import _register_installed_skills

                registry = ToolAndWorkflowRegistry()
                _register_installed_skills(registry)

                bundles = list(mock_register.call_args.kwargs["bundles"])
                self.assertEqual(len(bundles), 1)
                self.assertEqual(bundles[0].command_name, "enabled-demo")
            finally:
                if old_home is None:
                    os.environ.pop("STABILITY_AGENT_SKILL_HOME", None)
                else:
                    os.environ["STABILITY_AGENT_SKILL_HOME"] = old_home
                if old_disabled is not None:
                    os.environ["STABILITY_AGENT_DISABLED_SKILLS"] = old_disabled


if __name__ == "__main__":
    unittest.main()
