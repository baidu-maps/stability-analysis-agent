#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块→知识域路由器。

根据崩溃调用栈涉及的 module 名称，选择性加载相关知识域，
减少 RAG 检索中无关 pattern 的噪声。
"""

from __future__ import annotations
import json
import re
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Default mapping (used when JSON file not available)
DEFAULT_MAPPINGS = [
    {"module_pattern": r"libace|arkui|librender|libflutter", "knowledge_domain": "ui_framework", "tags": ["lifecycle", "rendering", "layout"]},
    {"module_pattern": r"libweb|webkit|chromium|libcef", "knowledge_domain": "webview", "tags": ["js_bridge", "rendering", "memory"]},
    {"module_pattern": r"pthread|dispatch|std::thread|libconcurrency", "knowledge_domain": "concurrency", "tags": ["deadlock", "race_condition", "sync"]},
    {"module_pattern": r"malloc|jemalloc|tcmalloc|scudo|libmemory|free", "knowledge_domain": "memory_allocator", "tags": ["heap_corruption", "double_free", "overflow"]},
    {"module_pattern": r"sqlite|leveldb|rocksdb|realm", "knowledge_domain": "database", "tags": ["corruption", "locking", "io"]},
    {"module_pattern": r"libssl|libcrypto|Security\.framework", "knowledge_domain": "crypto_security", "tags": ["cert", "handshake", "buffer"]},
    {"module_pattern": r"libnetwork|CFNetwork|libcurl|okhttp", "knowledge_domain": "networking", "tags": ["timeout", "buffer", "protocol"]},
    {"module_pattern": r"AVFoundation|libavcodec|MediaCodec|libmedia", "knowledge_domain": "media", "tags": ["codec", "buffer", "sync"]},
    {"module_pattern": r"CoreGraphics|libskia|libhwui|libGPU", "knowledge_domain": "graphics", "tags": ["gpu", "texture", "render_pipeline"]},
    {"module_pattern": r"JavaScriptCore|libv8|hermes|libark_js", "knowledge_domain": "js_runtime", "tags": ["gc", "jit", "binding"]},
]


class ModuleKnowledgeRouter:
    """根据模块名选择性过滤知识域。"""

    def __init__(self, mapping_file: Optional[str] = None):
        """
        Args:
            mapping_file: module_knowledge_mapping.json 路径，None 时使用内置默认映射
        """
        self._mappings = self._load_mappings(mapping_file)

    def _load_mappings(self, path: Optional[str]) -> List[Dict[str, Any]]:
        if path and Path(path).exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("mappings", data) if isinstance(data, dict) else data
            except Exception as e:
                logger.warning("Failed to load module mapping from %s: %s", path, e)
        return DEFAULT_MAPPINGS

    def route(self, modules: List[str]) -> Dict[str, Any]:
        """根据模块列表确定相关知识域。

        Args:
            modules: 来自调用栈的 module 名列表

        Returns:
            {
                "domains": ["concurrency", "memory_allocator", ...],
                "tags": ["deadlock", "heap_corruption", ...],
                "matched_modules": {"libpthread": "concurrency", ...}
            }
        """
        domains: Set[str] = set()
        tags: Set[str] = set()
        matched: Dict[str, str] = {}

        modules_text = " ".join(m for m in modules if m)

        for mapping in self._mappings:
            pattern = mapping.get("module_pattern", "")
            if not pattern:
                continue
            try:
                if re.search(pattern, modules_text, re.IGNORECASE):
                    domain = mapping.get("knowledge_domain", "")
                    if domain:
                        domains.add(domain)
                    for tag in mapping.get("tags", []):
                        tags.add(tag)
                    # Record which module matched
                    for m in modules:
                        if m and re.search(pattern, m, re.IGNORECASE):
                            matched[m] = domain
            except re.error:
                continue

        return {
            "domains": sorted(domains),
            "tags": sorted(tags),
            "matched_modules": matched,
        }

    def get_vector_filter(self, route_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """生成用于 ChromaDB where 过滤的条件。

        Args:
            route_result: route() 的输出

        Returns:
            ChromaDB where clause or None (if no filtering needed)
        """
        domains = route_result.get("domains", [])
        if not domains:
            return None
        # ChromaDB $in filter on crash_category metadata
        # Map knowledge_domain to crash_category used in pattern_index
        domain_to_category = {
            "concurrency": "concurrency",
            "memory_allocator": "memory",
            "ui_framework": "logic",
            "webview": "logic",
            "database": "logic",
            "crypto_security": "memory",
            "networking": "logic",
            "media": "memory",
            "graphics": "memory",
            "js_runtime": "memory",
        }
        categories = list(set(
            domain_to_category.get(d, d)
            for d in domains
            if d in domain_to_category
        ))
        if not categories:
            return None
        if len(categories) == 1:
            return {"crash_category": categories[0]}
        return {"crash_category": {"$in": categories}}
