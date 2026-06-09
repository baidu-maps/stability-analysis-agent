#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill CLI 子命令。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .manager import SkillManager, _default_skill_home
from .runtime import SkillRuntime
from .templates import write_skill_scaffold


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _skill_manager_from_args(args: argparse.Namespace) -> SkillManager:
    roots: Optional[List[Path]] = None
    if getattr(args, "skill_dirs", None):
        roots = [Path(item) for item in args.skill_dirs]
    installed_root = Path(args.skill_home) if getattr(args, "skill_home", None) else _default_skill_home()
    return SkillManager(skill_roots=roots, installed_root=installed_root)


def _add_common_root_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--skill-home",
        dest="skill_home",
        help="技能安装根目录（默认：~/.config/stability-analysis-agent/skills）",
    )
    parser.add_argument(
        "--skill-dir",
        action="append",
        dest="skill_dirs",
        help="额外技能发现目录（可重复）",
    )


def _cmd_list(args: argparse.Namespace) -> int:
    manager = _skill_manager_from_args(args)
    summaries = [summary.to_dict() for summary in manager.list()]
    if args.json:
        _print_json({"skills": summaries})
        return 0
    if not summaries:
        print("未发现任何技能。")
        return 0
    for item in summaries:
        print(f"{item['command_name']}\t{item['version']}\t{item['type']}\t{item['path']}")
        if item.get("description"):
            print(f"  {item['description']}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    manager = _skill_manager_from_args(args)
    bundle = manager.resolve(args.name)
    payload = {
        "summary": bundle.to_summary().to_dict(),
        "frontmatter": bundle.frontmatter.to_dict(),
        "package": bundle.package.to_dict(),
        "body": bundle.body,
    }
    if args.json:
        _print_json(payload)
        return 0
    summary = payload["summary"]
    print(f"Name: {summary['display_name']}")
    print(f"Command: {summary['command_name']}")
    print(f"Path: {summary['path']}")
    print(f"Type: {summary['type']}")
    print(f"Version: {summary['version']}")
    print(f"Entrypoint: {summary['entrypoint']}")
    if summary.get("description"):
        print(f"Description: {summary['description']}")
    print("")
    print(bundle.body.strip())
    return 0


def _cmd_lint(args: argparse.Namespace) -> int:
    manager = _skill_manager_from_args(args)
    issues = manager.lint(args.source)
    payload = [issue.to_dict() for issue in issues]
    if args.json:
        _print_json({"issues": payload})
        return 0 if not any(item["level"] == "error" for item in payload) else 1
    if not payload:
        print("OK")
        return 0
    for issue in payload:
        print(f"[{issue['level'].upper()}] {issue['message']}")
        if issue.get("path"):
            print(f"  path: {issue['path']}")
    return 0 if not any(item["level"] == "error" for item in payload) else 1


def _cmd_install(args: argparse.Namespace) -> int:
    manager = _skill_manager_from_args(args)
    result = manager.install_from_path(
        args.source,
        target_root=args.target_root,
        overwrite=bool(args.overwrite),
    )
    if args.json:
        _print_json(result.to_dict())
        return 0
    print(f"已安装: {result.command_name}")
    print(f"路径: {result.installed_path}")
    print(f"版本: {result.version}")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    manager = _skill_manager_from_args(args)
    ok = manager.uninstall(args.name, target_root=args.target_root)
    if args.json:
        _print_json({"name": args.name, "removed": ok})
        return 0 if ok else 1
    print("已卸载" if ok else "未找到技能")
    return 0 if ok else 1


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser().resolve()
    written = write_skill_scaffold(target, args.name, skill_type=args.type, overwrite=bool(args.overwrite))
    if args.json:
        _print_json({"target": str(target), "written": [str(path) for path in written]})
        return 0
    print(f"已创建技能模板: {target}")
    for path in written:
        print(f"  {path}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    manager = _skill_manager_from_args(args)
    runtime = SkillRuntime(manager)
    arguments = " ".join(args.arguments or []).strip()
    payload: Optional[Dict[str, Any]] = None
    if args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("--input 文件必须是 JSON object")
    result = runtime.execute(args.name, arguments=arguments, input_payload=payload)
    if args.json:
        _print_json(result.to_dict())
        return 0
    if result.mode == "workflow":
        print(json.dumps(result.result, ensure_ascii=False, indent=2))
        return 0
    print(result.prompt or "")
    return 0


def build_skill_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sa-agent skill", description="Skill 管理命令")
    sub = parser.add_subparsers(dest="skill_cmd", required=True)

    p_list = sub.add_parser("list", help="列出可发现的 skills")
    _add_common_root_args(p_list)
    p_list.add_argument("--json", action="store_true", help="JSON 输出")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="显示 skill 详情")
    _add_common_root_args(p_show)
    p_show.add_argument("name", help="skill 名称")
    p_show.add_argument("--json", action="store_true", help="JSON 输出")
    p_show.set_defaults(func=_cmd_show)

    p_lint = sub.add_parser("lint", help="校验 skill")
    _add_common_root_args(p_lint)
    p_lint.add_argument("source", help="skill 路径或待校验目录")
    p_lint.add_argument("--json", action="store_true", help="JSON 输出")
    p_lint.set_defaults(func=_cmd_lint)

    p_install = sub.add_parser("install", help="安装 skill")
    _add_common_root_args(p_install)
    p_install.add_argument("source", help="skill 目录或 zip 包")
    p_install.add_argument("--target-root", help="安装到指定目录")
    p_install.add_argument("--overwrite", action="store_true", help="覆盖已存在 skill")
    p_install.add_argument("--json", action="store_true", help="JSON 输出")
    p_install.set_defaults(func=_cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="卸载 skill")
    _add_common_root_args(p_uninstall)
    p_uninstall.add_argument("name", help="skill 名称")
    p_uninstall.add_argument("--target-root", help="卸载目标根目录")
    p_uninstall.add_argument("--json", action="store_true", help="JSON 输出")
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_init = sub.add_parser("init", help="生成 skill 模板")
    p_init.add_argument("name", help="skill 名称")
    p_init.add_argument("target", help="生成到的目标目录")
    p_init.add_argument(
        "--type",
        default="prompt",
        choices=["prompt", "workflow", "tool", "plugin"],
        help="模板类型",
    )
    p_init.add_argument("--overwrite", action="store_true", help="覆盖已存在目录")
    p_init.add_argument("--json", action="store_true", help="JSON 输出")
    p_init.set_defaults(func=_cmd_init)

    p_run = sub.add_parser("run", help="运行 skill")
    _add_common_root_args(p_run)
    p_run.add_argument("name", help="skill 名称")
    p_run.add_argument("arguments", nargs="*", help="传给 skill 的参数")
    p_run.add_argument("--input", help="运行输入 JSON 文件")
    p_run.add_argument("--json", action="store_true", help="JSON 输出")
    p_run.set_defaults(func=_cmd_run)

    return parser


def handle_skill_command(argv: List[str]) -> int:
    parser = build_skill_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))

