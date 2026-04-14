#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一 CLI 入口（Tool System only）。

设计目标：
- 仅使用 Tool/Skill 注册机制执行分析；
- 支持第三方通过模块扩展注册表；
- 作为唯一命令行入口。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

# 支持从任意 cwd 运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tool_system import (  # type: ignore
    ToolAndSkillRegistry,
    SystemConfig,
    LLMConfig,
    ToolConfig,
    SkillConfig,
    ConfigDrivenExecutor,
    LLMAdapterFactory,
    register_all_tools_and_skills,
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


def _load_agent_config_file() -> dict:
    config_dir = PROJECT_ROOT / "tools" / "configs"
    local_path = config_dir / "agent_config.local.json"
    base_path = config_dir / "agent_config.json"
    target = local_path if local_path.exists() else base_path
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


def _register_third_party_modules(registry: ToolAndSkillRegistry, modules: List[str]) -> None:
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
            output_path = str((PROJECT_ROOT / "cli_reports" / "vector_db_snapshot.json").resolve())
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


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

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

    registry = ToolAndSkillRegistry()
    register_all_tools_and_skills(registry)

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
            skills=[SkillConfig(name="crash_analysis", enabled=True)],
        )

    llm_adapter = None
    if not args.skip_ai:
        if config.llm is None:
            llm_config = _build_llm_config_from_agent_config(args.engine)
            if llm_config is not None:
                config.llm = llm_config
        if config.llm is not None:
            try:
                llm_adapter = LLMAdapterFactory.create(config.llm.to_dict())
            except Exception as exc:
                print(f"警告: LLM 适配器初始化失败，将继续执行工具链。错误: {exc}", file=sys.stderr)

    executor = ConfigDrivenExecutor(registry, config, llm_adapter)
    result = executor.execute_skill("crash_analysis", problem)

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
            if result.get("analysis"):
                lines.append("## AI 分析")
                lines.append(str(result.get("analysis")))
            output = "\n".join(lines)
        else:
            output = f"# 错误\n\n{result.get('error')}"

    if args.output_file:
        Path(args.output_file).write_text(output, encoding="utf-8")
        print(f"结果已保存到: {args.output_file}", file=sys.stderr)
    else:
        print(output)

    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())

