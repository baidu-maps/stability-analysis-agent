#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HarmonyOS native leak domain knowledge.

The rules are deliberately platform-scoped.  They turn DFX DMA labels into
actionable component/API hints without treating historical experience as
proof of a leak.
"""

from __future__ import annotations

from typing import Dict, List


DMA_RULES = (
    {
        "id": "pixelmap",
        "needles": ("pixelmap", "resource://", "file://", "http://", "https://"),
        "component": "Image / PixelMap",
        "suspect": "PixelMap C++ object or decoded image buffer was retained",
        "search_terms": ("PixelMap", "OH_Pixelmap", "Release", "Unreference"),
    },
    {
        "id": "xcomponent",
        "needles": ("xc-s-", "xc-t-", "xcomponent"),
        "component": "ArkUI XComponent",
        "suspect": "XComponent surface or texture lifecycle did not finish",
        "search_terms": ("XComponent", "Surface", "Destroy"),
    },
    {
        "id": "web",
        "needles": ("web-surface", "web-texture", "web-"),
        "component": "ArkWeb rendering / media",
        "suspect": "Web surface, texture, or media decode buffer was retained",
        "search_terms": ("Web", "Surface", "Texture", "heif"),
    },
    {
        "id": "hardware_video",
        "needles": ("hw-video-encoder", "hw-video-decoder"),
        "component": "Hardware video codec",
        "suspect": "Codec input/output buffers or codec instance were not released",
        "search_terms": ("AVCodec", "ReleaseOutputBuffer", "Stop", "Destroy"),
    },
    {
        "id": "software_video",
        "needles": ("sw-video-encoder", "sw-video-decoder"),
        "component": "Software video codec",
        "suspect": "Software codec buffers or decoder instance were retained",
        "search_terms": ("Decoder", "Release", "Destroy"),
    },
    {
        "id": "image_decode",
        "needles": ("srcimagesize-", "pixelmapsize-", "mimetype-"),
        "component": "Image decoder",
        "suspect": "Decoded image buffers were cached or not released",
        "search_terms": ("ImageSource", "CreatePixelMap", "Release", "Unreference"),
    },
    {
        "id": "native_surface",
        "needles": ("external", "nativewindow", "nativebuffer"),
        "component": "NativeWindow / NativeBuffer",
        "suspect": "Requested or allocated native buffer was not returned",
        "search_terms": (
            "OH_NativeWindow_NativeWindowRequestBuffer",
            "OH_NativeWindow_NativeWindowFlushBuffer",
            "OH_NativeWindow_NativeWindowAbortBuffer",
            "OH_NativeBuffer_Alloc",
            "OH_NativeBuffer_Unreference",
        ),
    },
)


def classify_dma_label(buf_name: str, leak_type: str, buf_type: str = "") -> Dict[str, object]:
    """Return the best platform-specific interpretation for a DMA record."""
    corpus = " ".join((buf_name or "", leak_type or "", buf_type or "")).lower()
    for rule in DMA_RULES:
        hits = [needle for needle in rule["needles"] if needle in corpus]
        if hits:
            return {
                "rule_id": rule["id"],
                "component": rule["component"],
                "suspect": rule["suspect"],
                "matched_labels": hits,
                "search_terms": list(rule["search_terms"]),
                "confidence": "medium",
                "scope": "HarmonyOS",
            }
    return {
        "rule_id": "unknown_dma",
        "component": "DMA buffer owner",
        "suspect": "DMA buffer ownership requires lifecycle inspection",
        "matched_labels": [],
        "search_terms": [],
        "confidence": "low",
        "scope": "HarmonyOS",
    }


LEAK_FIX_DIRECTIONS: Dict[str, List[str]] = {
    "jemalloc": [
        "Check allocation/release pairing on the top outstanding call chains.",
        "Inspect early-return, error, cancellation, and destructor paths.",
        "Distinguish bounded caches from allocations that continue growing.",
    ],
    "arkts": [
        "Collect two comparable .heapsnapshot files and inspect retainer paths to GC roots.",
        "Check ArkTS/native cross-language references and callback unregistration.",
    ],
    "ashmem": [
        "Inspect Image, PixelMap, and shared-memory handle ownership and release paths.",
        "Correlate the largest ashmem mappings with their creating component.",
    ],
    "anon": [
        "Inspect mmap/munmap pairing and long-lived thread stacks.",
        "Filter outstanding MmapEvent call chains in the profiler trace.",
    ],
    "file_mapping": [
        "Check databases, fonts, shared libraries, and resource files kept open unnecessarily.",
    ],
    "dma": [
        "Check buffer ownership across the application, render service, codec, and camera processes.",
        "Verify NativeWindow request/flush/abort and NativeBuffer alloc/unreference pairing.",
    ],
    "gpu": [
        "Check EGL context, GLES/Vulkan texture, buffer, and render-cache destruction.",
    ],
}
