#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
与 add2line_resolver / crash_log_parser 共用的库路径匹配逻辑：
在指定 library_dir 下列出库文件，并按堆栈帧的 module 名匹配（与 add2line 行为一致）。
"""

from pathlib import Path
from typing import List, Optional

__all__ = [
    "find_library_files_in_dir",
    "is_system_native_module",
    "match_libraries_for_module",
]

_SYSTEM_SO_NAMES = frozenset(
    {
        "libc.so",
        "libm.so",
        "libdl.so",
        "liblog.so",
        "libc++.so",
        "libstdc++.so",
        "libart.so",
        "libandroid.so",
        "libbinder.so",
        "libutils.so",
        "libandroid_runtime.so",
        "linker",
        "linker64",
        "app_process32",
        "app_process64",
        "libsystem_c.dylib",
        "libsystem_malloc.dylib",
        "libsystem_kernel.dylib",
        "libsystem_pthread.dylib",
        "libobjc.A.dylib",
        "libdispatch.dylib",
        "libdyld.dylib",
    }
)

_SYSTEM_MODULE_PREFIXES = (
    "libsystem_",
    "libobjc",
    "libdispatch",
    "libc++",
    "libc++abi",
)


def is_system_native_module(module_name: Optional[str]) -> bool:
    """判断堆栈 module 是否为 Android/iOS 系统库（不应拿业务 library_dir 去做 addr2line）。"""
    if not module_name:
        return False
    raw = str(module_name).strip().replace("\\", "/")
    name = Path(raw).name
    lowered = name.lower()
    if lowered in _SYSTEM_SO_NAMES:
        return True
    if any(lowered.startswith(prefix) for prefix in _SYSTEM_MODULE_PREFIXES):
        return True
    if "/system/" in raw.lower() or "/apex/" in raw.lower() or "/usr/lib/" in raw.lower():
        return True
    return False


def find_library_files_in_dir(library_dir: str, os_type: str) -> List[Path]:
    """递归查找目录下的库文件（与 Add2lineResolver._find_library_files 一致）。"""
    library_dir_path = Path(library_dir)
    library_files: List[Path] = []
    extensions = {
        "android": [".so", ".a"],
        "harmonyos": [".so", ".a"],
        "ios": [".dylib", ".a"],
        "linux": [".so", ".a"],
        "macos": [".dylib", ".a"],
        "windows": [".dll", ".lib", ".a"],
        "unknown": [".so", ".dylib", ".dll", ".a", ".lib"],
    }
    target_extensions = extensions.get(os_type, extensions["unknown"])
    for ext in target_extensions:
        library_files.extend(library_dir_path.rglob(f"*{ext}"))
    library_files = list(set(library_files))
    library_files.sort()
    return library_files


def match_libraries_for_module(
    module_name: Optional[str],
    library_files: List[Path],
) -> List[Path]:
    """
    根据帧上的 module 名称，在 library_files 中查找对应库文件。
    与 add2line_resolver.resolve_stack_trace 内联逻辑一致。
    """
    if not module_name:
        return []
    module_basename = Path(str(module_name).replace("\\", "/")).name
    if is_system_native_module(module_name):
        return [f for f in library_files if f.name == module_basename]
    exact_matches = [f for f in library_files if f.name == module_basename]
    if exact_matches:
        return exact_matches
    lib_prefixed = f"lib{module_basename}"
    lib_matches = [f for f in library_files if f.name == lib_prefixed]
    if lib_matches:
        return lib_matches
    suffix_matches = [f for f in library_files if f.name.endswith(module_basename)]
    if suffix_matches:
        return suffix_matches
    module_short = (
        module_basename.replace(".so", "").replace(".dylib", "").replace(".dll", "")
    )
    # 过短的短名（如 libc）会误伤 libcrypto 等，禁止子串匹配
    if not module_short or len(module_short) < 8:
        return []
    substring_matches = [f for f in library_files if module_short in f.name]
    return substring_matches
