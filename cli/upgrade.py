#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开源版包版本检查与升级（公网 PyPI）。"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

PYPI_SIMPLE_INDEX = "https://pypi.org/simple/"
PYPI_PROJECT_URL = "https://pypi.org/project/stability-analysis-agent/"
DIST_NAME = "stability-analysis-agent"
DEFAULT_QUERY_TIMEOUT_SEC = 15
DEFAULT_UPGRADE_TIMEOUT_SEC = 600


@dataclass
class PackageVersionInfo:
    """单个发行包的版本信息。"""

    dist_name: str
    installed: str = ""
    latest: str = ""
    query_error: str = ""

    @property
    def has_installed(self) -> bool:
        return bool(self.installed)

    @property
    def has_latest(self) -> bool:
        return bool(self.latest)

    @property
    def update_available(self) -> bool:
        if not self.has_installed or not self.has_latest:
            return False
        return _version_less_than(self.installed, self.latest)


@dataclass
class InstallContext:
    """当前 CLI 的安装方式。"""

    mode: str  # pip | pipx | editable | binary | unknown
    detail: str = ""


def _version_less_than(left: str, right: str) -> bool:
    try:
        from packaging.version import Version

        return Version(left) < Version(right)
    except Exception:
        return left != right and left < right


def get_installed_version(dist_name: str) -> str:
    """读取已安装发行包版本；未安装时返回空字符串。"""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return str(version(dist_name)).strip()
    except PackageNotFoundError:
        return ""
    except Exception:
        return ""


def _parse_pip_index_versions(stdout: str, dist_name: str = "") -> Tuple[str, List[str]]:
    """从 ``pip index versions`` 输出解析 LATEST 与可用版本列表。"""
    latest = ""
    versions: List[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        match_latest = re.match(r"^LATEST:\s*(\S+)", stripped)
        if match_latest:
            latest = match_latest.group(1)
            continue
        match_available = re.match(r"^Available versions:\s*(.+)$", stripped)
        if match_available:
            versions = [item.strip() for item in match_available.group(1).split(",") if item.strip()]
            continue
        if dist_name:
            match_paren = re.match(rf"^{re.escape(dist_name)} \(([^)]+)\)$", stripped)
            if match_paren and not latest:
                latest = match_paren.group(1).strip()
    if not latest and versions:
        latest = versions[0]
    return latest, versions


def query_latest_version(
    dist_name: str,
    *,
    index_url: Optional[str] = None,
    timeout_sec: int = DEFAULT_QUERY_TIMEOUT_SEC,
) -> Tuple[str, str]:
    """查询远程仓库最新版本，返回 ``(latest_version, error_message)``。"""
    cmd = [sys.executable, "-m", "pip", "index", "versions", dist_name]
    if index_url:
        cmd.extend(["-i", index_url])
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
        )
    except subprocess.TimeoutExpired:
        return "", f"查询超时（>{timeout_sec}s）"
    except OSError as exc:
        return "", str(exc)

    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    if completed.returncode != 0:
        detail = output.strip().splitlines()
        tail = detail[-1] if detail else f"pip 退出码 {completed.returncode}"
        return "", tail

    latest, _versions = _parse_pip_index_versions(output, dist_name)
    if not latest:
        return "", "未能解析远程版本信息"
    return latest, ""


def detect_install_context() -> InstallContext:
    """检测 ``stability-analysis-agent`` 的安装方式。"""
    if getattr(sys, "frozen", False):
        return InstallContext(mode="binary", detail=str(Path(sys.executable).resolve()))

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "show", DIST_NAME],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        completed = None

    if completed and completed.returncode == 0:
        stdout = completed.stdout or ""
        for line in stdout.splitlines():
            if line.lower().startswith("editable project location:"):
                location = line.split(":", 1)[1].strip()
                return InstallContext(mode="editable", detail=location)

    exe = Path(sys.executable).resolve()
    exe_parts = {part.lower() for part in exe.parts}
    if "pipx" in exe_parts and "venvs" in exe_parts:
        return InstallContext(mode="pipx", detail=str(exe))

    if completed and completed.returncode == 0:
        return InstallContext(mode="pip", detail=str(exe))

    return InstallContext(mode="unknown", detail=str(exe))


def is_rag_runtime_available() -> bool:
    """粗略判断当前环境是否具备 RAG 运行时依赖。"""
    try:
        from rag.runtime import get_ai_stability_analyzer_class

        return get_ai_stability_analyzer_class() is not None
    except Exception:
        return False


def preferred_upgrade_spec() -> str:
    """按当前环境推断升级规格：核心包或 ``[rag]``。"""
    return f"{DIST_NAME}[rag]" if is_rag_runtime_available() else DIST_NAME


def collect_version_report(
    *,
    query_timeout_sec: int = DEFAULT_QUERY_TIMEOUT_SEC,
) -> Tuple[PackageVersionInfo, InstallContext, str]:
    """收集当前版本、远程最新版本、安装方式与推荐升级规格。"""
    pkg = PackageVersionInfo(dist_name=DIST_NAME, installed=get_installed_version(DIST_NAME))
    latest, err = query_latest_version(DIST_NAME, index_url=PYPI_SIMPLE_INDEX, timeout_sec=query_timeout_sec)
    pkg.latest = latest
    pkg.query_error = err
    return pkg, detect_install_context(), preferred_upgrade_spec()


def _format_pkg_line(info: PackageVersionInfo) -> str:
    installed = info.installed or "未检测到"
    if info.query_error:
        return f"- {info.dist_name}: 当前 {installed}；最新版查询失败（{info.query_error}）"
    latest = info.latest or "未知"
    if not info.has_installed:
        return f"- {info.dist_name}: 未安装；远程最新 {latest}"
    if info.update_available:
        return f"- {info.dist_name}: 当前 {info.installed} → 可升级至 {info.latest}"
    if info.has_latest:
        return f"- {info.dist_name}: 当前 {info.installed}（已是最新）"
    return f"- {info.dist_name}: 当前 {info.installed}"


def _upgrade_via_pip(package_spec: str, *, timeout_sec: int) -> Tuple[bool, str]:
    cmd = [sys.executable, "-m", "pip", "install", "-U", package_spec]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            text=True,
            timeout=max(30, int(timeout_sec)),
        )
    except subprocess.TimeoutExpired:
        return False, f"升级超时（>{timeout_sec}s）"
    except OSError as exc:
        return False, str(exc)
    if completed.returncode != 0:
        return False, f"pip 升级失败，退出码 {completed.returncode}"
    return True, ""


def _upgrade_via_pipx(*, timeout_sec: int) -> Tuple[bool, str]:
    cmd = ["pipx", "upgrade", DIST_NAME]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            text=True,
            timeout=max(30, int(timeout_sec)),
        )
    except subprocess.TimeoutExpired:
        return False, f"升级超时（>{timeout_sec}s）"
    except FileNotFoundError:
        return False, "未找到 pipx 命令，请手动执行 pipx upgrade"
    except OSError as exc:
        return False, str(exc)
    if completed.returncode != 0:
        return False, f"pipx 升级失败，退出码 {completed.returncode}"
    return True, ""


def run_upgrade(
    install_ctx: InstallContext,
    *,
    package_spec: Optional[str] = None,
    timeout_sec: int = DEFAULT_UPGRADE_TIMEOUT_SEC,
) -> Tuple[bool, str]:
    """按安装方式执行升级。"""
    if install_ctx.mode == "editable":
        return False, "检测到开发模式安装（pip install -e .）。请在源码目录 git pull 后重新执行 pip install -e ."
    if install_ctx.mode == "binary":
        return False, "检测到预编译 CLI 二进制。请从 GitHub Releases 下载新版二进制。"
    if install_ctx.mode == "pipx":
        return _upgrade_via_pipx(timeout_sec=timeout_sec)
    if install_ctx.mode in {"pip", "unknown"}:
        return _upgrade_via_pip(package_spec or preferred_upgrade_spec(), timeout_sec=timeout_sec)
    return False, f"不支持的安装方式: {install_ctx.mode}"


def run_upgrade_check_interactive() -> None:
    """设置菜单：检查更新并在用户确认后升级。"""
    from cli.main import _prompt_select

    print("")
    print("正在检查版本（公网 PyPI）...")
    pkg, install_ctx, package_spec = collect_version_report()

    print("")
    print("版本信息")
    print(_format_pkg_line(pkg))
    print(f"- 安装方式: {_install_mode_label(install_ctx)}")
    if install_ctx.detail and install_ctx.mode in {"editable", "binary"}:
        label = "源码目录" if install_ctx.mode == "editable" else "当前可执行文件"
        print(f"- {label}: {install_ctx.detail}")
    print(f"- PyPI 项目页: {PYPI_PROJECT_URL}")
    print(f"- 推荐升级规格: {package_spec}")
    print("")

    if not pkg.update_available:
        if pkg.query_error:
            print("⚠️  未能查询远程版本，请检查网络后重试。")
        else:
            print("✅ 当前已是最新版本。")
        print("")
        return

    if install_ctx.mode == "editable":
        print("⚠️  检测到开发模式安装，菜单内不会自动 pip 升级。")
        print("请在开源仓库更新代码后执行：")
        print("  git pull")
        print("  pip install -e .")
        print("")
        return

    if install_ctx.mode == "binary":
        print("⚠️  检测到预编译 CLI 二进制，菜单内不会自动升级。")
        print(f"请前往 {PYPI_PROJECT_URL} 或 GitHub Releases 获取新版。")
        print("")
        return

    choice = _prompt_select(
        f"发现新版本 {pkg.latest}，是否立即升级 {package_spec}？",
        [
            ("upgrade", "立即升级"),
            ("manual", "仅显示手动命令"),
            ("cancel", "取消"),
        ],
        default_index=2,
    )
    if choice == "cancel":
        print("")
        return

    if choice == "manual":
        _print_manual_upgrade_commands(install_ctx, package_spec)
        print("")
        return

    print("")
    print("正在升级，请稍候（若包含 RAG 依赖，可能需要数分钟）...")
    ok, err = run_upgrade(install_ctx, package_spec=package_spec)
    if not ok:
        print(f"❌ 升级失败: {err}")
        _print_manual_upgrade_commands(install_ctx, package_spec)
        print("")
        return

    print("✅ 升级完成。请退出并重新运行 sa-agent / sa 以加载新版本。")
    print("")


def _install_mode_label(ctx: InstallContext) -> str:
    mapping = {
        "pip": "pip（当前 Python 环境）",
        "pipx": "pipx（隔离环境）",
        "editable": "开发模式（editable）",
        "binary": "预编译 CLI 二进制",
        "unknown": "未知（将尝试 pip 升级）",
    }
    return mapping.get(ctx.mode, ctx.mode)


def _print_manual_upgrade_commands(ctx: InstallContext, package_spec: str) -> None:
    print("")
    print("手动升级命令：")
    if ctx.mode == "pipx":
        print(f"  pipx upgrade {DIST_NAME}")
    elif ctx.mode == "editable":
        print("  # 在开源仓库目录")
        print("  git pull")
        print("  pip install -e .")
    elif ctx.mode == "binary":
        print(f"  打开 {PYPI_PROJECT_URL}")
        print("  或前往 GitHub Releases 下载新版 CLI 二进制")
    else:
        print(f"  pip install --upgrade \"{package_spec}\"")
