#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块责任归属判定。

基于 .so / .dylib / .framework 路径和名称，判定崩溃的责任方
（应用代码 / 第三方SDK / 系统框架 / 厂商定制）。
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ResponsibilityAttribution:
    """责任归属结果。"""
    responsibility: str  # "application" / "system" / "third_party" / "vendor" / "undetermined"
    module: str          # 责任模块名
    reasoning: str       # 归属理由
    confidence: float    # 0.0-1.0


# Platform-specific path rules for responsibility attribution
ATTRIBUTION_RULES: Dict[str, List[Tuple[str, str, str]]] = {
    # (regex_pattern, responsibility, reasoning_template)
    "android": [
        (r"/data/app/[^/]+/", "application", "位于应用安装目录 /data/app/"),
        (r"/data/data/[^/]+/", "application", "位于应用数据目录 /data/data/"),
        (r"/system/lib", "system", "位于系统库目录 /system/lib/"),
        (r"/system/framework", "system", "位于系统框架目录"),
        (r"/apex/", "system", "位于 APEX 模块目录"),
        (r"/vendor/lib", "vendor", "位于厂商库目录 /vendor/lib/"),
        (r"/odm/lib", "vendor", "位于 ODM 库目录"),
        (r"/product/lib", "vendor", "位于产品定制库目录"),
    ],
    "harmonyos": [
        (r"/data/storage/el\d/bundle/", "application", "位于应用 bundle 目录"),
        (r"/data/app/", "application", "位于应用安装目录"),
        (r"/system/lib", "system", "位于系统库目录"),
        (r"/system/app", "system", "位于系统应用目录"),
        (r"/chipset/lib", "vendor", "位于芯片厂商库目录"),
        (r"/vendor/", "vendor", "位于厂商定制目录"),
    ],
    "ios": [
        # iOS 没有 .so 但有 image 路径
        (r"/var/containers/Bundle/Application/", "application", "位于应用沙盒"),
        (r"/private/var/containers/", "application", "位于应用容器目录"),
        (r"/System/Library/Frameworks/", "system", "位于系统框架目录"),
        (r"/System/Library/PrivateFrameworks/", "system", "位于系统私有框架"),
        (r"/usr/lib/", "system", "位于系统库目录"),
    ],
    "macos": [
        (r"/Applications/", "application", "位于应用目录"),
        (r"/Users/[^/]+/", "application", "位于用户目录"),
        (r"/System/Library/", "system", "位于系统库目录"),
        (r"/usr/lib/", "system", "位于系统库目录"),
        (r"/Library/Frameworks/", "third_party", "位于第三方框架目录"),
    ],
    "linux": [
        (r"/usr/lib/", "system", "位于系统库目录"),
        (r"/lib/x86_64-linux-gnu/", "system", "位于系统库目录"),
        (r"/lib/aarch64-linux-gnu/", "system", "位于系统库目录"),
        (r"/opt/", "third_party", "位于第三方软件目录"),
        (r"/home/", "application", "位于用户目录"),
    ],
}

# Well-known system libraries (module name only, no path)
SYSTEM_MODULES = {
    "libc.so", "libm.so", "libdl.so", "libpthread.so", "librt.so",
    "libstdc++.so", "libc++.so", "libc++abi.so",
    "libart.so", "libandroid_runtime.so", "libbinder.so",
    "libutils.so", "libcutils.so", "liblog.so", "libhwbinder.so",
    "libsystem_kernel.dylib", "libsystem_c.dylib", "libsystem_malloc.dylib",
    "libdispatch.dylib", "libobjc.A.dylib", "libxpc.dylib",
    "CoreFoundation", "Foundation", "UIKit", "AppKit",
    "libace.z.so", "libark_jsruntime.so", "libace_napi.z.so",
    "libhilog.so", "libhitrace.so",
}

# Well-known third-party SDK patterns
THIRD_PARTY_PATTERNS = [
    r"libcrashlytics", r"libfirebase", r"libflutter",
    r"libReact", r"libhermes", r"libfb", r"libfolly",
    r"libweex", r"libucr", r"libsentry", r"libbugly",
    r"libcrash_report", r"libunity", r"libUE4",
    r"Bugly\.framework", r"Firebase", r"Sentry",
]


def attribute_responsibility(
    module: str,
    image_path: str = "",
    platform: str = "",
) -> ResponsibilityAttribution:
    """基于模块名和路径判定责任归属。

    Args:
        module: 模块名（如 "libMyApp.so"）
        image_path: 完整路径（如 "/data/app/com.example/lib/libMyApp.so"）
        platform: 平台（android/ios/harmonyos/macos/linux）

    Returns:
        ResponsibilityAttribution 结果
    """
    # 1. Check known system modules
    if module in SYSTEM_MODULES:
        return ResponsibilityAttribution(
            responsibility="system",
            module=module,
            reasoning=f"{module} 为已知系统库",
            confidence=0.95,
        )

    # 2. Check third-party SDK patterns
    for pattern in THIRD_PARTY_PATTERNS:
        if re.search(pattern, module, re.IGNORECASE):
            return ResponsibilityAttribution(
                responsibility="third_party",
                module=module,
                reasoning=f"{module} 匹配已知三方 SDK 模式 ({pattern})",
                confidence=0.85,
            )

    # 3. Check path-based rules
    if image_path and platform:
        platform_lower = platform.lower()
        # Normalize platform name
        if "harmony" in platform_lower or "ohos" in platform_lower:
            platform_key = "harmonyos"
        elif "android" in platform_lower:
            platform_key = "android"
        elif "ios" in platform_lower or "iphone" in platform_lower:
            platform_key = "ios"
        elif "mac" in platform_lower or "darwin" in platform_lower:
            platform_key = "macos"
        elif "linux" in platform_lower:
            platform_key = "linux"
        else:
            platform_key = ""

        rules = ATTRIBUTION_RULES.get(platform_key, [])
        for pattern, resp, reason_tmpl in rules:
            if re.search(pattern, image_path, re.IGNORECASE):
                return ResponsibilityAttribution(
                    responsibility=resp,
                    module=module,
                    reasoning=f"{module} {reason_tmpl}",
                    confidence=0.80,
                )

    # 4. Heuristic: if module contains app-like naming
    if re.search(r"^lib[A-Z]", module):  # Typically app libraries start with libCapital
        return ResponsibilityAttribution(
            responsibility="application",
            module=module,
            reasoning=f"{module} 命名模式符合应用自有库",
            confidence=0.50,
        )

    return ResponsibilityAttribution(
        responsibility="undetermined",
        module=module,
        reasoning=f"无法确定 {module} 的责任归属，需更多上下文",
        confidence=0.0,
    )


def attribute_from_stack_frames(
    frames: List[Dict[str, Any]],
    platform: str = "",
) -> Dict[str, Any]:
    """对调用栈中的关键帧进行责任归属分析。

    Args:
        frames: 符号化后的帧列表
        platform: 平台

    Returns:
        {
            "crash_frame_attribution": {...},
            "first_app_attribution": {...},
            "overall_responsibility": "application" | "system" | ...
        }
    """
    if not frames:
        return {"overall_responsibility": "undetermined"}

    # Attribute crash frame
    crash_frame = frames[0] if frames else {}
    crash_module = crash_frame.get("module") or ""
    crash_path = crash_frame.get("resolved_file") or crash_frame.get("image_path") or ""
    crash_attr = attribute_responsibility(crash_module, crash_path, platform)

    # Find first app frame
    first_app_attr: Optional[ResponsibilityAttribution] = None
    for f in frames[1:]:
        module = f.get("module") or ""
        path = f.get("resolved_file") or f.get("image_path") or ""
        attr = attribute_responsibility(module, path, platform)
        if attr.responsibility == "application":
            first_app_attr = attr
            break

    # Overall responsibility: prefer application if found
    if first_app_attr:
        overall = "application"
    elif crash_attr.responsibility != "system":
        overall = crash_attr.responsibility
    else:
        overall = "system"

    result: Dict[str, Any] = {
        "crash_frame_attribution": {
            "module": crash_attr.module,
            "responsibility": crash_attr.responsibility,
            "reasoning": crash_attr.reasoning,
            "confidence": crash_attr.confidence,
        },
        "overall_responsibility": overall,
    }
    if first_app_attr:
        result["first_app_attribution"] = {
            "module": first_app_attr.module,
            "responsibility": first_app_attr.responsibility,
            "reasoning": first_app_attr.reasoning,
            "confidence": first_app_attr.confidence,
        }

    return result
