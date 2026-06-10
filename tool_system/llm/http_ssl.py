#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTPS / SSL 辅助：urllib 统一 certifi CA，联通性预检与错误分类。"""

from __future__ import annotations

import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

_SSL_PROBE_URLS: Tuple[str, ...] = (
    "https://example.com",
    "https://www.baidu.com",
)

_URLLIB_REQUEST_FORMATS = frozenset(
    {
        "anthropic_messages_compatible",
        "openai_responses_compatible",
    }
)


def uses_urllib_transport(request_format: str) -> bool:
    """当前 request_format 是否走 urllib（而非 OpenAI SDK / httpx）。"""
    fmt = str(request_format or "openai_chat_completions_compatible").strip().lower()
    return fmt in _URLLIB_REQUEST_FORMATS


def get_urllib_ssl_context() -> ssl.SSLContext:
    """构建 urllib 使用的 SSL 上下文；优先 certifi CA bundle。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def urllib_urlopen(request: urllib.request.Request, **kwargs):
    """urllib.request.urlopen 包装：默认注入 certifi SSL context。"""
    if "context" not in kwargs:
        kwargs["context"] = get_urllib_ssl_context()
    return urllib.request.urlopen(request, **kwargs)


def is_ssl_certificate_error(exc: BaseException) -> bool:
    """判断异常是否为本机 SSL/CA 证书校验失败。"""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    text = str(exc).lower()
    markers = (
        "certificate verify failed",
        "ssl: certificate_verify_failed",
        "sslcertverificationerror",
        "unable to get local issuer certificate",
    )
    return any(marker in text for marker in markers)


def _probe_https_with_urllib(url: str, timeout: float) -> None:
    req = urllib.request.Request(url, method="HEAD")
    try:
        urllib_urlopen(req, timeout=timeout)
    except urllib.error.HTTPError:
        # HTTP 响应说明 TLS 握手与证书校验已通过。
        return


def _probe_https_with_httpx(url: str, timeout: float) -> None:
    import httpx

    try:
        httpx.head(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPStatusError:
        return


def precheck_https_ssl_environment(*, use_urllib: bool, timeout: float = 5.0) -> None:
    """同栈 HTTPS 快检：仅 SSL 证书类错误向上抛出，其它网络问题忽略。"""
    last_exc: Optional[BaseException] = None
    for url in _SSL_PROBE_URLS:
        try:
            if use_urllib:
                _probe_https_with_urllib(url, timeout)
            else:
                _probe_https_with_httpx(url, timeout)
            return
        except Exception as exc:
            if is_ssl_certificate_error(exc):
                raise
            last_exc = exc
    if last_exc is not None:
        return


def python_ssl_diagnostics() -> List[str]:
    """收集本机 Python / SSL 环境诊断信息（用于失败面板展示）。"""
    lines: List[str] = []
    lines.append(f"- Python: {sys.version.split()[0]} @ {sys.executable}")
    try:
        paths = ssl.get_default_verify_paths()
        cafile = paths.cafile or "（未设置）"
        capath = paths.capath or "（未设置）"
        lines.append(f"- ssl 默认 CA 文件: {cafile}")
        lines.append(f"- ssl 默认 CA 目录: {capath}")
    except Exception as exc:
        lines.append(f"- ssl 默认路径: 无法读取（{exc}）")
    try:
        import certifi

        lines.append(f"- certifi CA: {certifi.where()}")
    except ImportError:
        lines.append("- certifi: 未安装")
    return lines


@dataclass
class ConnectivityFailureInfo:
    category: str
    headline: str
    reason: str
    fix_steps: List[str] = field(default_factory=list)
    show_raw_by_default: bool = True


def classify_connectivity_failure(exc: BaseException) -> ConnectivityFailureInfo:
    """将联通性探测异常分类为用户可读信息。"""
    msg = str(exc)
    lower = msg.lower()

    if is_ssl_certificate_error(exc):
        return ConnectivityFailureInfo(
            category="ssl_environment",
            headline="本机 SSL 证书环境异常",
            reason=(
                "当前 Python 无法校验 HTTPS 证书，通常不是大模型密钥或 base_url 配置问题。"
                "多见于 macOS 官方 Python 未安装系统 CA，或企业代理证书未导入信任库。"
            ),
            fix_steps=[
                "macOS 官方 Python：运行安装目录下「Install Certificates.command」",
                "或改用 Homebrew / pyenv 等已配置 CA 的 Python 环境",
                "企业内网：向 IT 索取并导入公司根证书到系统或 Python 信任库",
                "详见 docs/cli/INSTALL_TROUBLESHOOTING.md 中 SSL 小节",
            ],
            show_raw_by_default=False,
        )

    if "404" in lower:
        return ConnectivityFailureInfo(
            category="not_found",
            headline="联通性检测失败",
            reason="接口路径可能错误（404），请检查 base_url 是否包含正确的 API 路径。",
        )
    if "401" in lower or "403" in lower:
        return ConnectivityFailureInfo(
            category="auth",
            headline="联通性检测失败",
            reason="鉴权失败（401/403），请检查 API 密钥、Authorization 及账号权限。",
        )
    if "timeout" in lower or "timed out" in lower:
        return ConnectivityFailureInfo(
            category="timeout",
            headline="联通性检测失败",
            reason="请求超时，可能是网络较慢、网关限流或 base_url 不可达。",
        )
    if "name or service not known" in lower or "nodename nor servname" in lower:
        return ConnectivityFailureInfo(
            category="dns",
            headline="联通性检测失败",
            reason="域名解析失败，请检查 base_url 域名、DNS 或是否需要内网/VPN。",
        )

    return ConnectivityFailureInfo(
        category="request",
        headline="联通性检测失败",
        reason="请求失败，请检查 base_url、网络与网关配置。",
    )
