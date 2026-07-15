#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户级与仓库级 Tool / Workflow 扩展发现与注册。

用法：

- **仓库自带示例**（``extensions/tools/example_tool.py``、
  ``extensions/workflows/example_workflow.py``）作为模板随开源版发布。
- **第三方插件**（个人 / 团队 / 企业）通过两种方式被发现：

  1. **本地扩展目录**：把 ``.py`` 文件或子包放到以下任一目录，
     运行 ``sa-agent`` 时会被自动扫描并 ``import``，凡使用
     ``@register_tool`` / ``@register_workflow`` 装饰器的类都会
     注册到全局 Registry：

     - ``~/.config/stability-analysis-agent/extensions`` （默认）
     - ``<cwd>/.stability-analysis-agent/extensions``
     - 通过环境变量 ``STABILITY_AGENT_EXT_DIRS`` 追加（PATH 列表分隔符）

  2. **Python 入口点**：在自己包的 ``pyproject.toml`` 中声明::

         [project.entry-points."stability_analysis_agent.tools"]
         my_tool = "my_pkg.my_tool:MyTool"

     运行 ``sa-agent`` 时会被自动加载。

- **优先级**：用户级 / 入口点扩展默认 ``Priority.EXTENSION``，
  若要覆盖内置实现，装饰器显式声明 ``priority=Priority.CUSTOM``
  并保证 ``force_override=True``（参见 ``tool_system.registry``）。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "USER_EXTENSIONS_DIR",
    "discover_user_extension_dirs",
    "iter_user_extension_files",
    "iter_entry_point_modules",
    "register_user_extensions",
    "register_all",
]


def USER_EXTENSIONS_DIR() -> Path:
    """默认用户级扩展目录（``~/.config/stability-analysis-agent/extensions``）。"""
    override = os.environ.get("STABILITY_AGENT_USER_EXT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".config" / "stability-analysis-agent" / "extensions").resolve()


def discover_user_extension_dirs() -> List[Path]:
    """汇总所有需要扫描的扩展根目录（去重、保持优先级）。"""
    roots: List[Path] = []
    env = os.environ.get("STABILITY_AGENT_EXT_DIRS", "").strip()
    if env:
        for raw in env.split(os.pathsep):
            if raw.strip():
                roots.append(Path(raw).expanduser().resolve())
    cwd_local = Path.cwd() / ".stability-analysis-agent" / "extensions"
    roots.append(cwd_local.resolve())
    roots.append(USER_EXTENSIONS_DIR())

    deduped: List[Path] = []
    seen: set = set()
    for item in roots:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def iter_user_extension_files(roots: Optional[Iterable[Path]] = None) -> List[Path]:
    """在 ``roots`` 中扫描所有 ``.py`` 文件，返回去重后的路径列表。"""
    search_roots = list(roots) if roots is not None else discover_user_extension_dirs()
    found: List[Path] = []
    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.name == "__init__.py":
                continue
            if path.name.startswith("_"):
                continue
            found.append(path.resolve())
    seen: set = set()
    deduped: List[Path] = []
    for path in found:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _load_module_from_path(path: Path) -> Optional[str]:
    """从绝对路径导入一个 Python 模块，返回全名，失败时返回 None。"""
    module_name = f"_stability_agent_ext_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - 保护性日志
        logger.warning("加载用户扩展失败 %s: %s", path, exc)
        sys.modules.pop(module_name, None)
        return None
    return module_name


def iter_entry_point_modules(group: str) -> List[str]:
    """读取 ``[group]`` 入口点，返回全部模块名（不 import）。"""
    try:
        from importlib.metadata import entry_points
    except Exception:  # pragma: no cover
        return []

    eps = entry_points()
    if hasattr(eps, "select"):
        candidates = eps.select(group=group)
    else:  # Python < 3.10 兼容
        candidates = eps.get(group, []) if hasattr(eps, "get") else []

    return [f"{ep.module}:{getattr(ep, 'name', '')}" for ep in candidates if getattr(ep, "module", None)]


def _load_entry_points(group: str) -> List[str]:
    """入口点触发 ``import``，使 ``@register_tool`` 副作用生效。"""
    loaded: List[str] = []
    try:
        from importlib.metadata import entry_points
    except Exception:  # pragma: no cover
        return loaded

    eps = entry_points()
    if hasattr(eps, "select"):
        candidates = list(eps.select(group=group))
    else:  # Python < 3.10 兼容
        candidates = list(eps.get(group, [])) if hasattr(eps, "get") else []

    for ep in candidates:
        target = getattr(ep, "module", None)
        if not target:
            continue
        try:
            importlib.import_module(target)
            loaded.append(f"{target}:{getattr(ep, 'name', '')}")
        except Exception as exc:  # pragma: no cover
            logger.warning("加载入口点扩展失败 %s: %s", target, exc)
    return loaded


def register_user_extensions() -> List[str]:
    """扫描用户扩展目录 + 入口点并触发注册。返回已加载条目。"""
    loaded: List[str] = []
    for path in iter_user_extension_files():
        name = _load_module_from_path(path)
        if name:
            loaded.append(str(path))
    return loaded


def register_all() -> List[str]:
    """仓库自带示例 + 用户扩展 + 入口点一并加载。"""
    loaded: List[str] = []

    # 1. 仓库自带示例：通过 importlib.import_module 触发装饰器
    for subpkg in ("tools", "workflows"):
        module_name = f"extensions.{subpkg}"
        try:
            importlib.import_module(module_name)
            loaded.append(module_name)
        except Exception as exc:
            logger.debug("内置扩展模块缺失或加载失败 %s: %s", module_name, exc)

    # 2. 用户级目录扩展
    user_loaded = register_user_extensions()
    loaded.extend(user_loaded)

    # 3. Python 入口点
    loaded.extend(_load_entry_points("stability_analysis_agent.tools"))
    loaded.extend(_load_entry_points("stability_analysis_agent.workflows"))

    if loaded:
        logger.info("已加载扩展: %s", loaded)
    return loaded
