#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill 文件解析器。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import SkillBundle, SkillFrontmatter, SkillPackageManifest


_FRONTMATTER_RE = re.compile(r"^---\s*$", re.MULTILINE)


def split_skill_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """拆分 `SKILL.md` 的 frontmatter 与正文。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, text
    frontmatter_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    return _parse_simple_yaml(frontmatter_text), body.lstrip("\n")


def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    """解析一个足够覆盖 Claude skill frontmatter 的简单 YAML 子集。"""
    out: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: Optional[List[Any]] = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = line.strip()
        if indent > 0 and current_key and current_list is not None and stripped.startswith("- "):
            current_list.append(_parse_scalar(stripped[2:].strip()))
            continue
        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        current_key = key
        current_list = None
        if value == "":
            out[key] = []
            current_list = out[key]
            continue
        if value.startswith("[") and value.endswith("]"):
            items = [item.strip() for item in value[1:-1].split(",") if item.strip()]
            out[key] = [_parse_scalar(item) for item in items]
            continue
        if value in {"|", ">"}:
            out[key] = ""
            continue
        out[key] = _parse_scalar(value)
    return out


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except Exception:
            return value
    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except Exception:
            return value
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def parse_skill_frontmatter(text: str) -> SkillFrontmatter:
    frontmatter, _ = split_skill_frontmatter(text)
    normalized = _normalize_frontmatter_keys(frontmatter)
    return SkillFrontmatter.from_dict(normalized)


def parse_skill_package_manifest(skill_dir: Path) -> SkillPackageManifest:
    """解析可选的 `skill.json`。"""
    manifest_path = skill_dir / "skill.json"
    if not manifest_path.exists():
        return SkillPackageManifest()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"无法读取 skill.json: {manifest_path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"skill.json 必须是对象: {manifest_path}")
    normalized = _normalize_manifest_keys(payload)
    return SkillPackageManifest.from_dict(normalized)


def parse_skill_markdown(skill_md_path: Path) -> SkillBundle:
    """解析单个 `SKILL.md` 文件。"""
    if skill_md_path.name != "SKILL.md":
        raise ValueError(f"不是有效的 SKILL.md: {skill_md_path}")
    if not skill_md_path.exists():
        raise FileNotFoundError(skill_md_path)
    text = skill_md_path.read_text(encoding="utf-8")
    frontmatter, body = split_skill_frontmatter(text)
    normalized = _normalize_frontmatter_keys(frontmatter)
    fm = SkillFrontmatter.from_dict(normalized)
    skill_dir = skill_md_path.parent
    package = parse_skill_package_manifest(skill_dir)
    if not package.name:
        package.name = fm.name or skill_dir.name
    if not package.command_name:
        package.command_name = skill_dir.name
    if not package.description:
        package.description = fm.description or _first_paragraph(body)
    if not package.type:
        package.type = "prompt"
    return SkillBundle(path=skill_dir, frontmatter=fm, body=body, package=package, source="filesystem")


def parse_skill_directory(skill_dir: Path) -> SkillBundle:
    """解析 Skill 目录。"""
    skill_dir = skill_dir.expanduser().resolve()
    return parse_skill_markdown(skill_dir / "SKILL.md")


def load_skill_bundle(path: Path | str) -> SkillBundle:
    """从目录或 `SKILL.md` 路径加载 Skill。"""
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        return parse_skill_directory(p)
    if p.is_file() and p.name == "SKILL.md":
        return parse_skill_markdown(p)
    raise FileNotFoundError(f"找不到 Skill: {p}")


def _normalize_frontmatter_keys(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(frontmatter)
    if "when-to-use" in out and "when_to_use" not in out:
        out["when_to_use"] = out["when-to-use"]
    if "argument-hint" in out and "argument_hint" not in out:
        out["argument_hint"] = out["argument-hint"]
    if "disable-model-invocation" in out and "disable_model_invocation" not in out:
        out["disable_model_invocation"] = out["disable-model-invocation"]
    if "user-invocable" in out and "user_invocable" not in out:
        out["user_invocable"] = out["user-invocable"]
    if "allowed-tools" in out and "allowed_tools" not in out:
        out["allowed_tools"] = out["allowed-tools"]
    return out


def _normalize_manifest_keys(manifest: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(manifest)
    if "command-name" in out and "command_name" not in out:
        out["command_name"] = out["command-name"]
    return out


def _first_paragraph(text: str) -> str:
    raw = text.strip()
    if not raw:
        return ""
    return " ".join(raw.split("\n\n", 1)[0].split())

