#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill 模板生成器。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SkillPreset:
    """内置 skill 模板预置。"""

    key: str
    display_name: str
    description: str
    when_to_use: str
    purpose: str
    inputs: List[str]
    workflow: List[str]
    outputs: List[str]
    allowed_tools: List[str]


_SKILL_PRESETS: Dict[str, SkillPreset] = {
    "automation-testing": SkillPreset(
        key="automation-testing",
        display_name="Automation Testing Skill",
        description="Validate a fix by running automated tests, smoke checks, or regression checks.",
        when_to_use="Use when a repaired feature needs automated verification before merge or release.",
        purpose="Define the verification flow for this project.",
        inputs=[
            "Bug or fix summary",
            "Target platform or module",
            "Test command or checklist",
        ],
        workflow=[
            "Identify the smallest reliable verification scope.",
            "Run the relevant automated checks.",
            "Capture failures, logs, and artifacts.",
            "Summarize pass/fail status and next actions.",
        ],
        outputs=[
            "Verification result",
            "Failing command output or logs",
            "Suggested follow-up work",
        ],
        allowed_tools=["shell"],
    ),
    "cicd-pipeline": SkillPreset(
        key="cicd-pipeline",
        display_name="CICD Pipeline Skill",
        description="Package, build, and publish a repaired artifact through a CI/CD pipeline.",
        when_to_use="Use when a verified fix needs to be packaged, signed, uploaded, or handed off.",
        purpose="Define the release or packaging flow for this project.",
        inputs=[
            "Build target",
            "Artifact naming rule",
            "Publish or handoff destination",
        ],
        workflow=[
            "Prepare the build environment.",
            "Produce the artifact.",
            "Run a lightweight sanity check.",
            "Publish or hand off the package.",
        ],
        outputs=[
            "Packaged artifact path",
            "Build or publish logs",
            "Release or handoff status",
        ],
        allowed_tools=["shell"],
    ),
    "bug-platform-fetcher": SkillPreset(
        key="bug-platform-fetcher",
        display_name="Bug Platform Fetcher Skill",
        description=(
            "根据缺陷管理平台（Jira / iCafe / WorkTile / 自建系统等）编号，"
            "拉取工单详情并下载崩溃日志与对应调试库文件，"
            "为 sa-agent 标准分析流程提供 crash_log 与 library_dir 路径。"
        ),
        when_to_use=(
            "Use when a fix should be driven directly by a ticket ID rather than "
            "manual selection of crash_log / library_dir / code-root paths."
        ),
        purpose=(
            "把「工单号 → crash 上下文」的拉取动作抽象为可替换的 Skill 实现。"
            "本仓库只提供空模板；具体的 REST/SDK 调用由开发者按所选平台补齐。"
        ),
        inputs=[
            "Ticket ID（工单 / 缺陷单编号，字符串）",
            "Platform auth token / cookie / API key（通过环境变量读取，不写入 skill）",
            "Optional：下载目录前缀（默认 ~/.cache/sa-agent/bug-platform/<ticket_id>）",
        ],
        workflow=[
            "校验 ticket_id 格式（非空、命名规则按所选平台自定）",
            "调用平台 API 拉取工单详情（标题、描述、自定义字段、附件清单）",
            "在所有附件中识别出唯一的崩溃日志（其它类型附件：截图、视频暂不处理）",
            "下载对应的 .dSYM / .so / .pdb 等调试库到 library_dir",
            "解析可选的 build_id / branch / platform 字段",
            "输出 JSON：{crash_log, library_dir, ticket_id, build_id, branch, platform}",
        ],
        outputs=[
            "crash_log 绝对路径",
            "library_dir 目录路径",
            "ticket_id / build_id / branch / platform 元数据",
            "download_dir 缓存目录（如需复用）",
        ],
        allowed_tools=["shell", "http"],
    ),
}


def _skill_name(raw: str) -> str:
    cleaned = []
    for ch in str(raw).strip().lower():
        if ch.isalnum() or ch in {"-", "_"}:
            cleaned.append(ch)
        elif ch.isspace():
            cleaned.append("-")
    name = "".join(cleaned).strip("-_")
    return name or "skill"


def available_skill_presets() -> Dict[str, SkillPreset]:
    """返回内置 skill 预置。"""
    return dict(_SKILL_PRESETS)


def _render_preset_skill_md(preset: SkillPreset) -> str:
    inputs = "\n".join(f"- {item}" for item in preset.inputs) or "- TODO"
    workflow = "\n".join(f"{idx + 1}. {step}" for idx, step in enumerate(preset.workflow)) or "1. TODO"
    outputs = "\n".join(f"- {item}" for item in preset.outputs) or "- TODO"
    tools = "\n".join(f"  - {item}" for item in preset.allowed_tools) or "  - shell"
    return f"""---
name: {preset.display_name}
description: {preset.description}
when_to_use: {preset.when_to_use}
disable-model-invocation: true
allowed-tools:
{tools}
context: inline
---

## Purpose

{preset.purpose}

## Inputs

{inputs}

## Workflow

{workflow}

## Outputs

{outputs}

## Notes

- Replace the placeholders with project-specific steps.
- Keep secrets, credentials, and environment details outside the skill body.
"""


def render_skill_scaffold(skill_name: str, skill_type: str = "prompt", preset: Optional[str] = None) -> Dict[str, str]:
    """生成一个可直接落盘的 skill 模板。"""
    command_name = _skill_name(skill_name)
    display_name = skill_name.strip() or command_name
    skill_type = (skill_type or "prompt").strip().lower()
    preset_key = _skill_name(preset or "")
    files: Dict[str, str] = {}

    preset_spec = _SKILL_PRESETS.get(preset_key)
    if preset_spec is not None:
        display_name = preset_spec.display_name
        skill_type = "prompt"
        files["SKILL.md"] = _render_preset_skill_md(preset_spec)
    elif skill_type == "prompt":
        files["SKILL.md"] = f"""---
name: {display_name}
description: Describe what this skill does and when to use it.
---

## What this skill does

Write short, actionable instructions here.

## Supporting context

- Add references, checklists, or examples in supporting files.
- Keep this file concise so it stays cheap to load.
"""
    else:
        files["SKILL.md"] = f"""---
name: {display_name}
description: Describe what this skill does and when to use it.
disable-model-invocation: true
---

## Instructions

Write the workflow or task instructions here.
"""

    manifest: Dict[str, Any] = {
        "id": command_name,
        "name": display_name,
        "command_name": command_name,
        "version": "0.1.0",
        "type": skill_type,
        "description": preset_spec.description if preset_spec is not None else f"{display_name} skill",
        "entrypoint": "prompt" if skill_type == "prompt" else f"workflow:{command_name}",
        "exports": [],
        "dependencies": [],
        "tags": ["stability-analysis-agent", "skill"],
        "metadata": {
            "generated": True,
            "skill_kind": skill_type,
        },
    }
    if preset_spec is not None:
        manifest["metadata"]["preset"] = preset_spec.key
    if skill_type in {"workflow", "tool", "plugin"}:
        manifest["exports"] = [
            {
                "kind": "module",
                "ref": "skill_module:register_all",
                "name": command_name,
                "priority": "CUSTOM",
                "force_override": False,
                "enabled": True,
                "params": {},
                "metadata": {
                    "note": "Replace with your real module path or register function.",
                },
            }
        ]

    files["skill.json"] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    return files


def write_skill_scaffold(
    target_dir: Path,
    skill_name: str,
    skill_type: str = "prompt",
    overwrite: bool = False,
    preset: Optional[str] = None,
) -> List[Path]:
    """把 skill 模板写入目标目录。"""
    target_dir = target_dir.expanduser().resolve()
    if target_dir.exists() and not overwrite:
        raise FileExistsError(f"目标目录已存在: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    for rel, content in render_skill_scaffold(skill_name, skill_type, preset=preset).items():
        path = target_dir / rel
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
