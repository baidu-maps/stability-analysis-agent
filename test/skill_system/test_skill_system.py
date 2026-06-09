#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from skill_system import SkillManager, SkillRuntime, load_skill_bundle


class TestSkillSystem(unittest.TestCase):
    def _write_skill(self, base: Path, name: str, skill_md: str, skill_json: dict | None = None) -> Path:
        skill_dir = base / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        if skill_json is not None:
            (skill_dir / "skill.json").write_text(json.dumps(skill_json, ensure_ascii=False, indent=2), encoding="utf-8")
        return skill_dir

    def test_parse_claude_style_skill_and_render_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = self._write_skill(
                root,
                "summarize-changes",
                """---
name: Summarize Changes
description: Summarize uncommitted changes and flag risks
when_to_use: Use when asked what changed.
argument-hint: [issue-id, output-format]
arguments:
  - issue-id
  - output-format
disable-model-invocation: true
allowed-tools: Read Grep
---

Summarize $ARGUMENTS for $SKILL_NAME.
""",
            )
            bundle = load_skill_bundle(skill_dir)
            self.assertEqual(bundle.command_name, "summarize-changes")
            self.assertEqual(bundle.display_name, "Summarize Changes")
            self.assertTrue(bundle.frontmatter.disable_model_invocation)
            self.assertEqual(bundle.frontmatter.arguments, ["issue-id", "output-format"])
            self.assertIn("Summarize issue-123 json", bundle.render("issue-123 json"))

    def test_install_and_discover_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = self._write_skill(
                root,
                "prompt-skill",
                """---
name: Prompt Skill
description: A prompt-only skill
---

Use this skill when needed.
""",
            )
            install_home = root / "installed"
            manager = SkillManager(skill_roots=[install_home], installed_root=install_home)
            result = manager.install_from_path(source_dir)
            self.assertEqual(result.command_name, "prompt-skill")
            discovered = manager.list()
            self.assertTrue(any(item.command_name == "prompt-skill" for item in discovered))

    def test_execute_workflow_skill_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_dir = root / "modules"
            module_dir.mkdir(parents=True, exist_ok=True)
            sys.path.insert(0, str(module_dir))
            try:
                (module_dir / "demo_skill_module.py").write_text(
                    """from tool_system import BaseWorkflow, WorkflowDefinition, WorkflowContext


class DemoWorkflow(BaseWorkflow):
    @property
    def definition(self):
        return WorkflowDefinition(
            name="demo_workflow",
            description="Demo workflow skill",
            problem_type="demo",
            required_tools=[],
        )

    def solve(self, problem, context: WorkflowContext):
        return {"status": "success", "echo": problem.get("value")}
""",
                    encoding="utf-8",
                )
                skill_dir = self._write_skill(
                    root,
                    "demo-workflow-skill",
                    """---
name: Demo Workflow Skill
description: Demo workflow skill
disable-model-invocation: true
---

Execute the workflow.
""",
                    skill_json={
                        "id": "demo-workflow-skill",
                        "name": "Demo Workflow Skill",
                        "command_name": "demo-workflow-skill",
                        "version": "0.1.0",
                        "type": "workflow",
                        "entrypoint": "workflow:demo_workflow",
                        "exports": [
                            {
                                "kind": "workflow",
                                "ref": "demo_skill_module:DemoWorkflow",
                                "name": "demo_workflow",
                                "priority": "CUSTOM",
                                "force_override": False,
                                "enabled": True,
                                "params": {},
                            }
                        ],
                    },
                )
                install_home = root / "installed"
                manager = SkillManager(skill_roots=[install_home], installed_root=install_home)
                manager.install_from_path(skill_dir)
                runtime = SkillRuntime(manager)
                result = runtime.execute("demo-workflow-skill", input_payload={"value": 7})
                self.assertEqual(result.mode, "workflow")
                self.assertEqual(result.result, {"status": "success", "echo": 7})
            finally:
                if str(module_dir) in sys.path:
                    sys.path.remove(str(module_dir))


if __name__ == "__main__":
    unittest.main()

