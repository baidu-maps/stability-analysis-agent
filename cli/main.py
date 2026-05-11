#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一 CLI 入口（Tool System only）。

设计目标：
- 仅使用 Tool/Workflow 注册机制执行分析；
- 支持第三方通过模块扩展注册表；
- 作为唯一命令行入口。

可编程调用（闭源包装器、自动化脚本）：见同包 `cli.api`，例如
`execute_analysis`、`collect_interactive_run_state`、`interactive_state_to_argv`。
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import termios
import time
import tty
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 支持从任意 cwd 运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    # Avoid importing urllib3 here; importing it can itself trigger the warning.
    warnings.filterwarnings(
        "ignore",
        message=r".*urllib3 v2 only supports OpenSSL.*",
        module=r"urllib3\..*",
    )
except Exception:
    pass
warnings.filterwarnings(
    "ignore",
    message=r".*urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)
warnings.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")

from tool_system import (  # type: ignore
    ToolAndWorkflowRegistry,
    SystemConfig,
    LLMConfig,
    ToolConfig,
    WorkflowConfig,
    ConfigDrivenExecutor,
    LLMAdapterFactory,
    register_all_tools_and_workflows,
)

# RAG（chromadb / sentence-transformers 等）仅在向量库子命令需要时加载，避免 `sa-agent` 首屏被拖慢。
_rag_runtime_resolved = False
RAG_RUNTIME_AVAILABLE = False
AIStabilityAnalyzerWithVectorDB = None  # type: ignore
init_vector_rules = None  # type: ignore
init_vector_patterns = None  # type: ignore
init_vector_evidence = None  # type: ignore
init_vector_strategies = None  # type: ignore
init_vector_guidance_blocks = None  # type: ignore


def _ensure_rag_runtime_loaded() -> None:
    global _rag_runtime_resolved, RAG_RUNTIME_AVAILABLE
    global AIStabilityAnalyzerWithVectorDB, init_vector_rules, init_vector_patterns
    global init_vector_evidence, init_vector_strategies, init_vector_guidance_blocks
    if _rag_runtime_resolved:
        return
    _rag_runtime_resolved = True
    try:
        from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB as _RagAnalyzer
        from rag.init_vector_db_data import (
            init_rules as _init_vector_rules,
            init_patterns as _init_vector_patterns,
            init_evidence as _init_vector_evidence,
            init_strategies as _init_vector_strategies,
            init_guidance_blocks as _init_vector_guidance_blocks,
        )

        AIStabilityAnalyzerWithVectorDB = _RagAnalyzer
        init_vector_rules = _init_vector_rules
        init_vector_patterns = _init_vector_patterns
        init_vector_evidence = _init_vector_evidence
        init_vector_strategies = _init_vector_strategies
        init_vector_guidance_blocks = _init_vector_guidance_blocks
        RAG_RUNTIME_AVAILABLE = True
    except ImportError:
        AIStabilityAnalyzerWithVectorDB = None
        init_vector_rules = None
        init_vector_patterns = None
        init_vector_evidence = None
        init_vector_strategies = None
        init_vector_guidance_blocks = None
        RAG_RUNTIME_AVAILABLE = False


def _read_crash_log(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return Path(path).read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                return Path(path).read_text(encoding="utf-16")
            except UnicodeDecodeError:
                raw = Path(path).read_bytes()
                return raw.decode("utf-8", errors="ignore")


def _normalize_code_roots(raw_roots: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for r in raw_roots or []:
        if not r or not str(r).strip():
            continue
        ar = str(Path(r).expanduser().resolve())
        if ar not in seen:
            seen.add(ar)
            out.append(ar)
    return out


def _user_config_dir() -> Path:
    override = os.environ.get("STABILITY_AGENT_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".config" / "stability-analysis-agent").resolve()


def _runtime_output_root() -> Path:
    """
    Runtime output base directory.
    Default to current working directory so pip-installed CLI writes
    reports near where the user runs commands.
    """
    return Path.cwd().resolve()


def _load_agent_config_file() -> dict:
    target = _user_agent_config_file()
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_llm_config_from_agent_config(engine: str) -> Optional[LLMConfig]:
    cfg = _load_agent_config_file()
    llm_cfg = cfg.get("llm_config", {}) if isinstance(cfg, dict) else {}
    providers = llm_cfg.get("providers", {}) if isinstance(llm_cfg, dict) else {}
    if not isinstance(providers, dict):
        return None
    provider_defaults = llm_cfg.get("provider_defaults", {}) if isinstance(llm_cfg, dict) else {}
    if not isinstance(provider_defaults, dict):
        provider_defaults = {}

    active_provider = llm_cfg.get("active_provider", "openai")
    provider_cfg = {**provider_defaults, **(providers.get(active_provider, {}) or {})}
    if not isinstance(provider_cfg, dict):
        return None

    mapped_engine = engine if engine in ("direct", "langchain", "langgraph") else "direct"

    default_models = {
        "openai": "gpt-4o",
        "zhipu_bigmodel": "glm-4",
        "deepseek": "deepseek-chat",
        "baidu_qianfan": "ernie-4.0-8k",
    }
    default_base_urls = {
        "zhipu_bigmodel": "https://open.bigmodel.cn/api/paas/v4",
        "deepseek": "https://api.deepseek.com/v1",
        "baidu_qianfan": "https://qianfan.baidubce.com/v2",
    }

    auth_type = str(provider_cfg.get("auth_type") or "").strip().lower()
    if not auth_type:
        auth_type = "authorization" if (active_provider == "baidu_qianfan" or provider_cfg.get("authorization")) else "api_key"

    if auth_type == "authorization":
        secret = provider_cfg.get("authorization")
    else:
        secret = provider_cfg.get("api_key")
    if _is_placeholder_secret(secret):
        return None

    model = provider_cfg.get("model") or default_models.get(active_provider, "gpt-4o")
    base_url = provider_cfg.get("base_url") or default_base_urls.get(active_provider)
    adapter_provider = str(provider_cfg.get("adapter_provider") or "").strip()
    if not adapter_provider:
        if active_provider == "baidu_qianfan":
            adapter_provider = "baidu_qianfan"
        elif active_provider == "deepseek":
            adapter_provider = "deepseek"
        else:
            adapter_provider = "openai"

    # Prevent duplicated "/chat/completions/chat/completions" in OpenAI-style clients.
    if isinstance(base_url, str):
        normalized_base_url = base_url.strip().rstrip("/")
        if normalized_base_url.endswith("/chat/completions"):
            normalized_base_url = normalized_base_url[: -len("/chat/completions")]
        base_url = normalized_base_url

    extra: Dict[str, Any] = {}
    if adapter_provider == "deepseek":
        extra["deepseek_api_key"] = secret
        extra["deepseek_base_url"] = base_url or "https://api.deepseek.com/v1"
    if auth_type == "authorization":
        extra["authorization"] = secret
        if active_provider == "baidu_qianfan":
            extra["baidu_qianfan_authorization"] = secret

    request_format = str(provider_cfg.get("request_format") or "openai_chat_completions_compatible").strip().lower()
    extra["request_format"] = request_format
    extra["auth_header"] = str(provider_cfg.get("auth_header") or "Authorization").strip() or "Authorization"
    extra["auth_prefix"] = str(provider_cfg.get("auth_prefix") or "")

    return LLMConfig(
        engine=mapped_engine,
        provider=adapter_provider,
        model=model,
        api_key=secret,
        base_url=base_url,
        timeout=int(provider_cfg.get("request_timeout") or provider_cfg.get("timeout") or 120),
        temperature=float(provider_cfg.get("temperature") or 0.7),
        max_tokens=int(provider_cfg.get("max_tokens") or 4096),
        extra=extra,
    )


_LLM_REQUEST_TIMEOUT_MIN_SECONDS = 30
_LLM_REQUEST_TIMEOUT_DEFAULT_SECONDS = 120


def _merge_llm_provider_config_for_timeout() -> Dict[str, Any]:
    """与 _build_llm_config_from_agent_config 相同的 defaults + active provider 合并方式，用于读写超时。"""
    cfg = _load_agent_config_file()
    llm_cfg = cfg.get("llm_config", {}) if isinstance(cfg, dict) else {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    provider_defaults = llm_cfg.get("provider_defaults", {})
    if not isinstance(provider_defaults, dict):
        provider_defaults = {}
    active_provider = str(llm_cfg.get("active_provider") or "openai").strip() or "openai"
    providers = llm_cfg.get("providers", {})
    if not isinstance(providers, dict):
        providers = {}
    provider_cfg = {**provider_defaults, **(providers.get(active_provider, {}) or {})}
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}
    return provider_cfg


def _effective_llm_request_timeout_seconds() -> int:
    """与 LLMConfig.timeout 一致：request_timeout 优先，否则 timeout，否则 120。"""
    merged = _merge_llm_provider_config_for_timeout()
    return int(merged.get("request_timeout") or merged.get("timeout") or _LLM_REQUEST_TIMEOUT_DEFAULT_SECONDS)


def _persist_llm_request_timeout_seconds(seconds: int) -> None:
    """写入 agent_config.local.json：更新 provider_defaults 与当前 active provider，避免仅改 defaults 仍被 per-provider 覆盖。"""
    target = _user_agent_config_file()
    data = _load_json_or_empty(target)
    llm_cfg = data.get("llm_config")
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
        data["llm_config"] = llm_cfg

    defaults = llm_cfg.get("provider_defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        llm_cfg["provider_defaults"] = defaults
    defaults["request_timeout"] = int(seconds)

    active = str(llm_cfg.get("active_provider") or "").strip()
    providers = llm_cfg.get("providers")
    if active and isinstance(providers, dict) and active in providers:
        node = providers.get(active)
        if isinstance(node, dict):
            node["request_timeout"] = int(seconds)
        else:
            providers[active] = {"request_timeout": int(seconds)}

    _write_json_pretty(target, data)


def _configure_llm_request_timeout_interactive() -> None:
    current = _effective_llm_request_timeout_seconds()
    print("")
    print(_yellow("调整大模型请求超时时间"))
    print("说明：此处为调用大模型 API 时，单次 HTTP 请求的最长等待时间。")
    print(f"单位：秒。请输入不小于 {_LLM_REQUEST_TIMEOUT_MIN_SECONDS} 的整数；不能为负数。")
    print("直接回车：不修改并返回上一级。")
    print("")
    while True:
        raw = _safe_input(f"请输入超时秒数（当前 {current}，回车取消）: ").strip()
        if raw == "__EOF__":
            return
        if not raw:
            return
        try:
            val = int(raw, 10)
        except ValueError:
            print(_red("请输入整数秒数（例如 120），不要包含小数或其他字符。"))
            continue
        if val < 0:
            print(_red("超时不能为负数，请重新输入。"))
            continue
        if val < _LLM_REQUEST_TIMEOUT_MIN_SECONDS:
            print(
                _red(
                    f"超时过短容易导致分析失败，请设为不小于 {_LLM_REQUEST_TIMEOUT_MIN_SECONDS} 秒。"
                )
            )
            continue
        _persist_llm_request_timeout_seconds(val)
        print(_green(f"已保存：大模型请求超时时间为 {val} 秒。"))
        return


def _bundled_config_dir() -> Path:
    return PROJECT_ROOT / "tools" / "configs"


def _user_agent_config_file() -> Path:
    return _user_config_dir() / "agent_config.local.json"


def _user_add2line_config_file() -> Path:
    return _user_config_dir() / "add2line_resolver_config.local.json"


def _profile_dir() -> Path:
    return _user_config_dir() / "profiles"


def _session_state_file() -> Path:
    return _user_config_dir() / "session_state.json"


def _ensure_user_config_templates() -> None:
    config_dir = _user_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    template_map = {
        "agent_config.local.example.json": _user_agent_config_file(),
        "add2line_resolver_config.local.example.json": _user_add2line_config_file(),
    }
    for src_name, dest in template_map.items():
        if dest.exists():
            continue
        src = _bundled_config_dir() / src_name
        if src.exists():
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _detect_add2line_tools() -> Dict[str, Optional[str]]:
    status = _detect_add2line_tool_status()
    return {name: (meta.get("path") or None) for name, meta in status.items()}


def _candidate_tool_dirs_from_config() -> List[Path]:
    cfg = _load_json_or_empty(_user_add2line_config_file())
    out: List[Path] = []
    seen: set = set()

    def _push(p: str) -> None:
        if not p:
            return
        rp = str(Path(p).expanduser().resolve())
        if rp in seen:
            return
        seen.add(rp)
        out.append(Path(rp))

    platforms = cfg.get("platforms", {}) if isinstance(cfg, dict) else {}
    if isinstance(platforms, dict):
        for _, platform_cfg in platforms.items():
            if not isinstance(platform_cfg, dict):
                continue
            tool_paths = platform_cfg.get("tool_paths", [])
            if isinstance(tool_paths, list):
                for item in tool_paths:
                    if isinstance(item, str) and item.strip():
                        _push(item)

    global_cfg = cfg.get("global", {}) if isinstance(cfg, dict) else {}
    if isinstance(global_cfg, dict):
        tool_paths = global_cfg.get("tool_paths", [])
        if isinstance(tool_paths, list):
            for item in tool_paths:
                if isinstance(item, str) and item.strip():
                    _push(item)
    return out


def _candidate_tool_dirs_from_env_with_source() -> List[Tuple[Path, List[str]]]:
    out: List[Tuple[Path, List[str]]] = []
    seen_to_idx: Dict[str, int] = {}

    def _push(p: str, keys: List[str]) -> None:
        if not p:
            return
        rp = str(Path(p).expanduser().resolve())
        if rp in seen_to_idx:
            existing = out[seen_to_idx[rp]][1]
            for k in keys:
                if k not in existing:
                    existing.append(k)
            return
        out.append((Path(rp), list(keys)))
        seen_to_idx[rp] = len(out) - 1

    ndk_home = os.environ.get("ANDROID_NDK_HOME", "").strip()
    if ndk_home:
        base = Path(ndk_home).expanduser().resolve()
        ndk_keys = ["ANDROID_NDK_HOME"]
        _push(str(base / "toolchains" / "llvm" / "prebuilt" / "darwin-x86_64" / "bin"), ndk_keys)
        _push(str(base / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64" / "bin"), ndk_keys)
        _push(str(base / "toolchains" / "llvm" / "prebuilt" / "windows-x86_64" / "bin"), ndk_keys)
        _push(str(base / "toolchains" / "llvm" / "prebuilt" / "windows" / "bin"), ndk_keys)

    llvm_home = os.environ.get("LLVM_HOME", "").strip()
    if llvm_home:
        base = Path(llvm_home).expanduser().resolve()
        llvm_keys = ["LLVM_HOME"]
        _push(str(base / "bin"), llvm_keys)
        _push(str(base), llvm_keys)

    sdk_keys: List[str] = []
    if os.environ.get("ANDROID_SDK_HOME", "").strip():
        sdk_keys.append("ANDROID_SDK_HOME")
    if os.environ.get("ANDROID_HOME", "").strip():
        sdk_keys.append("ANDROID_HOME")
    android_sdk_home = os.environ.get("ANDROID_SDK_HOME", "").strip() or os.environ.get("ANDROID_HOME", "").strip()
    if android_sdk_home and sdk_keys:
        _push(
            str(
                Path(android_sdk_home).expanduser().resolve()
                / "ndk-bundle" / "toolchains" / "llvm" / "prebuilt" / "darwin-x86_64" / "bin"
            ),
            sdk_keys,
        )
    return out


def _candidate_tool_dirs_from_env() -> List[Path]:
    return [d for d, _ in _candidate_tool_dirs_from_env_with_source()]


def _version_key(name: str) -> Tuple[int, ...]:
    """从目录名中抽取整数序列作为版本号排序键。

    例如 "27.0.12077973" → (27, 0, 12077973)，"21.4.7075529" → (21, 4, 7075529)。
    无法解析时返回 (0,)，确保排序稳定。
    """
    parts = re.findall(r"\d+", str(name or ""))
    if not parts:
        return (0,)
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


def _push_unique_dir(
    out: List[Tuple[Path, List[str]]],
    seen: Dict[str, int],
    bin_dir: Path,
    labels: List[str],
) -> None:
    """统一去重：同一物理路径仅保留一份，多个标签合并到 labels 列表。"""
    try:
        if not bin_dir.exists():
            return
        rp = str(bin_dir.expanduser().resolve())
    except Exception:
        return
    if rp in seen:
        existing_labels = out[seen[rp]][1]
        for lbl in labels:
            if lbl and lbl not in existing_labels:
                existing_labels.append(lbl)
        return
    out.append((Path(rp), [lbl for lbl in labels if lbl]))
    seen[rp] = len(out) - 1


def _android_studio_ndk_dirs() -> List[Tuple[Path, List[str]]]:
    """探测 Android Studio 默认安装的 NDK 工具链 bin 目录（多版本，按版本号倒序）。"""
    home = Path.home()
    sdk_roots: List[Path] = [
        home / "Library" / "Android" / "sdk",  # macOS
        home / "Android" / "Sdk",              # Linux 通用
    ]
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            sdk_roots.append(Path(local_app) / "Android" / "Sdk")
        userprofile = os.environ.get("USERPROFILE", "").strip()
        if userprofile:
            sdk_roots.append(Path(userprofile) / "AppData" / "Local" / "Android" / "Sdk")

    out: List[Tuple[Path, List[str]]] = []
    seen: Dict[str, int] = {}
    host_dirs = (
        "darwin-x86_64",
        "darwin-arm64",
        "linux-x86_64",
        "windows-x86_64",
    )

    for sdk in sdk_roots:
        if not sdk.exists() or not sdk.is_dir():
            continue
        ndk_root = sdk / "ndk"
        if ndk_root.exists() and ndk_root.is_dir():
            try:
                ver_dirs = sorted(
                    (p for p in ndk_root.iterdir() if p.is_dir()),
                    key=lambda p: _version_key(p.name),
                    reverse=True,
                )
            except Exception:
                ver_dirs = []
            for ver_dir in ver_dirs:
                # NDK 版本根目录：包含 ndk-stack / ndk-build / ndk-gdb / ndk-lldb / ndk-which 等包装脚本
                _push_unique_dir(
                    out, seen, ver_dir,
                    [f"Android Studio NDK ({ver_dir.name})"],
                )
                for host in host_dirs:
                    bin_dir = ver_dir / "toolchains" / "llvm" / "prebuilt" / host / "bin"
                    _push_unique_dir(
                        out, seen, bin_dir,
                        [f"Android Studio NDK ({ver_dir.name})"],
                    )

        ndk_bundle = sdk / "ndk-bundle"
        if ndk_bundle.exists() and ndk_bundle.is_dir():
            # ndk-bundle 根目录同样含 ndk-stack 等包装脚本
            _push_unique_dir(
                out, seen, ndk_bundle,
                ["Android Studio NDK (ndk-bundle)"],
            )
            for host in host_dirs:
                bin_dir = ndk_bundle / "toolchains" / "llvm" / "prebuilt" / host / "bin"
                _push_unique_dir(
                    out, seen, bin_dir,
                    ["Android Studio NDK (ndk-bundle)"],
                )
    return out


def _xcode_tool_dirs() -> List[Tuple[Path, List[str]]]:
    """探测 Xcode 工具链目录：优先 `xcrun --find` 与 `xcode-select -p`，并兼容 Command Line Tools。"""
    if sys.platform != "darwin":
        return []
    out: List[Tuple[Path, List[str]]] = []
    seen: Dict[str, int] = {}

    if shutil.which("xcrun"):
        for tool in ("atos", "llvm-addr2line", "llvm-symbolizer", "addr2line"):
            try:
                proc = subprocess.run(
                    ["xcrun", "--find", tool],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except Exception:
                continue
            if proc.returncode != 0:
                continue
            located = (proc.stdout or "").strip()
            if not located:
                continue
            _push_unique_dir(
                out, seen, Path(located).parent,
                [f"Xcode (xcrun --find {tool})"],
            )

    if shutil.which("xcode-select"):
        try:
            proc = subprocess.run(
                ["xcode-select", "-p"],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            proc = None
        if proc is not None and proc.returncode == 0:
            dev = (proc.stdout or "").strip()
            if dev:
                dev_path = Path(dev)
                _push_unique_dir(
                    out, seen,
                    dev_path / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin",
                    ["Xcode (xcode-select)"],
                )
                _push_unique_dir(
                    out, seen,
                    dev_path / "usr" / "bin",
                    ["Xcode Developer (xcode-select)"],
                )

    _push_unique_dir(
        out, seen,
        Path("/Library/Developer/CommandLineTools/usr/bin"),
        ["Xcode Command Line Tools"],
    )
    return out


def _openharmony_sdk_dirs() -> List[Tuple[Path, List[str]]]:
    """探测 DevEco Studio / OpenHarmony Native SDK 的 LLVM bin 目录（多版本 + 扁平结构）。"""
    home = Path.home()
    sdk_roots: List[Path] = [
        home / "Library" / "OpenHarmony" / "Sdk",   # macOS DevEco Studio
        home / "OpenHarmony" / "Sdk",                # Linux 通用
        home / ".deveco-studio" / "sdk",             # DevEco Studio 用户级缓存
    ]
    if os.name == "nt":
        userprofile = os.environ.get("USERPROFILE", "").strip()
        if userprofile:
            sdk_roots.append(Path(userprofile) / "OpenHarmony" / "Sdk")
        sdk_roots.append(Path("C:/Program Files/Huawei/OpenHarmony/Sdk"))
        sdk_roots.append(Path("C:/Program Files/OpenHarmony/Sdk"))

    for var in ("OHOS_SDK_HOME", "HOS_SDK_HOME", "OPENHARMONY_SDK_HOME", "DEVECO_HOME"):
        val = os.environ.get(var, "").strip()
        if val:
            sdk_roots.append(Path(val).expanduser())

    out: List[Tuple[Path, List[str]]] = []
    seen: Dict[str, int] = {}

    for root in sdk_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            ver_dirs = sorted(
                (p for p in root.iterdir() if p.is_dir() and (p / "native" / "llvm" / "bin").exists()),
                key=lambda p: _version_key(p.name),
                reverse=True,
            )
        except Exception:
            ver_dirs = []
        for ver_dir in ver_dirs:
            _push_unique_dir(
                out, seen,
                ver_dir / "native" / "llvm" / "bin",
                [f"OpenHarmony SDK ({ver_dir.name})"],
            )

        flat_bin = root / "native" / "llvm" / "bin"
        _push_unique_dir(
            out, seen, flat_bin,
            ["OpenHarmony SDK (flat layout)"],
        )
    return out


def _homebrew_llvm_versioned_dirs() -> List[Tuple[Path, List[str]]]:
    """枚举 Homebrew 的多版本 LLVM bin 目录（覆盖 `llvm` 与 `llvm@<n>`）。"""
    if sys.platform == "win32":
        return []
    out: List[Tuple[Path, List[str]]] = []
    seen: Dict[str, int] = {}
    for opt_root in (
        Path("/opt/homebrew/opt"),     # Apple Silicon
        Path("/usr/local/opt"),        # Intel macOS / Linuxbrew
        Path("/home/linuxbrew/.linuxbrew/opt"),
    ):
        if not opt_root.exists() or not opt_root.is_dir():
            continue
        try:
            entries = list(opt_root.iterdir())
        except Exception:
            entries = []
        for entry in entries:
            name = entry.name
            if name == "llvm" or name.startswith("llvm@"):
                _push_unique_dir(out, seen, entry / "bin", [f"Homebrew {name}"])
            elif name == "binutils" or name.startswith("binutils@"):
                _push_unique_dir(out, seen, entry / "bin", [f"Homebrew {name}"])
    return out


def _linux_llvm_distro_dirs() -> List[Tuple[Path, List[str]]]:
    """枚举 Linux 发行版包管理 LLVM 目录（apt: /usr/lib/llvm-NN，rpm: /usr/lib64/llvm）。"""
    if not sys.platform.startswith("linux"):
        return []
    out: List[Tuple[Path, List[str]]] = []
    seen: Dict[str, int] = {}
    for libdir in (Path("/usr/lib"), Path("/usr/lib64"), Path("/usr/local/lib")):
        if not libdir.exists() or not libdir.is_dir():
            continue
        try:
            entries = list(libdir.iterdir())
        except Exception:
            entries = []
        for entry in entries:
            name = entry.name
            if name == "llvm" or name.startswith("llvm-"):
                _push_unique_dir(out, seen, entry / "bin", [f"Linux Distro {name}"])
    return out


def _candidate_tool_dirs_from_ides() -> List[Tuple[Path, List[str]]]:
    """聚合所有 IDE / SDK 默认安装路径下的工具链 bin 目录。

    返回值与 `_candidate_tool_dirs_from_env_with_source` 同构：[(Path, [来源标签...])]，
    标签用于 UI 提示与配置写入注释（例如 "Android Studio NDK (27.0.12077973)"）。
    """
    out: List[Tuple[Path, List[str]]] = []
    seen: Dict[str, int] = {}
    for finder in (
        _xcode_tool_dirs,
        _android_studio_ndk_dirs,
        _openharmony_sdk_dirs,
        _homebrew_llvm_versioned_dirs,
        _linux_llvm_distro_dirs,
    ):
        try:
            entries = finder()
        except Exception:
            entries = []
        for path, labels in entries:
            _push_unique_dir(out, seen, path, list(labels))
    return out


def _find_tool_in_dirs(tool: str, dirs: List[Path]) -> Optional[str]:
    for d in dirs:
        candidate = d / tool
        if candidate.exists() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def _detect_add2line_tool_status() -> Dict[str, Dict[str, Any]]:
    """按 env > config > path > ide 的优先级探测各工具的可用路径。

    - env：用户显式设置的 `ANDROID_NDK_HOME` / `LLVM_HOME` 等推导出的 bin 目录。
    - config：用户编辑的 `add2line_resolver_config.local.json` 中已写入的 `tool_paths`。
    - path：当前 shell `PATH` 中能直接找到的工具。
    - ide：常见 IDE / SDK 的默认安装路径（Xcode / Android Studio / DevEco Studio / Homebrew / Linux distro）。

    ide 排在最末，作为兜底来源；只要前面任一层级命中，就不会被覆盖，避免"自动探测的临时路径
    把用户主动配置的设置挤掉"。
    """
    tools = ["atos", "addr2line", "llvm-addr2line", "llvm-symbolizer", "ndk-stack"]
    config_dirs = _candidate_tool_dirs_from_config()
    env_dirs_with_src = _candidate_tool_dirs_from_env_with_source()
    ide_dirs_with_src = _candidate_tool_dirs_from_ides()
    result: Dict[str, Dict[str, Any]] = {}

    for tool in tools:
        from_env_path: Optional[str] = None
        from_env_keys: List[str] = []
        for d, keys in env_dirs_with_src:
            candidate = d / tool
            if candidate.exists() and os.access(str(candidate), os.X_OK):
                from_env_path = str(candidate)
                from_env_keys = list(keys)
                break
        if from_env_path:
            result[tool] = {
                "path": from_env_path,
                "source": "env",
                "env_keys": from_env_keys,
                "ide_labels": [],
            }
            continue
        from_config = _find_tool_in_dirs(tool, config_dirs)
        if from_config:
            result[tool] = {
                "path": from_config,
                "source": "config",
                "env_keys": [],
                "ide_labels": [],
            }
            continue
        from_path = shutil.which(tool)
        if from_path:
            result[tool] = {
                "path": from_path,
                "source": "path",
                "env_keys": [],
                "ide_labels": [],
            }
            continue
        from_ide_path: Optional[str] = None
        from_ide_labels: List[str] = []
        for d, labels in ide_dirs_with_src:
            candidate = d / tool
            if candidate.exists() and os.access(str(candidate), os.X_OK):
                from_ide_path = str(candidate)
                from_ide_labels = list(labels)
                break
        if from_ide_path:
            result[tool] = {
                "path": from_ide_path,
                "source": "ide",
                "env_keys": [],
                "ide_labels": from_ide_labels,
            }
            continue
        result[tool] = {
            "path": "",
            "source": "missing",
            "env_keys": [],
            "ide_labels": [],
        }

    # 兼容回退：llvm-addr2line 实质是 llvm-symbolizer 的别名（默认参数不同）。
    # 若系统未直接提供 llvm-addr2line，但提供了 llvm-symbolizer，则把后者合成进 llvm-addr2line 的检测条目，
    # 与 tools/add2line_resolver_tool.py 中 `_llvm_addr2line_alias_path` 的运行时回退保持一致，避免 UI 上误报"未找到"。
    addr2line_meta = result.get("llvm-addr2line") or {}
    symbolizer_meta = result.get("llvm-symbolizer") or {}
    if not (addr2line_meta.get("path") or "").strip() and (symbolizer_meta.get("path") or "").strip():
        result["llvm-addr2line"] = {
            "path": symbolizer_meta.get("path", ""),
            "source": symbolizer_meta.get("source", "missing"),
            "env_keys": list(symbolizer_meta.get("env_keys") or []),
            "ide_labels": list(symbolizer_meta.get("ide_labels") or []),
            "aliased_from": "llvm-symbolizer",
        }
    return result


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def _is_placeholder_secret(value: Any) -> bool:
    secret = str(value or "").strip()
    if not secret:
        return True
    placeholders = {
        "YOUR_ANTHROPIC_API_KEY",
        "YOUR_OPENAI_API_KEY",
        "YOUR_API_KEY",
        "YOUR_DEEPSEEK_API_KEY",
        "YOUR_ZHIPU_API_KEY",
        "YOUR_BAIDU_QIANFAN_AUTHORIZATION",
    }
    if secret in placeholders:
        return True
    return secret.startswith("YOUR_")


def _safe_input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return "__EOF__"


_ANSI_RED = "\033[31;1m"
_ANSI_YELLOW = "\033[33;1m"
_ANSI_GREEN = "\033[32;1m"
_ANSI_RESET = "\033[0m"


def _term_supports_color() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("FORCE_COLOR", "").strip():
        return True
    term = os.environ.get("TERM", "").strip().lower()
    if term in {"", "dumb"}:
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _stderr_supports_color() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("FORCE_COLOR", "").strip():
        return True
    term = os.environ.get("TERM", "").strip().lower()
    if term in {"", "dumb"}:
        return False
    try:
        return bool(sys.stderr.isatty())
    except Exception:
        return False


def _colorize(text: str, code: str) -> str:
    if not text or not _term_supports_color():
        return text
    return f"{code}{text}{_ANSI_RESET}"


def _red(text: str) -> str:
    return _colorize(text, _ANSI_RED)


def _yellow(text: str) -> str:
    return _colorize(text, _ANSI_YELLOW)


def _green(text: str) -> str:
    return _colorize(text, _ANSI_GREEN)


_cli_analysis_logging_configured = False


class _CliColorLogFormatter(logging.Formatter):
    """仅在终端支持颜色时高亮 WARNING/ERROR 级别。"""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not _stderr_supports_color():
            return text
        if record.levelno >= logging.ERROR:
            return f"{_ANSI_RED}{text}{_ANSI_RESET}"
        if record.levelno >= logging.WARNING:
            return f"{_ANSI_YELLOW}{text}{_ANSI_RESET}"
        return text


def _configure_cli_analysis_logging() -> None:
    """分析流程中默认将第三方/工具链日志提到 WARNING，避免 INFO 刷屏。"""
    global _cli_analysis_logging_configured
    if _cli_analysis_logging_configured:
        return
    _cli_analysis_logging_configured = True
    fmt = "%(levelname)s:%(name)s:%(message)s"
    try:
        logging.basicConfig(level=logging.WARNING, format=fmt, force=True)  # type: ignore[call-arg]
        root = logging.getLogger()
        for h in root.handlers:
            h.setFormatter(_CliColorLogFormatter(fmt))
    except TypeError:
        root = logging.getLogger()
        root.setLevel(logging.WARNING)
        if not root.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(_CliColorLogFormatter(fmt))
            root.addHandler(handler)
        else:
            for h in root.handlers:
                h.setFormatter(_CliColorLogFormatter(fmt))
    for name in ("httpx", "httpcore", "chromadb"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _stdout_is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _prompt_select(question: str, options: List[Tuple[str, str]], default_index: int = 0) -> str:
    if not options:
        raise ValueError("options 不能为空")
    idx = max(0, min(default_index, len(options) - 1))

    if not _is_tty_interactive():
        print(question)
        for i, (_, label) in enumerate(options, start=1):
            print(f"  {i}) {label}")
        while True:
            raw = _safe_input("请输入选项编号: ").strip()
            if raw == "__EOF__":
                return options[idx][0]
            for value, _ in options:
                if raw.lower() == str(value).lower():
                    return value
            if raw.isdigit():
                n = int(raw)
                if 1 <= n <= len(options):
                    return options[n - 1][0]
            print("输入无效，请重试。")

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    lines = len(options) + 1

    use_color = ("NO_COLOR" not in os.environ) and (
        os.environ.get("FORCE_COLOR", "").strip() != ""
        or os.environ.get("TERM", "").strip().lower() not in {"", "dumb"}
    )
    color_selected = "\033[32;1m"
    color_reset = "\033[0m"

    def _render(cur: int) -> None:
        # In raw mode, use CRLF to avoid staircase indentation.
        sys.stdout.write(f"\r{question}（↑/↓ 选择，Enter 确认）\r\n")
        for i, (_, label) in enumerate(options):
            prefix = "❯" if i == cur else " "
            line = f"{prefix} {label}"
            if i == cur and use_color:
                line = f"{color_selected}{line}{color_reset}"
            sys.stdout.write(f"\r{line}\r\n")
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        _render(idx)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\r", "\n"):
                break
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A":
                    idx = (idx - 1) % len(options)
                elif seq == "[B":
                    idx = (idx + 1) % len(options)
            elif ch in ("k", "K"):
                idx = (idx - 1) % len(options)
            elif ch in ("j", "J"):
                idx = (idx + 1) % len(options)
            elif ch.isdigit():
                n = int(ch)
                if 1 <= n <= len(options):
                    idx = n - 1
                    break
            elif ch.isalpha():
                hit = None
                for i, (value, _) in enumerate(options):
                    if len(str(value)) == 1 and ch.lower() == str(value).lower():
                        hit = i
                        break
                if hit is not None:
                    idx = hit
                    break
            # Refresh only the current menu block; keep previous logs untouched.
            sys.stdout.write(f"\x1b[{lines}A")
            _render(idx)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        # Keep rendered menu content in terminal history for readability.
        sys.stdout.write("\r\n")
        sys.stdout.flush()
    return options[idx][0]


def _prompt_yes_no(question: str, default_yes: bool = True) -> bool:
    if _is_tty_interactive():
        choice = _prompt_select(
            question,
            [("yes", "是"), ("no", "否")],
            default_index=0 if default_yes else 1,
        )
        return choice == "yes"
    suffix = "[Y/n]" if default_yes else "[y/N]"
    raw = _safe_input(f"{question} {suffix} ").strip().lower()
    if raw == "__eof__":
        return default_yes
    if not raw:
        return default_yes
    return raw in {"y", "yes"}


def _prompt_non_empty(question: str, default_value: str = "") -> str:
    while True:
        raw = _safe_input(f"{question}{f' (默认: {default_value})' if default_value else ''}: ").strip()
        if raw == "__EOF__":
            return default_value or ""
        if raw:
            return raw
        if default_value:
            return default_value
        print("输入不能为空，请重试。")


def _display_width(s: str) -> int:
    import unicodedata as _ud

    width = 0
    for ch in s:
        if _ud.east_asian_width(ch) in ("F", "W"):
            width += 2
        else:
            width += 1
    return width


def _pad_display(s: str, target_width: int) -> str:
    pad = target_width - _display_width(s)
    return s + (" " * max(0, pad))


def _prompt_base_url_with_examples(provider: str, default_value: str) -> str:
    # Keep these examples aligned with tools/configs/agent_config.local.example.json.
    provider_examples: List[Tuple[str, str, str]] = [
        ("openai", "OpenAI", "https://api.openai.com/v1/chat/completions"),
        ("deepseek", "DeepSeek", "https://api.deepseek.com/v1/chat/completions"),
        ("zhipu_bigmodel", "智谱 BigModel", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
        ("baidu_qianfan", "百度千帆", "https://qianfan.baidubce.com/v2/chat/completions"),
        ("qwen", "通义 Qwen（DashScope）", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
        ("kimi", "Kimi (Moonshot)", "https://api.moonshot.cn/v1/chat/completions"),
        ("minimax", "MiniMax", "https://api.minimaxi.com/v1/chat/completions"),
        ("claude", "Claude (Anthropic)", "https://api.anthropic.com/v1/messages"),
        ("xiaomi", "小米", "https://api.mi.com/llm/v1/chat/completions"),
    ]
    provider_url_map: Dict[str, str] = {pid: url for pid, _, url in provider_examples}
    provider_label_map: Dict[str, str] = {pid: label for pid, label, _ in provider_examples}
    suggested = (
        default_value
        or provider_url_map.get(provider)
        or provider_url_map["openai"]
    ).strip()

    print("base_url 是大模型 API 的完整请求地址（不要省略协议路径，避免拼接出错）。")
    print("常见厂商示例（仅供参考，请以该厂商官方文档为准）：")
    label_width = max(_display_width(label) for _, label, _ in provider_examples)
    for pid, label, url in provider_examples:
        marker = " ← 当前 provider 推荐" if pid == provider else ""
        print(f"- {_pad_display(label, label_width)} : {url}{marker}")
    if provider and provider not in provider_url_map:
        print(
            f"提示: 未识别 provider「{provider}」，请参考上方任一厂商或查阅其官方文档，"
            "填写完整的 chat completion / messages 接口地址。"
        )
    print("")

    current_label = provider_label_map.get(provider) or (provider or "未指定")
    raw = _safe_input(
        f"请输入 base_url（直接回车使用「{current_label}」默认） (默认: {suggested}): "
    ).strip()
    if raw == "__EOF__":
        return suggested
    return raw or suggested


def _open_file_with_editor(path: Path) -> bool:
    editor = os.environ.get("EDITOR", "").strip()
    target = str(path)
    if editor:
        try:
            cmd = [part for part in editor.split(" ") if part]
            if cmd:
                cmd.append(target)
                code = subprocess.call(cmd)
                return code == 0
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            if subprocess.call(["open", "-t", target]) == 0:
                return True
        except Exception:
            pass
        try:
            return subprocess.call(["open", target]) == 0
        except Exception:
            pass
    print("未检测到可用的 EDITOR 环境变量，且无法用系统默认方式打开。请手动打开该文件。")
    return False


def _show_success_panel(
    title: str,
    lines: List[str],
    follow_up: Optional[List[Tuple[str, str]]] = None,
) -> Optional[str]:
    print("━━━━━━━━━━━━━━━━━━━━━━")
    print(f"{title}")
    print("")
    for line in lines:
        print(line)
    print("━━━━━━━━━━━━━━━━━━━━━━")
    if not _is_tty_interactive():
        return None
    if not follow_up:
        _safe_input("按回车继续... ")
        return None
    return _prompt_select("请选择下一步", follow_up, default_index=0)


def _load_json_or_empty(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_json_pretty(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_session_state() -> Dict[str, Any]:
    return _load_json_or_empty(_session_state_file())


def _save_session_state(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        return
    _write_json_pretty(_session_state_file(), data)


def _profile_file(name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    if not safe_name:
        raise ValueError("profile 名称不能为空")
    return _profile_dir() / f"{safe_name}.json"


def _save_profile(name: str, data: Dict[str, Any]) -> Path:
    scope_value = str(data.get("scope") or "full").strip()
    if scope_value not in {"full", "prompt_only", "parse_only", "parse_log_only"}:
        scope_value = "full"
    profile = {
        "crash_log": data.get("crash_log") or "",
        "library_dir": data.get("library_dir") or "",
        "code_roots": data.get("code_roots") or [],
        "engine": data.get("engine") or "direct",
        "scope": scope_value,
    }
    target = _profile_file(name)
    _write_json_pretty(target, profile)
    return target


def _load_profile(name: str) -> Dict[str, Any]:
    target = _profile_file(name)
    return _load_json_or_empty(target)


def _list_profiles() -> List[str]:
    d = _profile_dir()
    if not d.exists():
        return []
    return sorted([p.stem for p in d.glob("*.json") if p.is_file()])


def _delete_profile(name: str) -> bool:
    target = _profile_file(name)
    if not target.exists():
        return False
    target.unlink()
    return True


def _config_command_path() -> int:
    print(f"config_dir: {_user_config_dir()}")
    for p in [_user_agent_config_file(), _user_add2line_config_file()]:
        print(f"{p.name}: {'exists' if p.exists() else 'missing'} ({p})")
    effective_agent = str(_user_agent_config_file())
    effective_add2line = os.environ.get("STABILITY_AGENT_ADD2LINE_CONFIG_FILE", "").strip() or str(_user_add2line_config_file())
    print(f"effective_agent_config: {effective_agent}")
    print(f"effective_add2line_config: {effective_add2line}")
    return 0


def _config_command_doctor() -> int:
    problems: List[str] = []
    agent_cfg = _load_json_or_empty(_user_agent_config_file())
    llm_cfg = agent_cfg.get("llm_config", {}) if isinstance(agent_cfg, dict) else {}
    providers = llm_cfg.get("providers", {}) if isinstance(llm_cfg, dict) else {}
    provider_defaults = llm_cfg.get("provider_defaults", {}) if isinstance(llm_cfg, dict) else {}
    if not isinstance(provider_defaults, dict):
        provider_defaults = {}
    active_provider = llm_cfg.get("active_provider") if isinstance(llm_cfg, dict) else None
    if not active_provider:
        problems.append("LLM active_provider 未配置")
    provider_cfg = (
        {**provider_defaults, **(providers.get(active_provider, {}) or {})}
        if isinstance(providers, dict) and active_provider else {}
    )
    if active_provider:
        auth_type = str(provider_cfg.get("auth_type") or "").strip().lower()
        if not auth_type:
            auth_type = "authorization" if (active_provider == "baidu_qianfan" or provider_cfg.get("authorization")) else "api_key"
        auth_field = "authorization" if auth_type == "authorization" else "api_key"
        if _is_placeholder_secret(provider_cfg.get(auth_field)):
            problems.append(f"{active_provider}.{auth_field} 为空或仍为占位符")
    else:
        problems.append("LLM provider 配置缺失")

    tool_status = _detect_add2line_tool_status()
    tools = {name: (meta.get("path") or None) for name, meta in tool_status.items()}
    if not any(tools.values()):
        problems.append("未检测到 atos/addr2line/llvm-addr2line/llvm-symbolizer/ndk-stack 任一工具")

    print("== 配置检查结果 ==")
    print(f"agent_config: {_user_agent_config_file()}")
    print(f"add2line_config: {_user_add2line_config_file()}")
    print("tools:")
    for name, meta in tool_status.items():
        path = meta.get("path") or "missing"
        source = meta.get("source") or "unknown"
        print(f"  - {name}: {path} (source={source})")
    if problems:
        print("status: WARN")
        for item in problems:
            print(f"  - {item}")
        print("建议：重新运行 `sa-agent` 并按引导完成配置。")
        return 1
    print("status: PASS")
    return 0


def _load_llm_menu_extension_module() -> Optional[Any]:
    """
    可选 LLM 菜单扩展模块（通用 hook）。
    通过环境变量 STABILITY_AGENT_LLM_MENU_EXTENSION 指定模块名。
    """
    mod_name = os.environ.get("STABILITY_AGENT_LLM_MENU_EXTENSION", "").strip()
    if not mod_name:
        return None
    try:
        return importlib.import_module(mod_name)
    except Exception:
        return None


def _get_llm_reconfig_extension_actions() -> List[Tuple[str, str]]:
    """
    从扩展模块读取“进入设置”菜单动作。
    约定扩展模块提供: get_llm_reconfig_actions() -> [{id, label}, ...]
    """
    mod = _load_llm_menu_extension_module()
    if mod is None:
        return []
    getter = getattr(mod, "get_llm_reconfig_actions", None)
    if not callable(getter):
        return []
    try:
        items = getter()
    except Exception:
        return []
    out: List[Tuple[str, str]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            action_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            if action_id and label:
                out.append((action_id, label))
    return out


def _invoke_llm_reconfig_extension_action(action_id: str) -> bool:
    """
    调用扩展动作。
    约定扩展模块提供: handle_llm_reconfig_action(action_id: str, config_path: str) -> bool
    """
    _ensure_user_config_templates()
    target = _user_agent_config_file()
    mod = _load_llm_menu_extension_module()
    if mod is None:
        return False
    handler = getattr(mod, "handle_llm_reconfig_action", None)
    if not callable(handler):
        return False
    try:
        return bool(handler(str(action_id), str(target)))
    except Exception as exc:
        print(f"调用 LLM 菜单扩展失败: {exc}")
        return False


def _update_llm_config_interactive() -> bool:
    target = _user_agent_config_file()
    data = _load_json_or_empty(target)
    llm_cfg = data.get("llm_config", {}) if isinstance(data, dict) else {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    providers = llm_cfg.get("providers", {})
    if not isinstance(providers, dict):
        providers = {}

    provider_candidates = [
        "openai",
        "deepseek",
        "zhipu_bigmodel",
        "baidu_qianfan",
        "qwen",
        "kimi",
        "minimax",
        "claude",
        "xiaomi",
    ]
    existing_keys = [k for k in providers.keys() if isinstance(k, str) and k.strip()]
    provider_keys = []
    for key in existing_keys + provider_candidates:
        if key not in provider_keys:
            provider_keys.append(key)

    def _is_provider_configured(pkey: str) -> bool:
        cfg = providers.get(pkey)
        if not isinstance(cfg, dict):
            return False
        auth_field = "authorization" if pkey == "baidu_qianfan" else "api_key"
        if not _is_placeholder_secret(cfg.get(auth_field)):
            return True
        other_field = "api_key" if auth_field == "authorization" else "authorization"
        return not _is_placeholder_secret(cfg.get(other_field))

    active_provider = llm_cfg.get("active_provider") if isinstance(llm_cfg, dict) else None

    def _provider_label(pkey: str) -> str:
        configured = _is_provider_configured(pkey)
        is_active = isinstance(active_provider, str) and active_provider == pkey
        if configured and is_active:
            return f"{pkey}（已配置·当前使用）"
        if configured:
            return f"{pkey}（已配置）"
        return pkey

    provider_options: List[Tuple[str, str]] = [("back", "返回")]
    provider_options.extend([(k, _provider_label(k)) for k in provider_keys])
    provider_options.append(("custom", "自定义输入 provider 名称"))
    selected_provider = _prompt_select(
        "请选择大模型厂商",
        provider_options,
        default_index=(provider_keys.index("openai") + 1) if "openai" in provider_keys else 1,
    )
    if selected_provider == "back":
        return False
    if selected_provider == "custom":
        provider = _prompt_non_empty("请输入自定义 provider 名称", "")
        if not provider:
            return False
    else:
        provider = selected_provider
    model_defaults = {
        "openai": "gpt-4o",
        "deepseek": "deepseek-chat",
        "zhipu_bigmodel": "glm-4",
        "baidu_qianfan": "ernie-4.0-8k",
    }
    base_url_defaults = {
        "openai": "https://api.openai.com/v1/chat/completions",
        "deepseek": "https://api.deepseek.com/v1/chat/completions",
        "zhipu_bigmodel": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "baidu_qianfan": "https://qianfan.baidubce.com/v2/chat/completions",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "kimi": "https://api.moonshot.cn/v1/chat/completions",
        "minimax": "https://api.minimaxi.com/v1/chat/completions",
        "claude": "https://api.anthropic.com/v1/messages",
        "xiaomi": "https://api.mi.com/llm/v1/chat/completions",
    }
    provider_cfg = providers.get(provider, {})
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}

    def _prompt_secret_keep_existing(field_label: str, existing_value: Any) -> str:
        existing_str = str(existing_value or "").strip()
        if existing_str and not _is_placeholder_secret(existing_str):
            prompt = (
                f"请输入 {field_label}（直接回车保留当前: {_mask_secret(existing_str)}，输入时隐藏）: "
            )
            entered = getpass.getpass(prompt).strip()
            return entered or existing_str
        return getpass.getpass(f"请输入 {field_label}（输入时隐藏）: ").strip()

    if provider == "baidu_qianfan":
        secret = _prompt_secret_keep_existing("authorization", provider_cfg.get("authorization"))
        provider_cfg["authorization"] = secret
        provider_cfg["model"] = _prompt_non_empty(
            "请输入模型名（直接回车使用默认）",
            provider_cfg.get("model") or model_defaults.get(provider, "ernie-4.0-8k"),
        )
        base_url = _prompt_base_url_with_examples(
            provider,
            str(provider_cfg.get("base_url") or base_url_defaults.get(provider, "")).strip(),
        )
        provider_cfg["base_url"] = base_url
    else:
        secret = _prompt_secret_keep_existing("api_key", provider_cfg.get("api_key"))
        provider_cfg["api_key"] = secret
        provider_cfg["model"] = _prompt_non_empty(
            "请输入模型名（直接回车使用默认）",
            provider_cfg.get("model") or model_defaults.get(provider, "gpt-4o"),
        )
        base_url = _prompt_base_url_with_examples(
            provider,
            str(provider_cfg.get("base_url") or base_url_defaults.get(provider, "")).strip(),
        )
        provider_cfg["base_url"] = base_url

    providers[provider] = provider_cfg
    llm_cfg["providers"] = providers
    llm_cfg["active_provider"] = provider
    data["llm_config"] = llm_cfg
    _write_json_pretty(target, data)
    follow_up_choice = _show_success_panel(
        "✅ LLM 配置已保存",
        [
            f"配置文件: {target}",
            f"provider: {provider}",
            f"secret: {_mask_secret(secret)}",
        ],
        follow_up=[
            ("connectivity", "检测联通性（验证是否能连接到大模型）"),
            ("back", "返回"),
        ],
    )
    if follow_up_choice == "connectivity":
        _check_llm_connectivity()
    return True


_AUTO_PLATFORM_LABELS: Dict[str, str] = {
    "ios": "iOS / macOS",
    "android": "Android",
    "linux": "Linux",
    "harmonyos": "HarmonyOS",
}
_AUTO_PLATFORM_ORDER: List[str] = ["ios", "android", "linux", "harmonyos"]


def _auto_detect_per_platform_plan() -> List[Dict[str, Any]]:
    tool_status = _detect_add2line_tool_status()
    recommendations = _platform_tool_recommendations()
    ide_dirs_with_src = _candidate_tool_dirs_from_ides()
    affinity_map = _platform_ide_affinity()
    plan: List[Dict[str, Any]] = []
    for plat in _AUTO_PLATFORM_ORDER:
        chosen_tool: Optional[str] = None
        for t in recommendations.get(plat, []):
            meta = tool_status.get(t) or {}
            if (meta.get("path") or "").strip():
                chosen_tool = t
                break
        if not chosen_tool:
            plan.append({"platform": plat, "tool": None, "source": "missing"})
            continue
        meta = dict(tool_status.get(chosen_tool) or {})
        # 平台亲和度覆写：仅当初次命中的来源是 IDE 时，尝试在 IDE 候选目录里
        # 找到一个"和当前平台原生匹配"的来源（如 harmonyos 优先 OpenHarmony SDK）。
        # env / config / path 来源代表用户显式配置，不在此覆写。
        if meta.get("source") == "ide":
            preferred = _find_preferred_ide_meta(
                chosen_tool,
                ide_dirs_with_src,
                affinity_map.get(plat, []),
            )
            if preferred and preferred.get("path") and preferred["path"] != meta.get("path"):
                meta = preferred
        plan.append(
            {
                "platform": plat,
                "tool": chosen_tool,
                "source": str(meta.get("source") or "missing"),
                "tool_path": str(meta.get("path") or ""),
                "env_keys": list(meta.get("env_keys") or []),
                "ide_labels": list(meta.get("ide_labels") or []),
                "aliased_from": str(meta.get("aliased_from") or ""),
            }
        )
    return plan


def _auto_configure_add2line(
    data: Dict[str, Any], platforms: Dict[str, Any], target: Path
) -> bool:
    plan = _auto_detect_per_platform_plan()

    print("")
    print("== 自动检测堆栈地址解析工具 ==")
    has_any_hit = False
    label_width = max(len(_AUTO_PLATFORM_LABELS[p]) for p in _AUTO_PLATFORM_ORDER)
    pending_env: List[Tuple[str, List[str]]] = []                         # 待写 environment_vars 的 (plat, keys)
    pending_paths: List[Tuple[str, str, List[str]]] = []                  # 待写 tool_paths 的 (plat, bin_dir, labels)
    for entry in plan:
        plat = entry["platform"]
        label = _AUTO_PLATFORM_LABELS.get(plat, plat).ljust(label_width)
        if entry["tool"] is None:
            print(f"- {label}: {_red('⚠ 未找到推荐工具（跳过）')}")
            continue
        has_any_hit = True
        tool = entry["tool"]
        src = entry["source"]
        alias_src = (entry.get("aliased_from") or "").strip()
        alias_suffix = f"，via {alias_src}" if alias_src else ""
        existed = isinstance(platforms.get(plat), dict) and bool(platforms.get(plat))
        if existed:
            print(
                f"- {label}: {_yellow(f'⚠ {tool} 可用，但 {plat} 已有手动配置，跳过覆盖')}"
            )
            continue
        if src == "path":
            print(
                f"- {label}: {_green(f'✅ {tool}')} （来源: PATH{alias_suffix}，无需写入配置）"
            )
            continue
        if src == "config":
            print(
                f"- {label}: {_green(f'✅ {tool}')} （来源: 已有配置{alias_suffix}，无需重复写入）"
            )
            continue
        if src == "env":
            keys = entry.get("env_keys") or []
            keys_disp = ", ".join(keys) if keys else "（未知）"
            print(
                f"- {label}: {_green(f'✅ {tool}')} "
                f"（来源: env{alias_suffix}，将写入 platforms.{plat}.environment_vars: {keys_disp}）"
            )
            if keys:
                pending_env.append((plat, list(keys)))
            continue
        if src == "ide":
            tool_path = (entry.get("tool_path") or "").strip()
            ide_labels = entry.get("ide_labels") or []
            label_disp = ", ".join(ide_labels) if ide_labels else "未知"
            bin_dir = ""
            if tool_path:
                try:
                    bin_dir = str(Path(tool_path).parent)
                except Exception:
                    bin_dir = ""
            print(
                f"- {label}: {_green(f'✅ {tool}')} "
                f"（来源: {label_disp}{alias_suffix}，将写入 platforms.{plat}.tool_paths: {bin_dir or tool_path}）"
            )
            if bin_dir:
                pending_paths.append((plat, bin_dir, list(ide_labels)))
            continue
        print(f"- {label}: {tool}（来源: {src}{alias_suffix}）")
    print("")

    if not has_any_hit:
        print(_red("❌ 未在 PATH、常用环境变量、已有配置、IDE 默认安装路径中找到任何推荐工具。"))
        print("- 排查建议：")
        print("  · macOS / iOS：安装 Xcode 或 Command Line Tools（`xcode-select --install`）")
        print("  · Android：安装 Android Studio 或 NDK 并 export ANDROID_NDK_HOME；或安装 LLVM 并 export LLVM_HOME")
        print("  · HarmonyOS：安装 DevEco Studio（自带 OpenHarmony Native SDK）")
        print("  · Linux：通过包管理器安装 binutils 或 llvm")
        print("- 你也可以手动设置环境变量或 bin 目录。")
        print("")
        action = _prompt_select(
            "请选择操作",
            [
                ("back", "返回上一级"),
                ("manual_env", "手动设置环境变量"),
                ("manual_bin", "手动设置符号化工具链 bin 目录"),
            ],
            default_index=0,
        )
        if action == "manual_env":
            return _update_add2line_config_interactive(initial_mode="env")
        if action == "manual_bin":
            return _update_add2line_config_interactive(initial_mode="path")
        return False

    if not pending_env and not pending_paths:
        print(_green("✅ 检测到的工具均已可用（来自 PATH 或已有配置），无需写入配置文件。"))
        print("如检测结果不符合预期，可手动指定环境变量或 bin 目录。")
        print("")
        action = _prompt_select(
            "请选择操作",
            [
                ("done", "完成，返回上一级"),
                ("manual_env", "不符预期？手动设置环境变量"),
                ("manual_bin", "不符预期？手动设置符号化工具链 bin 目录"),
            ],
            default_index=0,
        )
        if action == "manual_env":
            return _update_add2line_config_interactive(initial_mode="env")
        if action == "manual_bin":
            return _update_add2line_config_interactive(initial_mode="path")
        return False

    print("即将写入的最小配置：")
    for plat, keys in pending_env:
        for k in keys:
            v = os.environ.get(k, "").strip() or "（当前 shell 未设置）"
            print(f"  - platforms.{plat}.environment_vars.{k} = {v}")
    for plat, bin_dir, labels in pending_paths:
        labels_disp = f"  # 来源: {', '.join(labels)}" if labels else ""
        print(f"  - platforms.{plat}.tool_paths += {bin_dir}{labels_disp}")
    print("如检测结果不符合预期，可手动指定环境变量或 bin 目录。")
    print("")

    action = _prompt_select(
        "请选择操作",
        [
            ("confirm", "确认写入配置文件"),
            ("manual_env", "不符预期？手动设置环境变量"),
            ("manual_bin", "不符预期？手动设置符号化工具链 bin 目录"),
            ("back", "返回上一级（不写入）"),
        ],
        default_index=0,
    )
    if action == "manual_env":
        return _update_add2line_config_interactive(initial_mode="env")
    if action == "manual_bin":
        return _update_add2line_config_interactive(initial_mode="path")
    if action != "confirm":
        return False

    written: List[str] = []
    for plat, keys in pending_env:
        plat_cfg_raw = platforms.get(plat)
        plat_cfg: Dict[str, Any] = plat_cfg_raw if isinstance(plat_cfg_raw, dict) else {}
        env_cfg_raw = plat_cfg.get("environment_vars")
        env_cfg: Dict[str, str] = env_cfg_raw if isinstance(env_cfg_raw, dict) else {}
        for k in keys:
            v = os.environ.get(k, "")
            if v:
                env_cfg[k] = str(v)
                written.append(f"platforms.{plat}.environment_vars.{k}")
        if env_cfg:
            plat_cfg["environment_vars"] = env_cfg
        platforms[plat] = plat_cfg

    for plat, bin_dir, _labels in pending_paths:
        plat_cfg_raw = platforms.get(plat)
        plat_cfg: Dict[str, Any] = plat_cfg_raw if isinstance(plat_cfg_raw, dict) else {}
        paths_raw = plat_cfg.get("tool_paths")
        paths_list: List[str] = list(paths_raw) if isinstance(paths_raw, list) else []
        if bin_dir and bin_dir not in paths_list:
            paths_list.append(bin_dir)
            written.append(f"platforms.{plat}.tool_paths += {bin_dir}")
        plat_cfg["tool_paths"] = paths_list
        platforms[plat] = plat_cfg

    if not written:
        print(_red("❌ 取消写入：所选环境变量在当前 shell 中均无值，且没有可写入的 IDE 路径。"))
        return False

    data["platforms"] = platforms
    _write_json_pretty(target, data)
    _show_success_panel(
        "✅ 已自动写入堆栈地址解析工具配置",
        [f"配置文件: {target}", *written],
    )
    return True


def _update_add2line_config_interactive(initial_mode: Optional[str] = None) -> bool:
    target = _user_add2line_config_file()
    data = _load_json_or_empty(target)
    if not isinstance(data, dict):
        data = {}
    platforms = data.get("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}

    if initial_mode in {"env", "path"}:
        mode = initial_mode
    else:
        mode = _prompt_select(
            "请选择配置方式",
            [
                ("back", "返回"),
                ("auto", "自动获取（推荐）"),
                ("env", "手动设置环境变量"),
                ("path", "手动设置符号化工具链 bin 目录"),
            ],
            default_index=1,
        )
        if mode == "back":
            return False
        if mode == "auto":
            return _auto_configure_add2line(data, platforms, target)

    os_choice = _prompt_select(
        "请选择目标平台",
        [
            ("back", "返回"),
            ("ios", "ios"),
            ("android", "android"),
            ("linux", "linux"),
            ("harmonyos", "harmonyos"),
            ("custom", "自定义平台"),
        ],
        default_index=1,
    )
    if os_choice == "back":
        return False
    if os_choice == "custom":
        os_choice = _prompt_non_empty("请输入自定义平台名称", "")
        if not os_choice:
            return False
    os_cfg = platforms.get(os_choice, {})
    if not isinstance(os_cfg, dict):
        os_cfg = {}

    env_vars: Dict[str, str] = {}
    if mode == "env":
        raw_env_key = _safe_input("请输入要读取的环境变量名 KEY（直接回车返回上一级）: ").strip()
        if raw_env_key == "__EOF__" or not raw_env_key or raw_env_key.lower() in {"back", "b"}:
            return False
        key = raw_env_key
        val = os.environ.get(key)
        if val is None or str(val).strip() == "":
            print(_red(f"❌ 当前 shell 未读取到环境变量「{key}」的值，已取消保存。"))
            print("- 排查建议：")
            print(f"  · 检查变量名是否拼写正确（区分大小写）；")
            print(f"  · 在当前 shell 执行 `echo ${key}` 确认是否已 export；")
            print("  · 若刚配置过 ~/.zshrc / ~/.bashrc，重新打开终端或 `source` 之后再试；")
            print("  · 或改用「手动设置符号化工具链 bin 目录」模式。")
            return False
        env_vars[key] = str(val)
    else:
        raw_bin = _safe_input("请输入符号化工具链 bin 目录（直接回车返回上一级）: ").strip()
        if raw_bin == "__EOF__" or not raw_bin or raw_bin.lower() in {"back", "b"}:
            return False
        os_cfg["tool_paths"] = [str(Path(raw_bin).expanduser().resolve())]

    if env_vars:
        os_cfg["environment_vars"] = env_vars

    platforms[os_choice] = os_cfg
    data["platforms"] = platforms
    _write_json_pretty(target, data)
    _show_success_panel(
        "✅ 堆栈地址解析工具配置已保存",
        [
            f"配置文件: {target}",
            f"平台: {os_choice}",
            f"tool_paths: {os_cfg.get('tool_paths') or []}",
            f"environment_vars: {list((os_cfg.get('environment_vars') or {}).keys())}",
        ],
    )
    return True


def _config_command_init() -> int:
    _ensure_user_config_templates()
    print(f"配置目录: {_user_config_dir()}")

    if _prompt_yes_no("是否现在配置大模型？", True):
        mode = _prompt_select(
            "请选择大模型配置方式",
            [("wizard", "交互向导填写"), ("manual", "手动编辑配置文件")],
            default_index=0,
        )
        if mode == "manual":
            _configure_llm_only()
        else:
            _update_llm_config_interactive()

    detected_tools = _detect_add2line_tools()
    print("自动检测堆栈地址解析工具（addr2line / atos 等）：")
    for name, path in detected_tools.items():
        print(f"  - {name}: {path or 'missing'}")
    env_keys = ["ANDROID_NDK_HOME", "LLVM_HOME", "ANDROID_SDK_HOME"]
    print("默认的环境变量：")
    for key in env_keys:
        print(f"  - {key}: {os.environ.get(key) or 'missing'}")

    if _prompt_yes_no("是否配置堆栈地址解析工具路径？", not any(detected_tools.values())):
        mode = _prompt_select(
            "请选择堆栈地址解析配置方式",
            [("1", "手动编辑配置文件"), ("2", "交互向导填写")],
            default_index=1,
        )
        if mode == "1":
            _configure_add2line_only()
        else:
            _update_add2line_config_interactive()

    print("初始化完成。")
    return 0


def _print_llm_detection_summary(status: Dict[str, Any]) -> None:
    print("== 大模型配置检测 ==")
    print(f"- 配置文件: {_user_agent_config_file()}")
    if status.get("llm_ok"):
        print(f"- 状态: {_green('已配置（当前设置了密钥）')}")
        print(f"- 当前 provider: {status.get('active_provider') or '未知'}")
        model_disp = status.get("model") or "（未填写 model）"
        print(f"- 当前模型: {model_disp}")
        print("- 注: 仅检查了配置完整性，未验证联通性。如需验证，请选择「检测联通性」。")
    else:
        print(f"- 状态: {_red('未就绪（缺少 provider、密钥或未替换占位符等）')}")
        ap = status.get("active_provider")
        if ap:
            print(f"- active_provider: {ap}（{_red('请检查密钥等字段是否有效')}）")
        else:
            print(f"- active_provider: {_red('未设置')}")
    print("")


def _resolve_active_provider_runtime_config() -> Optional[Dict[str, Any]]:
    cfg = _load_agent_config_file()
    llm_cfg = cfg.get("llm_config", {}) if isinstance(cfg, dict) else {}
    providers = llm_cfg.get("providers", {}) if isinstance(llm_cfg, dict) else {}
    if not isinstance(providers, dict):
        return None
    provider_defaults = llm_cfg.get("provider_defaults", {}) if isinstance(llm_cfg, dict) else {}
    if not isinstance(provider_defaults, dict):
        provider_defaults = {}
    active_provider = str(llm_cfg.get("active_provider") or "").strip()
    if not active_provider:
        return None
    provider_cfg = {**provider_defaults, **(providers.get(active_provider, {}) or {})}
    if not isinstance(provider_cfg, dict):
        return None

    auth_type = str(provider_cfg.get("auth_type") or "").strip().lower()
    if not auth_type:
        auth_type = "authorization" if (active_provider == "baidu_qianfan" or provider_cfg.get("authorization")) else "api_key"
    if auth_type == "authorization":
        secret = str(provider_cfg.get("authorization") or "").strip()
    elif auth_type == "none":
        secret = ""
    else:
        secret = str(provider_cfg.get("api_key") or "").strip()
    if auth_type != "none" and _is_placeholder_secret(secret):
        return None

    request_format = str(provider_cfg.get("request_format") or "openai_chat_completions_compatible").strip().lower()
    auth_header = str(provider_cfg.get("auth_header") or ("Authorization" if auth_type != "authorization" else "Authorization")).strip() or "Authorization"
    auth_prefix = str(provider_cfg.get("auth_prefix") or ("Bearer " if auth_header.lower() == "authorization" else "")).strip()
    if auth_prefix:
        auth_prefix = auth_prefix + ("" if auth_prefix.endswith(" ") else " ")
    base_url = str(provider_cfg.get("base_url") or "").strip()
    model = str(provider_cfg.get("model") or "").strip()
    timeout = float(provider_cfg.get("request_timeout") or 10)
    if timeout <= 0:
        timeout = 10

    return {
        "active_provider": active_provider,
        "request_format": request_format,
        "auth_type": auth_type,
        "auth_header": auth_header,
        "auth_prefix": auth_prefix,
        "secret": secret,
        "base_url": base_url,
        "model": model,
        "timeout": timeout,
    }


def _join_endpoint(base_url: str, suffix: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return suffix
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def _connectivity_probe_via_http(runtime_cfg: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(runtime_cfg.get("active_provider") or "unknown")
    request_format = str(runtime_cfg.get("request_format") or "openai_chat_completions_compatible")
    base_url = str(runtime_cfg.get("base_url") or "").strip()
    model = str(runtime_cfg.get("model") or "").strip() or "unknown-model"
    timeout = float(runtime_cfg.get("timeout") or 10)

    if not base_url:
        raise RuntimeError("base_url 为空，无法执行联通性检测")

    headers = {"Content-Type": "application/json"}
    auth_type = str(runtime_cfg.get("auth_type") or "api_key")
    if auth_type != "none":
        auth_header = str(runtime_cfg.get("auth_header") or "Authorization")
        auth_prefix = str(runtime_cfg.get("auth_prefix") or "")
        secret = str(runtime_cfg.get("secret") or "")
        if not secret:
            raise RuntimeError("密钥为空，无法执行联通性检测")
        headers[auth_header] = f"{auth_prefix}{secret}"

    if request_format == "anthropic_messages_compatible":
        url = _join_endpoint(base_url, "/messages")
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "pong"}],
            "max_tokens": 16,
        }
        # Anthropic official endpoint requires this header; OneAPI usually ignores it.
        if "anthropic.com" in base_url and "anthropic-version" not in {k.lower(): v for k, v in headers.items()}:
            headers["anthropic-version"] = "2023-06-01"
    elif request_format == "openai_responses_compatible":
        url = _join_endpoint(base_url, "/responses")
        body = {
            "model": model,
            "input": "pong",
            "max_output_tokens": 16,
        }
    else:
        # Default to OpenAI Chat Completions compatible protocol.
        url = _join_endpoint(base_url, "/chat/completions")
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "pong"}],
            "temperature": 0.0,
            "max_tokens": 16,
        }

    req = urllib.request.Request(
        url=url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return {
        "provider": provider,
        "request_format": request_format,
        "url": url,
        "response_preview": raw[:120].replace("\n", " "),
    }


def _check_llm_connectivity() -> None:
    def _ack_result() -> None:
        if _is_tty_interactive():
            _prompt_select(
                "联通性检测已完成",
                [("back", "返回")],
                default_index=0,
            )
        else:
            _safe_input("联通性检测已完成，按回车返回... ")

    runtime_cfg = _resolve_active_provider_runtime_config()
    if runtime_cfg is None:
        print(_red("❌ 当前配置未就绪：请先完成 provider / 密钥配置。"))
        _ack_result()
        return

    print("正在检测联通性（最长约 10 秒，可按 Ctrl+C 取消）...")
    start = time.time()
    try:
        probe = _connectivity_probe_via_http(runtime_cfg)
        elapsed = time.time() - start
        content = str(probe.get("response_preview") or "").strip()
        if len(content) > 80:
            content = content[:80] + "..."
        print("✅ 联通性检测通过")
        print(f"- provider: {probe.get('provider') or (_doctor_status().get('active_provider') or 'unknown')}")
        print(f"- request_format: {probe.get('request_format')}")
        print(f"- endpoint: {probe.get('url')}")
        print(f"- 耗时: {elapsed:.2f}s")
        if content:
            print(f"- 响应片段: {content}")
        _ack_result()
    except KeyboardInterrupt:
        print("\n已取消联通性检测。")
        _ack_result()
    except Exception as exc:
        elapsed = time.time() - start
        msg = str(exc)
        lower = msg.lower()
        reason = "请求失败"
        if "404" in lower:
            reason = "接口路径可能错误（404）"
        elif "401" in lower or "403" in lower:
            reason = "鉴权失败（401/403，检查密钥与权限）"
        elif "timeout" in lower or "timed out" in lower:
            reason = "请求超时（网络或网关较慢）"
        elif "name or service not known" in lower or "nodename nor servname" in lower:
            reason = "域名解析失败（DNS/地址配置问题）"
        print("❌ 联通性检测失败")
        print(f"- 原因: {reason}")
        print(f"- 耗时: {elapsed:.2f}s")
        print(f"- 错误: {msg}")
        _ack_result()


def _configure_llm_only() -> None:
    _ensure_user_config_templates()
    while True:
        status = _doctor_status()
        _print_llm_detection_summary(status)

        if status.get("llm_ok"):
            rerun = _prompt_select(
                "已检测到大模型配置完成，是否需要重新进入设置？",
                [
                    ("keep", "不用了，返回"),
                    ("reconfig", "进入设置"),
                    ("connectivity", "检测联通性（验证是否能连接到大模型）"),
                ],
                default_index=0,
            )
            if rerun == "keep":
                return
            if rerun == "connectivity":
                _check_llm_connectivity()
                print("")
                continue
            if rerun == "reconfig":
                ext_actions = _get_llm_reconfig_extension_actions()
                if ext_actions:
                    sub_options: List[Tuple[str, str]] = [("wizard", "通用交互向导（选择 provider…）")]
                    sub_options.extend(ext_actions)
                    sub_options.append(("back", "返回"))
                    sub = _prompt_select(
                        "进入设置方式",
                        sub_options,
                        default_index=0,
                    )
                    if sub == "back":
                        continue
                    if sub != "wizard":
                        if _invoke_llm_reconfig_extension_action(sub):
                            return
                        continue
        else:
            print("请完成大模型配置。\n")
            pre_opts: List[Tuple[str, str]] = [
                ("reconfig", "进入设置"),
            ]
            pre_opts.extend(_get_llm_reconfig_extension_actions())
            pre_opts.extend(
                [
                    ("connectivity", "检测联通性（验证是否能连接到大模型）"),
                    ("back", "返回"),
                ]
            )
            pre_action = _prompt_select(
                "请选择下一步",
                pre_opts,
                default_index=0,
            )
            if pre_action == "back":
                return
            if pre_action == "connectivity":
                _check_llm_connectivity()
                print("")
                continue
            if pre_action not in {"reconfig", "connectivity", "back"}:
                if _invoke_llm_reconfig_extension_action(pre_action):
                    return
                continue
        print("")
        print("—— 进入交互向导（将依次选择 provider、填写密钥等）——")
        print("")
        changed = _update_llm_config_interactive()
        if changed:
            return
        print("已取消向导，未保存修改。")
        print("")


def _configure_llm_manual_panel() -> None:
    _ensure_user_config_templates()
    target = _user_agent_config_file()
    while True:
        status = _doctor_status()
        _print_llm_detection_summary(status)
        print("━━━━━━━━━━━━━━━━━━━━━━")
        print("配置大模型（手动方式）")
        print("")
        print("请编辑以下文件并填写密钥：")
        print("")
        print(str(target))
        print("")
        print("完成后返回此窗口。")
        print("━━━━━━━━━━━━━━━━━━━━━━")
        action = _prompt_select(
            "请选择操作",
            [
                ("open", "[o] 打开文件"),
                ("check", "[c] 我已完成，检查配置是否正确"),
                ("help", "[h] 查看最小必填示例"),
                ("back", "[b] 返回"),
            ],
            default_index=0,
        )
        if action == "back":
            return
        if action == "help":
            print("最小必填：")
            print("- llm_config.active_provider")
            print("- llm_config.providers.<provider>.model")
            print("- llm_config.providers.<provider>.base_url")
            print("- llm_config.providers.<provider>.api_key 或 authorization（非占位符）")
            print("")
            print("— 请在下方的菜单中继续操作 —")
            if _is_tty_interactive():
                _safe_input("按回车显示菜单... ")
            continue
        if action == "open":
            ok = _open_file_with_editor(target)
            if ok:
                print("已尝试用系统默认文本编辑器打开；若未看到窗口，请检查是否被其他桌面空间遮挡。")
            else:
                print(f"无法自动打开，请手动打开: {target}")
            print("")
            print("— 请在下方的菜单中继续操作 —")
            if _is_tty_interactive():
                _safe_input("按回车显示菜单... ")
            continue
        st2 = _doctor_status()
        if st2.get("llm_ok"):
            _show_success_panel("✅ LLM 配置检测通过", [f"provider: {st2.get('active_provider')}", f"model: {st2.get('model') or 'N/A'}"])
            return
        print("LLM 配置仍未完成，请继续编辑后再检查。")
        print("")
        print("— 请在下方的菜单中继续操作 —")
        if _is_tty_interactive():
            _safe_input("按回车显示菜单... ")


def _current_platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _platform_tool_recommendations() -> Dict[str, List[str]]:
    return {
        "macos": ["atos", "llvm-addr2line"],
        "ios": ["atos", "llvm-addr2line"],
        "android": ["llvm-addr2line", "ndk-stack"],
        "linux": ["addr2line", "llvm-addr2line"],
        "harmonyos": ["llvm-addr2line"],
        "windows": ["llvm-addr2line", "addr2line"],
    }


def _platform_ide_affinity() -> Dict[str, List[str]]:
    """各平台对应的"原生" IDE 来源关键词。

    用途：当 `_detect_add2line_tool_status` 对某个工具只记录一个最佳来源时，
    平台维度的"亲和度"信息会丢失。例如 harmonyos 平台需要 `llvm-addr2line`，
    若 Android Studio NDK 与 OpenHarmony SDK 都提供该工具，前者通常会"先到先得"
    被记录到 tool_status，从而让 harmonyos 平台错配到 Android NDK。

    本表用于 `_auto_detect_per_platform_plan` 中按平台亲和度重新挑选 IDE 来源：
    若初次命中的来源是 `ide` 但来源标签不匹配本平台，则在 IDE 候选目录里查找
    一个标签匹配的来源进行替换。env / config / path 来源不会被覆写
    （尊重用户显式配置）。
    """
    return {
        "ios": ["Xcode", "Command Line Tools"],
        "macos": ["Xcode", "Command Line Tools"],
        "android": ["Android Studio NDK", "Android NDK", "ndk-bundle"],
        "linux": ["Linux Distro", "Homebrew"],
        "harmonyos": ["OpenHarmony", "OHOS", "DevEco"],
        "windows": ["LLVM"],
    }


def _find_preferred_ide_meta(
    tool: str,
    ide_dirs_with_src: List[Tuple[Path, List[str]]],
    affinity_keywords: List[str],
) -> Optional[Dict[str, Any]]:
    """在 IDE 候选目录中查找命中 `tool`、且来源标签包含 `affinity_keywords` 任一关键词的条目。

    返回 `_detect_add2line_tool_status` 风格的 meta 字典；未命中时返回 None。
    `ide_dirs_with_src` 已按发现顺序排好（同探测器内按版本号倒序），首个命中即返回。
    """
    if not affinity_keywords:
        return None
    for d, labels in ide_dirs_with_src:
        joined = " | ".join(str(x) for x in labels)
        if not any(kw in joined for kw in affinity_keywords):
            continue
        candidate = d / tool
        if candidate.exists() and os.access(str(candidate), os.X_OK):
            return {
                "path": str(candidate),
                "source": "ide",
                "env_keys": [],
                "ide_labels": list(labels),
            }
    return None


def _print_add2line_detection_explainer(status: Dict[str, Any]) -> None:
    detected = {
        name
        for name, meta in (status.get("tool_status") or {}).items()
        if (meta.get("path") or "").strip()
    }
    recommendations = _platform_tool_recommendations()
    platform_key = _current_platform_key()
    platform_labels = {
        "macos": "macOS / iOS",
        "android": "Android",
        "linux": "Linux",
        "harmonyos": "HarmonyOS",
    }
    ordered_platforms = ["macos", "android", "linux", "harmonyos"]
    platform_hits: Dict[str, List[str]] = {}
    for p in ordered_platforms:
        platform_hits[p] = [t for t in recommendations.get(p, []) if t in detected]

    print("各平台可用工具：")
    for p in ordered_platforms:
        hits = platform_hits[p]
        if hits:
            print(f"- {platform_labels[p]}: {_green(', '.join(hits))}")
        else:
            print(f"- {platform_labels[p]}: {_red('无可用工具')}")
    print("")

    addr2line_meta = (status.get("tool_status") or {}).get("llvm-addr2line") or {}
    if addr2line_meta.get("aliased_from") == "llvm-symbolizer":
        sym_path = addr2line_meta.get("path") or ""
        print(_yellow(
            "提示：未直接发现 llvm-addr2line，已自动使用 llvm-symbolizer 作为兼容回退（"
            f"{sym_path}）。两者底层是同一个工具，将以 GNU addr2line 兼容输出方式调用。"
        ))
        print("")

    ide_hits: List[Tuple[str, str, List[str]]] = []
    for tool_name, meta in (status.get("tool_status") or {}).items():
        if meta.get("source") != "ide":
            continue
        if not (meta.get("path") or "").strip():
            continue
        ide_hits.append(
            (tool_name, str(meta.get("path") or ""), list(meta.get("ide_labels") or []))
        )
    if ide_hits:
        print(_yellow("提示：以下工具来自 IDE / SDK 默认安装路径（IDE 升级 / 卸载后路径可能变化，建议正式环境用环境变量固定）："))
        for tool_name, tool_path, labels in ide_hits:
            label_disp = ", ".join(labels) if labels else "未知"
            print(f"- {tool_name}  ← {label_disp}  ({tool_path})")
        print("")

    need_config = [platform_labels[p] for p in ordered_platforms if not platform_hits[p]]
    if need_config:
        print(_red(f"需要配置的平台: {', '.join(need_config)}"))
    else:
        print(_green("所有平台均检测到至少一个推荐工具。"))
    print("")


def _configure_add2line_only() -> None:
    _ensure_user_config_templates()
    status = _doctor_status()
    print("== 符号化工具检测 ==")
    print("")
    _print_add2line_detection_explainer(status)

    recommendations = _platform_tool_recommendations()
    detected_names = {
        name
        for name, meta in (status.get("tool_status") or {}).items()
        if (meta.get("path") or "").strip()
    }
    current_platform = _current_platform_key()
    current_hits = [
        t for t in recommendations.get(current_platform, []) if t in detected_names
    ]
    if current_hits:
        rerun = _prompt_select(
            "是否需要重新设置？",
            [
                ("keep", "不用了，返回"),
                ("reconfig", "重新设置"),
            ],
            default_index=0,
        )
        if rerun == "keep":
            return

    print("")
    print("—— 进入交互向导（将选择平台并填写工具路径等）——")
    print("")
    changed = _update_add2line_config_interactive()
    if changed:
        _show_success_panel("✅ 已完成符号化工具配置", ["你可以返回上一级继续操作。"])
        return
    _prompt_select(
        "已取消向导或未保存修改。",
        [
            ("back", "返回"),
        ],
        default_index=0,
    )
    return


def _configure_add2line_manual_panel() -> None:
    _ensure_user_config_templates()
    target = _user_add2line_config_file()
    while True:
        status = _doctor_status()
        print("== 符号化工具检测 ==")
        print(f"- 配置文件: {_user_add2line_config_file()}")
        print(
            "- 状态: "
            + (_green("已检测到可用工具") if status.get("tool_ok") else _red("未检测到可用工具"))
        )
        available_tools: List[str] = []
        for name, meta in status.get("tool_status", {}).items():
            if not (meta.get("path") or "").strip():
                continue
            tag = str(meta.get("source") or "")
            if tag == "ide":
                ide_labels = meta.get("ide_labels") or []
                first_label = next((str(x) for x in ide_labels if x), "")
                if first_label:
                    tag = f"ide: {first_label}"
            if meta.get("aliased_from"):
                tag = f"{tag} via {meta.get('aliased_from')}"
            available_tools.append(f"{name}({tag})")
        print(f"- 已检测工具: {', '.join(available_tools) if available_tools else '无'}")
        print("")

        print("━━━━━━━━━━━━━━━━━━━━━━")
        print("配置堆栈地址解析工具（手动方式）")
        print("")
        print("请编辑以下文件并填写工具路径：")
        print("")
        print(str(target))
        print("")
        print("完成后返回此窗口。")
        print("━━━━━━━━━━━━━━━━━━━━━━")
        action = _prompt_select(
            "请选择操作",
            [
                ("open", "[o] 打开文件"),
                ("check", "[c] 我已完成，检查配置是否正确"),
                ("help", "[h] 查看关键字段"),
                ("back", "[b] 返回"),
            ],
            default_index=0,
        )
        if action == "back":
            return
        if action == "help":
            print("关键字段：")
            print("- platforms.<platform>.tool_paths")
            print("- platforms.<platform>.environment_vars")
            print("")
            print("— 请在下方的菜单中继续操作 —")
            if _is_tty_interactive():
                _safe_input("按回车显示菜单... ")
            continue
        if action == "open":
            ok = _open_file_with_editor(target)
            if ok:
                print("已尝试用系统默认文本编辑器打开；若未看到窗口，请检查是否被其他桌面空间遮挡。")
            else:
                print(f"无法自动打开，请手动打开: {target}")
            print("")
            print("— 请在下方的菜单中继续操作 —")
            if _is_tty_interactive():
                _safe_input("按回车显示菜单... ")
            continue
        st2 = _doctor_status()
        if st2.get("tool_ok"):
            _show_success_panel("✅ 符号化工具检测通过", [f"已检测工具: {', '.join(available_tools) if available_tools else 'N/A'}"])
            return
        print(_red("仍未检测到可用的堆栈地址解析工具，请继续编辑后再检查。"))
        print("")
        print("— 请在下方的菜单中继续操作 —")
        if _is_tty_interactive():
            _safe_input("按回车显示菜单... ")


def _handle_config_command(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Stability Analysis Agent 配置管理")
    sub = parser.add_subparsers(dest="config_cmd")
    sub.add_parser("init", help="交互初始化配置")
    sub.add_parser("path", help="显示配置路径与生效文件")
    sub.add_parser("doctor", help="检查配置与工具可用性")
    args = parser.parse_args(argv)
    cmd = args.config_cmd
    if cmd == "init":
        return _config_command_init()
    if cmd == "path":
        return _config_command_path()
    if cmd == "doctor":
        return _config_command_doctor()
    parser.print_help()
    return 1


def _handle_profile_command(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Stability Analysis Agent Profile 管理")
    sub = parser.add_subparsers(dest="profile_cmd")

    sub.add_parser("list", help="列出 profile")
    p_use = sub.add_parser("use", help="设置默认 profile")
    p_use.add_argument("name", help="profile 名称")
    p_delete = sub.add_parser("delete", help="删除 profile")
    p_delete.add_argument("name", help="profile 名称")
    p_show = sub.add_parser("show", help="显示 profile 内容")
    p_show.add_argument("name", help="profile 名称")
    p_save = sub.add_parser("save", help="从当前 session 保存 profile")
    p_save.add_argument("name", help="profile 名称")
    args = parser.parse_args(argv)

    cmd = args.profile_cmd
    if cmd == "list":
        items = _list_profiles()
        if not items:
            print("暂无 profile")
            return 0
        state = _load_session_state()
        default_name = str(state.get("default_profile", "")).strip()
        for item in items:
            suffix = " (default)" if item == default_name else ""
            print(f"- {item}{suffix}")
        return 0
    if cmd == "use":
        _load_profile(args.name)
        state = _load_session_state()
        state["default_profile"] = args.name
        _save_session_state(state)
        print(f"默认 profile 已设置为: {args.name}")
        return 0
    if cmd == "delete":
        if _delete_profile(args.name):
            print(f"已删除 profile: {args.name}")
            return 0
        print(f"profile 不存在: {args.name}", file=sys.stderr)
        return 1
    if cmd == "show":
        print(json.dumps(_load_profile(args.name), ensure_ascii=False, indent=2))
        return 0
    if cmd == "save":
        state = _load_session_state()
        last = state.get("last_run", {})
        if not isinstance(last, dict) or not last.get("crash_log"):
            print("错误: 当前没有可保存的 last_run，会话成功运行一次后再执行。", file=sys.stderr)
            return 1
        target = _save_profile(args.name, last)
        print(f"profile 已保存: {target}")
        return 0

    parser.print_help()
    return 1


def _handle_cancel_command(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="取消 daemon 中正在运行的任务")
    parser.add_argument("run_id", help="需要取消的 run_id")
    parser.add_argument("--daemon", default="http://127.0.0.1:8765", help="daemon 地址（默认: http://127.0.0.1:8765）")
    parser.add_argument("--timeout", type=float, default=10.0, help="请求超时时间（秒，默认 10）")
    args = parser.parse_args(argv)

    base = str(args.daemon).strip().rstrip("/")
    run_id = str(args.run_id).strip()
    if not run_id:
        print("错误: run_id 不能为空", file=sys.stderr)
        return 1
    url = f"{base}/runs/{run_id}/cancel"
    req = urllib.request.Request(url=url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=max(0.1, float(args.timeout))) as resp:
            body = resp.read().decode("utf-8", errors="ignore").strip()
            if body:
                print(body)
            else:
                print(json.dumps({"run_id": run_id, "status": "canceled"}, ensure_ascii=False))
            return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore").strip()
        print(f"错误: 取消任务失败（HTTP {exc.code}）", file=sys.stderr)
        if detail:
            print(detail, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"错误: 无法连接 daemon: {exc}", file=sys.stderr)
        return 1


def _register_third_party_modules(registry: ToolAndWorkflowRegistry, modules: List[str]) -> None:
    for mod_name in modules:
        m = (mod_name or "").strip()
        if not m:
            continue
        module = importlib.import_module(m)
        if hasattr(module, "register_all"):
            module.register_all(registry)  # type: ignore[attr-defined]
        elif hasattr(module, "register"):
            module.register(registry)  # type: ignore[attr-defined]
        else:
            raise RuntimeError(f"插件模块 {m} 缺少 register_all(registry) 或 register(registry)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Stability Analysis Agent CLI (Tool System Unified)",
        epilog="无参数运行 `sa-agent` 将进入交互向导并完成配置与分析。",
    )
    p.add_argument("--crash-log", required=False, help="崩溃日志文件路径；使用 '-' 表示从 stdin 读取")
    p.add_argument(
        "--library-dir",
        required=False,
        help="符号库目录（含 .so / .dylib / .dSYM 等带调试符号的二进制；日志已含函数名+行号可省略）",
    )
    p.add_argument(
        "--code-root",
        action="append",
        dest="code_roots",
        help="项目 C/C++ 源码目录（可重复指定；建议精确到工程/模块根，避免传整个仓库根）",
    )
    p.add_argument("--config", required=False, help="SystemConfig JSON 文件")
    p.add_argument("--vector-db-path", default="./vector_db", help="向量数据库目录（默认: ./vector_db）")
    p.add_argument("--vector-db-max-results", type=int, default=3, help="向量检索最大返回数")
    p.add_argument("--rule-confidence-threshold", type=float, default=0.85, help="规则高置信阈值")
    p.add_argument("--init-vector-db", action="store_true", help="初始化向量数据库（先清空再写入种子）")
    p.add_argument("--vector-db-stats", action="store_true", help="输出向量数据库统计信息")
    p.add_argument("--export-vector-db", nargs="?", const="", default=None, help="导出向量库快照；可选输出文件路径")
    p.add_argument("--import-vector-db", default=None, help="从快照文件导入向量库（upsert 合并）")
    p.add_argument("--pattern-feedback", default=None, help="记录 pattern 反馈，参数为 pattern_id")
    p.add_argument("--feedback-type", choices=["adopted", "rejected"], default=None, help="反馈类型")
    p.add_argument("--feedback-comment", default="", help="反馈备注")
    p.add_argument("--vector-db-decay", type=float, default=None, help="执行置信度衰减（示例 0.01）")
    p.add_argument("--vector-db-gc", action="store_true", help="执行模式治理（低置信或高拒绝标记 deprecated）")
    p.add_argument("--gc-min-confidence", type=float, default=0.2, help="GC 最低置信阈值")
    p.add_argument("--gc-rejected-threshold", type=int, default=5, help="GC 拒绝次数阈值")
    p.add_argument("--output-format", default="markdown", choices=["markdown", "json", "text"], help="输出格式")
    p.add_argument("--output-file", default=None, help="输出文件；不指定则打印到 stdout")
    p.add_argument(
        "--scope",
        default="full",
        choices=["full", "prompt_only", "parse_only", "parse_log_only"],
        help=(
            "Agent 执行流程范围（默认 full）："
            "full=解析+符号化+取代码上下文+AI 推理；"
            "prompt_only=完整工具链但不调用 AI，仅生成可复用提示词；"
            "parse_only=仅解析+符号化；"
            "parse_log_only=仅解析崩溃日志"
        ),
    )
    p.add_argument(
        "--apply-ai-fixes",
        dest="apply_ai_fixes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否基于 AI 建议回写源码（默认开启；仅在 --scope full 且 LLM 可用时生效，使用 --no-apply-ai-fixes 关闭）",
    )
    p.add_argument(
        "--backup-original-sources",
        dest="backup_original_sources",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="应用 AI 修复前是否在 cli_reports 下备份改前源码（默认开启；代码已由 Git 管理可用 --no-backup-original-sources 关闭）",
    )
    p.add_argument(
        "--engine",
        default="direct",
        choices=["direct", "langchain", "langgraph"],
        help="AI 推理模式：direct=直调 LLM, langchain=工具编排, langgraph=多轮 Agent",
    )
    p.add_argument(
        "--plugin-module",
        action="append",
        dest="plugin_modules",
        help="第三方扩展模块（可重复）：需提供 register_all(registry) 或 register(registry)",
    )
    p.add_argument(
        "--interactive",
        dest="interactive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否启用交互模式（默认：无参数且在 TTY 中自动启用）",
    )
    p.add_argument(
        "--print-full-report",
        action="store_true",
        help=(
            "将完整报告打印到标准输出（默认：在 stdout 为终端且已生成 cli_reports/final_output.md 时"
            "仅打印摘要，避免与落盘内容重复；管道或非终端仍输出全文）"
        ),
    )
    p.add_argument(
        "--max-sibling-member-functions",
        type=int,
        default=0,
        help=(
            "code_content_provider：同类「兄弟成员函数」最多纳入条数（默认 0=关闭）。"
            "大代码库建议保持 0，依赖共享变量关联扩展相关函数。"
        ),
    )
    p.add_argument(
        "--max-shared-var-related-functions",
        type=int,
        default=20,
        help=(
            "code_content_provider：共享变量关联的函数-变量关系最多保留条数（默认 20，范围 1～512）；"
            "final_tip 中「共享状态的其它读写方」源码块数量亦参考该上限（另设硬顶 20）。"
        ),
    )
    p.add_argument(
        "--min-key-read-related-functions",
        type=int,
        default=2,
        help=(
            "code_content_provider：在共享变量关系截断时，关键读路径最少保留条数（默认 2，允许 0）；"
            "用于避免 modify/get/read 类关键读取函数被写路径完全挤出。"
        ),
    )
    return p


def _run_vector_db_command(args: argparse.Namespace) -> Optional[int]:
    need_vector_cmd = any(
        [
            bool(args.init_vector_db),
            bool(args.vector_db_stats),
            args.export_vector_db is not None,
            bool(args.import_vector_db),
            bool(args.pattern_feedback),
            args.vector_db_decay is not None,
            bool(args.vector_db_gc),
        ]
    )
    if not need_vector_cmd:
        return None

    _ensure_rag_runtime_loaded()
    if not RAG_RUNTIME_AVAILABLE or AIStabilityAnalyzerWithVectorDB is None:
        print("错误: RAG 运行时不可用，请安装向量数据库依赖后重试。", file=sys.stderr)
        return 1

    analyzer = AIStabilityAnalyzerWithVectorDB(vector_db_path=args.vector_db_path)

    if args.init_vector_db:
        analyzer.clear_all()
        init_vector_rules(analyzer)
        init_vector_patterns(analyzer)
        init_vector_evidence(analyzer)
        init_vector_strategies(analyzer)
        init_vector_guidance_blocks(analyzer)
        print(json.dumps(analyzer.get_database_statistics(), ensure_ascii=False, indent=2))
        return 0

    if args.vector_db_stats:
        print(json.dumps(analyzer.get_database_statistics(), ensure_ascii=False, indent=2))
        return 0

    if args.export_vector_db is not None:
        snapshot = analyzer.export_snapshot()
        output_path = args.export_vector_db.strip() if isinstance(args.export_vector_db, str) else ""
        if not output_path:
            output_path = str((_runtime_output_root() / "cli_reports" / "vector_db_snapshot.json").resolve())
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(out_file))
        return 0

    if args.import_vector_db:
        in_file = Path(args.import_vector_db).expanduser().resolve()
        if not in_file.exists():
            print(f"错误: 快照文件不存在: {in_file}", file=sys.stderr)
            return 1
        snapshot = json.loads(in_file.read_text(encoding="utf-8"))
        result = analyzer.import_snapshot(snapshot)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.pattern_feedback:
        if not args.feedback_type:
            print("错误: 使用 --pattern-feedback 时必须提供 --feedback-type", file=sys.stderr)
            return 1
        analyzer.record_feedback(args.pattern_feedback, args.feedback_type, args.feedback_comment or "")
        print(json.dumps({"status": "ok"}, ensure_ascii=False))
        return 0

    if args.vector_db_decay is not None:
        analyzer.decay_confidence(args.vector_db_decay)
        print(json.dumps({"status": "ok", "decay": args.vector_db_decay}, ensure_ascii=False))
        return 0

    if args.vector_db_gc:
        deprecated = analyzer.gc_patterns(
            min_confidence=args.gc_min_confidence,
            rejected_threshold=args.gc_rejected_threshold,
        )
        print(json.dumps({"deprecated_pattern_ids": deprecated}, ensure_ascii=False, indent=2))
        return 0

    return None


def _sanitize_report_name(name: str) -> str:
    text = (name or "stdin").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    return text.strip("_") or "stdin"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _strip_outer_fence(text: Optional[str]) -> Optional[str]:
    """去除模型返回最外层 markdown 围栏，保留内部代码块。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s.startswith("```"):
        return s
    first_newline = s.find("\n")
    if first_newline < 0:
        return s
    if not s.endswith("```"):
        return s
    inner = s[first_newline + 1 : -3].strip()
    return inner


def _build_report_dir(args: argparse.Namespace) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    scope = str(getattr(args, "scope", "full") or "full")
    mode = f"analysis_{scope}"
    crash_name = _sanitize_report_name(Path(args.crash_log).stem if args.crash_log and args.crash_log != "-" else "stdin")
    dirname = f"{stamp}_{mode}_{args.engine}_{crash_name}"
    return _runtime_output_root() / "cli_reports" / dirname


def _write_cli_report(
    report_dir: Path,
    result: Dict[str, Any],
    rendered_output: str,
    applied_fix_result: Optional[Dict[str, Any]] = None,
    write_readme_output: bool = True,
    scope: str = "full",
) -> Optional[Path]:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        if result.get("parse_result") is not None:
            _write_json(report_dir / "01_crash_log_parser.json", result.get("parse_result"))
        if scope in {"full", "prompt_only", "parse_only"} and result.get("resolved_stack") is not None:
            _write_json(report_dir / "02_add2line_resolver.json", result.get("resolved_stack"))
        if scope in {"full", "prompt_only"} and result.get("code_context") is not None:
            _write_json(report_dir / "03_code_content_provider.json", result.get("code_context"))
        final_tip = result.get("final_tip")
        if final_tip is None:
            final_tip = result.get("analysis")
        if final_tip is not None:
            round_dir = report_dir / "round_0"
            round_dir.mkdir(parents=True, exist_ok=True)
            (round_dir / "05_ai_final_tip.txt").write_text(str(final_tip), encoding="utf-8")
        analysis_text = _strip_outer_fence(result.get("analysis"))
        if analysis_text is not None:
            round_dir = report_dir / "round_0"
            round_dir.mkdir(parents=True, exist_ok=True)
            (round_dir / "06_ai_res.txt").write_text(str(analysis_text), encoding="utf-8")
        if applied_fix_result is not None:
            _write_json(report_dir / "06_apply_ai_fixes.json", applied_fix_result)
        if write_readme_output:
            final_output_text = analysis_text if analysis_text is not None else rendered_output
            (report_dir / "final_output.md").write_text(str(final_output_text), encoding="utf-8")
        return report_dir
    except Exception as exc:
        print(f"警告: 写入 cli_reports 失败: {exc}", file=sys.stderr)
        return None


def _extract_candidate_nodes(code_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    graph = code_context.get("graph", {}) if isinstance(code_context, dict) else {}
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    out: List[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        file_path = node.get("file")
        signature = node.get("signature")
        snippet = node.get("snippet")
        if not file_path or not signature or not isinstance(snippet, list) or not snippet:
            continue
        out.append(
            {
                "file": str(Path(file_path).resolve()),
                "signature": str(signature),
                "snippet": [str(line) for line in snippet],
                "snippet_start_line": node.get("snippet_start_line"),
                "snippet_end_line": node.get("snippet_end_line"),
            }
        )
    return out


def _extract_json_payload(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("AI 未返回结构化修改计划")
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence_match:
        text = fence_match.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("结构化修改计划必须是 JSON 对象")
    return payload


def _safe_extract_json_payload(raw_text: str) -> Dict[str, Any]:
    try:
        payload = _extract_json_payload(raw_text)
        normalized_edits: List[Dict[str, Any]] = []
        for edit in payload.get("edits", []) if isinstance(payload.get("edits"), list) else []:
            if not isinstance(edit, dict):
                continue
            normalized_edits.append(
                {
                    "file": str(edit.get("file", "")),
                    "function_signature": str(edit.get("function_signature", "")),
                    "replacement_code": str(
                        edit.get("replacement_code")
                        or edit.get("replacement")
                        or edit.get("code")
                        or ""
                    ).strip(),
                    "reason": str(edit.get("reason", "")),
                }
            )
        payload = {"summary": str(payload.get("summary", "")), "edits": normalized_edits}
        if isinstance(payload.get("edits"), list):
            return payload
        return {"summary": str(payload.get("summary") or ""), "edits": []}
    except Exception:
        return {"summary": "", "edits": []}


def _build_fix_plan_prompt(
    parse_result: Dict[str, Any],
    code_context: Dict[str, Any],
    analysis_text: str,
    candidate_nodes: List[Dict[str, Any]],
    required_targets: Optional[List[Dict[str, str]]] = None,
) -> str:
    crash_summary = code_context.get("crash_summary", {}) if isinstance(code_context, dict) else {}
    concise_nodes = [
        {
            "file": node["file"],
            "function_signature": node["signature"],
            "snippet_start_line": node.get("snippet_start_line"),
            "snippet_end_line": node.get("snippet_end_line"),
            "snippet": node["snippet"],
        }
        for node in candidate_nodes
    ]
    required_text = ""
    if required_targets:
        required_text = (
            f"\n必须覆盖的目标函数（请为每个目标至少输出一个 edit）："
            f"{json.dumps(required_targets, ensure_ascii=False)}\n"
        )
    return (
        "你是代码修复执行器。请根据崩溃上下文和现有 AI 分析，输出“可直接落盘”的最小修改计划。\n"
        "只允许修改下面 candidate_nodes 中出现的函数；不要新建文件，不要引用不存在的文件，不要输出 Markdown。\n"
        "若无法安全修改，必须在 reason 里给出具体阻塞原因；不要省略目标。\n"
        "先执行一致性自检：必须先以 candidate_nodes.snippet 为准校验 analysis_text；若两者冲突，忽略 analysis_text 的冲突结论。\n"
        "禁止基于与 snippet 冲突的假设生成 edits（例如把“已存在的锁/判空/边界检查”当作缺失）。\n"
        "严禁输出与候选函数 snippet 等价的 replacement_code（仅空白、缩进、换行或注释差异也视为等价）。\n"
        "每条 edit 必须包含可验证的行为变化（条件、锁、边界、生命周期、错误处理等至少一类）。\n"
        "若无法给出“行为变化”且可编译的修复代码，必须返回 edits=[]，并在 summary 说明阻塞原因。\n"
        "非常重要：你的回复必须是一个 JSON 对象，不允许包含任何额外文本、解释、标题、代码围栏。\n\n"
        "输出必须是严格 JSON，格式如下：\n"
        "{\n"
        '  "summary": "一句话说明修复意图",\n'
        '  "edits": [\n'
        "    {\n"
        '      "file": "candidate_nodes 中的绝对路径",\n'
        '      "function_signature": "candidate_nodes 中的函数签名",\n'
        '      "replacement_code": "完整的替换后函数代码",\n'
        '      "reason": "为什么修改这个函数"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "要求：\n"
        "1. replacement_code 必须是完整函数代码，能够直接替换原函数。\n"
        "2. replacement_code 必须与原 snippet 存在“实质行为差异”；仅格式变化禁止。\n"
        "3. 若 analysis_text 与 snippet 冲突，必须以 snippet 为准，并在 reason 中简述已纠偏。\n"
        "4. 优先做最小修复，避免引入新依赖；但必须覆盖要求的目标函数。\n"
        "5. 不要修改无关逻辑。\n"
        "6. 如果现有 AI 分析与 candidate_nodes 冲突，以 candidate_nodes 的真实代码为准。\n\n"
        f"parse_result={json.dumps(parse_result, ensure_ascii=False)}\n\n"
        f"crash_summary={json.dumps(crash_summary, ensure_ascii=False)}\n\n"
        f"candidate_nodes={json.dumps(concise_nodes, ensure_ascii=False)}\n"
        f"{required_text}\n"
        f"analysis_text={analysis_text}"
    )


def _extract_required_function_names_from_analysis(analysis_text: str) -> List[str]:
    names: List[str] = []
    in_section = False
    for raw_line in (analysis_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "需要修改的函数" in line:
            in_section = True
            continue
        if in_section and line.startswith("#### "):
            break
        if not in_section:
            continue
        m = re.match(r"^-\s*`([^`]+)`", line)
        if m:
            names.append(m.group(1).strip())
    dedup: List[str] = []
    for n in names:
        if n and n not in dedup:
            dedup.append(n)
    return dedup


def _extract_simple_function_name(signature: str) -> str:
    s = str(signature or "").strip()
    m = re.search(r"([~]?[A-Za-z_]\w*)\s*\(", s)
    return m.group(1) if m else s


def _select_required_targets(
    candidate_nodes: List[Dict[str, Any]],
    required_names: List[str],
) -> List[Dict[str, str]]:
    targets: List[Dict[str, str]] = []
    if not required_names:
        return targets
    for name in required_names:
        simple = _extract_simple_function_name(name)
        for node in candidate_nodes:
            sig = str(node.get("signature", ""))
            sig_simple = _extract_simple_function_name(sig)
            if name in sig or sig in name or (simple and simple == sig_simple):
                targets.append({"file": str(node["file"]), "function_signature": sig})
                break
    dedup: List[Dict[str, str]] = []
    seen = set()
    for t in targets:
        key = (t["file"], t["function_signature"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(t)
    return dedup


def _merge_fix_plan_edits(base_plan: Dict[str, Any], new_plan: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base_plan or {})
    merged["summary"] = (new_plan or {}).get("summary") or merged.get("summary") or ""
    merged_edits: List[Dict[str, Any]] = []
    seen = set()
    for plan in (base_plan or {}, new_plan or {}):
        for edit in plan.get("edits", []) if isinstance(plan, dict) else []:
            if not isinstance(edit, dict):
                continue
            key = (str(edit.get("file", "")), str(edit.get("function_signature", "")))
            if key in seen:
                continue
            seen.add(key)
            merged_edits.append(edit)
    merged["edits"] = merged_edits
    return merged


def _upsert_fix_plan_edit(fix_plan: Dict[str, Any], edit: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(fix_plan, dict):
        fix_plan = {"summary": "", "edits": []}
    edits = fix_plan.get("edits", [])
    if not isinstance(edits, list):
        edits = []
    key = (str(edit.get("file", "")), str(edit.get("function_signature", "")))
    out: List[Dict[str, Any]] = []
    replaced = False
    for e in edits:
        if not isinstance(e, dict):
            continue
        k = (str(e.get("file", "")), str(e.get("function_signature", "")))
        if k == key:
            out.append(edit)
            replaced = True
        else:
            out.append(e)
    if not replaced:
        out.append(edit)
    return {"summary": str(fix_plan.get("summary", "")), "edits": out}


def _build_single_target_fix_prompt(
    parse_result: Dict[str, Any],
    crash_summary: Dict[str, Any],
    analysis_text: str,
    target_node: Dict[str, Any],
) -> str:
    node_payload = {
        "file": target_node["file"],
        "function_signature": target_node["signature"],
        "snippet_start_line": target_node.get("snippet_start_line"),
        "snippet_end_line": target_node.get("snippet_end_line"),
        "snippet": target_node["snippet"],
    }
    return (
        "你是代码修复执行器。现在只允许修复一个函数，输出严格 JSON。\n"
        "不要输出 Markdown，不要解释。\n"
        "严禁输出与 target_node.snippet 等价的 replacement_code（仅空白或注释差异也视为等价）。\n"
        "replacement_code 必须包含至少一处可验证的行为变化；否则返回 edits=[] 并在 summary 写明阻塞原因。\n"
        "输出格式：\n"
        "{\n"
        '  "summary": "一句话",\n'
        '  "edits": [\n'
        "    {\n"
        f'      "file": "{target_node["file"]}",\n'
        f'      "function_signature": "{target_node["signature"]}",\n'
        '      "replacement_code": "完整函数代码",\n'
        '      "reason": "修改原因"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "要求：\n"
        "1. edits 只能包含这个目标函数，且最多 1 条。\n"
        "2. replacement_code 必须是完整函数代码，可直接替换，且具有实质行为差异。\n"
        "3. 若无法安全修改，返回 edits 为空数组，并在 summary 说明原因。\n\n"
        f"parse_result={json.dumps(parse_result, ensure_ascii=False)}\n\n"
        f"crash_summary={json.dumps(crash_summary, ensure_ascii=False)}\n\n"
        f"target_node={json.dumps(node_payload, ensure_ascii=False)}\n\n"
        f"analysis_text={analysis_text}"
    )


def _extract_code_blocks_from_analysis_text(analysis_text: str) -> List[str]:
    blocks: List[str] = []
    lines = str(analysis_text or "").splitlines()
    in_block = False
    cur: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_block:
            if re.match(r"^```(?:cpp|c\+\+|cc|cxx)?\s*$", stripped, re.I):
                in_block = True
                cur = []
            continue
        if stripped.startswith("```"):
            block = "\n".join(cur).strip("\n")
            if block:
                blocks.append(block)
            in_block = False
            cur = []
            continue
        cur.append(line)
    if in_block and cur:
        # 容忍模型输出缺失 closing fence 的情况，直接截到文件末尾
        block = "\n".join(cur).strip("\n")
        if block:
            blocks.append(block)
    return blocks


def _contains_placeholder_code(text: str) -> bool:
    s = str(text or "")
    markers = ("...", "…", "其他代码保持不变", "前置代码", "后续代码")
    return any(mark in s for mark in markers)


def _is_valid_replacement_code(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    if _contains_placeholder_code(s):
        return False
    return s.count("{") > 0 and s.count("{") == s.count("}")


def _build_single_target_completion_prompt(
    parse_result: Dict[str, Any],
    crash_summary: Dict[str, Any],
    analysis_text: str,
    target_node: Dict[str, Any],
    draft_code: str = "",
) -> str:
    node_payload = {
        "file": target_node["file"],
        "function_signature": target_node["signature"],
        "snippet_start_line": target_node.get("snippet_start_line"),
        "snippet_end_line": target_node.get("snippet_end_line"),
        "snippet": target_node["snippet"],
    }
    return (
        "你是 C++ 代码修复器。请仅输出一个函数的完整 replacement_code（可直接替换原函数）。\n"
        "严禁输出省略号、占位注释、伪代码。\n"
        "严禁修改函数签名、文件路径。\n"
        "严禁输出与 target_node.snippet 等价的代码（仅空白/注释差异也视为等价）。\n"
        "replacement_code 必须包含至少一处可验证的行为变化；否则返回 edits=[] 并说明原因。\n"
        "若无法给出完整函数，返回 edits 为空数组并给出原因。\n\n"
        "输出必须是严格 JSON：\n"
        "{\n"
        '  "summary": "一句话",\n'
        '  "edits": [\n'
        "    {\n"
        f'      "file": "{target_node["file"]}",\n'
        f'      "function_signature": "{target_node["signature"]}",\n'
        '      "replacement_code": "完整函数代码（必须可编译）",\n'
        '      "reason": "修改原因"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"parse_result={json.dumps(parse_result, ensure_ascii=False)}\n\n"
        f"crash_summary={json.dumps(crash_summary, ensure_ascii=False)}\n\n"
        f"target_node={json.dumps(node_payload, ensure_ascii=False)}\n\n"
        f"draft_code={draft_code}\n\n"
        f"analysis_text={analysis_text}"
    )


def _extract_function_block_from_code(code_text: str, start_index: int) -> Optional[str]:
    brace_index = code_text.find("{", start_index)
    if brace_index < 0:
        return None
    line_start = code_text.rfind("\n", 0, start_index)
    if line_start < 0:
        line_start = 0
    else:
        line_start += 1
    depth = 0
    end_index = -1
    for i in range(brace_index, len(code_text)):
        ch = code_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_index = i
                break
    if end_index < 0:
        return None
    block = code_text[line_start : end_index + 1].strip()
    if not block:
        return None
    if _contains_placeholder_code(block):
        return None
    return block


def _extract_replacement_from_analysis_text(
    analysis_text: str,
    target_signature: str,
) -> Optional[str]:
    blocks = _extract_code_blocks_from_analysis_text(analysis_text)
    if not blocks:
        return None
    target_signature = str(target_signature or "").strip()
    simple_name = _extract_simple_function_name(target_signature)
    for code in blocks:
        idx = code.find(target_signature) if target_signature else -1
        if idx >= 0:
            block = _extract_function_block_from_code(code, idx)
            if block:
                return block
        if simple_name:
            pattern = re.compile(rf"\b{re.escape(simple_name)}\s*\(", re.M)
            for m in pattern.finditer(code):
                block = _extract_function_block_from_code(code, m.start())
                if block and _extract_simple_function_name(block) == simple_name:
                    return block
    return None


def _fill_empty_replacements_from_analysis_text(
    fix_plan: Dict[str, Any],
    analysis_text: str,
) -> Dict[str, Any]:
    if not isinstance(fix_plan, dict):
        return {"summary": "", "edits": []}
    edits = fix_plan.get("edits", [])
    if not isinstance(edits, list):
        return {"summary": str(fix_plan.get("summary", "")), "edits": []}
    new_edits: List[Dict[str, Any]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        merged = dict(edit)
        replacement = str(merged.get("replacement_code", "")).strip()
        if not replacement:
            sig = str(merged.get("function_signature", ""))
            fallback = _extract_replacement_from_analysis_text(analysis_text, sig)
            if fallback:
                merged["replacement_code"] = fallback
                merged["reason"] = str(merged.get("reason") or "") + "；fallback=analysis_text"
        new_edits.append(merged)
    return {"summary": str(fix_plan.get("summary", "")), "edits": new_edits}


def _append_missing_edits_from_analysis_text(
    fix_plan: Dict[str, Any],
    analysis_text: str,
    required_targets: List[Dict[str, str]],
) -> Dict[str, Any]:
    if not isinstance(fix_plan, dict):
        fix_plan = {"summary": "", "edits": []}
    edits = fix_plan.get("edits", [])
    if not isinstance(edits, list):
        edits = []
    existing_keys = {
        (str(e.get("file", "")), str(e.get("function_signature", "")))
        for e in edits
        if isinstance(e, dict)
    }
    appended = list(edits)
    for t in required_targets or []:
        file_path = str(t.get("file", ""))
        signature = str(t.get("function_signature", ""))
        key = (file_path, signature)
        if not file_path or not signature or key in existing_keys:
            continue
        replacement = _extract_replacement_from_analysis_text(analysis_text, signature)
        if not replacement:
            continue
        appended.append(
            {
                "file": file_path,
                "function_signature": signature,
                "replacement_code": replacement,
                "reason": "fallback=analysis_text_missing_edit",
            }
        )
        existing_keys.add(key)
    return {"summary": str(fix_plan.get("summary", "")), "edits": appended}


def _is_within_code_roots(path: Path, code_roots: List[str]) -> bool:
    for root in code_roots:
        try:
            path.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _find_containing_code_root(path: Path, code_roots: List[str]) -> Optional[Path]:
    """返回包含 path 的最具体 code_root（最长前缀匹配）。"""
    best: Optional[Path] = None
    for root in code_roots:
        root_path = Path(root).resolve()
        try:
            path.relative_to(root_path)
        except ValueError:
            continue
        if best is None or len(str(root_path)) > len(str(best)):
            best = root_path
    return best


def _replace_function_block(source: str, signature: str, replacement_code: str) -> Tuple[str, Optional[str]]:
    sig_index = source.find(signature)
    if sig_index < 0:
        # 兜底：签名文本可能因换行/空格差异无法直接命中，退化为类名+函数名匹配
        owner = ""
        m_owner = re.search(r"([A-Za-z_]\w*)::[~]?[A-Za-z_]\w*\s*\(", signature)
        if m_owner:
            owner = m_owner.group(1)
        func_name = _extract_simple_function_name(signature)
        idx_candidates: List[int] = []
        if owner and func_name:
            pattern = re.compile(
                rf"{re.escape(owner)}\s*::\s*{re.escape(func_name)}\s*\(",
                re.M,
            )
            idx_candidates = [m.start() for m in pattern.finditer(source)]
        if not idx_candidates and func_name:
            pattern2 = re.compile(rf"\b{re.escape(func_name)}\s*\(", re.M)
            idx_candidates = [m.start() for m in pattern2.finditer(source)]
        if idx_candidates:
            sig_index = idx_candidates[0]
    if sig_index < 0:
        return source, None
    brace_start = source.find("{", sig_index)
    if brace_start < 0:
        return source, None
    depth = 0
    end_index = -1
    for idx in range(brace_start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_index = idx
                break
    if end_index < 0:
        return source, None
    old_block = source[sig_index : end_index + 1]
    return source[:sig_index] + replacement_code + source[end_index + 1 :], old_block


def _normalize_code_for_equivalence(text: str) -> str:
    """用于判定改码是否仅为空白差异，避免误报“已修改”。"""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in s.strip().split("\n"))


def _apply_ai_fix_plan(
    fix_plan: Dict[str, Any],
    candidate_nodes: List[Dict[str, Any]],
    code_roots: List[str],
    report_dir: Optional[Path],
    backup_original_sources: bool,
) -> Dict[str, Any]:
    edits = fix_plan.get("edits", []) if isinstance(fix_plan, dict) else []
    candidate_map = {(node["file"], node["signature"]): node for node in candidate_nodes}
    applied: List[Dict[str, Any]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        file_path = Path(str(edit.get("file", ""))).resolve()
        signature = str(edit.get("function_signature", ""))
        replacement_code = str(edit.get("replacement_code", "")).strip("\n")
        reason = str(edit.get("reason", ""))
        record: Dict[str, Any] = {
            "file": str(file_path),
            "function_signature": signature,
            "reason": reason,
            "status": "skipped",
        }
        node = candidate_map.get((str(file_path), signature))
        if node is None:
            record["error"] = "目标函数不在本次代码上下文候选列表中"
            applied.append(record)
            continue
        if not _is_within_code_roots(file_path, code_roots):
            record["error"] = "目标文件不在 code_root 范围内"
            applied.append(record)
            continue
        if not file_path.exists():
            record["error"] = "目标文件不存在"
            applied.append(record)
            continue
        if not replacement_code:
            record["error"] = "replacement_code 为空"
            applied.append(record)
            continue
        original = file_path.read_text(encoding="utf-8")
        snippet_text = "\n".join(node["snippet"])
        new_text = original
        old_block: Optional[str] = None
        if snippet_text in original:
            old_block = snippet_text
            new_text = original.replace(snippet_text, replacement_code, 1)
        else:
            new_text, old_block = _replace_function_block(original, signature, replacement_code)
        if old_block is None or new_text == original:
            record["error"] = "未能在源码中定位待替换函数"
            applied.append(record)
            continue
        if _normalize_code_for_equivalence(old_block) == _normalize_code_for_equivalence(replacement_code):
            record["error"] = "replacement_code 与原函数等价（仅空白或格式差异），已跳过写入"
            applied.append(record)
            continue
        file_path.write_text(new_text, encoding="utf-8")
        if report_dir is not None and backup_original_sources:
            backup_root = report_dir / "original_sources"
            containing_root = _find_containing_code_root(file_path, code_roots)
            if containing_root is None:
                record["error"] = "目标文件未命中任何 code_root，无法回写备份"
                applied.append(record)
                continue
            backup_path = backup_root / file_path.relative_to(containing_root)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if not backup_path.exists():
                backup_path.write_text(original, encoding="utf-8")
            record["backup_path"] = str(backup_path)
        record["status"] = "applied"
        record["replaced_preview"] = old_block
        applied.append(record)
    return {
        "success": any(item.get("status") == "applied" for item in applied),
        "summary": fix_plan.get("summary") if isinstance(fix_plan, dict) else "",
        "applied": applied,
    }


def _maybe_apply_ai_fixes(
    llm_adapter: Any,
    result: Dict[str, Any],
    code_roots: List[str],
    report_dir: Optional[Path],
    backup_original_sources: bool,
) -> Optional[Dict[str, Any]]:
    if llm_adapter is None or not code_roots:
        return {
            "success": False,
            "error": "缺少 LLM 或 code_root，无法应用 AI 修复",
            "applied": [],
        }
    analysis_text = str(result.get("analysis") or "").strip()
    code_context = result.get("code_context", {}) or {}
    parse_result = result.get("parse_result", {}) or {}
    if not analysis_text or not isinstance(code_context, dict):
        return {
            "success": False,
            "error": "缺少 analysis/code_context，无法应用 AI 修复",
            "applied": [],
        }
    candidate_nodes = _extract_candidate_nodes(code_context)
    if not candidate_nodes:
        return {
            "success": False,
            "error": "代码上下文未提供可替换的函数候选",
            "applied": [],
        }
    required_names = _extract_required_function_names_from_analysis(analysis_text)
    required_targets = _select_required_targets(candidate_nodes, required_names)
    prompt = _build_fix_plan_prompt(
        parse_result,
        code_context,
        analysis_text,
        candidate_nodes,
        required_targets=required_targets,
    )
    try:
        first_response_preview = ""
        response = llm_adapter.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是代码修复计划生成器。"
                        "只允许输出一个 JSON 对象。"
                        "禁止输出 markdown、解释文本、代码围栏。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        first_response_preview = str(response.content or "")[:500].replace("\n", " ")
        fix_plan = _safe_extract_json_payload(response.content)
        if required_targets:
            target_keys = {(x["file"], x["function_signature"]) for x in required_targets}
            for _ in range(2):
                got_keys = {
                    (str(e.get("file", "")), str(e.get("function_signature", "")))
                    for e in fix_plan.get("edits", [])
                    if isinstance(e, dict) and str(e.get("replacement_code", "")).strip()
                }
                missing = [x for x in required_targets if (x["file"], x["function_signature"]) not in got_keys]
                if not missing:
                    break
                retry_prompt = (
                    "你上一次返回的 edits 未覆盖全部必改目标。"
                    "请仅补充 missing_targets 的 edits，严格 JSON 输出。\n"
                    f"missing_targets={json.dumps(missing, ensure_ascii=False)}\n"
                    f"candidate_nodes={json.dumps([{'file': n['file'], 'function_signature': n['signature'], 'snippet': n['snippet']} for n in candidate_nodes], ensure_ascii=False)}\n"
                    f"analysis_text={analysis_text}"
                )
                retry_resp = llm_adapter.chat(
                    [
                        {"role": "system", "content": "你输出严格 JSON，不要输出 Markdown 代码块以外的解释。"},
                        {"role": "user", "content": retry_prompt},
                    ],
                    temperature=0.1,
                )
                retry_plan = _safe_extract_json_payload(retry_resp.content)
                fix_plan = _merge_fix_plan_edits(fix_plan, retry_plan)
            # 后半段兜底：仍有缺失时，按函数逐个请求 replacement_code，提升多函数落盘成功率
            got_keys = {
                (str(e.get("file", "")), str(e.get("function_signature", "")))
                for e in fix_plan.get("edits", [])
                if isinstance(e, dict) and str(e.get("replacement_code", "")).strip()
            }
            missing = [x for x in required_targets if (x["file"], x["function_signature"]) not in got_keys]
            if missing:
                crash_summary = code_context.get("crash_summary", {}) if isinstance(code_context, dict) else {}
                node_map = {(str(n["file"]), str(n["signature"])): n for n in candidate_nodes}
                for target in missing:
                    node = node_map.get((target["file"], target["function_signature"]))
                    if node is None:
                        continue
                    single_prompt = _build_single_target_fix_prompt(
                        parse_result=parse_result,
                        crash_summary=crash_summary,
                        analysis_text=analysis_text,
                        target_node=node,
                    )
                    single_plan: Dict[str, Any] = {"summary": "", "edits": []}
                    for attempt in range(2):
                        prompt_text = single_prompt
                        if attempt > 0:
                            prompt_text = (
                                single_prompt
                                + "\n\n再次强调：edits 必须只包含该函数，且 replacement_code 必须为非空完整函数代码。"
                            )
                        single_resp = llm_adapter.chat(
                            [
                                {
                                    "role": "system",
                                    "content": "你输出严格 JSON，不要输出 Markdown 代码块以外的解释。",
                                },
                                {"role": "user", "content": prompt_text},
                            ],
                            temperature=0.1,
                        )
                        parsed_plan = _safe_extract_json_payload(single_resp.content)
                        single_plan = parsed_plan
                        if any(
                            isinstance(e, dict) and str(e.get("replacement_code", "")).strip()
                            for e in parsed_plan.get("edits", [])
                        ):
                            break
                    fix_plan = _merge_fix_plan_edits(fix_plan, single_plan)
        # 兜底补齐：若 fix_plan 中还没有某些 required_target，直接尝试从 06_ai_res 追加新 edit
        fix_plan = _append_missing_edits_from_analysis_text(fix_plan, analysis_text, required_targets)
        # 最终兜底：直接从 06_ai_res 文本代码块提取完整函数体，回填空 replacement_code
        fix_plan = _fill_empty_replacements_from_analysis_text(fix_plan, analysis_text)
        # 强制补全：对于仍缺失或包含占位符的目标函数，逐函数二次请求完整 replacement_code
        if required_targets:
            crash_summary = code_context.get("crash_summary", {}) if isinstance(code_context, dict) else {}
            node_map = {(str(n["file"]), str(n["signature"])): n for n in candidate_nodes}
            current_edits = fix_plan.get("edits", []) if isinstance(fix_plan.get("edits"), list) else []
            edit_map = {
                (str(e.get("file", "")), str(e.get("function_signature", ""))): e
                for e in current_edits
                if isinstance(e, dict)
            }
            for target in required_targets:
                key = (target["file"], target["function_signature"])
                node = node_map.get(key)
                if node is None:
                    continue
                existing = edit_map.get(key, {})
                existing_code = str(existing.get("replacement_code", "")) if isinstance(existing, dict) else ""
                if _is_valid_replacement_code(existing_code):
                    continue
                completion_prompt = _build_single_target_completion_prompt(
                    parse_result=parse_result,
                    crash_summary=crash_summary,
                    analysis_text=analysis_text,
                    target_node=node,
                    draft_code=existing_code,
                )
                completed_edit: Optional[Dict[str, Any]] = None
                for attempt in range(2):
                    prompt_text = completion_prompt
                    if attempt > 0:
                        prompt_text += "\n\n再次强调：replacement_code 必须是完整函数体，不能出现 ... 或“其他代码”。"
                    comp_resp = llm_adapter.chat(
                        [
                            {"role": "system", "content": "你输出严格 JSON，不要输出 Markdown 代码块以外的解释。"},
                            {"role": "user", "content": prompt_text},
                        ],
                        temperature=0.1,
                    )
                    comp_plan = _safe_extract_json_payload(comp_resp.content)
                    edits = comp_plan.get("edits", []) if isinstance(comp_plan.get("edits"), list) else []
                    if not edits:
                        continue
                    first = edits[0] if isinstance(edits[0], dict) else None
                    if not first:
                        continue
                    new_code = str(first.get("replacement_code", ""))
                    if _is_valid_replacement_code(new_code):
                        completed_edit = {
                            "file": target["file"],
                            "function_signature": target["function_signature"],
                            "replacement_code": new_code.strip(),
                            "reason": str(first.get("reason", "")) or "forced_single_target_completion",
                        }
                        break
                if completed_edit is not None:
                    fix_plan = _upsert_fix_plan_edit(fix_plan, completed_edit)
        apply_result = _apply_ai_fix_plan(
            fix_plan, candidate_nodes, code_roots, report_dir, backup_original_sources
        )
        if not apply_result.get("success"):
            applied_items = apply_result.get("applied", [])
            if not applied_items:
                edits = fix_plan.get("edits", []) if isinstance(fix_plan.get("edits"), list) else []
                if not edits:
                    apply_result["error"] = (
                        "AI 未返回可执行的结构化 edits（空 edits）。"
                        "请在模型输出中确保返回严格 JSON，且 edits 至少包含 1 个包含 replacement_code 的条目。"
                    )
                else:
                    apply_result["error"] = (
                        "AI 返回了 edits，但没有可应用项。"
                        "请检查 file/function_signature 是否与 candidate_nodes 精确匹配，"
                        "以及 replacement_code 是否为完整函数代码。"
                    )
                if first_response_preview:
                    apply_result["model_response_preview"] = first_response_preview
            elif not apply_result.get("error"):
                first_err = next(
                    (
                        str(item.get("error"))
                        for item in applied_items
                        if isinstance(item, dict) and item.get("error")
                    ),
                    "",
                ).strip()
                if first_err:
                    apply_result["error"] = f"未应用修改: {first_err}"
        return apply_result
    except Exception as exc:
        return {
            "success": False,
            "error": f"生成或应用 AI 修复计划失败: {exc}",
            "applied": [],
        }


def _print_execution_plan(state: Dict[str, Any]) -> None:
    print(_yellow("【执行计划】"))
    scope = str(state.get("scope", "full")).strip() or "full"
    if scope == "parse_log_only":
        print("  步骤 1/1：仅解析崩溃日志")
        return
    if scope == "parse_only":
        print("  步骤 1/2：解析崩溃日志")
        print("  步骤 2/2：符号化堆栈地址")
        return
    print("  步骤 1/4：解析崩溃日志")
    print("  步骤 2/4：符号化堆栈地址")
    print("  步骤 3/4：提取源码上下文")
    if scope == "prompt_only":
        print("  步骤 4/4：不调用 AI（仅生成可复用提示词）")
    else:
        print("  步骤 4/4：进行 AI 推理与修复建议")


def _print_user_parameter_confirmation(
    state: Dict[str, Any],
    *,
    library_dir_input: Optional[str] = None,
    code_roots_input: Optional[str] = None,
) -> None:
    print(_yellow("【参数确认】"))
    print(f"  崩溃日志: {state['crash_log']}")
    library_dir = str(state.get("library_dir") or "").strip()
    code_roots = state.get("code_roots") or []

    if library_dir_input == "skip":
        print("  符号库目录: skip")
    elif library_dir:
        print(f"  符号库目录: {library_dir}")

    if code_roots_input == "skip":
        print("  源码目录: skip")
    elif code_roots:
        scope = str(state.get("scope", "full")).strip() or "full"
        label = "源码目录（待修改）" if scope == "full" else "源码目录"
        if len(code_roots) == 1:
            print(f"  {label}: {code_roots[0]}")
        else:
            print(f"  {label}:")
            for idx, r in enumerate(code_roots, start=1):
                print(f"    {idx}. {r}")


def _primary_artifact_for_scope(scope: str, report_dir: Path) -> Optional[Path]:
    scope_norm = str(scope or "full").strip() or "full"
    if scope_norm == "parse_log_only":
        p = report_dir / "01_crash_log_parser.json"
    elif scope_norm == "parse_only":
        p = report_dir / "02_add2line_resolver.json"
    elif scope_norm == "prompt_only":
        p = report_dir / "round_0" / "05_ai_final_tip.txt"
    else:
        p = report_dir / "final_output.md"
    return p if p.exists() else None


def _print_tty_markdown_brief_summary(
    *,
    result: Dict[str, Any],
    scope: str,
    report_dir: Path,
    applied_fix_result: Optional[Dict[str, Any]],
    apply_ai_fixes_enabled: bool,
) -> None:
    """终端会话下仅展示阶段结果摘要与关键产物路径。"""
    ok = result.get("status") == "success"
    print("")
    if ok:
        print(_green("【结果】✓ 分析完成"))
    else:
        print(_red("【结果】✗ 分析未成功"))

    scope_norm = str(scope or "full").strip() or "full"
    artifact = _primary_artifact_for_scope(scope_norm, report_dir)
    if artifact is not None:
        if scope_norm == "parse_log_only":
            print("【产物】崩溃日志解析结果：")
        elif scope_norm == "parse_only":
            print("【产物】堆栈符号化结果：")
        elif scope_norm == "prompt_only":
            print("【产物】可复用提示词：")
        else:
            print("【产物】分析报告：")
        print(f"  {artifact.resolve()}")

    if scope_norm == "full":
        if not apply_ai_fixes_enabled:
            print("【改码】未启用自动改码")
        elif not isinstance(applied_fix_result, dict):
            print("【改码】未生成改码结果")
        elif applied_fix_result.get("success"):
            applied_items = applied_fix_result.get("applied", []) or []
            file_list: List[str] = []
            backup_list: List[str] = []
            seen = set()
            seen_backup = set()
            for item in applied_items:
                if not isinstance(item, dict) or item.get("status") != "applied":
                    continue
                fp = str(item.get("file") or "").strip()
                if not fp:
                    continue
                abs_fp = str(Path(fp).expanduser().resolve())
                if abs_fp in seen:
                    continue
                seen.add(abs_fp)
                file_list.append(abs_fp)
                backup_path = str(item.get("backup_path") or "").strip()
                if backup_path:
                    abs_backup = str(Path(backup_path).expanduser().resolve())
                    if abs_backup not in seen_backup:
                        seen_backup.add(abs_backup)
                        backup_list.append(abs_backup)
            if file_list:
                print(f"【改码】{_green(f'已修改 {len(file_list)} 个文件')}")
                for idx, fp in enumerate(file_list, start=1):
                    print(f"  {idx}. {fp}")
                if backup_list:
                    print("【回退保障】待修改的文件备份列表（修改前源码）")
                    for idx, bp in enumerate(backup_list, start=1):
                        print(f"  {idx}. {bp}")
                    print("  可直接从以上备份路径恢复修改前源码。")
                else:
                    print("【回退保障】未生成“待修改的文件”备份（当前可能关闭了源码备份）")
            else:
                print("【改码】未检测到已修改文件")
        else:
            reason = str(applied_fix_result.get("error") or "未知原因").strip()
            print("【改码】未应用修改")
            if reason:
                print(_yellow(f"  原因: {reason}"))

    if not ok:
        err = result.get("error")
        if err:
            print(_yellow(f"【错误】{err}"))


def _doctor_status() -> Dict[str, Any]:
    agent_cfg = _load_json_or_empty(_user_agent_config_file())
    llm_cfg = agent_cfg.get("llm_config", {}) if isinstance(agent_cfg, dict) else {}
    providers = llm_cfg.get("providers", {}) if isinstance(llm_cfg, dict) else {}
    provider_defaults = llm_cfg.get("provider_defaults", {}) if isinstance(llm_cfg, dict) else {}
    if not isinstance(provider_defaults, dict):
        provider_defaults = {}
    active_provider = llm_cfg.get("active_provider") if isinstance(llm_cfg, dict) else None
    provider_cfg = (
        {**provider_defaults, **(providers.get(active_provider, {}) or {})}
        if isinstance(providers, dict) and active_provider
        else {}
    )
    auth_type = str(provider_cfg.get("auth_type") or "").strip().lower()
    if not auth_type:
        auth_type = "authorization" if (active_provider == "baidu_qianfan" or provider_cfg.get("authorization")) else "api_key"
    auth_field = "authorization" if auth_type == "authorization" else "api_key"
    llm_ok = bool(active_provider and not _is_placeholder_secret(provider_cfg.get(auth_field)))
    tool_status = _detect_add2line_tool_status()
    tools = {name: (meta.get("path") or None) for name, meta in tool_status.items()}
    model_val = str(provider_cfg.get("model") or "").strip()
    return {
        "llm_ok": llm_ok,
        "active_provider": active_provider,
        "model": model_val or None,
        "auth_field": auth_field,
        "tool_status": tool_status,
        "tools": tools,
        "tool_ok": any(tools.values()),
    }


def _prompt_with_default(question: str, default_value: str = "") -> str:
    prompt = f"{question}{f' (默认: {default_value})' if default_value else ''}: "
    raw = _safe_input(prompt).strip()
    if raw == "__EOF__":
        return "quit"
    return raw


_VALID_SCOPES = {"full", "prompt_only", "parse_only", "parse_log_only"}


def _resolve_scope_from_record(record: Dict[str, Any]) -> str:
    """从历史 last_run/profile 记录里解析 scope，兼容旧字段（skip_ai + run_scope）。"""
    if not isinstance(record, dict):
        return "full"
    raw_scope = str(record.get("scope", "")).strip()
    if raw_scope in _VALID_SCOPES:
        return raw_scope
    legacy_scope = str(record.get("run_scope", "")).strip()
    if legacy_scope in {"parse_only", "parse_log_only"}:
        return legacy_scope
    if bool(record.get("skip_ai", False)):
        return "prompt_only"
    return "full"


def collect_interactive_run_state() -> Optional[Dict[str, Any]]:
    _ensure_user_config_templates()
    session_state = _load_session_state()
    last_run = session_state.get("last_run", {}) if isinstance(session_state.get("last_run", {}), dict) else {}
    preferred_engine = str(last_run.get("engine", "direct")).strip() if isinstance(last_run, dict) else "direct"
    if preferred_engine not in {"direct", "langchain", "langgraph"}:
        preferred_engine = "direct"
    preferred_scope = _resolve_scope_from_record(last_run)

    def _show_command_reference() -> None:
        print("== 全部命令参考（完整参数手册） ==")
        print("[主流程参数]")
        print("1) --crash-log PATH：崩溃日志路径（支持 '-' 从 stdin 读取）")
        print("2) --library-dir DIR：符号库目录（含 .so / .dylib / .dSYM；日志已含函数名+行号可省略）")
        print("3) --code-root DIR：项目 C/C++ 源码目录（可重复指定；建议精确到工程/模块根，避免传整个仓库根）")
        print("4) --config PATH：指定 SystemConfig JSON（不填则使用内置默认工具链与工作流）")
        print("5) --scope {full|prompt_only|parse_only|parse_log_only}：Agent 执行流程范围")
        print("   - full（默认）：解析+符号化+取代码上下文+AI 推理")
        print("   - prompt_only：完整工具链但不调用 AI，仅生成可复用提示词")
        print("   - parse_only：仅解析+符号化")
        print("   - parse_log_only：仅解析崩溃日志")
        print("6) --engine {direct|langchain|langgraph}：AI 推理模式（直调 / 工具编排 / 多轮 Agent）")
        print("")
        print("[RAG 上下文参数（进入分析 problem）]")
        print("1) --vector-db-path PATH：向量数据库目录（默认 ./vector_db）")
        print("2) --vector-db-max-results INT：向量检索最大返回数（默认 3）")
        print("3) --rule-confidence-threshold FLOAT：规则高置信阈值（默认 0.85）")
        print("")
        print("[输出与交互]")
        print("1) --output-format {markdown|json|text}：控制输出格式")
        print("2) --output-file PATH：将结果写入文件（不指定则打印到终端）")
        print("3) --interactive / --no-interactive：开启或关闭交互模式")
        print("")
        print("[AI 自动改码]")
        print("1) --apply-ai-fixes / --no-apply-ai-fixes：是否自动把 AI 修复写回源码")
        print("2) --backup-original-sources / --no-backup-original-sources：改码前是否备份源码")
        print("")
        print("[向量数据库运维]")
        print("1) --init-vector-db：初始化向量库（清空后写入种子）")
        print("2) --vector-db-stats：查看向量库统计")
        print("3) --export-vector-db [PATH]：导出向量库快照")
        print("4) --import-vector-db PATH：导入向量库快照（合并）")
        print("5) --pattern-feedback + --feedback-type + --feedback-comment：记录模式反馈")
        print("6) --vector-db-decay FLOAT：执行置信度衰减")
        print("7) --vector-db-gc：执行模式治理（配合 gc 阈值参数）")
        print("8) --gc-min-confidence FLOAT：GC 最低置信阈值（默认 0.2）")
        print("9) --gc-rejected-threshold INT：GC 拒绝次数阈值（默认 5）")
        print("")
        print("[扩展能力]")
        print("1) --plugin-module MODULE：加载第三方扩展模块（可重复）")
        print("2) STABILITY_AGENT_PLUGIN_MODULES：逗号分隔注入插件模块（环境变量）")
        print("")
        print("[子命令]")
        print("1) sa-agent config path|doctor|init：配置路径/检测/初始化")
        print("2) sa-agent profile list|show|use|save|delete：管理会话 profile")
        print("3) sa-agent run ...：显式使用参数模式执行分析")
        print("")
        print("[常见组合]")
        print("1) 完整分析：--crash-log ... --library-dir ... --code-root ...")
        print("2) 只分析不改码：加 --no-apply-ai-fixes")
        print("3) 只跑工具链不走 LLM：加 --scope prompt_only")
        print("4) 仅解析+符号化：加 --scope parse_only")
        print("5) 仅解析崩溃日志：加 --scope parse_log_only")
        print("6) 向量库统计：--vector-db-stats")
        print("7) 向量库初始化：--init-vector-db")
        print("")

    def _pick_engine(current_engine: str) -> str:
        engine_choice = _prompt_select(
            "请选择 AI 推理模式",
            [
                ("back", "返回"),
                ("direct", "direct（默认，直调 LLM，启动快）"),
                ("langchain", "langchain（工具编排，适合增强流程）"),
                ("langgraph", "langgraph（多轮 Agent，适合复杂任务）"),
            ],
            default_index=(["direct", "langchain", "langgraph"].index(current_engine) + 1)
            if current_engine in {"direct", "langchain", "langgraph"}
            else 1,
        )
        if engine_choice == "back":
            return current_engine
        return engine_choice

    scope_options = [
        ("full", "完整分析（解析+符号化+取代码上下文+AI 推理与改码）"),
        ("prompt_only", "完整工具链，仅生成提示词，不调用 AI"),
        ("parse_only", "仅解析+符号化"),
        ("parse_log_only", "仅解析崩溃日志"),
    ]
    scope_label_map = {key: label for key, label in scope_options}

    def _pick_scope(current_scope: str) -> str:
        keys = [k for k, _ in scope_options]
        scope_choice = _prompt_select(
            "调整Agent执行流程",
            [("back", "返回"), *scope_options],
            default_index=(keys.index(current_scope) + 1) if current_scope in keys else 1,
        )
        if scope_choice == "back":
            return current_scope
        return scope_choice

    while True:
        recent_log = str(last_run.get("crash_log", "")).strip() if isinstance(last_run, dict) else ""
        has_recent = bool(recent_log)
        opts: List[Tuple[str, str]] = [
            ("1", "快速开始分析（推荐）"),
        ]
        if has_recent:
            opts.append(("5", "再次进行上一次分析"))
        opts.append(("2", "设置"))
        opts.append(("3", "帮助"))
        opts.append(("q", "退出"))
        choice = _prompt_select("请选择要执行的操作", opts, default_index=0).strip().lower()
        if choice == "__eof__":
            return None
        if choice == "q":
            return None
        if choice == "2":
            while True:
                sub_choice = _prompt_select(
                    "设置",
                    [
                        ("back", "返回"),
                        ("cfg_llm", "配置大模型（厂商 / 密钥 / 模型）"),
                        ("cfg_add2line", "配置堆栈地址解析工具（addr2line / atos 等）"),
                        ("advanced", "高级选项"),
                    ],
                    default_index=0,
                )
                if sub_choice == "cfg_llm":
                    _configure_llm_only()
                    print("")
                    continue
                if sub_choice == "cfg_add2line":
                    _configure_add2line_only()
                    print("")
                    continue
                if sub_choice == "advanced":
                    while True:
                        adv = _prompt_select(
                            "高级选项",
                            [
                                ("back", "返回"),
                                ("llm_manual", "手动编辑大模型配置文件"),
                                ("add2line_manual", "手动编辑堆栈地址解析配置文件"),
                                (
                                    "llm_timeout",
                                    f"调整大模型请求超时时间（当前: {_effective_llm_request_timeout_seconds()} 秒）",
                                ),
                                ("engine", f"调整 AI 推理模式（当前: {preferred_engine}）"),
                                (
                                    "scope",
                                    f"调整Agent执行流程（当前: {scope_label_map.get(preferred_scope, preferred_scope)}）",
                                ),
                            ],
                            default_index=0,
                        )
                        if adv == "back":
                            break
                        if adv == "llm_manual":
                            _configure_llm_manual_panel()
                            print("")
                            continue
                        if adv == "add2line_manual":
                            _configure_add2line_manual_panel()
                            print("")
                            continue
                        if adv == "llm_timeout":
                            _configure_llm_request_timeout_interactive()
                            print("")
                            continue
                        if adv == "engine":
                            chosen = _pick_engine(preferred_engine)
                            if chosen != preferred_engine:
                                preferred_engine = chosen
                                print(f"已调整 AI 推理模式: {preferred_engine}")
                            print("")
                            continue
                        if adv == "scope":
                            chosen_scope = _pick_scope(preferred_scope)
                            if chosen_scope != preferred_scope:
                                preferred_scope = chosen_scope
                                print(
                                    f"已调整Agent执行流程: {scope_label_map.get(preferred_scope, preferred_scope)}"
                                )
                            print("")
                            continue
                    continue
                break
            continue
        if choice == "3":
            while True:
                sub_choice = _prompt_select(
                    "帮助",
                    [
                        ("back", "返回"),
                        ("command_guide", "全部命令参考（完整参数手册）"),
                        ("example", "命令快速示例（最小可运行）"),
                    ],
                    default_index=0,
                )
                if sub_choice == "command_guide":
                    print("━━━━━━━━━━━━━━━━━━━━━━")
                    _show_command_reference()
                    print("━━━━━━━━━━━━━━━━━━━━━━")
                    _prompt_select(
                        "请选择操作",
                        [("done", "已掌握，返回")],
                        default_index=0,
                    )
                    print("")
                    continue
                if sub_choice == "example":
                    print("━━━━━━━━━━━━━━━━━━━━━━")
                    print("命令快速示例（最小可运行）")
                    print("")
                    print("推荐：输入 1 进入交互引导（新手首选）。")
                    print("或直接命令运行（适合熟手/脚本）：")
                    print("sa-agent --crash-log <log.crash> --library-dir <lib_dir> --code-root <code_dir>")
                    print("更多参数：sa-agent --help")
                    print("━━━━━━━━━━━━━━━━━━━━━━")
                    _prompt_select(
                        "请选择操作",
                        [("done", "已掌握，返回")],
                        default_index=0,
                    )
                    print("")
                    continue
                break
            continue
        if choice == "5" and has_recent:
            recent_state = {
                "crash_log": str(last_run.get("crash_log", "")).strip(),
                "library_dir": str(last_run.get("library_dir", "")).strip(),
                "code_roots": [str(x).strip() for x in (last_run.get("code_roots", []) or []) if str(x).strip()],
                "engine": str(last_run.get("engine", "direct")).strip() or "direct",
                "scope": _resolve_scope_from_record(last_run),
            }
            crash_path = Path(recent_state["crash_log"]).expanduser().resolve()
            if not recent_state["crash_log"] or not crash_path.exists() or not crash_path.is_file():
                print("未找到最近一次可复跑日志，已返回主菜单。")
                print("")
                continue
            recent_state["crash_log"] = str(crash_path)
            if recent_state["library_dir"]:
                lib_path = Path(recent_state["library_dir"]).expanduser().resolve()
                if lib_path.exists() and lib_path.is_dir():
                    recent_state["library_dir"] = str(lib_path)
                else:
                    print(f"提示: 最近一次库目录不存在，将忽略: {recent_state['library_dir']}")
                    recent_state["library_dir"] = ""
            cleaned_roots: List[str] = []
            missing_roots: List[str] = []
            for root in recent_state["code_roots"]:
                rp = Path(root).expanduser().resolve()
                if rp.exists() and rp.is_dir():
                    cleaned_roots.append(str(rp))
                else:
                    missing_roots.append(str(rp))
            recent_state["code_roots"] = cleaned_roots
            if missing_roots:
                print("提示: 以下最近一次代码目录不存在，将忽略：")
                for item in missing_roots:
                    print(f"- {item}")
            print("将复跑最近一次分析：")
            print(f"- crash_log: {recent_state['crash_log']}")
            if recent_state["library_dir"]:
                print(f"- library_dir: {recent_state['library_dir']}")
            if recent_state["code_roots"]:
                print(f"- code_roots: {recent_state['code_roots']}")
            quick_confirm = _prompt_select(
                "请选择下一步",
                [("run", "立即重跑"), ("edit", "编辑参数"), ("cancel", "取消")],
                default_index=0,
            )
            if quick_confirm == "cancel":
                print("")
                continue
            if quick_confirm == "edit":
                break
            print("")
            _print_execution_plan(recent_state)
            _print_user_parameter_confirmation(recent_state)
            print(_yellow("提示: 运行中可按 Ctrl+C 立即终止当前任务。"))
            session_state["last_run"] = recent_state
            _save_session_state(session_state)
            return recent_state
        if choice == "1":
            break

    status = _doctor_status()
    seed = last_run
    engine_default = preferred_engine
    scope_default = preferred_scope

    needs_llm = scope_default == "full"
    needs_symbol = scope_default in {"full", "prompt_only", "parse_only"}

    if needs_llm and not status.get("llm_ok"):
        print("")
        print(_red("⚠ 当前为「完整分析」模式，但大模型尚未配置完成。"))
        while True:
            action = _prompt_select(
                "请选择操作",
                [
                    ("configure", "进入大模型配置"),
                    ("back", "返回上一级菜单"),
                ],
                default_index=0,
            )
            if action == "back":
                return None
            _configure_llm_only()
            status = _doctor_status()
            if status.get("llm_ok"):
                break
            print("")
            print(_red("⚠ 大模型仍未配置完成。"))

    if needs_symbol and not status.get("tool_ok"):
        print("")
        print(_red("⚠ 当前流程需要符号化能力，但未检测到可用的堆栈地址解析工具。"))
        print("提示：如果崩溃日志已完成符号化，可选择「忽略并继续」。")
        while True:
            action = _prompt_select(
                "请选择操作",
                [
                    ("configure", "进入堆栈地址解析工具配置"),
                    ("ignore", "忽略并继续（日志已完成符号化）"),
                    ("back", "返回上一级菜单"),
                ],
                default_index=0,
            )
            if action == "back":
                return None
            if action == "ignore":
                break
            _configure_add2line_only()
            status = _doctor_status()
            if status.get("tool_ok"):
                break
            print("")
            print(_red("⚠ 仍未检测到可用的堆栈地址解析工具。"))

    total_steps = 1 if scope_default == "parse_log_only" else (2 if scope_default == "parse_only" else 3)

    print("")
    print(_yellow(f"[步骤 1/{total_steps}] 崩溃日志路径"))
    while True:
        raw = _safe_input("请输入崩溃日志路径（直接回车返回上一级）: ").strip()
        if raw == "__EOF__":
            return None
        if not raw:
            return collect_interactive_run_state()
        crash_log = raw
        if not crash_log:
            print(_red("崩溃日志路径不能为空。"))
            continue
        p = Path(crash_log).expanduser().resolve()
        if not p.exists() or not p.is_file():
            print(_red(f"路径无效（需要是文件）: {p}"))
            continue
        crash_log = str(p)
        break

    library_dir_input: Optional[str] = None
    code_roots_input: Optional[str] = None

    if scope_default == "parse_log_only":
        library_dir = ""
        code_roots = []
    else:
        print("")
        print(_yellow(f"[步骤 2/{total_steps}] 符号库目录"))
        print(_yellow("提示："))
        print("- 符号库目录：含调试符号的二进制库所在目录")
        print("  · macOS / iOS：含 .dylib 或 .dSYM 的目录")
        print("  · Android / 鸿蒙 / Linux：含 .so（未 strip，保留调试信息）的目录")
        print("  · Windows：含 .pdb / .exe 的目录")
        while True:
            raw = _safe_input("请输入符号库目录（直接回车返回上一级；日志已含函数名+行号可输入 skip 跳过）: ").strip()
            if raw == "__EOF__":
                return None
            if not raw:
                return collect_interactive_run_state()
            if raw.lower() == "skip":
                library_dir_input = "skip"
                library_dir = ""
                print(_green("✓ 已跳过符号库目录（按已符号化日志处理）"))
                break
            library_dir = raw
            library_dir_input = library_dir
            p = Path(library_dir).expanduser().resolve()
            if not p.exists() or not p.is_dir():
                print(_red(f"路径无效（需要是目录）: {p}"))
                continue
            library_dir = str(p)
            break

        if scope_default == "parse_only":
            code_roots = []
        else:
            title_prefix = "待修改的 " if scope_default == "full" else ""
            input_prefix = title_prefix if title_prefix else " "
            print("")
            print(_yellow(f"[步骤 3/{total_steps}] {title_prefix}C/C++ 源码目录"))
            print(_yellow("提示："))
            print(f"- {title_prefix}C/C++ 源码目录{_yellow('（必填）')}：与崩溃二进制对应的本地源码所在目录")
            print("  · 多个目录用中文或英文逗号分隔，例：~/code/MyApp/src, ~/code/MySDK/src")
            print(_yellow("  · 建议精确到工程/模块根，太大的目录（如 ~/code 整个仓库根）可能会拖慢源文件定位"))
            input_prompt = f"请输入{input_prefix}C/C++ 源码目录（支持输入多个目录，可用中文或英文逗号分隔，直接回车返回上一级）: "
            while True:
                raw = _safe_input(input_prompt).strip()
                if raw == "__EOF__":
                    return None
                if not raw:
                    return collect_interactive_run_state()
                normalized_raw = raw.replace("，", ",")
                path_items = [item.strip() for item in normalized_raw.split(",") if item.strip()]
                bad = []
                normalized: List[str] = []
                for item in path_items:
                    p = Path(item).expanduser().resolve()
                    if not p.exists() or not p.is_dir():
                        bad.append(str(p))
                    else:
                        normalized.append(str(p))
                if bad:
                    print(_red("以下代码目录无效："))
                    for item in bad:
                        print(_red(f"- {item}"))
                    continue
                code_roots = normalized
                if code_roots:
                    code_roots_input = ",".join(normalized)
                break

    engine = engine_default if engine_default in {"direct", "langchain", "langgraph"} else "direct"

    state = {
        "crash_log": crash_log,
        "library_dir": library_dir,
        "code_roots": code_roots,
        "engine": engine,
        "scope": scope_default,
    }

    print("")
    _print_execution_plan(state)
    _print_user_parameter_confirmation(
        state,
        library_dir_input=library_dir_input,
        code_roots_input=code_roots_input,
    )
    print(_yellow("提示: 运行中可按 Ctrl+C 立即终止当前任务。"))

    session_state["last_run"] = state
    _save_session_state(session_state)
    return state


def execute_analysis(args: argparse.Namespace) -> int:
    _configure_cli_analysis_logging()
    vector_cmd_exit = _run_vector_db_command(args)
    if vector_cmd_exit is not None:
        return vector_cmd_exit

    if not args.crash_log:
        print("错误: 缺少 --crash-log", file=sys.stderr)
        return 1

    crash_log_content = _read_crash_log(args.crash_log)
    if not crash_log_content.strip():
        print("错误: 崩溃日志内容为空", file=sys.stderr)
        return 1

    scope = str(getattr(args, "scope", "full") or "full").strip()
    if scope not in {"full", "prompt_only", "parse_only", "parse_log_only"}:
        print(
            f"错误: --scope 取值无效: {scope}（仅支持 full / prompt_only / parse_only / parse_log_only）",
            file=sys.stderr,
        )
        return 1
    code_roots = _normalize_code_roots(args.code_roots)
    call_llm = scope == "full"
    problem = {
        "crash_log": crash_log_content,
        "library_dir": args.library_dir,
        "code_roots": code_roots,
        "engine": args.engine,
        "scope": scope,
        "vector_db_path": args.vector_db_path,
        "vector_db_max_results": args.vector_db_max_results,
        "rule_confidence_threshold": args.rule_confidence_threshold,
        "max_sibling_member_functions": int(getattr(args, "max_sibling_member_functions", 0) or 0),
        "max_shared_var_related_functions": int(
            getattr(args, "max_shared_var_related_functions", 20) or 20
        ),
        "min_key_read_related_functions": int(
            getattr(args, "min_key_read_related_functions", 2) or 0
        ),
    }

    registry = ToolAndWorkflowRegistry()
    register_all_tools_and_workflows(registry)

    env_modules = [m.strip() for m in os.environ.get("STABILITY_AGENT_PLUGIN_MODULES", "").split(",") if m.strip()]
    cli_modules = args.plugin_modules or []
    _register_third_party_modules(registry, env_modules + cli_modules)

    if args.config:
        config = SystemConfig.from_file(args.config)
    else:
        tool_entries = [ToolConfig(name="crash_log_parser", enabled=True)]
        if scope in {"full", "prompt_only", "parse_only"}:
            tool_entries.append(ToolConfig(name="add2line_resolver", enabled=True))
        if scope in {"full", "prompt_only"}:
            tool_entries.append(ToolConfig(name="code_content_provider", enabled=True))
        config = SystemConfig(
            tools=tool_entries,
            workflows=[WorkflowConfig(name="crash_analysis", enabled=True)],
        )

    llm_adapter = None
    if call_llm:
        if config.llm is None:
            llm_config = _build_llm_config_from_agent_config(args.engine)
            if llm_config is not None:
                config.llm = llm_config
            else:
                print(
                    "错误: 未检测到可用 LLM 配置。请直接运行 `sa-agent` 按引导配置，"
                    "或改用 `--scope prompt_only` 仅生成提示词。",
                    file=sys.stderr,
                )
                return 1
        if config.llm is not None:
            try:
                llm_adapter = LLMAdapterFactory.create(config.llm.to_dict())
            except Exception as exc:
                print(f"警告: LLM 适配器初始化失败，将继续执行工具链。错误: {exc}", file=sys.stderr)

    executor = ConfigDrivenExecutor(registry, config, llm_adapter)
    result = executor.execute_workflow("crash_analysis", problem)
    report_dir = _build_report_dir(args)
    applied_fix_result: Optional[Dict[str, Any]] = None

    if args.apply_ai_fixes and result.get("status") == "success" and scope == "full":
        applied_fix_result = _maybe_apply_ai_fixes(
            llm_adapter, result, code_roots, report_dir, args.backup_original_sources
        )
        result["applied_ai_fixes"] = applied_fix_result

    if args.output_format == "json":
        output = json.dumps(result, ensure_ascii=False, indent=2)
    elif args.output_format == "text":
        if result.get("status") == "success":
            parse_result = result.get("parse_result", {}) or {}
            crash_info = parse_result.get("crash_info", {}) or {}
            lines = [
                f"错误类型: {crash_info.get('signal', 'N/A')}",
                f"崩溃原因: {crash_info.get('crash_reason', 'N/A')}",
                f"崩溃地址: {crash_info.get('crash_address', 'N/A')}",
                f"分类: {crash_info.get('category', 'N/A')}",
            ]
            if result.get("analysis"):
                lines.append("")
                lines.append(f"AI 分析: {result.get('analysis')}")
            output = "\n".join(lines)
        else:
            output = f"错误: {result.get('error')}"
    else:
        if result.get("status") == "success":
            parse_result = result.get("parse_result", {}) or {}
            crash_info = parse_result.get("crash_info", {}) or {}
            lines = [
                "# 崩溃分析结果",
                "",
                "## 崩溃信息",
                f"- **错误类型**: {crash_info.get('signal', 'N/A')}",
                f"- **崩溃原因**: {crash_info.get('crash_reason', 'N/A')}",
                f"- **崩溃地址**: {crash_info.get('crash_address', 'N/A')}",
                f"- **分类**: {crash_info.get('category', 'N/A')}",
                f"- **线程**: {crash_info.get('thread_type', 'N/A')}",
                "",
            ]
            resolved = result.get("resolved_stack", {}) or {}
            frames = resolved.get("resolved_frames", []) or []
            if frames:
                lines.append("## 解析后的堆栈")
                for frame in frames:
                    lines.append(
                        f"- {frame.get('function', 'N/A')} ({frame.get('file', 'N/A')}:{frame.get('line', 'N/A')})"
                    )
                lines.append("")
            code_context = result.get("code_context", {}) or {}
            crash_summary = code_context.get("crash_summary", {}) if isinstance(code_context, dict) else {}
            graph = code_context.get("graph", {}) if isinstance(code_context, dict) else {}
            if isinstance(crash_summary, dict) and isinstance(graph, dict):
                has_loc = False
                for frame in frames:
                    if frame.get("file") not in (None, "", "N/A") and frame.get("line") not in (None, "", "N/A"):
                        has_loc = True
                        break
                if not has_loc:
                    node_id = crash_summary.get("node_id")
                    node_map = {
                        n.get("id"): n
                        for n in (graph.get("nodes", []) if isinstance(graph.get("nodes", []), list) else [])
                        if isinstance(n, dict) and isinstance(n.get("id"), str)
                    }
                    node = node_map.get(node_id) if isinstance(node_id, str) else None
                    if node is None and isinstance(node_id, str):
                        node = node_map.get(node_id.rstrip().rstrip("{").rstrip())
                    if isinstance(node, dict):
                        lines.append("## 崩溃点源码定位（回退）")
                        lines.append(
                            f"- {node.get('signature', 'N/A')} "
                            f"({node.get('file', 'N/A')}:{crash_summary.get('crash_line_number', 'N/A')})"
                        )
                        lines.append("")
            if result.get("analysis"):
                lines.append("## AI 分析")
                lines.append(str(result.get("analysis")))
            if applied_fix_result is not None:
                lines.append("")
                lines.append("## AI 自动改码结果")
                if applied_fix_result.get("success"):
                    for item in applied_fix_result.get("applied", []):
                        if item.get("status") == "applied":
                            lines.append(f"- 已修改: {item.get('file')} -> {item.get('function_signature')}")
                else:
                    lines.append(f"- 未应用修改: {applied_fix_result.get('error', '未知原因')}")
            output = "\n".join(lines)
        else:
            output = f"# 错误\n\n{result.get('error')}"

    report_dir = _write_cli_report(
        report_dir,
        result,
        output,
        applied_fix_result,
        write_readme_output=(scope == "full"),
        scope=scope,
    )

    use_tty_brief = (
        not args.output_file
        and not bool(getattr(args, "print_full_report", False))
        and args.output_format == "markdown"
        and report_dir is not None
        and _stdout_is_tty()
    )

    if args.output_file:
        Path(args.output_file).write_text(output, encoding="utf-8")
        print(f"结果已保存到: {args.output_file}", file=sys.stderr)
    elif use_tty_brief:
        _print_tty_markdown_brief_summary(
            result=result,
            scope=scope,
            report_dir=report_dir,
            applied_fix_result=applied_fix_result,
            apply_ai_fixes_enabled=bool(args.apply_ai_fixes),
        )
    else:
        print(output)

    if report_dir is not None:
        if not use_tty_brief:
            print(f"cli_report 已保存到: {report_dir}", file=sys.stderr)

    return 0 if result.get("status") == "success" else 1


def _is_tty_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    try:
        if raw_argv and raw_argv[0] == "config":
            return _handle_config_command(raw_argv[1:])
        if raw_argv and raw_argv[0] == "profile":
            return _handle_profile_command(raw_argv[1:])
        if raw_argv and raw_argv[0] == "cancel":
            return _handle_cancel_command(raw_argv[1:])

        if raw_argv and raw_argv[0] == "run":
            raw_argv = raw_argv[1:]

        parser = build_parser()
        args = parser.parse_args(raw_argv)
        has_business_args = any(
            [
                bool(args.crash_log),
                bool(args.init_vector_db),
                bool(args.vector_db_stats),
                args.export_vector_db is not None,
                bool(args.import_vector_db),
                bool(args.pattern_feedback),
                args.vector_db_decay is not None,
                bool(args.vector_db_gc),
            ]
        )

        interactive_requested = args.interactive is True
        interactive_forced_off = args.interactive is False
        auto_interactive = (not raw_argv and _is_tty_interactive())
        should_interactive = (interactive_requested or auto_interactive) and not has_business_args and not interactive_forced_off

        if should_interactive:
            state = collect_interactive_run_state()
            if state is None:
                print("已退出交互模式。")
                return 0
            argv_from_state: List[str] = [
                "--crash-log",
                state["crash_log"],
                "--engine",
                state["engine"],
                "--no-interactive",
            ]
            if state.get("library_dir"):
                argv_from_state.extend(["--library-dir", state["library_dir"]])
            for code_root in state.get("code_roots", []):
                argv_from_state.extend(["--code-root", code_root])
            scope = str(state.get("scope", "full")).strip() or "full"
            if scope != "full":
                argv_from_state.extend(["--scope", scope])
            args = parser.parse_args(argv_from_state)
            return execute_analysis(args)

        if interactive_requested and not _is_tty_interactive():
            print("错误: 当前为非交互环境，无法启用 --interactive。", file=sys.stderr)
            return 1

        if not args.crash_log and not has_business_args:
            print("错误: 缺少 --crash-log（或直接运行 `sa-agent` 进入交互模式）", file=sys.stderr)
            return 1
        return execute_analysis(args)
    except KeyboardInterrupt:
        print("\n已中止当前任务（Ctrl+C）。")
        return 130


if __name__ == "__main__":
    sys.exit(main())

