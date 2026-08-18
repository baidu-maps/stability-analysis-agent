#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill 管理器：发现、安装、卸载、校验、注册。
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import SkillBundle, SkillExport, SkillInstallResult, SkillSummary
from .parser import load_skill_bundle


@dataclass
class SkillLintIssue:
    """Skill 校验问题。"""

    level: str
    message: str
    path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "message": self.message, "path": self.path}


def _normalize_name(name: str) -> str:
    raw = (name or "").strip().lower()
    cleaned = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_", "."}:
            cleaned.append(ch)
        elif ch.isspace():
            cleaned.append("-")
    normalized = "".join(cleaned).strip("-_.")
    return normalized or "skill"


def _default_skill_home() -> Path:
    override = os.environ.get("STABILITY_AGENT_SKILL_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".config" / "stability-analysis-agent" / "skills").resolve()


def _default_discovery_roots() -> List[Path]:
    roots: List[Path] = []
    env = os.environ.get("STABILITY_AGENT_SKILL_DIRS", "").strip()
    if env:
        for raw in env.split(os.pathsep):
            if raw.strip():
                roots.append(Path(raw).expanduser().resolve())

    cwd = Path.cwd().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    defaults = [
        _default_skill_home(),
        Path.home() / ".claude" / "skills",
        cwd / ".claude" / "skills",
        repo_root / ".claude" / "skills",
    ]
    for item in defaults:
        p = item.expanduser().resolve()
        if p not in roots:
            roots.append(p)
    return roots


class SkillManager:
    """Skill 的发现与安装管理。"""

    def __init__(
        self,
        skill_roots: Optional[List[Path | str]] = None,
        installed_root: Optional[Path | str] = None,
    ):
        self.installed_root = Path(installed_root).expanduser().resolve() if installed_root else _default_skill_home()
        self.skill_roots = [Path(p).expanduser().resolve() for p in skill_roots] if skill_roots else _default_discovery_roots()
        if self.installed_root not in self.skill_roots:
            self.skill_roots.insert(0, self.installed_root)

    # -------------------- discovery --------------------

    def discover(self) -> List[SkillBundle]:
        bundles: List[tuple[int, SkillBundle]] = []
        seen: set[str] = set()
        for root_index, root in enumerate(self.skill_roots):
            if not root.exists():
                continue
            for skill_md in root.rglob("SKILL.md"):
                try:
                    bundle = load_skill_bundle(skill_md)
                except Exception:
                    continue
                key = str(bundle.path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                bundles.append((root_index, bundle))
        bundles.sort(key=lambda item: (item[0], item[1].command_name, str(item[1].path)))
        return [bundle for _, bundle in bundles]

    def list(self) -> List[SkillSummary]:
        return [bundle.to_summary() for bundle in self.discover()]

    def discover_installed(self) -> List[SkillBundle]:
        """仅扫描 installed_root（Web UI / Agent 运行时使用的已安装 skill）。"""
        root = self.installed_root
        if not root.exists():
            return []
        bundles: List[SkillBundle] = []
        seen: set[str] = set()
        for skill_md in root.rglob("SKILL.md"):
            try:
                bundle = load_skill_bundle(skill_md)
            except Exception:
                continue
            key = str(bundle.path.resolve())
            if key in seen:
                continue
            seen.add(key)
            bundles.append(bundle)
        bundles.sort(key=lambda item: (item.command_name, str(item.path)))
        return bundles

    def list_installed(self) -> List[SkillSummary]:
        return [bundle.to_summary() for bundle in self.discover_installed()]

    def find_installed(self, name: str) -> Optional[SkillBundle]:
        normalized = _normalize_name(name)
        for bundle in self.discover_installed():
            candidates = {
                _normalize_name(bundle.command_name),
                _normalize_name(bundle.display_name),
                _normalize_name(bundle.frontmatter.name or ""),
                _normalize_name(bundle.package.name or ""),
                _normalize_name(bundle.path.name),
            }
            if normalized in candidates:
                return bundle
        return None

    def find(self, name: str) -> Optional[SkillBundle]:
        normalized = _normalize_name(name)
        for bundle in self.discover():
            candidates = {
                _normalize_name(bundle.command_name),
                _normalize_name(bundle.display_name),
                _normalize_name(bundle.frontmatter.name or ""),
                _normalize_name(bundle.package.name or ""),
                _normalize_name(bundle.path.name),
            }
            if normalized in candidates:
                return bundle
        return None

    # -------------------- install / uninstall --------------------

    def install_from_path(
        self,
        source: Path | str,
        *,
        target_root: Optional[Path | str] = None,
        overwrite: bool = False,
    ) -> SkillInstallResult:
        source_path = Path(source).expanduser().resolve()
        if source_path.is_file() and source_path.suffix.lower() == ".zip":
            source_path = self._extract_zip(source_path)
        if not source_path.exists():
            raise FileNotFoundError(source_path)

        bundle = load_skill_bundle(source_path if source_path.is_dir() else source_path.parent)
        target_base = Path(target_root).expanduser().resolve() if target_root else self.installed_root
        target_base.mkdir(parents=True, exist_ok=True)
        target_dir = target_base / _normalize_name(bundle.command_name)

        if target_dir.exists():
            if not overwrite:
                raise FileExistsError(f"目标 skill 已存在: {target_dir}")
            shutil.rmtree(target_dir)

        shutil.copytree(bundle.path, target_dir)
        installed = load_skill_bundle(target_dir)
        return SkillInstallResult(
            source=str(source_path),
            installed_path=target_dir,
            command_name=installed.command_name,
            display_name=installed.display_name,
            version=installed.package.version,
        )

    def uninstall(self, name: str, *, target_root: Optional[Path | str] = None) -> bool:
        target_base = Path(target_root).expanduser().resolve() if target_root else self.installed_root
        target_dir = target_base / _normalize_name(name)
        if not target_dir.exists():
            return False
        shutil.rmtree(target_dir)
        return True

    def _extract_zip(self, archive: Path) -> Path:
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="stability-agent-skill-"))
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp_dir)
        candidates = [p for p in tmp_dir.iterdir() if p.is_dir()]
        if len(candidates) == 1:
            return candidates[0]
        if (tmp_dir / "SKILL.md").exists():
            return tmp_dir
        raise ValueError(f"zip 包中未找到单一 skill 根目录: {archive}")

    # -------------------- validation / registration --------------------

    def lint(self, source: Path | str) -> List[SkillLintIssue]:
        issues: List[SkillLintIssue] = []
        path = Path(source).expanduser().resolve()
        try:
            bundle = load_skill_bundle(path)
        except Exception as exc:
            return [SkillLintIssue(level="error", message=str(exc), path=str(path))]

        if not bundle.display_name:
            issues.append(SkillLintIssue(level="error", message="缺少 name / display name", path=str(bundle.path)))
        if not bundle.description:
            issues.append(SkillLintIssue(level="warning", message="缺少 description，自动发现能力会变弱", path=str(bundle.path)))
        if bundle.package.type not in {"prompt", "workflow", "tool", "plugin"}:
            issues.append(
                SkillLintIssue(
                    level="error",
                    message=f"不支持的 skill 类型: {bundle.package.type}",
                    path=str(bundle.path),
                )
            )
        if bundle.package.type in {"workflow", "tool", "plugin"} and not bundle.package.exports:
            issues.append(
                SkillLintIssue(
                    level="warning",
                    message="声明了可执行 skill 类型，但没有 exports，安装后只能作为提示词使用",
                    path=str(bundle.path),
                )
            )
        return issues

    def register_into_registry(self, registry: Any, bundles: Optional[Iterable[SkillBundle]] = None) -> List[str]:
        """把已安装 skill 的导出注册到 tool_system 注册表。"""
        from .runtime import register_skill_exports

        active_bundles = list(bundles) if bundles is not None else self.discover()
        registered: List[str] = []
        for bundle in active_bundles:
            names = register_skill_exports(bundle, registry)
            registered.extend(names)
        return registered

    # -------------------- helpers --------------------

    def summaries_as_dicts(self) -> List[Dict[str, Any]]:
        return [summary.to_dict() for summary in self.list()]

    def summaries_installed_as_dicts(self) -> List[Dict[str, Any]]:
        return [summary.to_dict() for summary in self.list_installed()]

    def resolve(self, name: str) -> SkillBundle:
        bundle = self.find_installed(name) or self.find(name)
        if bundle is None:
            raise KeyError(f"未找到 skill: {name}")
        return bundle
