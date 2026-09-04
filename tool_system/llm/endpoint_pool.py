#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM endpoint discovery, health cache, and short probes."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .capability_registry import score_model

logger = logging.getLogger(__name__)

HEALTHY_TTL_SEC = 600
UNREACHABLE_TTL_SEC = 60
AUTH_FAILED_TTL_SEC = 1800
PROBE_TIMEOUT_SEC = 5

_CACHE_LOCK = threading.RLock()
_MEMORY_HEALTH: Dict[str, Dict[str, Any]] = {}


def _is_placeholder_secret(value: Any) -> bool:
    secret = str(value or "").strip()
    if not secret:
        return True
    if secret in {
        "YOUR_ANTHROPIC_API_KEY",
        "YOUR_OPENAI_API_KEY",
        "YOUR_API_KEY",
        "YOUR_DEEPSEEK_API_KEY",
        "YOUR_ZHIPU_API_KEY",
        "YOUR_BAIDU_QIANFAN_AUTHORIZATION",
    }:
        return True
    return secret.startswith("YOUR_")


def _cache_path() -> Path:
    return Path.home() / ".cache" / "stability-analysis-agent" / "llm_health.json"


def _endpoint_id(provider_key: str, model: str) -> str:
    return f"{provider_key}::{model}"


@dataclass
class LLMEndpoint:
    """One configured LLM endpoint (provider key + model)."""

    provider_key: str
    model: str
    adapter_provider: str
    base_url: str
    auth_type: str
    secret: str
    request_format: str
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    request_timeout: int = 180
    stream: Optional[bool] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    tier: str = "default"
    score: int = 50
    health_status: str = "unknown"  # healthy|unreachable|auth_failed|rate_limited|unknown
    health_error: Optional[str] = None
    health_latency_ms: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint_id(self) -> str:
        return _endpoint_id(self.provider_key, self.model)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider_key,
            "model": self.model,
            "adapter_provider": self.adapter_provider,
            "tier": self.tier,
            "score": self.score,
            "status": self.health_status,
            "error": self.health_error,
            "latency_ms": self.health_latency_ms,
            "request_format": self.request_format,
        }


def resolve_provider_secret(provider_key: str, provider_cfg: Dict[str, Any]) -> tuple:
    """Return (auth_type, secret) resolving env over config."""
    auth_type = str(provider_cfg.get("auth_type") or "").strip().lower()
    if not auth_type:
        if provider_key == "baidu_qianfan" or provider_cfg.get("authorization"):
            auth_type = "authorization"
        else:
            auth_type = "api_key"

    env_candidates: List[str] = []
    custom_env = provider_cfg.get("api_key_env")
    if isinstance(custom_env, list):
        env_candidates.extend([str(x).strip() for x in custom_env if str(x).strip()])
    elif isinstance(custom_env, str) and custom_env.strip():
        env_candidates.extend([x.strip() for x in custom_env.split(",") if x.strip()])

    if provider_key == "zhipu_bigmodel":
        env_candidates.extend(["ZHIPU_API_KEY", "BIGMODEL_API_KEY"])
    elif provider_key == "baidu_qianfan":
        env_candidates.append("BAIDU_QIANFAN_AUTHORIZATION")
    elif provider_key == "openai":
        env_candidates.append("OPENAI_API_KEY")
    elif provider_key == "claude":
        env_candidates.append("ANTHROPIC_API_KEY")
    elif provider_key == "deepseek":
        env_candidates.append("DEEPSEEK_API_KEY")
    else:
        env_candidates.append(f"{provider_key.upper()}_API_KEY")

    env_secret = next((os.getenv(k) for k in env_candidates if os.getenv(k)), None)
    if auth_type == "authorization":
        config_secret = provider_cfg.get("authorization")
    elif auth_type == "none":
        config_secret = ""
    else:
        config_secret = provider_cfg.get("api_key")
    secret = env_secret or config_secret
    return auth_type, str(secret) if secret is not None else ""


def _normalize_base_url(base_url: Optional[str]) -> str:
    if not isinstance(base_url, str):
        return ""
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    return normalized


def _adapter_provider_for(provider_key: str, provider_cfg: Dict[str, Any]) -> str:
    adapter = str(provider_cfg.get("adapter_provider") or "").strip()
    if adapter:
        return adapter
    if provider_key == "baidu_qianfan":
        return "baidu_qianfan"
    if provider_key == "deepseek":
        return "deepseek"
    if provider_key == "claude":
        return "openai"  # anthropic uses request_format, adapter still openai-compatible path or anthropic
    return "openai"


def discover_candidates(llm_config: Dict[str, Any]) -> List[LLMEndpoint]:
    """Statically discover configured providers with real secrets (no network)."""
    if not isinstance(llm_config, dict):
        return []
    providers = llm_config.get("providers") or {}
    if not isinstance(providers, dict):
        return []
    defaults = llm_config.get("provider_defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}

    default_models = {
        "openai": "gpt-4o",
        "zhipu_bigmodel": "glm-4",
        "deepseek": "deepseek-chat",
        "baidu_qianfan": "ernie-4.0-8k",
        "claude": "claude-sonnet-4-20250514",
        "qwen": "qwen-plus",
        "kimi": "moonshot-v1-8k",
        "minimax": "MiniMax-M2.5",
    }
    default_base_urls = {
        "zhipu_bigmodel": "https://open.bigmodel.cn/api/paas/v4",
        "deepseek": "https://api.deepseek.com/v1",
        "baidu_qianfan": "https://qianfan.baidubce.com/v2",
        "openai": "https://api.openai.com/v1",
        "claude": "https://api.anthropic.com/v1/messages",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "kimi": "https://api.moonshot.cn/v1",
        "minimax": "https://api.minimaxi.com/v1",
    }

    out: List[LLMEndpoint] = []
    for provider_key, raw_cfg in providers.items():
        if not isinstance(provider_key, str) or not provider_key.strip():
            continue
        if not isinstance(raw_cfg, dict):
            continue
        merged = {**defaults, **raw_cfg}
        request_format = str(
            merged.get("request_format") or "openai_chat_completions_compatible"
        ).strip().lower()
        if request_format == "custom_unsupported_need_adapter":
            continue

        auth_type, secret = resolve_provider_secret(provider_key, merged)
        if auth_type != "none" and _is_placeholder_secret(secret):
            continue

        model = str(merged.get("model") or default_models.get(provider_key) or "").strip()
        if not model:
            continue
        base_url = _normalize_base_url(
            merged.get("base_url") or default_base_urls.get(provider_key)
        )
        if not base_url:
            continue

        tier, score = score_model(provider_key, model)
        adapter_provider = _adapter_provider_for(provider_key, merged)
        stream_val = merged.get("stream")
        if "auth_prefix" in merged:
            auth_prefix = str(merged.get("auth_prefix"))
        elif "auth_prefix" in defaults:
            auth_prefix = str(defaults.get("auth_prefix"))
        elif request_format.startswith("anthropic"):
            auth_prefix = ""
        else:
            auth_prefix = "Bearer "
        auth_header = str(merged.get("auth_header") or "Authorization").strip() or "Authorization"
        if request_format.startswith("anthropic") and "auth_header" not in merged and "auth_header" not in defaults:
            auth_header = "x-api-key"
        ep = LLMEndpoint(
            provider_key=provider_key,
            model=model,
            adapter_provider=adapter_provider,
            base_url=base_url,
            auth_type=auth_type,
            secret=secret if auth_type != "none" else "",
            request_format=request_format,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            request_timeout=int(merged.get("request_timeout") or merged.get("timeout") or 180),
            stream=bool(stream_val) if stream_val is not None else None,
            temperature=float(merged.get("temperature") or 0.7),
            max_tokens=int(merged.get("max_tokens") or 4096),
            tier=tier,
            score=score,
        )
        out.append(ep)
    return out


def _load_disk_health() -> Dict[str, Dict[str, Any]]:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_disk_health(data: Dict[str, Dict[str, Any]]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("failed to write llm health cache: %s", exc)


def get_cached_health(endpoint_id: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _CACHE_LOCK:
        entry = _MEMORY_HEALTH.get(endpoint_id)
        if entry is None:
            disk = _load_disk_health()
            entry = disk.get(endpoint_id)
            if entry:
                _MEMORY_HEALTH[endpoint_id] = entry
        if not entry:
            return None
        status = str(entry.get("status") or "unknown")
        checked_at = float(entry.get("checked_at") or 0)
        if status == "healthy":
            ttl = HEALTHY_TTL_SEC
        elif status == "auth_failed":
            ttl = AUTH_FAILED_TTL_SEC
        else:
            ttl = UNREACHABLE_TTL_SEC
        if now - checked_at > ttl:
            return None
        return dict(entry)


def set_cached_health(
    endpoint_id: str,
    *,
    status: str,
    error: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> None:
    entry = {
        "status": status,
        "error": error,
        "latency_ms": latency_ms,
        "checked_at": time.time(),
    }
    with _CACHE_LOCK:
        _MEMORY_HEALTH[endpoint_id] = entry
        disk = _load_disk_health()
        disk[endpoint_id] = entry
        _save_disk_health(disk)


def clear_health_cache() -> None:
    with _CACHE_LOCK:
        _MEMORY_HEALTH.clear()
        try:
            path = _cache_path()
            if path.is_file():
                path.unlink()
        except OSError:
            pass


def endpoint_to_adapter_dict(ep: LLMEndpoint, *, engine: str = "direct", probe: bool = False) -> Dict[str, Any]:
    """Build LLMAdapterFactory config dict from an endpoint."""
    if engine not in {"direct", "langchain", "langgraph"}:
        raise ValueError("engine must be one of: direct, langchain, langgraph")
    timeout = PROBE_TIMEOUT_SEC if probe else int(ep.request_timeout or 120)
    if probe:
        timeout = max(1, min(PROBE_TIMEOUT_SEC, timeout))
    extra: Dict[str, Any] = {
        "request_format": ep.request_format,
        "auth_header": ep.auth_header,
        "auth_prefix": ep.auth_prefix,
        "config_provider_key": ep.provider_key,
    }
    if ep.stream is not None:
        extra["stream"] = ep.stream
    if ep.adapter_provider == "deepseek":
        extra["deepseek_api_key"] = ep.secret
        extra["deepseek_base_url"] = ep.base_url
    if ep.auth_type == "authorization":
        extra["authorization"] = ep.secret
        if ep.provider_key == "baidu_qianfan":
            extra["baidu_qianfan_authorization"] = ep.secret

    cfg: Dict[str, Any] = {
        "engine": engine if engine in ("direct", "langchain", "langgraph") else "direct",
        "provider": ep.adapter_provider,
        "model": ep.model,
        "api_key": ep.secret if ep.auth_type != "authorization" else None,
        "base_url": ep.base_url,
        "timeout": timeout,
        "temperature": 0.0 if probe else ep.temperature,
        "max_tokens": 16 if probe else ep.max_tokens,
    }
    cfg.update(extra)
    return cfg


def probe_endpoint(ep: LLMEndpoint, *, engine: str = "direct") -> LLMEndpoint:
    """Short connectivity probe; updates ep.health_* and disk cache."""
    cached = get_cached_health(ep.endpoint_id)
    if cached:
        ep.health_status = str(cached.get("status") or "unknown")
        ep.health_error = cached.get("error")
        ep.health_latency_ms = cached.get("latency_ms")
        return ep

    from .llm_adapter import LLMAdapterFactory

    adapter_cfg = endpoint_to_adapter_dict(ep, engine="direct", probe=True)
    started = time.perf_counter()
    try:
        adapter = LLMAdapterFactory.create(adapter_cfg)
        adapter.chat(
            [{"role": "user", "content": "pong"}],
            max_tokens=16,
            temperature=0.0,
        )
        latency = int(round((time.perf_counter() - started) * 1000))
        ep.health_status = "healthy"
        ep.health_error = None
        ep.health_latency_ms = latency
        set_cached_health(ep.endpoint_id, status="healthy", latency_ms=latency)
    except Exception as exc:
        latency = int(round((time.perf_counter() - started) * 1000))
        msg = str(exc)
        lower = msg.lower()
        if any(x in lower for x in ("401", "403", "unauthorized", "forbidden", "invalid api", "authentication")):
            status = "auth_failed"
        elif "429" in lower or "rate" in lower:
            status = "rate_limited"
        else:
            status = "unreachable"
        ep.health_status = status
        ep.health_error = msg[:300]
        ep.health_latency_ms = latency
        set_cached_health(ep.endpoint_id, status=status, error=ep.health_error, latency_ms=latency)
        logger.info("LLM probe %s -> %s: %s", ep.endpoint_id, status, ep.health_error)
    return ep


def probe_candidates(
    candidates: List[LLMEndpoint],
    *,
    parallel: bool = True,
    engine: str = "direct",
) -> List[LLMEndpoint]:
    if not candidates:
        return []
    if not parallel or len(candidates) == 1:
        return [probe_endpoint(ep, engine=engine) for ep in candidates]

    results: Dict[str, LLMEndpoint] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
        futures = {pool.submit(probe_endpoint, ep, engine=engine): ep.endpoint_id for ep in candidates}
        for fut in as_completed(futures):
            eid = futures[fut]
            try:
                results[eid] = fut.result()
            except Exception as exc:
                logger.warning("probe future failed for %s: %s", eid, exc)
    # Preserve order
    out: List[LLMEndpoint] = []
    for ep in candidates:
        out.append(results.get(ep.endpoint_id, ep))
    return out


def mark_endpoint_unhealthy(ep: LLMEndpoint, *, error: str, status: str = "unreachable") -> None:
    ep.health_status = status
    ep.health_error = (error or "")[:300]
    set_cached_health(ep.endpoint_id, status=status, error=ep.health_error)


def mark_endpoint_healthy(ep: LLMEndpoint, *, latency_ms: Optional[int] = None) -> None:
    ep.health_status = "healthy"
    ep.health_error = None
    if latency_ms is not None:
        ep.health_latency_ms = latency_ms
    set_cached_health(ep.endpoint_id, status="healthy", latency_ms=latency_ms)
