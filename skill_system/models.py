#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill System 数据模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


@dataclass
class SkillFrontmatter:
    """SKILL.md 顶部 YAML frontmatter 的结构化表示。"""

    name: Optional[str] = None
    description: str = ""
    when_to_use: str = ""
    argument_hint: str = ""
    arguments: List[str] = field(default_factory=list)
    disable_model_invocation: bool = False
    user_invocable: Optional[bool] = None
    allowed_tools: List[str] = field(default_factory=list)
    context: str = "inline"
    agent: Optional[str] = None
    hooks: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillFrontmatter":
        raw_args = data.get("arguments")
        if isinstance(raw_args, list):
            arguments = [str(item).strip() for item in raw_args if str(item).strip()]
        elif raw_args is None:
            arguments = []
        else:
            arguments = [item for item in str(raw_args).split() if item.strip()]

        raw_tools = data.get("allowed-tools", data.get("allowed_tools"))
        if isinstance(raw_tools, list):
            allowed_tools = [str(item).strip() for item in raw_tools if str(item).strip()]
        elif raw_tools is None:
            allowed_tools = []
        else:
            allowed_tools = [str(raw_tools).strip()] if str(raw_tools).strip() else []

        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            hooks = {}

        known_keys = {
            "name",
            "description",
            "when_to_use",
            "when-to-use",
            "argument_hint",
            "argument-hint",
            "arguments",
            "disable_model_invocation",
            "disable-model-invocation",
            "user_invocable",
            "user-invocable",
            "allowed_tools",
            "allowed-tools",
            "context",
            "agent",
            "hooks",
        }
        metadata = {k: v for k, v in data.items() if k not in known_keys}

        return cls(
            name=str(data.get("name")).strip() if data.get("name") is not None else None,
            description=str(data.get("description") or "").strip(),
            when_to_use=str(data.get("when_to_use") or data.get("when-to-use") or "").strip(),
            argument_hint=str(data.get("argument_hint") or data.get("argument-hint") or "").strip(),
            arguments=arguments,
            disable_model_invocation=_normalize_bool(
                data.get("disable_model_invocation", data.get("disable-model-invocation")),
                default=False,
            ),
            user_invocable=(
                _normalize_bool(data.get("user_invocable", data.get("user-invocable")))
                if ("user_invocable" in data or "user-invocable" in data)
                else None
            ),
            allowed_tools=allowed_tools,
            context=str(data.get("context") or "inline").strip() or "inline",
            agent=str(data.get("agent")).strip() if data.get("agent") is not None else None,
            hooks=hooks,
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        metadata = payload.pop("metadata", {})
        payload["allowed-tools"] = payload.pop("allowed_tools")
        payload["argument-hint"] = payload.pop("argument_hint")
        payload["when-to-use"] = payload.pop("when_to_use")
        payload["disable-model-invocation"] = payload.pop("disable_model_invocation")
        payload["user-invocable"] = payload.pop("user_invocable")
        payload.update(metadata)
        return payload


@dataclass
class SkillExport:
    """Skill 对外导出的 Tool / Workflow / 注册模块。"""

    kind: str
    ref: str
    name: Optional[str] = None
    priority: str = "EXTENSION"
    force_override: bool = False
    enabled: bool = True
    params: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillExport":
        params = data.get("params", {})
        if not isinstance(params, dict):
            params = {}
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            kind=str(data.get("kind") or "module").strip().lower(),
            ref=str(data.get("ref") or data.get("module") or data.get("path") or "").strip(),
            name=str(data.get("name")).strip() if data.get("name") is not None else None,
            priority=str(data.get("priority") or "EXTENSION").strip().upper(),
            force_override=bool(data.get("force_override", data.get("force-override", False))),
            enabled=bool(data.get("enabled", True)),
            params=params,
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "name": self.name,
            "priority": self.priority,
            "force_override": self.force_override,
            "enabled": self.enabled,
            "params": self.params,
            "metadata": self.metadata,
        }


@dataclass
class SkillPackageManifest:
    """skill.json 的机器可读清单。"""

    id: Optional[str] = None
    name: Optional[str] = None
    command_name: Optional[str] = None
    version: str = "0.1.0"
    type: str = "prompt"
    description: str = ""
    entrypoint: Optional[str] = None
    exports: List[SkillExport] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillPackageManifest":
        exports = data.get("exports", [])
        if not isinstance(exports, list):
            exports = []
        dependencies = data.get("dependencies", [])
        if not isinstance(dependencies, list):
            dependencies = []
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        known_keys = {
            "id",
            "name",
            "command_name",
            "command-name",
            "version",
            "type",
            "description",
            "entrypoint",
            "exports",
            "dependencies",
            "tags",
            "metadata",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}
        if extra:
            metadata = {**metadata, **extra}

        return cls(
            id=str(data.get("id")).strip() if data.get("id") is not None else None,
            name=str(data.get("name")).strip() if data.get("name") is not None else None,
            command_name=str(data.get("command_name") or data.get("command-name") or "").strip() or None,
            version=str(data.get("version") or "0.1.0").strip(),
            type=str(data.get("type") or "prompt").strip().lower(),
            description=str(data.get("description") or "").strip(),
            entrypoint=str(data.get("entrypoint")).strip() if data.get("entrypoint") is not None else None,
            exports=[SkillExport.from_dict(item) for item in exports if isinstance(item, dict)],
            dependencies=[str(item).strip() for item in dependencies if str(item).strip()],
            tags=[str(item).strip() for item in tags if str(item).strip()],
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "command_name": self.command_name,
            "version": self.version,
            "type": self.type,
            "description": self.description,
            "entrypoint": self.entrypoint,
            "exports": [item.to_dict() for item in self.exports],
            "dependencies": self.dependencies,
            "tags": self.tags,
            "metadata": self.metadata,
        }


@dataclass
class SkillBundle:
    """一个已解析的 Skill 包。"""

    path: Path
    frontmatter: SkillFrontmatter
    body: str
    package: SkillPackageManifest = field(default_factory=SkillPackageManifest)
    source: str = "filesystem"

    @property
    def command_name(self) -> str:
        if self.package.command_name:
            return self.package.command_name
        return self.path.name

    @property
    def display_name(self) -> str:
        if self.frontmatter.name:
            return self.frontmatter.name
        if self.package.name:
            return self.package.name
        return self.command_name

    @property
    def description(self) -> str:
        if self.frontmatter.description:
            return self.frontmatter.description
        if self.package.description:
            return self.package.description
        body = self.body.strip()
        if not body:
            return ""
        first_para = body.split("\n\n", 1)[0].strip()
        return " ".join(first_para.split())

    @property
    def entrypoint(self) -> str:
        return self.package.entrypoint or "prompt"

    @property
    def capabilities(self) -> Dict[str, Any]:
        """Machine-readable capability and permission projection for harness use."""
        metadata = self.package.metadata if isinstance(self.package.metadata, dict) else {}
        return {
            "name": self.command_name,
            "type": self.package.type,
            "allowed_tools": list(self.frontmatter.allowed_tools or []),
            "context": self.frontmatter.context,
            "agent": self.frontmatter.agent,
            "permissions": dict(metadata.get("permissions") or {}),
            "tags": list(self.package.tags or []),
        }

    def render(self, arguments: Optional[str] = None, **context: Any) -> str:
        """渲染 SKILL.md 内容，做最小兼容替换。"""
        rendered = self.body
        args = (arguments or "").strip()
        substitutions = {
            "$ARGUMENTS": args,
            "$SKILL_NAME": self.display_name,
            "$SKILL_COMMAND": self.command_name,
            "$SKILL_DIR": str(self.path),
            "$SKILL_PATH": str(self.path / "SKILL.md"),
        }
        for key, value in context.items():
            substitutions[f"${str(key).upper()}"] = "" if value is None else str(value)
        for key, value in substitutions.items():
            rendered = rendered.replace(key, value)
        return rendered

    def to_summary(self) -> "SkillSummary":
        return SkillSummary(
            command_name=self.command_name,
            display_name=self.display_name,
            description=self.description,
            path=self.path,
            type=self.package.type,
            version=self.package.version,
            source=self.source,
            entrypoint=self.entrypoint,
            tags=list(self.package.tags),
        )


@dataclass
class SkillSummary:
    """用于 list / show 的轻量摘要。"""

    command_name: str
    display_name: str
    description: str
    path: Path
    type: str
    version: str
    source: str
    entrypoint: str
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_name": self.command_name,
            "display_name": self.display_name,
            "description": self.description,
            "path": str(self.path),
            "type": self.type,
            "version": self.version,
            "source": self.source,
            "entrypoint": self.entrypoint,
            "tags": list(self.tags),
        }


@dataclass
class SkillInstallResult:
    """安装结果。"""

    source: str
    installed_path: Path
    command_name: str
    display_name: str
    version: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "installed_path": str(self.installed_path),
            "command_name": self.command_name,
            "display_name": self.display_name,
            "version": self.version,
        }


@dataclass
class SkillRunResult:
    """Skill 运行结果。"""

    mode: str
    skill_name: str
    prompt: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    bundle: Optional[SkillBundle] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "mode": self.mode,
            "skill_name": self.skill_name,
            "prompt": self.prompt,
            "result": self.result,
            "metadata": self.metadata,
        }
        if self.bundle is not None:
            payload["bundle"] = {
                "path": str(self.bundle.path),
                "command_name": self.bundle.command_name,
                "display_name": self.bundle.display_name,
                "description": self.bundle.description,
                "entrypoint": self.bundle.entrypoint,
            }
        return payload
