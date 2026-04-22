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
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 支持从任意 cwd 运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
    file_override = os.environ.get("STABILITY_AGENT_CONFIG_FILE", "").strip()
    if file_override:
        target = Path(file_override).expanduser().resolve()
        if target.exists():
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    bundled_config_dir = PROJECT_ROOT / "tools" / "configs"
    user_config_dir = _user_config_dir()
    candidates = [
        user_config_dir / "agent_config.local.json",
        user_config_dir / "agent_config.json",
        bundled_config_dir / "agent_config.local.json",
        bundled_config_dir / "agent_config.json",
    ]
    target = next((p for p in candidates if p.exists()), None)
    if target is None:
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

    default_provider = llm_cfg.get("default_provider", "openai")
    provider_cfg = providers.get(default_provider, {})
    if not isinstance(provider_cfg, dict):
        return None

    mapped_engine = engine if engine in ("direct", "langchain", "langgraph") else "direct"

    if default_provider == "openai":
        api_key = provider_cfg.get("api_key")
        if not api_key:
            return None
        return LLMConfig(
            engine=mapped_engine,
            provider="openai",
            model=provider_cfg.get("model", "gpt-4o"),
            api_key=api_key,
            base_url=provider_cfg.get("base_url") or None,
        )

    if default_provider == "zhipu_bigmodel":
        api_key = provider_cfg.get("api_key")
        if not api_key:
            return None
        return LLMConfig(
            engine=mapped_engine,
            provider="openai",
            model=provider_cfg.get("model", "glm-4"),
            api_key=api_key,
            base_url=provider_cfg.get("base_url") or "https://open.bigmodel.cn/api/paas/v4",
        )

    if default_provider == "deepseek":
        api_key = provider_cfg.get("api_key")
        if not api_key:
            return None
        return LLMConfig(
            engine=mapped_engine,
            provider="deepseek",
            model=provider_cfg.get("model", "deepseek-chat"),
            api_key=api_key,
            base_url=provider_cfg.get("base_url") or "https://api.deepseek.com/v1",
            extra={
                "deepseek_api_key": api_key,
                "deepseek_base_url": provider_cfg.get("base_url") or "https://api.deepseek.com/v1",
            },
        )

    if default_provider == "baidu_qianfan":
        authorization = provider_cfg.get("authorization")
        if not authorization:
            return None
        base_url = provider_cfg.get("base_url") or "https://qianfan.baidubce.com/v2"
        return LLMConfig(
            engine=mapped_engine,
            provider="baidu_qianfan",
            model=provider_cfg.get("model", "ernie-4.0-8k"),
            api_key=authorization,
            base_url=base_url,
            extra={
                "authorization": authorization,
                "baidu_qianfan_authorization": authorization,
            },
        )

    return None


def _bundled_config_dir() -> Path:
    return PROJECT_ROOT / "tools" / "configs"


def _user_agent_config_file() -> Path:
    return _user_config_dir() / "agent_config.local.json"


def _user_add2line_config_file() -> Path:
    return _user_config_dir() / "add2line_resolver_config.local.json"


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
    tools = ["atos", "addr2line", "llvm-addr2line", "llvm-symbolizer", "ndk-stack"]
    return {tool: shutil.which(tool) for tool in tools}


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def _prompt_yes_no(question: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    raw = input(f"{question} {suffix} ").strip().lower()
    if not raw:
        return default_yes
    return raw in {"y", "yes"}


def _prompt_non_empty(question: str, default_value: str = "") -> str:
    while True:
        raw = input(f"{question}{f' (默认: {default_value})' if default_value else ''}: ").strip()
        if raw:
            return raw
        if default_value:
            return default_value
        print("输入不能为空，请重试。")


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


def _config_command_path() -> int:
    print(f"config_dir: {_user_config_dir()}")
    for p in [_user_agent_config_file(), _user_add2line_config_file()]:
        print(f"{p.name}: {'exists' if p.exists() else 'missing'} ({p})")
    effective_agent = os.environ.get("STABILITY_AGENT_CONFIG_FILE", "").strip() or str(_user_agent_config_file())
    effective_add2line = os.environ.get("STABILITY_AGENT_ADD2LINE_CONFIG_FILE", "").strip() or str(_user_add2line_config_file())
    print(f"effective_agent_config: {effective_agent}")
    print(f"effective_add2line_config: {effective_add2line}")
    return 0


def _config_command_doctor() -> int:
    problems: List[str] = []
    agent_cfg = _load_json_or_empty(_user_agent_config_file())
    llm_cfg = agent_cfg.get("llm_config", {}) if isinstance(agent_cfg, dict) else {}
    providers = llm_cfg.get("providers", {}) if isinstance(llm_cfg, dict) else {}
    default_provider = llm_cfg.get("default_provider") if isinstance(llm_cfg, dict) else None
    if not default_provider:
        problems.append("LLM default_provider 未配置")
    provider_cfg = providers.get(default_provider, {}) if isinstance(providers, dict) and default_provider else {}
    if default_provider == "baidu_qianfan":
        if not provider_cfg.get("authorization"):
            problems.append("baidu_qianfan.authorization 为空")
    elif default_provider:
        if not provider_cfg.get("api_key"):
            problems.append(f"{default_provider}.api_key 为空")
    else:
        problems.append("LLM provider 配置缺失")

    tools = _detect_add2line_tools()
    if not any(tools.values()):
        problems.append("未检测到 atos/addr2line/llvm-addr2line/llvm-symbolizer/ndk-stack 任一工具")

    print("== 配置检查结果 ==")
    print(f"agent_config: {_user_agent_config_file()}")
    print(f"add2line_config: {_user_add2line_config_file()}")
    print("tools:")
    for name, path in tools.items():
        print(f"  - {name}: {path or 'missing'}")
    if problems:
        print("status: WARN")
        for item in problems:
            print(f"  - {item}")
        print("建议执行: sa-agent config init")
        return 1
    print("status: PASS")
    return 0


def _update_llm_config_interactive() -> None:
    target = _user_agent_config_file()
    data = _load_json_or_empty(target)
    llm_cfg = data.get("llm_config", {}) if isinstance(data, dict) else {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    providers = llm_cfg.get("providers", {})
    if not isinstance(providers, dict):
        providers = {}

    provider = _prompt_non_empty("请输入 provider（示例: openai / deepseek / zhipu_bigmodel / baidu_qianfan）", "openai")
    model_defaults = {
        "openai": "gpt-4o",
        "deepseek": "deepseek-chat",
        "zhipu_bigmodel": "glm-4",
        "baidu_qianfan": "ernie-4.0-8k",
    }
    provider_cfg = providers.get(provider, {})
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}
    if provider == "baidu_qianfan":
        secret = getpass.getpass("请输入 authorization（输入时隐藏）: ").strip()
        provider_cfg["authorization"] = secret
        provider_cfg["model"] = _prompt_non_empty("请输入模型名", provider_cfg.get("model") or model_defaults.get(provider, "ernie-4.0-8k"))
        base_url = input("请输入 base_url（可空，回车跳过）: ").strip()
        if base_url:
            provider_cfg["base_url"] = base_url
    else:
        secret = getpass.getpass("请输入 api_key（输入时隐藏）: ").strip()
        provider_cfg["api_key"] = secret
        provider_cfg["model"] = _prompt_non_empty("请输入模型名", provider_cfg.get("model") or model_defaults.get(provider, "gpt-4o"))
        base_url = input("请输入 base_url（可空，回车跳过）: ").strip()
        if base_url:
            provider_cfg["base_url"] = base_url

    providers[provider] = provider_cfg
    llm_cfg["providers"] = providers
    llm_cfg["default_provider"] = provider
    data["llm_config"] = llm_cfg
    _write_json_pretty(target, data)
    print(f"LLM 配置已写入: {target}")
    print(f"provider={provider}, secret={_mask_secret(secret)}")


def _update_add2line_config_interactive() -> None:
    target = _user_add2line_config_file()
    data = _load_json_or_empty(target)
    if not isinstance(data, dict):
        data = {}
    platforms = data.get("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}

    os_choice = _prompt_non_empty("请输入目标平台（ios/android/linux/harmonyos）", "ios")
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
    print(f"add2line 配置已写入: {target}")


def _config_command_init() -> int:
    _ensure_user_config_templates()
    print(f"配置目录: {_user_config_dir()}")

    if _prompt_yes_no("是否现在配置大模型？", True):
        mode = _prompt_non_empty("请选择方式：1=手动编辑配置文件, 2=交互向导填写", "2")
        if mode == "1":
            print("请手动编辑以下文件并填写密钥：")
            print(f"  {_user_agent_config_file()}")
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
        mode = _prompt_non_empty("请选择方式：1=手动编辑配置文件, 2=交互向导填写", "2")
        if mode == "1":
            print("请手动编辑以下文件并填写工具路径：")
            print(f"  {_user_add2line_config_file()}")
        else:
            _update_add2line_config_interactive()

    print("初始化完成。建议运行: sa-agent config doctor")
    return 0


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
    p = argparse.ArgumentParser(description="Stability Analysis Agent CLI (Tool System Unified)")
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
    p.add_argument("--skip-ai", action="store_true", help="跳过 AI（仅执行工具链）")
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


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    if raw_argv and raw_argv[0] == "config":
        return _handle_config_command(raw_argv[1:])

    args = build_parser().parse_args(raw_argv)

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
                    "错误: 未检测到可用 LLM 配置。请执行 `sa-agent config init` 或添加 `--skip-ai` 继续非 AI 分析。",
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


if __name__ == "__main__":
    sys.exit(main())

