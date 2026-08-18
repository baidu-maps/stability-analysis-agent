#!/usr/bin/env python3
"""Feature-based conditional knowledge loading.

Each diagnosis module defines a FEATURE_REFERENCE_MAP that maps detected
log/stack features to specific knowledge YAML files. This module provides
the infrastructure to detect features and load only matching knowledge,
reducing noise and improving diagnosis precision.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .knowledge_loader import KNOWLEDGE_DIR, load_yaml_knowledge


# Type alias for feature-to-knowledge-file mapping
FeatureReferenceMap = Dict[str, List[str]]


# Domain-specific feature detection patterns
_FEATURE_PATTERNS: Dict[str, Dict[str, re.Pattern]] = {
    "cpp_crash": {
        "gwp_asan": re.compile(r"gwp[-_]?asan", re.I),
        "libffrt": re.compile(r"libffrt\.so|ffrt::", re.I),
        "libuv": re.compile(r"libuv\.so|uv_run|uv__io|uv_close|uv_async", re.I),
        "arkui": re.compile(r"libace_napi|ArkUI|libace_compatible", re.I),
        "sqlite": re.compile(r"sqlite3|libsqlite", re.I),
        "napi": re.compile(r"napi_|napi::|arkruntime|ArkNative", re.I),
        "memory_corruption": re.compile(r"heap-buffer|stack-buffer|use-after|double-free", re.I),
    },
    "appfreeze": {
        "libffrt": re.compile(r"libffrt\.so|ffrt::|FFRT", re.I),
        "libuv": re.compile(r"libuv\.so|uv_run|uv__io|EventLoop", re.I),
        "binder": re.compile(r"binder|BinderInvoker|IPC|transaction", re.I),
        "thermal": re.compile(r"thermal|hot_level|low memory", re.I),
    },
    "js_heap": {
        "globalhandler": re.compile(r"GlobalHandler|global_handler", re.I),
        "listener": re.compile(r"listener|addEventListener|EventEmitter", re.I),
        "napi": re.compile(r"napi|NativePointer|LocalHandle", re.I),
        "promise": re.compile(r"Promise|async|await|then\(", re.I),
    },
}


def detect_features(raw_content: str, *, domain: str) -> Set[str]:
    """Extract a set of feature keywords from raw log content.

    Args:
        raw_content: The raw log/stack text to analyze
        domain: The diagnosis domain (e.g., 'cpp_crash', 'appfreeze')

    Returns:
        Set of matched feature keys (e.g., {'gwp_asan', 'napi'})
    """
    patterns = _FEATURE_PATTERNS.get(domain, {})
    features: Set[str] = set()
    for feature_key, pattern in patterns.items():
        if pattern.search(raw_content):
            features.add(feature_key)
    return features


def resolve_features_to_files(
    features: Set[str],
    feature_map: FeatureReferenceMap,
) -> List[str]:
    """Given detected features and a mapping, return knowledge file paths to load.

    Args:
        features: Set of detected feature keys
        feature_map: Maps feature key -> list of knowledge file relative paths

    Returns:
        Deduplicated list of knowledge file paths that should be loaded.
    """
    files: List[str] = []
    seen: Set[str] = set()
    for feature in sorted(features):
        for filepath in feature_map.get(feature, []):
            if filepath not in seen:
                seen.add(filepath)
                files.append(filepath)
    return files


def load_conditional_knowledge(
    features: Set[str],
    feature_map: FeatureReferenceMap,
    *,
    knowledge_base_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load only knowledge files matching detected features.

    This avoids loading the full knowledge base and only brings in
    relevant fault modes for the current case.

    Args:
        features: Set of detected feature keys
        feature_map: Maps feature key -> knowledge file paths
        knowledge_base_dir: Override path to knowledge/ directory

    Returns:
        Merged list of fault mode dicts from all matched files.
    """
    base = Path(knowledge_base_dir) if knowledge_base_dir else KNOWLEDGE_DIR
    files = resolve_features_to_files(features, feature_map)
    all_modes: List[Dict[str, Any]] = []
    for filepath in files:
        parts = filepath.rsplit("/", 1)
        if len(parts) == 2:
            domain, filename = parts
        else:
            continue
        modes = load_yaml_knowledge(domain, filename, knowledge_dir=base)
        all_modes.extend(modes)
    return all_modes
