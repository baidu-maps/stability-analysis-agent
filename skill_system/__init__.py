#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill System 核心模块。

对外提供：
- Claude 风格 `SKILL.md` 解析
- Skill 安装 / 卸载 / 发现 / 校验
- Skill 到 Tool/Workflow 的注册桥接
- Skill CLI 子命令
"""

from .models import (
    SkillBundle,
    SkillExport,
    SkillFrontmatter,
    SkillInstallResult,
    SkillPackageManifest,
    SkillRunResult,
    SkillSummary,
)
from .manager import SkillManager
from .parser import (
    load_skill_bundle,
    parse_skill_directory,
    parse_skill_markdown,
    parse_skill_package_manifest,
    parse_skill_frontmatter,
    split_skill_frontmatter,
)
from .runtime import SkillRuntime

__all__ = [
    "SkillBundle",
    "SkillExport",
    "SkillFrontmatter",
    "SkillInstallResult",
    "SkillPackageManifest",
    "SkillRunResult",
    "SkillSummary",
    "SkillManager",
    "SkillRuntime",
    "load_skill_bundle",
    "parse_skill_directory",
    "parse_skill_markdown",
    "parse_skill_package_manifest",
    "parse_skill_frontmatter",
    "split_skill_frontmatter",
]

