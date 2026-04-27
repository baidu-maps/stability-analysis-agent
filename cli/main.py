#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一 CLI 入口（Tool System only）。

设计目标：
- 仅使用 Tool/Workflow 注册机制执行分析；
- 支持第三方通过模块扩展注册表；
- 作为唯一命令行入口。
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import termios
import tty
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

try:
    from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB
    from rag.init_vector_db_data import (
        init_rules as init_vector_rules,
        init_patterns as init_vector_patterns,
        init_evidence as init_vector_evidence,
        init_strategies as init_vector_strategies,
        init_guidance_blocks as init_vector_guidance_blocks,
    )
    RAG_RUNTIME_AVAILABLE = True
except ImportError:
    AIStabilityAnalyzerWithVectorDB = None  # type: ignore
    init_vector_rules = None  # type: ignore
    init_vector_patterns = None  # type: ignore
    init_vector_evidence = None  # type: ignore
    init_vector_strategies = None  # type: ignore
    init_vector_guidance_blocks = None  # type: ignore
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

    extra: Dict[str, Any] = {}
    if adapter_provider == "deepseek":
        extra["deepseek_api_key"] = secret
        extra["deepseek_base_url"] = base_url or "https://api.deepseek.com/v1"
    if auth_type == "authorization":
        extra["authorization"] = secret
        if active_provider == "baidu_qianfan":
            extra["baidu_qianfan_authorization"] = secret

    return LLMConfig(
        engine=mapped_engine,
        provider=adapter_provider,
        model=model,
        api_key=secret,
        base_url=base_url,
        extra=extra,
    )


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


def _candidate_tool_dirs_from_env() -> List[Path]:
    out: List[Path] = []
    seen: set = set()

    def _push(p: str) -> None:
        rp = str(Path(p).expanduser().resolve())
        if rp in seen:
            return
        seen.add(rp)
        out.append(Path(rp))

    ndk_home = os.environ.get("ANDROID_NDK_HOME", "").strip()
    if ndk_home:
        base = Path(ndk_home).expanduser().resolve()
        _push(str(base / "toolchains" / "llvm" / "prebuilt" / "darwin-x86_64" / "bin"))
        _push(str(base / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64" / "bin"))
        _push(str(base / "toolchains" / "llvm" / "prebuilt" / "windows-x86_64" / "bin"))
        _push(str(base / "toolchains" / "llvm" / "prebuilt" / "windows" / "bin"))

    llvm_home = os.environ.get("LLVM_HOME", "").strip()
    if llvm_home:
        base = Path(llvm_home).expanduser().resolve()
        _push(str(base / "bin"))
        _push(str(base))

    android_sdk_home = os.environ.get("ANDROID_SDK_HOME", "").strip() or os.environ.get("ANDROID_HOME", "").strip()
    if android_sdk_home:
        _push(str(Path(android_sdk_home).expanduser().resolve() / "ndk-bundle" / "toolchains" / "llvm" / "prebuilt" / "darwin-x86_64" / "bin"))
    return out


def _find_tool_in_dirs(tool: str, dirs: List[Path]) -> Optional[str]:
    for d in dirs:
        candidate = d / tool
        if candidate.exists() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def _detect_add2line_tool_status() -> Dict[str, Dict[str, str]]:
    tools = ["atos", "addr2line", "llvm-addr2line", "llvm-symbolizer", "ndk-stack"]
    config_dirs = _candidate_tool_dirs_from_config()
    env_dirs = _candidate_tool_dirs_from_env()
    result: Dict[str, Dict[str, str]] = {}

    for tool in tools:
        from_env = _find_tool_in_dirs(tool, env_dirs)
        if from_env:
            result[tool] = {"path": from_env, "source": "env"}
            continue
        from_path = shutil.which(tool)
        if from_path:
            result[tool] = {"path": from_path, "source": "path"}
            continue
        from_config = _find_tool_in_dirs(tool, config_dirs)
        if from_config:
            result[tool] = {"path": from_config, "source": "config"}
            continue
        result[tool] = {"path": "", "source": "missing"}
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


def _prompt_base_url_with_examples(provider: str, default_value: str) -> str:
    # Keep this aligned with tools/configs/agent_config.local.example.json request_format examples.
    protocol_examples = {
        "openai_chat_completions_compatible": "https://api.openai.com/v1/chat/completions",
        "anthropic_messages_compatible": "https://api.anthropic.com/v1/messages",
        "openai_responses_compatible": "https://api.openai.com/v1/responses",
        "minimax_text_chatcompletion_v2_compatible": "https://api.minimaxi.com/v1/chat/completions",
    }
    provider_examples = {
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
    suggested = (default_value or provider_examples.get(provider) or protocol_examples["openai_chat_completions_compatible"]).strip()

    print("base_url 建议填写完整请求地址（避免依赖隐式拼接）。")
    print("协议示例：")
    for name, url in protocol_examples.items():
        print(f"- {name}: {url}")
    print(f"当前 provider 推荐示例: {provider_examples.get(provider, suggested)}")

    raw = _safe_input(f"请输入 base_url（回车使用默认） (默认: {suggested}): ").strip()
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
            return False
    print("未检测到可用的 EDITOR 环境变量，请手动打开该文件。")
    return False


def _show_success_panel(title: str, lines: List[str]) -> None:
    print("━━━━━━━━━━━━━━━━━━━━━━")
    print(f"{title}")
    print("")
    for line in lines:
        print(line)
    print("━━━━━━━━━━━━━━━━━━━━━━")
    if _is_tty_interactive():
        _safe_input("按回车继续... ")


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
    profile = {
        "crash_log": data.get("crash_log") or "",
        "library_dir": data.get("library_dir") or "",
        "code_roots": data.get("code_roots") or [],
        "engine": data.get("engine") or "direct",
        "skip_ai": bool(data.get("skip_ai")),
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
    provider_options: List[Tuple[str, str]] = [(k, k) for k in provider_keys]
    provider_options.append(("custom", "自定义输入 provider 名称"))
    provider_options.append(("back", "返回"))
    selected_provider = _prompt_select(
        "请选择 provider",
        provider_options,
        default_index=provider_keys.index("openai") if "openai" in provider_keys else 0,
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
    if provider == "baidu_qianfan":
        secret = getpass.getpass("请输入 authorization（输入时隐藏）: ").strip()
        provider_cfg["authorization"] = secret
        provider_cfg["model"] = _prompt_non_empty("请输入模型名", provider_cfg.get("model") or model_defaults.get(provider, "ernie-4.0-8k"))
        base_url = _prompt_base_url_with_examples(
            provider,
            str(provider_cfg.get("base_url") or base_url_defaults.get(provider, "")).strip(),
        )
        provider_cfg["base_url"] = base_url
    else:
        secret = getpass.getpass("请输入 api_key（输入时隐藏）: ").strip()
        provider_cfg["api_key"] = secret
        provider_cfg["model"] = _prompt_non_empty("请输入模型名", provider_cfg.get("model") or model_defaults.get(provider, "gpt-4o"))
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
    _show_success_panel(
        "✅ LLM 配置已保存",
        [
            f"配置文件: {target}",
            f"provider: {provider}",
            f"secret: {_mask_secret(secret)}",
        ],
    )
    return True


def _update_add2line_config_interactive() -> bool:
    target = _user_add2line_config_file()
    data = _load_json_or_empty(target)
    if not isinstance(data, dict):
        data = {}
    platforms = data.get("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}

    os_choice = _prompt_select(
        "请选择目标平台",
        [
            ("ios", "ios"),
            ("android", "android"),
            ("linux", "linux"),
            ("harmonyos", "harmonyos"),
            ("custom", "自定义平台"),
            ("back", "返回"),
        ],
        default_index=0,
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
    path_line = input("请输入 tool_paths（多个路径用英文逗号分隔，可空）: ").strip()
    if path_line:
        os_cfg["tool_paths"] = [p.strip() for p in path_line.split(",") if p.strip()]
    pref_line = input("请输入 preferred_tools（多个工具用英文逗号分隔，可空）: ").strip()
    if pref_line:
        os_cfg["preferred_tools"] = [p.strip() for p in pref_line.split(",") if p.strip()]
    env_line = input("请输入环境变量（格式 KEY=VALUE，多个用英文逗号分隔，可空）: ").strip()
    env_vars: Dict[str, str] = {}
    if env_line:
        for pair in env_line.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip():
                    env_vars[k.strip()] = v.strip()
    if env_vars:
        os_cfg["environment_vars"] = env_vars

    platforms[os_choice] = os_cfg
    data["platforms"] = platforms
    _write_json_pretty(target, data)
    _show_success_panel(
        "✅ addr2line 配置已保存",
        [
            f"配置文件: {target}",
            f"平台: {os_choice}",
        ],
    )
    return True


def _config_command_init() -> int:
    _ensure_user_config_templates()
    print(f"配置目录: {_user_config_dir()}")

    if _prompt_yes_no("是否现在配置大模型？", True):
        mode = _prompt_select(
            "请选择大模型配置方式",
            [("1", "手动编辑配置文件"), ("2", "交互向导填写")],
            default_index=1,
        )
        if mode == "1":
            _configure_llm_only()
        else:
            _update_llm_config_interactive()

    detected_tools = _detect_add2line_tools()
    print("自动检测 add2line 相关工具：")
    for name, path in detected_tools.items():
        print(f"  - {name}: {path or 'missing'}")
    env_keys = ["ANDROID_NDK_HOME", "LLVM_HOME", "ANDROID_SDK_HOME"]
    print("环境变量：")
    for key in env_keys:
        print(f"  - {key}: {os.environ.get(key) or 'missing'}")

    if _prompt_yes_no("是否配置 add2line 工具路径？", not any(detected_tools.values())):
        mode = _prompt_select(
            "请选择 add2line 配置方式",
            [("1", "手动编辑配置文件"), ("2", "交互向导填写")],
            default_index=1,
        )
        if mode == "1":
            _configure_add2line_only()
        else:
            _update_add2line_config_interactive()

    print("初始化完成。")
    return 0


def _configure_llm_only() -> None:
    _ensure_user_config_templates()
    mode = _prompt_select(
        "请选择大模型配置方式",
        [("1", "手动编辑配置文件"), ("2", "交互向导填写"), ("back", "返回")],
        default_index=1,
    )
    if mode == "back":
        return
    if mode == "1":
        target = _user_agent_config_file()
        while True:
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
                    ("check", "[c] 我已完成，继续检测"),
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
                continue
            if action == "open":
                ok = _open_file_with_editor(target)
                print("已打开文件。" if ok else f"请手动打开: {target}")
                print("")
                continue
            status = _doctor_status()
            if status.get("llm_ok"):
                print("LLM 配置检测通过。")
                print("")
                return
            print("LLM 配置仍未完成，请继续编辑后再检测。")
            print("")
        return
    changed = _update_llm_config_interactive()
    if not changed:
        print("已返回上一层。")


def _configure_add2line_only() -> None:
    _ensure_user_config_templates()
    detected_tools = _detect_add2line_tools()
    print("自动检测 add2line 相关工具：")
    for name, path in detected_tools.items():
        print(f"  - {name}: {path or 'missing'}")
    env_keys = ["ANDROID_NDK_HOME", "LLVM_HOME", "ANDROID_SDK_HOME"]
    print("环境变量：")
    for key in env_keys:
        print(f"  - {key}: {os.environ.get(key) or 'missing'}")
    mode = _prompt_select(
        "请选择 add2line 配置方式",
        [("1", "手动编辑配置文件"), ("2", "交互向导填写"), ("back", "返回")],
        default_index=1,
    )
    if mode == "back":
        return
    if mode == "1":
        target = _user_add2line_config_file()
        while True:
            print("━━━━━━━━━━━━━━━━━━━━━━")
            print("配置 addr2line 工具（手动方式）")
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
                    ("check", "[c] 我已完成，继续检测"),
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
                print("- platforms.<platform>.preferred_tools")
                print("- platforms.<platform>.environment_vars")
                print("")
                continue
            if action == "open":
                ok = _open_file_with_editor(target)
                print("已打开文件。" if ok else f"请手动打开: {target}")
                print("")
                continue
            status = _doctor_status()
            if status.get("tool_ok"):
                print("add2line 工具检测通过。")
                print("")
                return
            print("仍未检测到可用 add2line 工具，请继续编辑后再检测。")
            print("")
        return
    changed = _update_add2line_config_interactive()
    if not changed:
        print("已返回上一层。")


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
    p.add_argument("--library-dir", required=False, help="符号库目录")
    p.add_argument("--code-root", action="append", dest="code_roots", help="代码目录，可重复指定")
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
    p.add_argument("--skip-ai", action="store_true", help="跳过 AI（不调用大模型；输出工具链结果并生成可复用提示词）")
    p.add_argument(
        "--apply-ai-fixes",
        dest="apply_ai_fixes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否基于 AI 建议回写源码（默认开启；仅在不加 --skip-ai 且 LLM 可用时生效，使用 --no-apply-ai-fixes 关闭）",
    )
    p.add_argument(
        "--backup-original-sources",
        dest="backup_original_sources",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="应用 AI 修复前是否在 cli_reports 下备份改前源码（默认开启；代码已由 Git 管理可用 --no-backup-original-sources 关闭）",
    )
    p.add_argument("--engine", default="direct", choices=["direct", "langchain", "langgraph"], help="执行引擎标记")
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
    mode = "analysis_skip_ai" if args.skip_ai else "analysis_ai"
    crash_name = _sanitize_report_name(Path(args.crash_log).stem if args.crash_log and args.crash_log != "-" else "stdin")
    dirname = f"{stamp}_{mode}_{args.engine}_{crash_name}"
    return _runtime_output_root() / "cli_reports" / dirname


def _write_cli_report(
    report_dir: Path,
    result: Dict[str, Any],
    rendered_output: str,
    applied_fix_result: Optional[Dict[str, Any]] = None,
    write_readme_output: bool = True,
) -> Optional[Path]:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        if result.get("parse_result") is not None:
            _write_json(report_dir / "01_crash_log_parser.json", result.get("parse_result"))
        if result.get("resolved_stack") is not None:
            _write_json(report_dir / "02_add2line_resolver.json", result.get("resolved_stack"))
        if result.get("code_context") is not None:
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


def _build_fix_plan_prompt(
    parse_result: Dict[str, Any],
    code_context: Dict[str, Any],
    analysis_text: str,
    candidate_nodes: List[Dict[str, Any]],
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
    return (
        "你是代码修复执行器。请根据崩溃上下文和现有 AI 分析，输出“可直接落盘”的最小修改计划。\n"
        "只允许修改下面 candidate_nodes 中出现的函数；不要新建文件，不要引用不存在的文件，不要输出 Markdown。\n"
        "若无法安全修改，请返回 edits 为空数组。\n\n"
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
        "2. 优先做最小修复，避免引入新依赖。\n"
        "3. 不要修改无关逻辑。\n"
        "4. 如果现有 AI 分析与 candidate_nodes 冲突，以 candidate_nodes 的真实代码为准。\n\n"
        f"parse_result={json.dumps(parse_result, ensure_ascii=False)}\n\n"
        f"crash_summary={json.dumps(crash_summary, ensure_ascii=False)}\n\n"
        f"candidate_nodes={json.dumps(concise_nodes, ensure_ascii=False)}\n\n"
        f"analysis_text={analysis_text}"
    )


def _is_within_code_roots(path: Path, code_roots: List[str]) -> bool:
    for root in code_roots:
        try:
            path.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _replace_function_block(source: str, signature: str, replacement_code: str) -> Tuple[str, Optional[str]]:
    sig_index = source.find(signature)
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
        file_path.write_text(new_text, encoding="utf-8")
        if report_dir is not None and backup_original_sources:
            backup_root = report_dir / "original_sources"
            backup_path = backup_root / file_path.relative_to(Path(code_roots[0]).resolve())
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
    prompt = _build_fix_plan_prompt(parse_result, code_context, analysis_text, candidate_nodes)
    try:
        response = llm_adapter.chat(
            [
                {"role": "system", "content": "你输出严格 JSON，不要输出 Markdown 代码块以外的解释。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        fix_plan = _extract_json_payload(response.content)
        return _apply_ai_fix_plan(
            fix_plan, candidate_nodes, code_roots, report_dir, backup_original_sources
        )
    except Exception as exc:
        return {
            "success": False,
            "error": f"生成或应用 AI 修复计划失败: {exc}",
            "applied": [],
        }


def _print_execution_plan(state: Dict[str, Any]) -> None:
    print("== 执行计划 ==")
    print("- [1/4] 解析崩溃日志")
    print("- [2/4] 符号化堆栈地址")
    print("- [3/4] 提取源码上下文")
    if state.get("skip_ai"):
        print("- [4/4] 跳过 AI（不调用大模型；输出工具链结果并生成可复用提示词）")
    else:
        print("- [4/4] 进行 AI 推理与修复建议")


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
    return {
        "llm_ok": llm_ok,
        "active_provider": active_provider,
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


def _collect_interactive_run_state() -> Optional[Dict[str, Any]]:
    _ensure_user_config_templates()
    session_state = _load_session_state()
    last_run = session_state.get("last_run", {}) if isinstance(session_state.get("last_run", {}), dict) else {}
    preferred_engine = str(last_run.get("engine", "direct")).strip() if isinstance(last_run, dict) else "direct"
    if preferred_engine not in {"direct", "langchain", "langgraph"}:
        preferred_engine = "direct"

    status = _doctor_status()
    print("== 环境速览 ==")
    print(f"- LLM 配置: {'OK' if status['llm_ok'] else 'Missing'}")
    print(f"- add2line 工具: {'OK' if status['tool_ok'] else 'Missing'}")
    available_tools = [
        f"{name}({meta.get('source')})"
        for name, meta in status.get("tool_status", {}).items()
        if (meta.get("path") or "").strip()
    ]
    print(f"- 已检测工具: {', '.join(available_tools) if available_tools else '无'}")
    print("")

    def _print_command_guide_grouped() -> None:
        while True:
            guide_choice = _prompt_select(
                "命令参考（请选择查看范围）",
                [
                    ("basic", "基础（推荐）"),
                    ("advanced", "进阶"),
                    ("back", "返回"),
                ],
                default_index=0,
            )
            if guide_choice == "back":
                return
            if guide_choice == "basic":
                print("== 命令参考：基础（推荐）==")
                print("[分析运行]")
                print("- sa-agent：交互引导，一步步完成配置与分析")
                print("- --crash-log PATH：分析必填，崩溃日志路径；支持 '-' 从 stdin")
                print("- --library-dir DIR：符号库目录（未符号化日志建议填写）")
                print("- --code-root DIR：源码目录（做源码上下文/AI分析时建议填写，可多次）")
                print("- --skip-ai：不调用大模型；输出工具链结果并生成可复用提示词")
                print("")
                print("[输出控制]")
                print("- --output-format {markdown,json,text}")
                print("- --output-file PATH")
                print("")
                print("[关键配置提示]")
                print("- LLM：active_provider + api_key/authorization（必须非占位符）")
                print("- add2line 检测优先级：env -> path -> config")
                print("")
                print("完整文档：docs/cli/CLI_COMMANDS_REFERENCE.md")
                print("命令帮助：sa-agent --help")
                print("")
                continue
            print("== 命令参考：进阶 == ")
            print("[AI 自动改码与备份]")
            print("- --apply-ai-fixes / --no-apply-ai-fixes：是否自动回写源码")
            print("- --backup-original-sources / --no-backup-original-sources：是否备份改前源码")
            print("")
            print("[向量数据库（RAG）运维（独占子流程）]")
            print("- --init-vector-db / --vector-db-stats")
            print("- --export-vector-db [PATH] / --import-vector-db PATH")
            print("- --pattern-feedback + --feedback-type + --feedback-comment")
            print("- --vector-db-decay / --vector-db-gc / --gc-min-confidence / --gc-rejected-threshold")
            print("")
            print("[扩展与高级入口]")
            print("- --plugin-module MODULE（可重复）")
            print("- STABILITY_AGENT_PLUGIN_MODULES（环境变量注入）")
            print("- sa-agent run ... / sa-agent profile ...")
            print("")
            print("完整文档：docs/cli/CLI_COMMANDS_REFERENCE.md")
            print("命令帮助：sa-agent --help")
            print("")

    while True:
        recent_log = str(last_run.get("crash_log", "")).strip() if isinstance(last_run, dict) else ""
        has_recent = bool(recent_log)
        opts: List[Tuple[str, str]] = [
            ("1", "快速开始分析（推荐）"),
            ("2", "更多选项"),
        ]
        if has_recent:
            opts.append(("5", "Analyze recent log again"))
        opts.append(("q", "退出"))
        choice = _prompt_select("请选择要执行的操作", opts, default_index=0).strip().lower()
        if choice == "__eof__":
            return None
        if choice == "q":
            return None
        if choice == "2":
            while True:
                sub_choice = _prompt_select(
                    "更多选项",
                    [
                        ("cfg_llm", "配置大模型"),
                        ("cfg_add2line", "配置 addr2line 工具"),
                        ("engine", f"调整执行引擎（当前: {preferred_engine}）"),
                        ("command_guide", "命令参考（分组说明）"),
                        ("example", "快速示例命令"),
                        ("back", "返回"),
                        ("quit", "退出"),
                    ],
                    default_index=0,
                )
                if sub_choice == "cfg_llm":
                    _configure_llm_only()
                    status = _doctor_status()
                    print("")
                    continue
                if sub_choice == "cfg_add2line":
                    _configure_add2line_only()
                    status = _doctor_status()
                    print("")
                    continue
                if sub_choice == "engine":
                    preferred_engine = _prompt_select(
                        "请选择执行引擎",
                        [
                            ("direct", "direct"),
                            ("langchain", "langchain"),
                            ("langgraph", "langgraph"),
                        ],
                        default_index=["direct", "langchain", "langgraph"].index(preferred_engine),
                    )
                    print(f"已设置默认引擎: {preferred_engine}")
                    print("")
                    continue
                if sub_choice == "example":
                    print("推荐：输入 1 进入交互引导（新手首选）。")
                    print("或直接命令运行（适合熟手/脚本）：")
                    print("sa-agent --crash-log <log.crash> --library-dir <lib_dir> --code-root <code_dir>")
                    print("更多参数：sa-agent --help")
                    print("")
                    continue
                if sub_choice == "command_guide":
                    _print_command_guide_grouped()
                    print("")
                    continue
                if sub_choice == "quit":
                    return None
                break
            continue
        if choice == "5" and has_recent:
            recent_state = {
                "crash_log": str(last_run.get("crash_log", "")).strip(),
                "library_dir": str(last_run.get("library_dir", "")).strip(),
                "code_roots": [str(x).strip() for x in (last_run.get("code_roots", []) or []) if str(x).strip()],
                "engine": str(last_run.get("engine", "direct")).strip() or "direct",
                "skip_ai": bool(last_run.get("skip_ai", False)),
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
            print(f"- library_dir: {recent_state['library_dir'] or 'N/A'}")
            print(f"- code_roots: {recent_state['code_roots'] or []}")
            print(f"- engine: {recent_state['engine']}")
            print(f"- skip_ai: {recent_state['skip_ai']}")
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
            session_state["last_run"] = recent_state
            _save_session_state(session_state)
            return recent_state
        if choice == "1":
            break

    seed = last_run
    crash_log_default = str(seed.get("crash_log", "")).strip()
    library_default = str(seed.get("library_dir", "")).strip()
    code_roots_default = seed.get("code_roots", []) if isinstance(seed.get("code_roots", []), list) else []
    engine_default = preferred_engine
    skip_ai_default = bool(seed.get("skip_ai", False))

    if not status["llm_ok"]:
        print("检测到 LLM 未配置完成。")
        while True:
            fix_choice = _prompt_select(
                "请选择后续操作",
                [
                    ("1", "现在配置大模型"),
                    ("2", "本次跳过 AI 继续"),
                    ("3", "退出"),
                ],
                default_index=0,
            ).strip()
            if fix_choice == "__EOF__":
                return None
            if fix_choice == "1":
                _config_command_init()
                status = _doctor_status()
                if status["llm_ok"]:
                    skip_ai_default = False
                    break
                print("仍未检测到可用 LLM，将默认跳过 AI。")
                skip_ai_default = True
                break
            if fix_choice == "2":
                skip_ai_default = True
                break
            if fix_choice == "3":
                return None
    skip_ai = skip_ai_default
    if status["llm_ok"]:
        skip_ai_choice = _prompt_select(
            "请选择 AI 模式",
            [
                ("no", "使用 AI 分析"),
                ("yes", "跳过 AI（不调用大模型；输出工具链结果并生成可复用提示词）"),
            ],
            default_index=1 if skip_ai_default else 0,
        )
        skip_ai = skip_ai_choice == "yes"

    while True:
        raw = _prompt_with_default("请输入崩溃日志路径（输入 quit 退出）", crash_log_default)
        if raw.lower() == "quit":
            return None
        crash_log = raw or crash_log_default
        if not crash_log:
            print("崩溃日志路径不能为空。")
            continue
        p = Path(crash_log).expanduser().resolve()
        if not p.exists() or not p.is_file():
            print(f"路径无效（需要是文件）: {p}")
            continue
        crash_log = str(p)
        break

    while True:
        raw = _prompt_with_default(
            "请输入库文件目录（若日志尚未完成符号化/地址解析，则必须填写；仅在日志已完成解析时可输入 skip）",
            library_default,
        )
        if raw.lower() == "skip":
            library_dir = ""
            break
        library_dir = raw or library_default
        if not library_dir:
            break
        p = Path(library_dir).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            print(f"路径无效（需要是目录）: {p}")
            continue
        library_dir = str(p)
        break

    while True:
        default_text = ",".join([str(Path(x).expanduser().resolve()) for x in code_roots_default if str(x).strip()])
        raw = _prompt_with_default(
            "请输入代码目录（可多个，英文逗号分隔；若仅做日志提取或堆栈地址解析可输入 skip，若需源码上下文/AI分析则必须填写）",
            default_text,
        )
        if raw.lower() == "skip":
            code_roots: List[str] = []
            break
        path_items = [item.strip() for item in (raw or default_text).split(",") if item.strip()]
        bad = []
        normalized: List[str] = []
        for item in path_items:
            p = Path(item).expanduser().resolve()
            if not p.exists() or not p.is_dir():
                bad.append(str(p))
            else:
                normalized.append(str(p))
        if bad:
            print("以下代码目录无效：")
            for item in bad:
                print(f"- {item}")
            continue
        code_roots = normalized
        break

    engine = engine_default if engine_default in {"direct", "langchain", "langgraph"} else "direct"

    state = {
        "crash_log": crash_log,
        "library_dir": library_dir,
        "code_roots": code_roots,
        "engine": engine,
        "skip_ai": skip_ai,
    }

    print("")
    _print_execution_plan(state)
    print("== 参数确认 ==")
    print(f"- crash_log: {state['crash_log']}")
    print(f"- library_dir: {state['library_dir'] or 'N/A'}")
    print(f"- code_roots: {state['code_roots'] or []}")
    print(f"- engine: {state['engine']}")
    print(f"- skip_ai: {state['skip_ai']}")
    confirm = _prompt_select(
        "请选择下一步",
        [("run", "立即执行"), ("edit", "重新填写参数"), ("cancel", "退出")],
        default_index=0,
    )
    if confirm == "cancel":
        return None
    if confirm == "edit":
        return _collect_interactive_run_state()

    session_state["last_run"] = state
    _save_session_state(session_state)
    return state


def _execute_analysis(args: argparse.Namespace) -> int:
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

    code_roots = _normalize_code_roots(args.code_roots)
    problem = {
        "crash_log": crash_log_content,
        "library_dir": args.library_dir,
        "code_roots": code_roots,
        "engine": args.engine,
        "skip_ai": bool(args.skip_ai),
        "vector_db_path": args.vector_db_path,
        "vector_db_max_results": args.vector_db_max_results,
        "rule_confidence_threshold": args.rule_confidence_threshold,
    }

    registry = ToolAndWorkflowRegistry()
    register_all_tools_and_workflows(registry)

    env_modules = [m.strip() for m in os.environ.get("STABILITY_AGENT_PLUGIN_MODULES", "").split(",") if m.strip()]
    cli_modules = args.plugin_modules or []
    _register_third_party_modules(registry, env_modules + cli_modules)

    if args.config:
        config = SystemConfig.from_file(args.config)
    else:
        config = SystemConfig(
            tools=[
                ToolConfig(name="crash_log_parser", enabled=True),
                ToolConfig(name="add2line_resolver", enabled=True),
                ToolConfig(name="code_content_provider", enabled=True),
            ],
            workflows=[WorkflowConfig(name="crash_analysis", enabled=True)],
        )

    llm_adapter = None
    if not args.skip_ai:
        if config.llm is None:
            llm_config = _build_llm_config_from_agent_config(args.engine)
            if llm_config is not None:
                config.llm = llm_config
            else:
                print(
                    "错误: 未检测到可用 LLM 配置。请直接运行 `sa-agent` 按引导配置，或添加 `--skip-ai` 继续非 AI 分析。",
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

    if args.apply_ai_fixes and result.get("status") == "success" and not args.skip_ai:
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
        write_readme_output=not args.skip_ai,
    )

    if args.output_file:
        Path(args.output_file).write_text(output, encoding="utf-8")
        print(f"结果已保存到: {args.output_file}", file=sys.stderr)
    else:
        print(output)

    if report_dir is not None:
        print(f"cli_report 已保存到: {report_dir}", file=sys.stderr)

    return 0 if result.get("status") == "success" else 1


def _is_tty_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    if raw_argv and raw_argv[0] == "config":
        return _handle_config_command(raw_argv[1:])
    if raw_argv and raw_argv[0] == "profile":
        return _handle_profile_command(raw_argv[1:])

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
        state = _collect_interactive_run_state()
        if state is None:
            print("已退出交互模式。")
            return 0
        argv_from_state: List[str] = [
            "--crash-log",
            state["crash_log"],
            "--engine",
            state["engine"],
            "--interactive=false",
        ]
        if state.get("library_dir"):
            argv_from_state.extend(["--library-dir", state["library_dir"]])
        for code_root in state.get("code_roots", []):
            argv_from_state.extend(["--code-root", code_root])
        if state.get("skip_ai"):
            argv_from_state.append("--skip-ai")
        args = parser.parse_args(argv_from_state)
        return _execute_analysis(args)

    if interactive_requested and not _is_tty_interactive():
        print("错误: 当前为非交互环境，无法启用 --interactive。", file=sys.stderr)
        return 1

    if not args.crash_log and not has_business_args:
        print("错误: 缺少 --crash-log（或直接运行 `sa-agent` 进入交互模式）", file=sys.stderr)
        return 1
    return _execute_analysis(args)


if __name__ == "__main__":
    sys.exit(main())

