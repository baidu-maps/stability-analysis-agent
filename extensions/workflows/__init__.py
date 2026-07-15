#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Workflow 扩展包。

复制 ``example_workflow.py`` 并按需修改（使用 ``@register_workflow``
装饰器），``sa-agent`` 启动时会被 ``extensions.register_all()`` 自动 import，
进而把自定义 Workflow 注册到全局 ``tool_system`` Registry。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import List

__all__: List[str] = []


def _auto_import_submodules() -> List[str]:
    """扫描本目录下的所有 Python 模块触发 ``@register_workflow`` 副作用。"""
    loaded: List[str] = []
    for _importer, modname, _ispkg in pkgutil.iter_modules(__path__):  # type: ignore[var-annotated]
        if modname.startswith("_") or modname == "example_workflow":
            # example_workflow 不再默认加载：保留为模板；如确需启用，请显式 import。
            continue
        full_name = f"{__name__}.{modname}"
        try:
            module = importlib.import_module(full_name)
            loaded.append(full_name)
            globals()[modname] = module
        except Exception as exc:  # pragma: no cover
            import logging
            logging.getLogger(__name__).warning("加载 %s 失败: %s", full_name, exc)
    return loaded


_auto_import_submodules()
