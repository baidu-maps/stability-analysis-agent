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

    raw = _safe_input(f"请输入 base_url（直接回车使用默认） (默认: {suggested}): ").strip()
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
    provider_options: List[Tuple[str, str]] = [("back", "返回")]
    provider_options.extend([(k, k) for k in provider_keys])
    provider_options.append(("custom", "自定义输入 provider 名称"))
    selected_provider = _prompt_select(
        "请选择 provider",
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
    if provider == "baidu_qianfan":
        secret = getpass.getpass("请输入 authorization（输入时隐藏）: ").strip()
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
        secret = getpass.getpass("请输入 api_key（输入时隐藏）: ").strip()
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
    # CLI 向导保持极简：优先通过环境变量配置，必要时再直接指定 bin 目录。
    mode = _prompt_select(
        "请选择配置方式",
        [
            ("back", "返回"),
            ("env", "通过环境变量配置（推荐）"),
            ("path", "直接指定符号化工具链 bin 目录"),
        ],
        default_index=1,
    )
    if mode == "back":
        return False

    env_vars: Dict[str, str] = {}
    missing_env_keys: List[str] = []
    if mode == "env":
        while True:
            raw_env_key = _safe_input("请输入要读取的环境变量名 KEY（必填）: ").strip()
            if raw_env_key == "__EOF__":
                return False
            if not raw_env_key:
                print("环境变量 KEY 不能为空，请重新输入。")
                continue
            key = raw_env_key.strip()
            val = os.environ.get(key)
            if val is None or str(val).strip() == "":
                missing_env_keys.append(key)
            else:
                env_vars[key] = str(val)
            break
    else:
        raw_bin = _safe_input("请输入符号化工具链 bin 目录（可空，输入 back 返回）: ").strip()
        if raw_bin == "__EOF__":
            return False
        if raw_bin.lower() == "back":
            return False
        if raw_bin:
            os_cfg["tool_paths"] = [str(Path(raw_bin).expanduser().resolve())]

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
            f"tool_paths: {os_cfg.get('tool_paths') or []}",
            f"environment_vars: {list((os_cfg.get('environment_vars') or {}).keys())}",
            *( [f"未读取到的环境变量（当前环境缺失/为空）: {', '.join(missing_env_keys)}"] if missing_env_keys else [] ),
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
    print("自动检测 add2line 相关工具：")
    for name, path in detected_tools.items():
        print(f"  - {name}: {path or 'missing'}")
    env_keys = ["ANDROID_NDK_HOME", "LLVM_HOME", "ANDROID_SDK_HOME"]
    print("默认的环境变量：")
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


def _print_llm_detection_summary(status: Dict[str, Any]) -> None:
    print("== 大模型配置检测 ==")
    print(f"- 配置文件: {_user_agent_config_file()}")
    if status.get("llm_ok"):
        print("- 状态: 已配置可用（当前密钥非占位）")
        print(f"- 当前 provider: {status.get('active_provider') or '未知'}")
        model_disp = status.get("model") or "（未填写 model）"
        print(f"- 当前模型: {model_disp}")
    else:
        print("- 状态: 未就绪（缺少 provider、密钥或未替换占位符等）")
        ap = status.get("active_provider")
        if ap:
            print(f"- active_provider: {ap}（请检查密钥等字段是否有效）")
        else:
            print("- active_provider: 未设置")
    print("")


def _check_llm_connectivity() -> None:
    def _ack_result() -> None:
        if _is_tty_interactive():
            _prompt_select(
                "联通性检测已完成，请选择",
                [("ok", "已确定"), ("back", "返回")],
                default_index=0,
            )
        else:
            _safe_input("联通性检测已完成，按回车返回... ")

    llm_cfg = _build_llm_config_from_agent_config("direct")
    if llm_cfg is None:
        print("❌ 当前配置未就绪：请先完成 provider / 密钥配置。")
        _ack_result()
        return
    llm_cfg.timeout = 10
    llm_cfg.max_tokens = 32
    try:
        adapter = LLMAdapterFactory.create(llm_cfg.to_dict())
    except Exception as exc:
        print(f"❌ 初始化 LLM 客户端失败: {exc}")
        _ack_result()
        return

    print("正在检测联通性（最长约 10 秒，可按 Ctrl+C 取消）...")
    start = time.time()
    try:
        resp = adapter.chat(
            [{"role": "user", "content": "请回复：pong"}],
            temperature=0.0,
            max_tokens=32,
        )
        elapsed = time.time() - start
        content = str(resp.content or "").strip().replace("\n", " ")
        if len(content) > 80:
            content = content[:80] + "..."
        print("✅ 联通性检测通过")
        print(f"- provider: {_doctor_status().get('active_provider') or 'unknown'}")
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
                "当前大模型已可用，是否需要重新设置？",
                [
                    ("keep", "不用了，返回"),
                    ("reconfig", "重新设置"),
                    ("connectivity", "检测联通性（可选，可能耗时）"),
                ],
                default_index=0,
            )
            if rerun == "keep":
                return
            if rerun == "connectivity":
                _check_llm_connectivity()
                print("")
                continue
        else:
            print("请完成大模型配置。\n")
            pre_action = _prompt_select(
                "请选择下一步",
                [
                    ("reconfig", "进入交互向导修复"),
                    ("connectivity", "检测联通性（高级，可选）"),
                    ("back", "返回"),
                ],
                default_index=0,
            )
            if pre_action == "back":
                return
            if pre_action == "connectivity":
                _check_llm_connectivity()
                print("")
                continue
        print("")
        print("—— 进入交互向导（将依次选择 provider、填写密钥等）——")
        print("")
        changed = _update_llm_config_interactive()
        if changed:
            return
        nxt2 = _prompt_select(
            "已取消向导或未保存修改。",
            [
                ("exit", "返回"),
                ("retry", "再次进入向导"),
            ],
            default_index=0,
        )
        if nxt2 == "exit":
            return


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
        print(f"- {platform_labels[p]}: {', '.join(hits) if hits else '无可用工具'}")
    print("")

    need_config = [platform_labels[p] for p in ordered_platforms if not platform_hits[p]]
    if need_config:
        print(f"需要配置的平台: {', '.join(need_config)}")
    else:
        print("所有平台均检测到至少一个推荐工具。")
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
        print(f"- 状态: {'已检测到可用工具' if status.get('tool_ok') else '未检测到可用工具'}")
        available_tools = [
            f"{name}({meta.get('source')})"
            for name, meta in status.get("tool_status", {}).items()
            if (meta.get("path") or "").strip()
        ]
        print(f"- 已检测工具: {', '.join(available_tools) if available_tools else '无'}")
        print("")

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
        print("仍未检测到可用 add2line 工具，请继续编辑后再检查。")
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
    p.add_argument("--parse-only", action="store_true", help="仅执行解析+符号化（不提取代码上下文，不调用 AI）")
    p.add_argument("--parse-log-only", action="store_true", help="仅解析崩溃日志（不符号化，不提取代码上下文，不调用 AI）")
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
    run_scope = str(state.get("run_scope", "full")).strip()
    if run_scope == "parse_log_only":
        print("- [1/1] 仅解析崩溃日志")
        return
    if run_scope == "parse_only":
        print("- [1/2] 解析崩溃日志")
        print("- [2/2] 符号化堆栈地址")
        return
    print("- [1/4] 解析崩溃日志")
    print("- [2/4] 符号化堆栈地址")
    print("- [3/4] 提取源码上下文")
    if state.get("skip_ai"):
        print("- [4/4] 跳过 AI（不调用大模型；输出工具链结果并生成可复用提示词）")
    else:
        print("- [4/4] 进行 AI 推理与修复建议")


def _print_user_parameter_confirmation(
    state: Dict[str, Any],
    *,
    library_dir_input: Optional[str] = None,
    code_roots_input: Optional[str] = None,
) -> None:
    lines: List[str] = [f"- crash_log: {state['crash_log']}"]
    library_dir = str(state.get("library_dir") or "").strip()
    code_roots = state.get("code_roots") or []

    if library_dir_input == "skip":
        lines.append("- library_dir: skip")
    elif library_dir:
        lines.append(f"- library_dir: {library_dir}")

    if code_roots_input == "skip":
        lines.append("- code_roots: skip")
    elif code_roots:
        lines.append(f"- code_roots: {code_roots}")

    if not lines:
        return
    print("== 参数确认 ==")
    for line in lines:
        print(line)


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


def collect_interactive_run_state() -> Optional[Dict[str, Any]]:
    _ensure_user_config_templates()
    session_state = _load_session_state()
    last_run = session_state.get("last_run", {}) if isinstance(session_state.get("last_run", {}), dict) else {}
    preferred_engine = str(last_run.get("engine", "direct")).strip() if isinstance(last_run, dict) else "direct"
    if preferred_engine not in {"direct", "langchain", "langgraph"}:
        preferred_engine = "direct"
    preferred_skip_ai = False
    preferred_run_scope = str(last_run.get("run_scope", "full")).strip() if isinstance(last_run, dict) else "full"
    if preferred_run_scope not in {"full", "parse_only", "parse_log_only"}:
        preferred_run_scope = "full"

    def _show_command_reference() -> None:
        print("== 命令参考 ==")
        print("[主流程参数]")
        print("1) --crash-log PATH：崩溃日志路径（支持 '-' 从 stdin 读取）")
        print("2) --library-dir DIR：符号库目录（日志未符号化时建议填写）")
        print("3) --code-root DIR：源码目录（可重复指定多个）")
        print("4) --config PATH：指定 SystemConfig JSON（不填则使用内置默认工具链与工作流）")
        print("5) --skip-ai：仅跳过 LLM 推理，仍执行工具链并生成可复用提示词")
        print("6) --engine {direct|langchain|langgraph}：选择执行引擎")
        print("7) --parse-only：仅执行解析+符号化")
        print("8) --parse-log-only：仅解析崩溃日志")
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
        print("3) 只跑工具链不走 LLM：加 --skip-ai")
        print("4) 向量库统计：--vector-db-stats")
        print("5) 向量库初始化：--init-vector-db")
        print("")

    def _pick_engine(current_engine: str) -> str:
        engine_choice = _prompt_select(
            "请选择执行引擎",
            [
                ("back", "返回"),
                ("direct", "direct（默认，启动快，单轮调用）"),
                ("langchain", "langchain（可编排工具链，适合增强流程）"),
                ("langgraph", "langgraph（多轮 Agent 编排，适合复杂任务）"),
            ],
            default_index=(["direct", "langchain", "langgraph"].index(current_engine) + 1)
            if current_engine in {"direct", "langchain", "langgraph"}
            else 1,
        )
        if engine_choice == "back":
            return current_engine
        return engine_choice

    def _pick_ai_mode(current_skip_ai: bool) -> bool:
        ai_choice = _prompt_select(
            "是否开启AI",
            [
                ("back", "返回"),
                ("use_ai", "使用 AI（默认，支持一步到位自动改码）"),
                ("skip_ai", "跳过 AI（仅跳过LLM，仍生成可复用提示词）"),
            ],
            default_index=2 if current_skip_ai else 1,
        )
        if ai_choice == "back":
            return current_skip_ai
        return ai_choice == "skip_ai"

    def _pick_run_scope(current_scope: str) -> str:
        scope_choice = _prompt_select(
            "设置Agent执行流程",
            [
                ("back", "返回"),
                ("full", "完整分析（解析+符号化+获取代码上下文+根据配置开启AI）"),
                ("parse_only", "仅解析+符号化"),
                ("parse_log_only", "仅解析日志"),
            ],
            default_index=(["full", "parse_only", "parse_log_only"].index(current_scope) + 1)
            if current_scope in {"full", "parse_only", "parse_log_only"}
            else 1,
        )
        if scope_choice == "back":
            return current_scope
        return scope_choice

    while True:
        recent_log = str(last_run.get("crash_log", "")).strip() if isinstance(last_run, dict) else ""
        has_recent = bool(recent_log)
        opts: List[Tuple[str, str]] = [
            ("1", "快速开始分析（推荐）"),
            ("2", "更多选项"),
        ]
        if has_recent:
            opts.append(("5", "再次进行上一次分析"))
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
                        ("back", "返回"),
                        ("cfg_llm", "配置大模型"),
                        ("cfg_add2line", "配置 addr2line 工具"),
                        ("advanced", "高级选项"),
                        ("command_guide", "命令参考"),
                        ("example", "手动输入命令示例"),
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
                                ("add2line_manual", "手动编辑 addr2line 配置文件"),
                                ("engine", f"调整执行引擎（当前: {preferred_engine}）"),
                                (
                                    "run_scope",
                                    f"设置Agent执行流程（当前: { {'full':'完整分析','parse_only':'仅解析+符号化','parse_log_only':'仅解析日志'}.get(preferred_run_scope, preferred_run_scope) }）",
                                ),
                                ("ai_mode", f"设置是否开启AI（当前: {'关闭' if preferred_skip_ai else '开启'}）"),
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
                        if adv == "engine":
                            chosen = _pick_engine(preferred_engine)
                            if chosen != preferred_engine:
                                preferred_engine = chosen
                                print(f"已设置默认引擎: {preferred_engine}")
                            print("")
                            continue
                        if adv == "ai_mode":
                            chosen_skip_ai = _pick_ai_mode(preferred_skip_ai)
                            if chosen_skip_ai != preferred_skip_ai:
                                preferred_skip_ai = chosen_skip_ai
                                print(f"已设置 AI 模式: {'跳过AI' if preferred_skip_ai else '使用AI'}")
                            print("")
                            continue
                        if adv == "run_scope":
                            chosen_scope = _pick_run_scope(preferred_run_scope)
                            if chosen_scope != preferred_run_scope:
                                preferred_run_scope = chosen_scope
                                print(
                                    f"已设置Agent执行流程: { {'full':'完整分析','parse_only':'仅解析+符号化','parse_log_only':'仅解析日志'}.get(preferred_run_scope, preferred_run_scope) }"
                                )
                            print("")
                            continue
                    continue
                if sub_choice == "example":
                    while True:
                        print("━━━━━━━━━━━━━━━━━━━━━━")
                        print("手动输入命令示例")
                        print("")
                        print("推荐：输入 1 进入交互引导（新手首选）。")
                        print("或直接命令运行（适合熟手/脚本）：")
                        print("sa-agent --crash-log <log.crash> --library-dir <lib_dir> --code-root <code_dir>")
                        print("更多参数：sa-agent --help")
                        print("━━━━━━━━━━━━━━━━━━━━━━")
                        ex_action = _prompt_select(
                            "请选择操作",
                            [
                                ("back", "返回"),
                                ("done", "已掌握"),
                            ],
                            default_index=0,
                        )
                        if ex_action in {"done", "back"}:
                            print("")
                            break
                    continue
                if sub_choice == "command_guide":
                    while True:
                        print("━━━━━━━━━━━━━━━━━━━━━━")
                        _show_command_reference()
                        print("━━━━━━━━━━━━━━━━━━━━━━")
                        cmd_action = _prompt_select(
                            "请选择操作",
                            [
                                ("back", "返回"),
                                ("done", "已掌握"),
                            ],
                            default_index=0,
                        )
                        if cmd_action in {"done", "back"}:
                            print("")
                            break
                    continue
                break
            continue
        if choice == "5" and has_recent:
            recent_state = {
                "crash_log": str(last_run.get("crash_log", "")).strip(),
                "library_dir": str(last_run.get("library_dir", "")).strip(),
                "code_roots": [str(x).strip() for x in (last_run.get("code_roots", []) or []) if str(x).strip()],
                "engine": str(last_run.get("engine", "direct")).strip() or "direct",
                "skip_ai": bool(last_run.get("skip_ai", False)),
                "run_scope": str(last_run.get("run_scope", "full")).strip() or "full",
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
            session_state["last_run"] = recent_state
            _save_session_state(session_state)
            return recent_state
        if choice == "1":
            break

    status = _doctor_status()
    seed = last_run
    engine_default = preferred_engine
    skip_ai_default = preferred_skip_ai
    run_scope_default = preferred_run_scope

    if not status["llm_ok"]:
        print("检测到 LLM 未配置完成，正在进入大模型配置引导...")
        while True:
            _configure_llm_only()
            status = _doctor_status()
            if status["llm_ok"]:
                skip_ai_default = False
                preferred_skip_ai = False
                break
            retry_choice = _prompt_select(
                "LLM 仍未配置完成。",
                [
                    ("retry", "继续配置大模型"),
                    ("back", "返回上一级菜单"),
                ],
                default_index=0,
            )
            if retry_choice == "back":
                return None
    skip_ai = skip_ai_default or run_scope_default in {"parse_only", "parse_log_only"}

    while True:
        raw = _safe_input("请输入崩溃日志路径（直接回车返回上一级）: ").strip()
        if raw == "__EOF__":
            return None
        if not raw:
            return collect_interactive_run_state()
        crash_log = raw
        if not crash_log:
            print("崩溃日志路径不能为空。")
            continue
        p = Path(crash_log).expanduser().resolve()
        if not p.exists() or not p.is_file():
            print(f"路径无效（需要是文件）: {p}")
            continue
        crash_log = str(p)
        break

    library_dir_input: Optional[str] = None
    code_roots_input: Optional[str] = None

    if run_scope_default == "parse_log_only":
        library_dir = ""
        code_roots = []
    else:
        while True:
            raw = _safe_input("请输入库文件目录（如果日志已完成堆栈解析可输入skip跳过，直接回车返回上一级）: ").strip()
            if raw == "__EOF__":
                return None
            if not raw:
                return collect_interactive_run_state()
            if raw.lower() == "skip":
                library_dir_input = "skip"
                library_dir = ""
                break
            library_dir = raw
            library_dir_input = library_dir
            p = Path(library_dir).expanduser().resolve()
            if not p.exists() or not p.is_dir():
                print(f"路径无效（需要是目录）: {p}")
                continue
            library_dir = str(p)
            break

        if run_scope_default == "parse_only":
            code_roots = []
        else:
            while True:
                raw = _safe_input("请输入代码目录（可多个，英文逗号分隔，直接回车返回上一级）: ").strip()
                if raw == "__EOF__":
                    return None
                if not raw:
                    return collect_interactive_run_state()
                if raw.lower() == "skip":
                    code_roots_input = "skip"
                    code_roots = []
                    break
                path_items = [item.strip() for item in raw.split(",") if item.strip()]
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
                if code_roots:
                    code_roots_input = raw
                break

    engine = engine_default if engine_default in {"direct", "langchain", "langgraph"} else "direct"

    state = {
        "crash_log": crash_log,
        "library_dir": library_dir,
        "code_roots": code_roots,
        "engine": engine,
        "skip_ai": skip_ai,
        "run_scope": run_scope_default,
    }

    print("")
    _print_execution_plan(state)
    _print_user_parameter_confirmation(
        state,
        library_dir_input=library_dir_input,
        code_roots_input=code_roots_input,
    )
    print("- 提示: 运行中按 Ctrl+C 可立即终止当前任务。")

    session_state["last_run"] = state
    _save_session_state(session_state)
    return state


def execute_analysis(args: argparse.Namespace) -> int:
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

    run_scope = "full"
    if getattr(args, "parse_log_only", False):
        run_scope = "parse_log_only"
    elif getattr(args, "parse_only", False):
        run_scope = "parse_only"
    code_roots = _normalize_code_roots(args.code_roots)
    effective_skip_ai = bool(args.skip_ai) or run_scope in {"parse_only", "parse_log_only"}
    problem = {
        "crash_log": crash_log_content,
        "library_dir": args.library_dir,
        "code_roots": code_roots,
        "engine": args.engine,
        "skip_ai": effective_skip_ai,
        "run_scope": run_scope,
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
        tool_entries = [ToolConfig(name="crash_log_parser", enabled=True)]
        if run_scope in {"full", "parse_only"}:
            tool_entries.append(ToolConfig(name="add2line_resolver", enabled=True))
        if run_scope == "full":
            tool_entries.append(ToolConfig(name="code_content_provider", enabled=True))
        config = SystemConfig(
            tools=tool_entries,
            workflows=[WorkflowConfig(name="crash_analysis", enabled=True)],
        )

    llm_adapter = None
    if not effective_skip_ai:
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

    if args.apply_ai_fixes and result.get("status") == "success" and not effective_skip_ai and run_scope == "full":
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
        write_readme_output=not effective_skip_ai,
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
            if state.get("skip_ai"):
                argv_from_state.append("--skip-ai")
            run_scope = str(state.get("run_scope", "full")).strip()
            if run_scope == "parse_only":
                argv_from_state.append("--parse-only")
            elif run_scope == "parse_log_only":
                argv_from_state.append("--parse-log-only")
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

