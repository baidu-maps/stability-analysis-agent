#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add2line解析工具
根据操作系统调用对应的add2line工具解析堆栈地址
"""

import json
import re
import subprocess
import logging
import os
import platform
import concurrent.futures
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path

from ._library_frame_whitelist import find_library_files_in_dir, match_libraries_for_module

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Apple 已符号化栈常见：symbol(file.m:42) 或 main(main.m:27)
_IOS_SOURCE_FILE_LINE_RE = re.compile(
    r"\(([^()]*?\.(?:m|mm|c|cc|cpp|cxx|swift|h|hpp)):(\d+)\)",
    re.IGNORECASE,
)


def ios_stack_frames_look_symbolicated(frames: List[Dict[str, Any]]) -> bool:
    """
    启发式判断 iOS crash_log_parser 输出的堆栈是否「已符号化」。
    用于在未提供库目录时，安全地走「仅从日志回填 02」路径。
    """
    if not frames:
        return False

    def _has_file_line_in_symbol(fn: str) -> bool:
        return bool(fn and _IOS_SOURCE_FILE_LINE_RE.search(fn))

    if any(_has_file_line_in_symbol(str(f.get("function") or "")) for f in frames):
        return True

    def _meaningful_symbol(fn: Optional[str]) -> bool:
        s = (fn or "").strip()
        if len(s) < 3:
            return False
        if re.match(r"^0x[0-9a-fA-F]+\s+\+\s+\d+\s*$", s):
            return False
        if re.match(r"^0x[0-9a-fA-F]+\s*$", s):
            return False
        return True

    with_addr = [
        f
        for f in frames
        if isinstance(f.get("address"), str) and f.get("address", "").strip().lower().startswith("0x")
    ]
    if not with_addr:
        return False
    good = sum(1 for f in with_addr if _meaningful_symbol(f.get("function")))
    if good >= 3:
        return True
    if len(with_addr) >= 2 and good >= 2:
        return True
    if len(with_addr) == 1 and good >= 1:
        return True
    return False


def stack_frames_look_symbolicated_generic(frames: List[Dict[str, Any]]) -> bool:
    """通用启发式：判断 01 的帧是否已带可读符号信息（无需再依赖库做二次解析）。"""
    if not frames:
        return False

    def _is_hex_only(s: str) -> bool:
        return bool(re.fullmatch(r"0x[0-9a-fA-F]+", s.strip()))

    meaningful = 0
    for f in frames:
        fn = (f.get("function") or "").strip()
        rf = (f.get("resolved_function") or "").strip()
        ff = (f.get("file") or "").strip()
        rr = (f.get("resolved_file") or "").strip()
        ln = f.get("line")
        rln = f.get("resolved_line")

        has_source = bool(ff or rr) or (ln not in (None, "", 0, "0")) or (rln not in (None, "", 0, "0"))
        has_symbol = False
        for s in (rf, fn):
            if not s:
                continue
            if _is_hex_only(s):
                continue
            has_symbol = True
            break

        if has_source or has_symbol:
            meaningful += 1

    if meaningful >= 3:
        return True
    if len(frames) <= 3 and meaningful >= 1:
        return True
    return False


def _is_probably_system_module(module: Optional[str]) -> bool:
    if not module or not isinstance(module, str):
        return False
    m = module.strip()
    if not m:
        return False
    m_lower = m.lower()
    if m_lower in {"(null)", "null"}:
        return True
    system_prefixes = (
        "libsystem",
        "libdispatch",
        "corefoundation",
        "uikit",
        "uikitcore",
        "graphicsservices",
        "foundation",
        "cfnetwork",
        "security",
        "network",
        "metal",
        "quartzcore",
    )
    return m_lower.startswith(system_prefixes)

@dataclass
class ResolvedFrame:
    """解析后的堆栈帧信息"""
    address: str
    function: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    raw_log_line: Optional[int] = None
    module: Optional[str] = None
    resolved_function: Optional[str] = None
    resolved_file: Optional[str] = None
    resolved_line: Optional[int] = None

@dataclass
class Add2lineResult:
    """add2line解析结果"""
    resolved_frames: List[ResolvedFrame]
    os_type: str
    library_path: str
    success_count: int
    total_count: int
    errors: List[str]
    # 新增字段：记录本次解析实际使用的堆栈地址解析工具，便于回溯与对比环境
    tool_name: Optional[str] = None          # 例如: "atos" / "addr2line" / "llvm-addr2line"
                                              # 注：当系统仅有 llvm-symbolizer（缺少 llvm-addr2line / addr2line）时，
                                              # 解析器会将 llvm-symbolizer 注册为 "llvm-addr2line" 的兼容回退，
                                              # 此处仍记为 "llvm-addr2line"，可结合 tool_path 区分真实可执行文件。
    tool_path: Optional[str] = None          # 例如: "/usr/bin/atos"
    tools_available: Optional[Dict[str, str]] = None  # 当前环境中探测到的所有可用工具 {name: path}
    resolution_source: Optional[str] = None  # 如 ios_log_symbolicated：无库时从已符号化日志回填

class Add2lineResolver:
    """增强的堆栈地址解析器，支持多种操作系统和工具"""
    
    def __init__(self, tool_search_paths: Optional[Dict[str, List[str]]] = None, config_file: Optional[str] = None, 
                 library_dir: Optional[str] = None, quick_mode: bool = False):
        """
        初始化堆栈地址解析器
        
        Args:
            tool_search_paths: 可选的工具搜索路径配置（优先级最高），格式为：
                {
                    "ANDROID_NDK_HOME": ["/path/to/ndk"],
                    "LLVM_HOME": ["/path/to/llvm"],
                    "TOOLCHAIN_PATH": ["/path/to/toolchain"],
                    "PATH": ["/custom/path1", "/custom/path2"]
                }
                如果提供，将优先使用这些路径
            config_file: 可选的配置文件路径，如果不提供，将尝试从默认位置读取
            library_dir: 库文件目录路径（仅解析该目录下存在的库，对系统库等不做符号解析）
            quick_mode: 快速模式（跳过慢速兜底策略，优先吞吐）
        """
        self.tool_search_paths = tool_search_paths or {}
        self.config_file = config_file
        self.library_dir = library_dir
        self.quick_mode = quick_mode
        self.config = self._load_config_file()
        self.os_type = self._detect_current_os()
        # 当系统未直接提供 llvm-addr2line / addr2line 但提供 llvm-symbolizer 时，
        # 我们会把 llvm-symbolizer 注册为 llvm-addr2line 的兼容回退；
        # 此字段记录该回退的真实可执行路径，调用 _resolve_with_addr2line 时据此切换命令行格式。
        self._llvm_addr2line_alias_path: Optional[str] = None
        self.resolver_tools = self._find_resolver_tools()
        self.primary_tool = self._select_primary_tool()
        self._function_location_cache: Dict[str, Optional[Tuple[str, int]]] = {}
        
        # 仅在需要时使用 library_dir 查找库文件，不再构建显式白名单

    def _emit_progress(self, message: str) -> None:
        # CLI 端默认由 workflow 统一展示阶段进展，工具级细节写入 report 文件即可。
        # 保留接口以兼容历史调用点，但默认不向终端打印逐条日志。
        _ = message
    
    def _load_config_file(self) -> Dict[str, Any]:
        """
        加载配置文件
        
        Returns:
            配置字典，如果加载失败返回空字典
        """
        config_candidates: list[Path] = []
        env_override = os.environ.get("STABILITY_AGENT_ADD2LINE_CONFIG_FILE", "").strip()
        if env_override:
            config_candidates.append(Path(env_override).expanduser().resolve())
        
        # 如果指定了配置文件路径，优先使用（保持兼容）
        if self.config_file:
            root = Path(__file__).resolve().parents[1]
            if Path(self.config_file).is_absolute():
                config_candidates.append(Path(self.config_file))
            else:
                # 相对路径，尝试多个位置
                config_candidates.extend([
                    Path(self.config_file),
                    root / "tools" / "configs" / self.config_file,
                    root / "configs" / self.config_file,
                ])
        else:
            # 默认配置文件位置：仅使用 .local.json，避免 base/local 双配置造成混淆
            local_name = "add2line_resolver_config.local.json"
            root = Path(__file__).resolve().parents[1]
            home = Path.home()
            cwd_configs = Path.cwd().resolve() / "configs"

            # 运行目录配置：
            # 发布产物拷贝后应优先读取同目录下 configs/
            config_candidates.extend([
                cwd_configs / local_name,
            ])

            # 项目配置目录
            config_candidates.extend([
                root / "tools" / "configs" / local_name,
            ])

            # 用户目录
            config_candidates.extend([
                home / ".config" / "stability-analysis-agent" / local_name,
            ])
        
        # 尝试加载配置文件（按候选顺序，先找到先用）
        for config_path in config_candidates:
            if config_path.exists():
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                    logger.info(f"成功加载配置文件: {config_path}")
                    return config
                except Exception as e:
                    logger.warning(f"加载配置文件失败 {config_path}: {e}")
                    continue
        
        logger.debug("未找到配置文件，使用默认配置")
        return {}
    
    def _detect_current_os(self) -> str:
        """检测当前操作系统，包括移动平台"""
        system = platform.system().lower()
        
        # 强制检测macOS环境
        if system == "darwin" or 'darwin' in platform.platform().lower():
            return "macos"
        
        # 检查是否是Android环境
        if self._is_android_environment():
            return "android"
        
        # 检查是否是iOS环境
        if self._is_ios_environment():
            return "ios"
        
        # 标准操作系统检测
        if system == "linux":
            return "linux"
        elif system == "windows":
            return "windows"
        else:
            return "unknown"
    
    def _is_android_environment(self) -> bool:
        """检测是否是Android开发环境"""
        # 检查环境变量
        android_vars = ['ANDROID_NDK_HOME', 'ANDROID_SDK_HOME', 'ANDROID_HOME']
        for var in android_vars:
            if var in os.environ:
                return True
        
        # 检查PATH中是否有Android工具
        if 'PATH' in os.environ:
            path_dirs = os.environ['PATH'].split(':')
            for path_dir in path_dirs:
                if any(android_tool in path_dir.lower() for android_tool in ['android', 'ndk', 'sdk']):
                    return True
        
        # 检查配置文件中的Android设置（跳过有问题的文件）
        config_files = ["~/.bash_profile", "~/.bashrc", "~/.zshrc", "~/.profile"]
        for config_file in config_files:
            config_path = Path(config_file).expanduser()
            if config_path.exists():
                try:
                    # 检查文件大小，跳过过大的文件
                    if config_path.stat().st_size > 100000:  # 100KB
                        continue
                    with open(config_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if any(android_var in content for android_var in ['ANDROID_NDK_HOME', 'ANDROID_SDK_HOME']):
                            return True
                except Exception:
                    continue
        
        return False
    
    def _is_ios_environment(self) -> bool:
        """检测是否是iOS开发环境"""
        # 检查环境变量
        ios_vars = ['XCODE_PATH', 'IOS_SDK_PATH', 'DEVELOPER_DIR']
        for var in ios_vars:
            if var in os.environ:
                return True
        
        # 检查PATH中是否有iOS工具
        if 'PATH' in os.environ:
            path_dirs = os.environ['PATH'].split(':')
            for path_dir in path_dirs:
                if any(ios_tool in path_dir.lower() for ios_tool in ['xcode', 'ios', 'developer']):
                    return True
        
        # 检查Xcode是否安装
        xcode_paths = [
            "/Applications/Xcode.app/Contents/Developer",
            "/Developer",
            "/usr/bin/xcodebuild"
        ]
        for xcode_path in xcode_paths:
            if Path(xcode_path).exists():
                return True
        
        return False
    
    def _get_tool_priority_list(self, os_type: Optional[str] = None) -> List[str]:
        """
        获取指定操作系统下的工具优先级列表
        
        Args:
            os_type: 操作系统类型，如果为None则使用当前运行环境的OS类型
        """
        if os_type is None:
            os_type = self.os_type
        
        # 首先尝试从配置文件读取
        if self.config and "platforms" in self.config:
            platform_config = self.config["platforms"].get(os_type, {})
            if "preferred_tools" in platform_config and platform_config["preferred_tools"]:
                logger.info(f"从配置文件读取 {os_type} 平台工具优先级: {platform_config['preferred_tools']}")
                return platform_config["preferred_tools"]
            
        # 默认工具优先级列表
        tool_priorities = {
            "linux": [
                "addr2line",           # 标准Linux工具
                "llvm-addr2line",      # LLVM版本
                "eu-addr2line",        # elfutils版本
                "gdb",                 # GNU调试器
                "objdump",             # 对象文件转储工具
            ],
            "macos": [
                "atos",                # macOS专用符号化工具
                "llvm-atos",           # LLVM版本
                "llvm-addr2line",      # LLVM地址行工具
                "addr2line",           # 标准工具
                "gdb",                 # GNU调试器
                "otool",               # 对象文件工具
            ],
            "windows": [
                "llvm-addr2line",      # LLVM版本
                "addr2line",           # 标准工具
                "dumpbin",             # Microsoft工具
                "windbg",              # Windows调试器
                "cdb",                 # 控制台调试器
            ],
            "android": [
                "llvm-addr2line",      # LLVM版本
                "addr2line",           # 标准工具
                "gdb",                 # GNU调试器
                "ndk-stack",           # Android NDK工具
            ],
            "ios": [
                "atos",                # iOS专用符号化工具
                "llvm-atos",           # LLVM版本
                "symbolicatecrash",    # Xcode崩溃符号化工具
                "llvm-addr2line",      # LLVM地址行工具
            ],
            "harmonyos": [
                "llvm-addr2line",      # LLVM版本（HarmonyOS推荐）
                "addr2line",           # 标准工具
                "gdb",                 # GNU调试器
            ]
        }
        
        return tool_priorities.get(os_type, ["addr2line", "llvm-addr2line"])
    
    def _find_resolver_tools(self) -> Dict[str, str]:
        """查找可用的堆栈地址解析工具，支持环境变量加载和跨平台检测
        
        优先级：命令参数 > 配置文件 > 环境变量/常规目录
        """
        available_tools = {}
        tool_priorities = self._get_tool_priority_list()
        
        # 加载环境变量和工具路径（按优先级）
        logger.info("正在加载工具路径配置...")
        self._load_environment_variables()
        
        # 显示当前环境信息
        self._show_environment_info()
        
        # 扩展工具搜索路径（包括配置文件中的路径）
        search_paths = self._get_extended_search_paths()
        
        # 如果配置文件中有平台特定的工具路径，添加到搜索路径
        if self.config and "platforms" in self.config:
            platform_config = self.config["platforms"].get(self.os_type, {})
            if "tool_paths" in platform_config:
                for tool_path in platform_config["tool_paths"]:
                    if Path(tool_path).exists() and tool_path not in search_paths:
                        search_paths.append(tool_path)
            
            # 添加全局工具路径
            if "global" in self.config and "tool_paths" in self.config["global"]:
                for tool_path in self.config["global"]["tool_paths"]:
                    if Path(tool_path).exists() and tool_path not in search_paths:
                        search_paths.append(tool_path)
        
        for tool in tool_priorities:
            try:
                # 检查工具是否可用（包括环境变量中的工具）
                tool_path = self._find_tool_in_paths(tool, search_paths)
                if tool_path:
                    available_tools[tool] = tool_path
                    logger.info(f"找到堆栈地址解析工具: {tool} -> {tool_path}")
            except Exception as e:
                logger.debug(f"工具 {tool} 不可用: {e}")
                continue
        
        # 如果没有找到标准工具，尝试查找平台特定的工具
        if not available_tools:
            platform_tools = self._find_platform_specific_tools()
            available_tools.update(platform_tools)
        
        # 在macOS环境下，强制查找atos工具
        if self.os_type == "macos" and "atos" not in available_tools:
            atos_path = self._find_tool_in_paths("atos", ["/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"])
            if atos_path:
                available_tools["atos"] = atos_path
                logger.info(f"强制添加macOS atos工具: {atos_path}")

        # 兼容回退：llvm-addr2line 实质上是 llvm-symbolizer 的别名（默认参数不同）。
        # 当未直接发现 llvm-addr2line（也无 addr2line）但能找到 llvm-symbolizer 时，
        # 将其注册为 llvm-addr2line 的等价实现，调用时自动切换为 llvm-symbolizer 兼容参数。
        if "llvm-addr2line" not in available_tools and "addr2line" not in available_tools:
            symbolizer_path = self._find_tool_in_paths("llvm-symbolizer", search_paths)
            if symbolizer_path:
                available_tools["llvm-addr2line"] = symbolizer_path
                self._llvm_addr2line_alias_path = symbolizer_path
                logger.info(
                    f"未找到 llvm-addr2line / addr2line，使用 llvm-symbolizer 作为 llvm-addr2line 兼容回退: {symbolizer_path}"
                )

        if not available_tools:
            logger.warning(f"未找到任何堆栈地址解析工具，当前系统: {self.os_type}")
            logger.info("建议安装以下工具之一:")
            for tool in tool_priorities[:3]:  # 显示前3个推荐工具
                logger.info(f"  - {tool}")
        
        return available_tools
    
    def _test_tool_availability(self, tool_name: str, tool_path: Optional[str] = None) -> bool:
        """测试工具是否可用"""
        try:
            # 使用提供的工具路径或工具名称
            executable = tool_path if tool_path else tool_name
            
            # 不同工具的版本检查命令
            version_commands = {
                "addr2line": ["--version"],
                "llvm-addr2line": ["--version"],
                "llvm-symbolizer": ["--version"],
                "eu-addr2line": ["--version"],
                "atos": ["--version"],
                "llvm-atos": ["--version"],
                "gdb": ["--version"],
                "objdump": ["--version"],
                "otool": ["--version"],
                "dumpbin": ["/?"],  # Windows dumpbin使用/?参数
                "ndk-stack": ["--help"],
                "symbolicatecrash": ["--help"],
                # Android特定工具
                "aarch64-linux-android-addr2line": ["--version"],
                "armv7a-linux-androideabi-addr2line": ["--version"],
                "x86_64-linux-android-addr2line": ["--version"],
                "i686-linux-android-addr2line": ["--version"],
            }
            
            cmd = [executable] + version_commands.get(tool_name, ["--version"])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            # 某些工具可能返回非零退出码但仍然可用
            return result.returncode == 0 or len(result.stdout) > 0 or len(result.stderr) > 0
            
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False
    
    def _load_environment_variables(self):
        """加载环境变量，确保PATH中包含所有必要的工具路径
        
        优先级：命令参数 > 配置文件 > 环境变量/常规目录
        """
        try:
            # 优先级1: 如果提供了tool_search_paths配置（命令参数），优先使用
            if self.tool_search_paths:
                logger.info("使用命令参数配置的工具搜索路径")
                self._apply_tool_paths(self.tool_search_paths)
                return  # 如果提供了命令参数，就不从配置文件和环境变量加载了
            
            # 优先级2: 从配置文件加载
            if self.config and "platforms" in self.config:
                platform_config = self.config["platforms"].get(self.os_type, {})
                config_tool_paths = {}
                
                # 读取当前运行平台特定的工具路径
                if "tool_paths" in platform_config and platform_config["tool_paths"]:
                    # 将工具路径添加到PATH
                    for tool_path in platform_config["tool_paths"]:
                        if Path(tool_path).exists():
                            current_path = os.environ.get('PATH', '')
                            if tool_path not in current_path:
                                os.environ['PATH'] = f"{tool_path}:{current_path}"
                                logger.info(f"从配置文件添加工具路径到PATH: {tool_path}")
                
                # 读取当前运行平台特定的环境变量
                if "environment_vars" in platform_config:
                    for var_name, var_value in platform_config["environment_vars"].items():
                        if var_value and Path(var_value).exists():
                            config_tool_paths[var_name] = [var_value]
                
                # 额外读取「所有平台」声明的环境变量（例如在 macOS 上调试 Android 崩溃时，需要 ANDROID_NDK_HOME）
                for os_key, plt_cfg in self.config["platforms"].items():
                    env_cfg = plt_cfg.get("environment_vars") or {}
                    for var_name, var_value in env_cfg.items():
                        if var_value and Path(var_value).exists():
                            if var_name not in config_tool_paths:
                                config_tool_paths[var_name] = [var_value]
                
                # 读取全局配置
                if "global" in self.config:
                    global_config = self.config["global"]
                    if "tool_paths" in global_config and global_config["tool_paths"]:
                        for tool_path in global_config["tool_paths"]:
                            if Path(tool_path).exists():
                                current_path = os.environ.get('PATH', '')
                                if tool_path not in current_path:
                                    os.environ['PATH'] = f"{tool_path}:{current_path}"
                                    logger.info(f"从全局配置添加工具路径到PATH: {tool_path}")
                    
                    if "environment_vars" in global_config:
                        for var_name, var_value in global_config["environment_vars"].items():
                            if var_value and Path(var_value).exists():
                                if var_name not in config_tool_paths:
                                    config_tool_paths[var_name] = [var_value]
                
                # 应用配置文件中的环境变量
                if config_tool_paths:
                    logger.info("使用配置文件中的工具路径")
                    self._apply_tool_paths(config_tool_paths)
                    return  # 如果从配置文件加载了，就不从环境变量加载了
            
            # 优先级3: 从环境变量和配置文件加载（常规方式）
            # 尝试加载常见的配置文件
            config_files = [
                "~/.bash_profile",
                "~/.bashrc", 
                "~/.zshrc",
                "~/.profile",
                "~/.bash_login"
            ]
            
            for config_file in config_files:
                config_path = Path(config_file).expanduser()
                if config_path.exists():
                    try:
                        # 检查文件大小，跳过过大的文件
                        if config_path.stat().st_size > 100000:  # 100KB
                            continue
                        
                        # 读取配置文件并提取PATH设置
                        with open(config_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        # 查找PATH设置，只处理单行设置
                        path_matches = re.findall(r'export\s+PATH=([^;\n]+)', content)
                        for path_match in path_matches:
                            # 清理路径字符串，移除换行符
                            path_str = path_match.strip().strip('"\'')
                            if path_str and '\n' not in path_str:  # 确保没有换行符
                                if path_str not in os.environ.get('PATH', ''):
                                    current_path = os.environ.get('PATH', '')
                                    os.environ['PATH'] = f"{path_str}:{current_path}"
                                    logger.debug(f"从 {config_file} 加载PATH: {path_str}")
                        
                        # 查找特定工具的环境变量
                        tool_vars = {
                            'ANDROID_NDK_HOME': r'export\s+ANDROID_NDK_HOME=([^;\n]+)',
                            'ANDROID_SDK_HOME': r'export\s+ANDROID_SDK_HOME=([^;\n]+)',
                            'LLVM_HOME': r'export\s+LLVM_HOME=([^;\n]+)',
                            'TOOLCHAIN_PATH': r'export\s+TOOLCHAIN_PATH=([^;\n]+)'
                        }
                        
                        for var_name, pattern in tool_vars.items():
                            matches = re.findall(pattern, content)
                            if matches:
                                var_value = matches[0].strip().strip('"\'')
                                if var_value and '\n' not in var_value:  # 确保没有换行符
                                    if var_value not in os.environ.get(var_name, ''):
                                        os.environ[var_name] = var_value
                                        logger.debug(f"从 {config_file} 加载{var_name}: {var_value}")
                                        
                                        # 将工具路径添加到PATH
                                        tool_paths = [
                                            f"{var_value}/bin",
                                            f"{var_value}/tools/bin",
                                            f"{var_value}/toolchains/llvm/prebuilt/linux-x86_64/bin",
                                            f"{var_value}/toolchains/llvm/prebuilt/darwin-x86_64/bin"
                                        ]
                                        
                                        for tool_path in tool_paths:
                                            if Path(tool_path).exists():
                                                current_path = os.environ.get('PATH', '')
                                                if tool_path not in current_path:
                                                    os.environ['PATH'] = f"{tool_path}:{current_path}"
                                                    logger.debug(f"添加工具路径到PATH: {tool_path}")
                                    
                    except Exception as e:
                        logger.debug(f"读取配置文件 {config_file} 失败: {e}")
                        continue
            
            # 尝试加载系统级环境变量
            self._load_system_environment()
            
        except Exception as e:
            logger.debug(f"加载环境变量失败: {e}")
    
    def _apply_tool_paths(self, tool_paths: Dict[str, List[str]]):
        """
        应用工具路径配置到环境变量
        
        Args:
            tool_paths: 工具路径配置字典
        """
        for var_name, paths in tool_paths.items():
            if isinstance(paths, list):
                for path in paths:
                    if Path(path).exists():
                        # 设置环境变量
                        if var_name != "PATH":
                            os.environ[var_name] = path
                            logger.debug(f"设置环境变量 {var_name}={path}")
                        
                        # 将路径添加到PATH
                        tool_path_list = self._get_tool_paths_from_env_var(var_name, path)
                        for tool_path in tool_path_list:
                            if Path(tool_path).exists():
                                current_path = os.environ.get('PATH', '')
                                if tool_path not in current_path:
                                    os.environ['PATH'] = f"{tool_path}:{current_path}"
                                    logger.debug(f"添加工具路径到PATH: {tool_path}")
            elif isinstance(paths, str):
                # 单个路径
                if Path(paths).exists():
                    if var_name != "PATH":
                        os.environ[var_name] = paths
                        logger.debug(f"设置环境变量 {var_name}={paths}")
                    
                    tool_path_list = self._get_tool_paths_from_env_var(var_name, paths)
                    for tool_path in tool_path_list:
                        if Path(tool_path).exists():
                            current_path = os.environ.get('PATH', '')
                            if tool_path not in current_path:
                                os.environ['PATH'] = f"{tool_path}:{current_path}"
                                logger.debug(f"添加工具路径到PATH: {tool_path}")
    
    def _get_tool_paths_from_env_var(self, var_name: str, base_path: str) -> List[str]:
        """
        根据环境变量名称和基础路径，生成可能的工具路径列表
        
        Args:
            var_name: 环境变量名称
            base_path: 基础路径
            
        Returns:
            工具路径列表
        """
        tool_paths = []
        
        if var_name == "ANDROID_NDK_HOME":
            tool_paths = [
                f"{base_path}/toolchains/llvm/prebuilt/linux-x86_64/bin",
                f"{base_path}/toolchains/llvm/prebuilt/darwin-x86_64/bin",
                f"{base_path}/toolchains/llvm/prebuilt/windows-x86_64/bin",
                f"{base_path}/toolchains/aarch64-linux-android-4.9/prebuilt/linux-x86_64/bin",
                f"{base_path}/toolchains/aarch64-linux-android-4.9/prebuilt/darwin-x86_64/bin",
                f"{base_path}/toolchains/aarch64-linux-android-4.9/prebuilt/windows-x86_64/bin",
            ]
        elif var_name == "ANDROID_SDK_HOME":
            tool_paths = [
                f"{base_path}/platform-tools",
                f"{base_path}/tools",
                f"{base_path}/tools/bin",
            ]
        elif var_name == "LLVM_HOME":
            tool_paths = [
                f"{base_path}/bin",
                f"{base_path}/tools/bin",
            ]
        elif var_name == "TOOLCHAIN_PATH":
            tool_paths = [
                f"{base_path}/bin",
                f"{base_path}/usr/bin",
            ]
        elif var_name == "PATH":
            # PATH本身就是路径列表，直接返回
            tool_paths = [base_path]
        else:
            # 默认尝试bin目录
            tool_paths = [
                f"{base_path}/bin",
                base_path,
            ]
        
        return tool_paths
    
    def _load_system_environment(self):
        """加载系统级环境变量"""
        try:
            # 尝试执行source命令加载环境变量
            if self.os_type == "linux" or self.os_type == "macos":
                # 获取当前shell
                shell = os.environ.get('SHELL', '/bin/bash')
                if 'zsh' in shell:
                    shell = '/bin/zsh'
                elif 'bash' in shell:
                    shell = '/bin/bash'
                
                # 尝试加载环境变量
                try:
                    # 使用subprocess加载环境变量
                    env_cmd = f"source ~/.{shell.split('/')[-1]}rc && env"
                    result = subprocess.run([shell, '-c', env_cmd], 
                                         capture_output=True, text=True, timeout=10)
                    
                    if result.returncode == 0:
                        # 解析环境变量输出
                        for line in result.stdout.split('\n'):
                            if '=' in line and '\n' not in line:  # 确保没有换行符
                                key, value = line.split('=', 1)
                                if key in ['PATH', 'ANDROID_NDK_HOME', 'ANDROID_SDK_HOME', 'LLVM_HOME']:
                                    # 清理值，移除换行符
                                    clean_value = value.replace('\n', '').strip()
                                    if clean_value:
                                        os.environ[key] = clean_value
                                        logger.debug(f"加载环境变量: {key}={clean_value}")
                except Exception as e:
                    logger.debug(f"执行shell命令加载环境变量失败: {e}")
                    
        except Exception as e:
            logger.debug(f"加载系统环境变量失败: {e}")
    
    def _get_extended_search_paths(self) -> List[str]:
        """获取扩展的搜索路径，包括环境变量中的工具路径"""
        search_paths = []
        
        # 添加标准PATH
        if 'PATH' in os.environ:
            # 清理PATH中的换行符和无效字符
            path_entries = os.environ['PATH'].split(':')
            for path_entry in path_entries:
                if path_entry and '\n' not in path_entry and len(path_entry) < 500:  # 跳过包含换行符或过长的路径
                    search_paths.append(path_entry)
        
        # 添加Android NDK路径
        if 'ANDROID_NDK_HOME' in os.environ:
            ndk_home = os.environ['ANDROID_NDK_HOME']
            if '\n' not in ndk_home and len(ndk_home) < 500:  # 确保路径有效
                ndk_paths = [
                    f"{ndk_home}/toolchains/llvm/prebuilt/linux-x86_64/bin",
                    f"{ndk_home}/toolchains/llvm/prebuilt/darwin-x86_64/bin",
                    f"{ndk_home}/toolchains/llvm/prebuilt/windows-x86_64/bin",
                    f"{ndk_home}/toolchains/aarch64-linux-android-4.9/prebuilt/linux-x86_64/bin",
                    f"{ndk_home}/toolchains/aarch64-linux-android-4.9/prebuilt/darwin-x86_64/bin",
                    f"{ndk_home}/toolchains/aarch64-linux-android-4.9/prebuilt/windows-x86_64/bin"
                ]
                search_paths.extend([p for p in ndk_paths if Path(p).exists()])
        
        # 添加Android SDK路径
        if 'ANDROID_SDK_HOME' in os.environ:
            sdk_home = os.environ['ANDROID_SDK_HOME']
            if '\n' not in sdk_home and len(sdk_home) < 500:  # 确保路径有效
                sdk_paths = [
                    f"{sdk_home}/platform-tools",
                    f"{sdk_home}/tools",
                    f"{sdk_home}/tools/bin"
                ]
                search_paths.extend([p for p in sdk_paths if Path(p).exists()])
        
        # 添加LLVM路径
        if 'LLVM_HOME' in os.environ:
            llvm_home = os.environ['LLVM_HOME']
            if '\n' not in llvm_home and len(llvm_home) < 500:  # 确保路径有效
                llvm_paths = [
                    f"{llvm_home}/bin",
                    f"{llvm_home}/tools/bin"
                ]
                search_paths.extend([p for p in llvm_paths if Path(p).exists()])
        
        # 添加常见工具链路径
        common_toolchain_paths = [
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/local/opt/llvm/bin",  # macOS Homebrew LLVM
            "/opt/homebrew/bin",        # macOS Apple Silicon Homebrew
            "/usr/local/opt/binutils/bin",  # macOS Homebrew binutils
            "/opt/homebrew/opt/binutils/bin",  # macOS Apple Silicon Homebrew binutils
            "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin",  # Xcode工具链
            "/Applications/Xcode.app/Contents/Developer/usr/bin"  # Xcode开发者工具
        ]
        search_paths.extend([p for p in common_toolchain_paths if Path(p).exists()])

        # 同步 IDE / SDK 默认安装路径（与 CLI 检测层保持一致）：
        #   Android Studio NDK / Xcode (xcrun) / DevEco Studio OpenHarmony SDK / Homebrew 多版本 / Linux LLVM 发行版包
        # 采用懒导入避免与 cli.main 的循环依赖；闭源 / 打包场景缺失 cli 包时安全降级。
        try:
            from cli.main import _candidate_tool_dirs_from_ides as _cli_ide_paths  # type: ignore
            for ide_path, _ide_labels in _cli_ide_paths():
                p = str(ide_path)
                if p and p not in search_paths and len(p) < 500:
                    search_paths.append(p)
        except Exception as exc:  # pragma: no cover - 仅做防御性降级
            logger.debug(f"未加载 CLI 端 IDE 路径探测器: {exc}")

        # 去重并过滤无效路径
        valid_paths = []
        seen_paths = set()
        for path in search_paths:
            if path and Path(path).exists() and path not in seen_paths and len(path) < 500:
                valid_paths.append(path)
                seen_paths.add(path)
        
        logger.debug(f"扩展搜索路径: {valid_paths}")
        return valid_paths
    
    def _find_tool_in_paths(self, tool_name: str, search_paths: List[str]) -> Optional[str]:
        """在指定路径中查找工具"""
        # 首先尝试使用which命令
        try:
            result = subprocess.run(["which", tool_name], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                tool_path = result.stdout.strip()
                if self._test_tool_availability(tool_name, tool_path):
                    return tool_path
        except Exception:
            pass
        
        # 在扩展路径中查找
        for search_path in search_paths:
            tool_path = Path(search_path) / tool_name
            if tool_path.exists() and tool_path.is_file():
                if self._test_tool_availability(tool_name, str(tool_path)):
                    return str(tool_path)
        
        # 尝试查找平台特定的工具名称
        platform_specific_names = self._get_platform_specific_tool_names(tool_name)
        for specific_name in platform_specific_names:
            for search_path in search_paths:
                tool_path = Path(search_path) / specific_name
                if tool_path.exists() and tool_path.is_file():
                    if self._test_tool_availability(tool_name, str(tool_path)):
                        return str(tool_path)
        
        return None
    
    def _get_platform_specific_tool_names(self, base_tool_name: str) -> List[str]:
        """获取平台特定的工具名称"""
        if base_tool_name == "addr2line":
            if self.os_type == "android":
                return [
                    "aarch64-linux-android-addr2line",
                    "armv7a-linux-androideabi-addr2line",
                    "x86_64-linux-android-addr2line",
                    "i686-linux-android-addr2line",
                    "llvm-addr2line"
                ]
            elif self.os_type == "ios":
                return [
                    "llvm-addr2line",
                    "clang-addr2line"
                ]
            elif self.os_type == "linux":
                return [
                    "llvm-addr2line",
                    "eu-addr2line",
                    "gcc-addr2line"
                ]
        elif base_tool_name == "atos":
            if self.os_type == "macos":
                return [
                    "llvm-atos",
                    "clang-atos"
                ]
            elif self.os_type == "ios":
                return [
                    "llvm-atos",
                    "clang-atos"
                ]
        
        return []
    
    def _find_platform_specific_tools(self) -> Dict[str, str]:
        """查找平台特定的工具"""
        platform_tools = {}
        
        # 根据操作系统查找特定工具
        if self.os_type == "android":
            android_tools = [
                "aarch64-linux-android-addr2line",
                "armv7a-linux-androideabi-addr2line",
                "x86_64-linux-android-addr2line",
                "i686-linux-android-addr2line",
                "llvm-addr2line",
                "ndk-stack"
            ]
            
            for tool in android_tools:
                tool_path = self._find_tool_in_paths(tool, self._get_extended_search_paths())
                if tool_path:
                    platform_tools[tool] = tool_path
                    logger.info(f"找到Android特定工具: {tool} -> {tool_path}")
        
        elif self.os_type == "ios":
            ios_tools = [
                "llvm-addr2line",
                "llvm-atos",
                "symbolicatecrash"
            ]
            
            for tool in ios_tools:
                tool_path = self._find_tool_in_paths(tool, self._get_extended_search_paths())
                if tool_path:
                    platform_tools[tool] = tool_path
                    logger.info(f"找到iOS特定工具: {tool} -> {tool_path}")
        
        return platform_tools
    
    def _show_environment_info(self):
        """显示当前环境信息，帮助调试"""
        logger.info(f"当前操作系统: {self.os_type}")
        
        # 显示PATH信息
        if 'PATH' in os.environ:
            path_dirs = os.environ['PATH'].split(':')
            logger.info(f"PATH包含 {len(path_dirs)} 个目录")
            
            # 显示包含关键字的路径
            key_paths = [p for p in path_dirs if any(keyword in p.lower() for keyword in 
                       ['android', 'ndk', 'sdk', 'llvm', 'toolchain', 'bin'])]
            if key_paths:
                logger.info("发现关键工具路径:")
                for path in key_paths[:5]:  # 只显示前5个
                    logger.info(f"  - {path}")
        
        # 显示Android相关环境变量
        android_vars = ['ANDROID_NDK_HOME', 'ANDROID_SDK_HOME', 'ANDROID_HOME']
        for var in android_vars:
            if var in os.environ:
                logger.info(f"{var}: {os.environ[var]}")
        
        # 显示LLVM相关环境变量
        llvm_vars = ['LLVM_HOME', 'TOOLCHAIN_PATH']
        for var in llvm_vars:
            if var in os.environ:
                logger.info(f"{var}: {os.environ[var]}")
    
    def _get_tool_path(self, tool_name: str) -> str:
        """获取工具的完整路径（保持向后兼容）"""
        # 这个方法现在主要用于向后兼容
        # 新的实现使用 _find_tool_in_paths
        tool_path = self._find_tool_in_paths(tool_name, self._get_extended_search_paths())
        return tool_path if tool_path else tool_name
    
    def _select_primary_tool(self) -> Optional[str]:
        """选择主要的解析工具"""
        if not self.resolver_tools:
            return None
        
        # 根据操作系统选择最佳工具
        if self.os_type == "macos":
            # macOS优先使用atos
            if "atos" in self.resolver_tools:
                return "atos"
            elif "llvm-atos" in self.resolver_tools:
                return "llvm-atos"
            elif "llvm-addr2line" in self.resolver_tools:
                return "llvm-addr2line"
            elif "addr2line" in self.resolver_tools:
                return "addr2line"
        elif self.os_type == "linux" and "addr2line" in self.resolver_tools:
            return "addr2line"
        elif self.os_type == "windows" and "llvm-addr2line" in self.resolver_tools:
            return "llvm-addr2line"
        else:
            # 返回第一个可用的工具
            return list(self.resolver_tools.keys())[0]
        
        return None
    
    def _resolve_address_with_tool(self, address: str, library_path: str, tool_name: str) -> Optional[ResolvedFrame]:
        """使用指定工具解析地址"""
        if tool_name not in self.resolver_tools:
            return None
        
        tool_path = self.resolver_tools[tool_name]
        
        try:
            if tool_name == "atos":
                return self._resolve_with_atos(address, library_path, tool_path)
            elif tool_name == "llvm-atos":
                return self._resolve_with_atos(address, library_path, tool_path)
            elif tool_name == "symbolicatecrash":
                return self._resolve_with_symbolicatecrash(address, library_path, tool_path)
            elif tool_name == "gdb":
                return self._resolve_with_gdb(address, library_path, tool_path)
            elif tool_name == "objdump":
                return self._resolve_with_objdump(address, library_path, tool_path)
            elif tool_name == "otool":
                return self._resolve_with_otool(address, library_path, tool_path)
            elif tool_name == "dumpbin":
                return self._resolve_with_dumpbin(address, library_path, tool_path)
            else:
                # 默认使用addr2line格式
                return self._resolve_with_addr2line(address, library_path, tool_path)
                
        except Exception as e:
            logger.warning(f"使用工具 {tool_name} 解析地址 {address} 时出错: {e}")
            return None
    
    def _parse_add2line_output(self, output: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        """解析add2line输出"""
        lines = output.strip().split('\n')
        if len(lines) < 2:
            return None, None, None
        
        function_line = lines[0].strip()
        file_line = lines[1].strip()

        def _is_unknown_marker(text: str) -> bool:
            """判断addr2line输出是否为未知占位（如 ??, ???, ??:0, ???:0 等）"""
            t = (text or "").strip()
            if not t:
                return True
            # 纯问号：??, ??? 等
            if re.fullmatch(r"\?+", t):
                return True
            # 问号 + :0：??:0, ???:0 等
            if re.fullmatch(r"\?+:0", t):
                return True
            return False

        # 解析函数名
        function = None
        if not _is_unknown_marker(function_line):
            function = function_line
        
        # 解析文件和行号
        file_path = None
        line_number = None

        if not _is_unknown_marker(file_line):
            # 格式: /path/to/file:123
            match = re.match(r'(.+):(\d+)$', file_line)
            if match:
                file_path = match.group(1)
                try:
                    line_number = int(match.group(2))
                except ValueError:
                    pass
        
        return function, file_path, line_number
    
    def _resolve_address(self, address: str, library_path: str) -> Optional[ResolvedFrame]:
        """解析单个地址"""
        if not self.primary_tool:
            logger.error("没有可用的堆栈地址解析工具")
            return None
        
        return self._resolve_address_with_tool(address, library_path, self.primary_tool)

    def _ios_extract_source_from_function(self, func: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
        if not func or not isinstance(func, str):
            return None, None
        m = _IOS_SOURCE_FILE_LINE_RE.search(func)
        if not m:
            return None, None
        try:
            return m.group(1).strip(), int(m.group(2))
        except (ValueError, IndexError):
            return None, None

    @staticmethod
    def _coerce_frame_line(val: Any) -> Optional[int]:
        if val is None:
            return None
        if isinstance(val, int):
            return val
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    def _json_ios_log_symbolicated(
        self,
        stack_frames: List[Dict[str, Any]],
        meta_info: Dict[str, Any],
        library_dir_display: str,
    ) -> str:
        """无库符号时，从已符号化日志回填与 02 同构结果（iOS 额外做噪音裁剪）。"""
        resolved_frames: List[ResolvedFrame] = []
        errors: List[str] = []
        success_count = 0
        filtered_count = 0
        os_type = (meta_info.get("os_type") or "unknown").lower()

        for frame in stack_frames:
            address = frame.get("address") or ""
            function = frame.get("function")
            raw_file = frame.get("file")
            raw_line = Add2lineResolver._coerce_frame_line(frame.get("line"))
            ext_file, ext_line = self._ios_extract_source_from_function(
                function if isinstance(function, str) else None
            )
            resolved_file = self._normalize_resolved_file_name(raw_file) if raw_file else None
            resolved_line = raw_line
            if resolved_file is None and ext_file:
                resolved_file = self._normalize_resolved_file_name(ext_file)
            if resolved_line is None and ext_line is not None:
                resolved_line = ext_line

            demangled = None
            if function:
                demangled = self._demangle_cpp_symbol(function)
            semantic_fn = self._normalize_passthrough_function_symbol(
                demangled if demangled else function
            )

            rf = ResolvedFrame(
                address=address if isinstance(address, str) else str(address),
                function=function,
                file=raw_file if isinstance(raw_file, str) else None,
                line=raw_line,
                raw_log_line=Add2lineResolver._coerce_frame_line(frame.get("raw_log_line")),
                module=frame.get("module"),
                resolved_function=semantic_fn,
                resolved_file=resolved_file,
                resolved_line=resolved_line,
            )
            # 过滤无效噪音帧（避免污染 03）：
            # - 完全无函数/无文件行的空帧；
            # - 无源码定位信息的系统库帧（module 为 UIKit/CoreFoundation/libdispatch 等）。
            has_symbol = bool((rf.resolved_function or "").strip() or (rf.function or "").strip())
            has_source = bool((rf.resolved_file or "").strip()) or (rf.resolved_line is not None)
            module_str = rf.module if isinstance(rf.module, str) else None
            should_filter_noise = (not has_symbol and not has_source)
            # iOS 场景下再额外裁剪「无源码定位 + 无符号」的系统库空帧
            if os_type == "ios":
                should_filter_noise = should_filter_noise or (
                    _is_probably_system_module(module_str) and not has_source and not has_symbol
                )
            if should_filter_noise:
                filtered_count += 1
                continue

            resolved_frames.append(rf)
            if has_symbol or has_source:
                success_count += 1

        result = Add2lineResult(
            resolved_frames=resolved_frames,
            os_type=meta_info.get("os_type", "unknown"),
            library_path=library_dir_display or "",
            success_count=success_count,
            total_count=len(stack_frames),
            errors=errors,
            tool_name="log_symbolicated_passthrough",
            tool_path=None,
            tools_available=self.resolver_tools or None,
            resolution_source="log_symbolicated_passthrough",
        )
        if filtered_count > 0:
            logger.info("log_symbolicated_passthrough 过滤噪音帧: %s", filtered_count)
        return json.dumps(asdict(result), ensure_ascii=False, indent=2)

    @staticmethod
    def _normalize_passthrough_function_symbol(function_name: Optional[str]) -> Optional[str]:
        """
        无库目录直通场景下，对部分 C++ 包装符号做轻量语义提取，提升可读性。
        当前覆盖：std::__function::__func<Owner::foo()::$_0, ...>::operator()()
        """
        s = (function_name or "").strip()
        if not s:
            return function_name
        m = re.search(
            r"std::__\d*::__function::__func<\s*([^,>]+)\s*,\s*std::__\d*::allocator<[^>]+>\s*,\s*void\s*\(\s*\)\s*>\s*::operator\(\)\(\)",
            s,
        )
        if not m:
            m = re.search(
                r"std::__\d*::__function::__func<\s*([^,>]+)\s*,\s*std::__\d*::allocator<[^>]+>\s*,\s*[^>]*>\s*::operator\(\)\(\)",
                s,
            )
        if not m:
            return function_name
        inner = m.group(1).strip()
        if not inner:
            return function_name
        inner = re.sub(r"::\$_\d+\b", "::<lambda>", inner)
        # 若是外层函数中的 lambda 包装帧，优先回落到外层函数本身，便于在源码中定位。
        if "::<lambda>" in inner:
            outer = inner.split("::<lambda>", 1)[0].strip()
            if outer:
                return outer
        return f"{inner}::operator()"

    def resolve_stack_trace(
        self, crash_json: str, library_dir: Optional[str] = None, max_frames: Optional[int] = None
    ) -> str:
        """
        解析堆栈跟踪
        
        Args:
            crash_json (str): 崩溃日志解析结果的JSON字符串
            library_dir (Optional[str]): 库文件目录或库文件路径；可空。空且为已符号化 iOS 堆栈时从日志回填。
            max_frames (Optional[int]): 最大处理的堆栈帧数量，None 表示不限制
            
        Returns:
            str: JSON格式的解析结果
        """
        try:
            logger.info("开始解析堆栈跟踪...")
            self._emit_progress(f"🔍 [add2line_resolver] 开始解析堆栈跟踪...")

            # 解析输入JSON
            crash_data = json.loads(crash_json)
            
            # 检查必要字段（支持新旧两种结构）：
            # - 新结构：threads[*].frames
            # - 旧结构：stack_frames
            if "meta_info" not in crash_data:
                raise ValueError("JSON中缺少meta_info字段")
            meta_info = crash_data["meta_info"]

            if "stack_frames" in crash_data:
                stack_frames = crash_data["stack_frames"]
            elif "threads" in crash_data:
                flat_frames = []
                threads = crash_data.get("threads") or []
                for t in threads:
                    for f in t.get("frames", []):
                        flat_frames.append(f)
                stack_frames = flat_frames
                crash_data["stack_frames"] = stack_frames
            else:
                raise ValueError("JSON中缺少 stack_frames 或 threads 字段")

            # 限制堆栈帧数量
            original_count = len(stack_frames)
            if max_frames is not None and max_frames > 0:
                stack_frames = stack_frames[:max_frames]
                self._emit_progress(f"📊 [add2line_resolver] 原始堆栈帧数量: {original_count}，限制后: {len(stack_frames)} (--parse-lines={max_frames})")
                logger.info(f"限制堆栈帧数量: {original_count} -> {len(stack_frames)}")
            else:
                self._emit_progress(f"📊 [add2line_resolver] 堆栈帧数量: {len(stack_frames)}")
            
            # 更新 crash_data 中的 stack_frames（用于后续处理）
            crash_data["stack_frames"] = stack_frames

            lib_arg = library_dir if library_dir is not None else ""
            lib_norm = lib_arg.strip()
            self._emit_progress(f"📁 [add2line_resolver] 库路径: {lib_norm or '(未指定)'}")
            crash_os_type = meta_info.get("os_type", "unknown")
            lib_path_ok = bool(lib_norm) and Path(lib_norm).exists()

            if not lib_path_ok:
                looks_symbolicated = stack_frames_look_symbolicated_generic(stack_frames)
                if crash_os_type == "ios":
                    looks_symbolicated = looks_symbolicated or ios_stack_frames_look_symbolicated(stack_frames)
                if looks_symbolicated:
                    self._emit_progress(
                        "📋 [add2line_resolver] 无有效库目录：检测到已符号化堆栈，从日志回填解析结果"
                    )
                    return self._json_ios_log_symbolicated(stack_frames, meta_info, lib_norm or lib_arg)
                return json.dumps(
                    {
                        "error": "库路径不存在或未提供",
                        "library_dir": lib_norm or None,
                        "os_type": crash_os_type,
                        "suggestion": (
                            "请提供有效的 --library-dir；若日志已符号化（含函数名或 file:line），"
                            "可省略库目录并由本工具从日志生成 02。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            library_dir = lib_norm
            library_path = Path(library_dir)
            
            # 检查库路径下的文件
            try:
                lib_file_list = os.listdir(library_dir)
                preview = lib_file_list[:20]
                suffix = " ..." if len(lib_file_list) > 20 else ""
                self._emit_progress(
                    f"📚 [add2line_resolver] 库路径文件总数: {len(lib_file_list)}，示例: {preview}{suffix}"
                )
                lib_files_filtered = [f for f in lib_file_list if f.endswith(('.dylib', '.so', '.dll', '.a')) or '.dSYM' in f]
                self._emit_progress(f"📚 [add2line_resolver] 库文件数量: {len(lib_files_filtered)}")
            except Exception as e:
                self._emit_progress(f"❌ [add2line_resolver] 无法读取库路径: {e}")
            
            # 如果提供的是文件路径，直接使用；如果是目录，查找库文件
            if library_path.is_file():
                library_files = [library_path]
                logger.info(f"使用指定的库文件: {library_dir}")
            elif library_path.is_dir():
                library_files = find_library_files_in_dir(
                    library_dir, meta_info.get("os_type", "unknown"),
                )
                if not library_files:
                    logger.warning("未找到库文件")
                    return json.dumps({
                        "error": "未找到库文件",
                        "library_dir": library_dir,
                        "os_type": meta_info.get('os_type', 'unknown')
                    }, ensure_ascii=False, indent=2)
                logger.info(f"在目录中找到 {len(library_files)} 个库文件")
            else:
                raise ValueError(f"指定的路径既不是文件也不是目录: {library_dir}")
            
            logger.info(f"库目录: {library_dir}")
            crash_os_type = meta_info.get('os_type', 'unknown')
            logger.info(f"检测到操作系统: {crash_os_type}")
            
            # 根据崩溃日志的OS类型选择工具（支持所有平台）
            selected_tool = None
            if crash_os_type in ['harmonyos', 'android', 'ios', 'linux', 'windows', 'macos']:
                # 获取该平台的工具优先级列表
                crash_tool_priorities = self._get_tool_priority_list(crash_os_type)
                # 从可用工具中选择优先级最高的
                for tool in crash_tool_priorities:
                    if tool in self.resolver_tools:
                        selected_tool = tool
                        logger.info(f"检测到{crash_os_type}崩溃日志，使用{selected_tool}工具: {self.resolver_tools[selected_tool]}")
                        break
            
            # 如果没找到对应平台的工具，尝试根据配置为 Android 等平台显式构造 llvm-addr2line 路径
            if not selected_tool:
                if crash_os_type == "android" and self.config and "platforms" in self.config:
                    android_cfg = self.config["platforms"].get("android", {})
                    android_tool_paths = android_cfg.get("tool_paths") or []
                    for tool_dir in android_tool_paths:
                        try:
                            candidate = Path(tool_dir) / "llvm-addr2line"
                            if candidate.exists():
                                selected_tool = "llvm-addr2line"
                                # 确保写入 resolver_tools，后续可以复用
                                self.resolver_tools[selected_tool] = str(candidate)
                                logger.info(f"为 Android 崩溃显式使用 llvm-addr2line: {candidate}")
                                break
                        except Exception:
                            continue
            
            # 如果仍然没找到对应平台的工具，回退到默认工具
            if not selected_tool:
                selected_tool = self.primary_tool
                if selected_tool:
                    logger.warning(f"未找到{crash_os_type}平台专用工具，使用默认工具: {selected_tool}")
                else:
                    logger.error(f"未找到任何可用的堆栈地址解析工具")
            
            # 检查是否有add2line工具
            if not selected_tool and not self.resolver_tools:
                logger.error("没有可用的堆栈地址解析工具")
                return json.dumps({
                    "error": "没有可用的堆栈地址解析工具（addr2line、atos等）",
                    "library_dir": library_dir,
                    "os_type": crash_os_type,
                    "suggestion": "请确保系统安装了addr2line（Linux）或atos（macOS）工具，对于HarmonyOS请安装llvm-addr2line"
                }, ensure_ascii=False, indent=2)
            
            # 解析每个堆栈帧（并行执行，按输入顺序汇总结果）
            resolved_frames = []
            errors = []
            success_count = 0
            filtered_count = 0  # 统计被过滤的帧数
            module_base_addresses = meta_info.get('module_base_addresses', {})

            def _resolve_one_frame(item: Tuple[int, Dict[str, Any]]) -> Dict[str, Any]:
                i, frame = item
                address = frame.get('address')
                function = frame.get('function')
                offset = frame.get('offset')
                module = frame.get('module')
                module_str = module if isinstance(module, str) else ""
                self._emit_progress(f"🔍 [add2line_resolver] 解析堆栈帧 {i+1}/{len(stack_frames)}: {address} ({module})")

                if not address:
                    self._emit_progress("⚠️ [add2line_resolver] 跳过无地址的堆栈帧")
                    return {"skip": True, "filtered": False, "resolved": None, "error": None}

                # 只解析 library_dir 下存在的库：若当前帧的模块在 library_files 中找不到对应库，则直接过滤
                target_library_files = match_libraries_for_module(
                    module_str if module_str else None, library_files,
                )
                if not target_library_files:
                    self._emit_progress(f"⊘ [add2line_resolver] 跳过非 library_dir 库的堆栈帧: module={module_str or 'unknown'}")
                    return {"skip": False, "filtered": True, "resolved": None, "error": None}

                # 策略1: macOS 精确 atos（quick_mode 下跳过）
                if (not self.quick_mode) and crash_os_type == 'macos' and module_str and not (
                    module_str.startswith('libsystem_') or module_str.startswith('libc.') or module_str.startswith('libobjc.')
                ):
                    dylib_path = f'{library_dir}/{module_str}'
                    dsym_path = f'{library_dir}/{module_str}.dSYM/Contents/Resources/DWARF/{module_str}'
                    if os.path.exists(dylib_path) or os.path.exists(dsym_path):
                        atos_result = self._resolve_with_atos_precise(address, function, module_str, module_base_addresses, library_dir)
                        if atos_result:
                            resolved_line = atos_result.get('line', None)
                            resolved = ResolvedFrame(
                                address=address,
                                function=function,
                                module=module,
                                resolved_function=atos_result.get('function', function),
                                resolved_file=self._normalize_resolved_file_name(atos_result.get('file')),
                                resolved_line=resolved_line
                            )
                            return {"skip": False, "filtered": False, "resolved": resolved, "error": None}

                # 策略2: 偏移量估算
                if function and offset:
                    demangled_function = self._demangle_cpp_symbol(function)
                    file_name = self._normalize_resolved_file_name(
                        self._infer_file_name_from_symbol(function)
                    )
                    line_number = self._calculate_precise_line_number(address, function, offset, module, module_base_addresses)
                    resolved = ResolvedFrame(
                        address=address,
                        function=function,
                        module=module,
                        resolved_function=demangled_function,
                        resolved_file=file_name,
                        resolved_line=line_number
                    )
                    return {"skip": False, "filtered": False, "resolved": resolved, "error": None}

                # 策略3: 工具解析（addr2line/atos 等）
                address_to_use = address
                if offset and module:
                    try:
                        if isinstance(offset, str) and (offset.startswith('0x') or offset.startswith('0X')):
                            address_to_use = offset
                        elif isinstance(offset, str):
                            offset_int = int(offset, 16) if all(c in '0123456789abcdefABCDEF' for c in offset) else int(offset)
                            address_to_use = f"0x{offset_int:x}"
                    except (ValueError, AttributeError, TypeError):
                        address_to_use = address

                resolved = None
                for lib_file in target_library_files:
                    if selected_tool:
                        tool_path = self.resolver_tools.get(selected_tool) if isinstance(selected_tool, str) else selected_tool
                        if tool_path:
                            resolved = self._resolve_address_with_tool(address_to_use, str(lib_file), selected_tool)
                            if resolved and (resolved.resolved_function or resolved.resolved_file):
                                break
                    if not resolved:
                        for tool_name in self.resolver_tools:
                            if tool_name != selected_tool:
                                resolved = self._resolve_address_with_tool(address_to_use, str(lib_file), tool_name)
                                if resolved and (resolved.resolved_function or resolved.resolved_file):
                                    break
                    if resolved and (resolved.resolved_function or resolved.resolved_file):
                        break

                if resolved:
                    resolved.address = address
                    resolved.function = frame.get('function')
                    resolved.file = frame.get('file')
                    resolved.line = frame.get('line')
                    resolved.raw_log_line = Add2lineResolver._coerce_frame_line(frame.get('raw_log_line'))
                    resolved.module = frame.get('module')
                    return {"skip": False, "filtered": False, "resolved": resolved, "error": None}

                unresolved = ResolvedFrame(
                    address=address,
                    function=frame.get('function'),
                    file=frame.get('file'),
                    line=frame.get('line'),
                    raw_log_line=Add2lineResolver._coerce_frame_line(frame.get('raw_log_line')),
                    module=frame.get('module')
                )
                return {"skip": False, "filtered": False, "resolved": unresolved, "error": f"无法解析地址: {address}"}

            workers = min(max(1, (os.cpu_count() or 4) * 2), max(1, len(stack_frames)), 16)
            serial_threshold = 12
            env_workers = os.environ.get("MAP_SDK_CRASH_AGENT_ADD2LINE_THREADS")
            if env_workers:
                try:
                    workers = max(1, int(env_workers))
                except Exception:
                    pass

            frame_items = list(enumerate(stack_frames))
            # 小样本自动降级单线程，避免线程调度开销反超。
            # 若用户显式设置 MAP_SDK_CRASH_AGENT_ADD2LINE_THREADS，则尊重用户设置。
            if not env_workers and len(frame_items) <= serial_threshold:
                workers = 1
            if workers <= 1 or len(frame_items) <= 1:
                frame_results = [_resolve_one_frame(item) for item in frame_items]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    frame_results = list(executor.map(_resolve_one_frame, frame_items))

            for res in frame_results:
                if res.get("skip"):
                    continue
                if res.get("filtered"):
                    filtered_count += 1
                    continue
                resolved = res.get("resolved")
                if resolved:
                    resolved_frames.append(resolved)
                    if not res.get("error"):
                        success_count += 1
                if res.get("error"):
                    errors.append(str(res["error"]))
            
            # 构建结果
            # 记录最终选定的解析工具及其路径，方便后续问题回溯与环境对比
            selected_tool_name: Optional[str] = None
            selected_tool_path: Optional[str] = None
            if isinstance(selected_tool, str):
                selected_tool_name = selected_tool
                selected_tool_path = self.resolver_tools.get(selected_tool)
            elif selected_tool is not None:
                # 兼容未来 selected_tool 可能为 Path/可调用对象的情况
                try:
                    selected_tool_name = str(selected_tool)
                    selected_tool_path = self.resolver_tools.get(str(selected_tool))
                except Exception:
                    selected_tool_name = str(selected_tool)
                    selected_tool_path = None

            result = Add2lineResult(
                resolved_frames=resolved_frames,
                os_type=meta_info.get('os_type', 'unknown'),
                library_path=library_dir,
                success_count=success_count,
                total_count=len(stack_frames),
                errors=errors,
                tool_name=selected_tool_name,
                tool_path=selected_tool_path,
                tools_available=self.resolver_tools or None
            )
            
            logger.info(f"解析完成: {success_count}/{len(stack_frames)} 个地址成功解析（其中跳过 {filtered_count} 个不在 library_dir 中的库帧）")
            
            return json.dumps(asdict(result), ensure_ascii=False, indent=2)
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析错误: {e}")
            return json.dumps({
                "error": f"JSON解析错误: {e}",
                "input": crash_json
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"解析堆栈跟踪时出错: {e}")
            return json.dumps({
                "error": str(e),
                "input": crash_json,
                "library_dir": library_dir
            }, ensure_ascii=False, indent=2)
    
    
    def _build_library_whitelist(self) -> set:
        """
        构建库白名单：扫描 library_dir 下的所有 .so/.a/.dylib 等文件
        返回库文件名集合 {1.so, 2.so, ...}
        
        Returns:
            set: 库文件名集合，仅包含文件基名（如 "libcrash.so"）
        """
        whitelist = set()
        if not self.library_dir:
            return whitelist
        
        lib_path = Path(self.library_dir)
        if not lib_path.exists():
            logger.warning(f"库目录不存在: {self.library_dir}")
            return whitelist
        
        lib_extensions = {'.so', '.a', '.dylib', '.dll', '.lib'}
        try:
            for lib_file in lib_path.rglob('*'):
                if lib_file.is_file() and lib_file.suffix in lib_extensions:
                    # 仅保存文件名（支持完整路径查找时使用）
                    whitelist.add(lib_file.name)
            
            if whitelist:
                logger.info(f"库白名单已构建，包含 {len(whitelist)} 个库: {whitelist}")
                self._emit_progress(f"✅ [add2line_resolver] 库白名单已构建，包含 {len(whitelist)} 个库")
            else:
                logger.warning(f"库目录 {self.library_dir} 中没有找到库文件")
                self._emit_progress(f"⚠️ [add2line_resolver] 库目录中没有找到库文件")
        except Exception as e:
            logger.error(f"构建库白名单失败: {e}")
            self._emit_progress(f"❌ [add2line_resolver] 构建库白名单失败: {e}")
        
        return whitelist
    
    # library_whitelist_only 逻辑已移除，不再需要 _should_resolve_frame；
    # 仅保留该占位以兼容可能存在的旧引用（当前代码内部已不再调用）。

    def _find_library_files(self, library_dir: str, os_type: str) -> List[Path]:
        """查找库文件（委托 library_frame_whitelist，与 crash_log_parser 帧过滤共用规则）。"""
        return find_library_files_in_dir(library_dir, os_type)



    def _resolve_with_addr2line(self, address: str, library_path: str, tool_path: str) -> Optional[ResolvedFrame]:
        """使用addr2line工具解析地址。

        若当前 tool_path 是 llvm-symbolizer 别名（系统未提供 llvm-addr2line / addr2line 时启用），
        则使用 llvm-symbolizer 的兼容参数集合，并通过 --output-style=GNU 让输出与 GNU addr2line 对齐，
        从而复用现有 _parse_add2line_output 的解析逻辑。
        """
        try:
            is_symbolizer_alias = bool(
                self._llvm_addr2line_alias_path
                and tool_path == self._llvm_addr2line_alias_path
            )
            if is_symbolizer_alias:
                cmd = [
                    tool_path,
                    "-e", library_path,
                    "--functions=linkage",  # 显示函数名（与 -f 等价）
                    "--demangle",           # 还原 C++/Rust 符号（与 -C 等价）
                    "--inlines=false",      # 关闭内联展开，确保输出仅有 [function, file:line] 两行
                    "--output-style=GNU",   # 让输出格式对齐 GNU addr2line
                    address,
                ]
            else:
                cmd = [
                    tool_path,
                    "-e", library_path,
                    "-f",  # 显示函数名
                    "-C",  # 显示C++符号名
                    address,
                ]

            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)

            if result.returncode == 0:
                function, file_path, line_number = self._parse_add2line_output(result.stdout)

                return ResolvedFrame(
                    address=address,
                    resolved_function=function,
                    resolved_file=file_path,
                    resolved_line=line_number
                )
            else:
                logger.warning(f"addr2line命令执行失败: {result.stderr}")
                return None

        except subprocess.TimeoutExpired:
            logger.warning(f"addr2line命令超时: {address}")
            return None
        except Exception as e:
            logger.error(f"使用addr2line解析地址时出错 {address}: {e}")
            return None
    
    def _resolve_with_atos(self, address: str, library_path: str, tool_path: str) -> Optional[ResolvedFrame]:
        """使用atos工具解析地址（macOS专用）"""
        try:
            # 对于dSYM文件，我们需要使用不同的方法
            if library_path.endswith('.dSYM') or 'Contents/Resources/DWARF' in library_path:
                # 这是dSYM文件，尝试直接解析
                cmd = [
                    tool_path,
                    "-o", library_path,
                    address
                ]
            else:
                # 这是普通的库文件，尝试计算基址
                base_address = self._calculate_base_address(address, library_path)
                if base_address:
                    cmd = [
                        tool_path,
                        "-o", library_path,
                        "-l", base_address,
                        address
                    ]
                else:
                    # 如果无法计算基址，尝试直接解析
                    cmd = [
                        tool_path,
                        "-o", library_path,
                        address
                    ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            if result.returncode == 0:
                output = result.stdout.strip()
                if output and output != "??" and output != address:
                    # 记录atos原始输出用于调试
                    logger.debug(f"atos原始输出: '{output}'")
                    
                    # 检查atos输出是否包含有用的信息
                    if ':' in output and '(' in output and ')' in output:
                        # 尝试提取file:line格式
                        file_line_match = re.search(r'([^:]+):(\d+)', output)
                        if file_line_match:
                            # 提取完整的文件路径部分
                            full_file_part = file_line_match.group(1).strip()
                            line_number = int(file_line_match.group(2))
                            
                            # 从完整部分中提取纯文件名
                            # 通常是源码文件名，或包含其他附加信息的字符串
                            file_path = full_file_part
                            
                            # 如果文件名包含路径，只取最后一部分
                            if '/' in file_path:
                                file_path = file_path.split('/')[-1]
                            
                            # 如果文件名包含括号或其他字符，尝试清理
                            if '(' in file_path:
                                file_path = file_path.split('(')[0].strip()
                            if ')' in file_path:
                                file_path = file_path.split(')')[0].strip()
                            
                            # 尝试从原始输出中查找文件名
                            # 查找包含文件扩展名的部分
                            file_ext_match = re.search(r'([^/\s]+\.(cpp|c|h|hpp))', output)
                            if file_ext_match:
                                file_path = file_ext_match.group(1)
                            
                            # 最终清理文件名，移除所有不需要的字符
                            file_path = file_path.strip('()[]{} \t\n\r')
                            
                            # 如果文件名仍然有问题，使用默认值
                            if not file_path or len(file_path) < 2:
                                file_path = ""
                            
                            # 然后尝试提取函数名
                            function = "unknown"
                            if '(' in output:
                                # 提取第一个括号前的内容作为函数名
                                function_match = re.match(r'([^(]+)', output)
                                if function_match:
                                    function = function_match.group(1).strip()
                            
                            # 验证文件名是否合理 - 放宽验证条件
                            if file_path and len(file_path) < 100:  # 允许更长的文件名
                                logger.info(f"atos解析成功: {function} -> {file_path}:{line_number}")
                                
                                return ResolvedFrame(
                                    address=address,
                                    resolved_function=function,
                                    resolved_file=file_path,
                                    resolved_line=line_number
                                )
            
            # 如果 atos 失败或返回异常，quick_mode 下不走 nm 兜底以避免额外耗时
            if self.quick_mode:
                logger.debug(f"quick_mode: atos解析失败，跳过nm兜底: {result.stdout.strip()}")
                return None
            logger.debug(f"atos解析失败，使用nm工具作为备用: {result.stdout.strip()}")
            return self._resolve_with_nm_fallback(address, library_path)
                
        except Exception as e:
            logger.error(f"使用atos解析地址时出错 {address}: {e}")
            if self.quick_mode:
                logger.debug("quick_mode: atos异常，跳过nm兜底")
                return None
            # 尝试备用方法
            try:
                return self._resolve_with_nm_fallback(address, library_path)
            except Exception as fallback_e:
                logger.error(f"备用解析方法也失败: {fallback_e}")
                return None
    
    def _calculate_base_address(self, address: str, library_path: str) -> Optional[str]:
        """计算库文件的基址"""
        try:
            logger.debug(f"计算基址: address={address}, library_path={library_path}")
            # 使用nm工具获取符号表
            cmd = ["nm", library_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            if result.returncode == 0:
                # 查找最接近目标地址的 T（文本段）符号
                lines = result.stdout.split('\n')
                best_symbol = None
                best_distance = float('inf')
                target_addr = int(address, 16)
                
                for line in lines:
                    if line.strip() and ' T ' in line:
                        parts = line.split()
                        if len(parts) >= 3:
                            symbol_addr = parts[0]
                            symbol_name = parts[-1]
                            if symbol_addr.startswith('000000000000'):
                                try:
                                    symbol_offset = int(symbol_addr, 16)
                                    # 计算相对偏移的距离（而不是绝对地址）
                                    target_offset = target_addr & 0xFFFFFFFF  # 取低32位作为偏移
                                    distance = abs(target_offset - symbol_offset)
                                    
                                    if distance < best_distance:
                                        best_distance = distance
                                        best_symbol = (symbol_addr, symbol_name)
                                except ValueError:
                                    continue
                
                if best_symbol:
                    symbol_addr, symbol_name = best_symbol
                    try:
                        addr_val = int(address, 16)
                        symbol_offset = int(symbol_addr, 16)
                        base_addr = addr_val - symbol_offset
                        logger.debug(f"基址计算: addr_val=0x{addr_val:x}, symbol_offset=0x{symbol_offset:x}, symbol_name={symbol_name}, base_addr=0x{base_addr:x}")
                        return f"0x{base_addr:x}"
                    except ValueError as e:
                        logger.debug(f"基址计算错误: {e}")
                        return None
            
            logger.debug("未找到合适的符号来计算基址")
            return None
                
        except Exception as e:
            logger.debug(f"计算基址时出错: {e}")
            return None
    
    def _resolve_with_dwarfdump(self, address: str, library_path: str) -> Optional[ResolvedFrame]:
        """使用dwarfdump工具解析dSYM文件中的地址"""
        try:
            # 尝试使用dwarfdump
            cmd = [
                "dwarfdump",
                "--lookup", address,
                library_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            
            if result.returncode == 0:
                output = result.stdout
                
                # 解析dwarfdump输出
                function_match = re.search(r'DW_AT_name\s*:\s*([^\n]+)', output)
                file_match = re.search(r'DW_AT_decl_file\s*:\s*([^\n]+)', output)
                line_match = re.search(r'DW_AT_decl_line\s*:\s*(\d+)', output)
                
                function = function_match.group(1).strip() if function_match else None
                file_path = file_match.group(1).strip() if file_match else None
                line_number = int(line_match.group(1)) if line_match else None
                
                if function or file_path or line_number:
                    return ResolvedFrame(
                        address=address,
                        resolved_function=function,
                        resolved_file=file_path,
                        resolved_line=line_number
                    )
            
            return None
                
        except Exception as e:
            logger.debug(f"使用dwarfdump解析地址时出错 {address}: {e}")
            return None
    
    def _resolve_with_gdb(self, address: str, library_path: str, tool_path: str) -> Optional[ResolvedFrame]:
        """使用GDB工具解析地址"""
        try:
            # 创建GDB脚本
            gdb_script = f"""
set confirm off
file {library_path}
info line *{address}
quit
"""
            
            cmd = [tool_path, "-batch", "-x", "-"]
            result = subprocess.run(cmd, input=gdb_script, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            
            if result.returncode == 0:
                # 解析GDB输出: Line 123 of "file.cpp" starts at address 0x...
                match = re.search(r'Line\s+(\d+)\s+of\s+"([^"]+)"', result.stdout)
                if match:
                    line_number = int(match.group(1))
                    file_path = match.group(2)
                    
                    return ResolvedFrame(
                        address=address,
                        resolved_function=None,  # GDB不直接提供函数名
                        resolved_file=file_path,
                        resolved_line=line_number
                    )
            
            return None
                
        except Exception as e:
            logger.error(f"使用GDB解析地址时出错 {address}: {e}")
            return None
    
    def _resolve_with_objdump(self, address: str, library_path: str, tool_path: str) -> Optional[ResolvedFrame]:
        """使用objdump工具解析地址"""
        try:
            cmd = [
                tool_path,
                "-t",  # 符号表
                library_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            if result.returncode == 0:
                # 查找最接近的符号
                lines = result.stdout.split('\n')
                best_match = None
                best_distance = float('inf')
                
                for line in lines:
                    # objdump -t 输出格式: address flags type symbol
                    match = re.match(r'([0-9a-fA-F]+)\s+([a-z])\s+([a-z])\s+(.+)', line)
                    if match:
                        sym_addr = int(match.group(1), 16)
                        addr_val = int(address, 16)
                        distance = abs(addr_val - sym_addr)
                        
                        if distance < best_distance:
                            best_distance = distance
                            best_match = match.group(4)
                
                if best_match and best_distance < 0x1000:  # 1KB范围内
                    return ResolvedFrame(
                        address=address,
                        resolved_function=best_match,
                        resolved_file=None,
                        resolved_line=None
                    )
            
            return None
                
        except Exception as e:
            logger.error(f"使用objdump解析地址时出错 {address}: {e}")
            return None
    
    def _resolve_with_otool(self, address: str, library_path: str, tool_path: str) -> Optional[ResolvedFrame]:
        """使用otool工具解析地址（macOS专用）"""
        try:
            cmd = [
                tool_path,
                "-tv",  # 反汇编并显示符号
                library_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            if result.returncode == 0:
                # 查找最接近的符号
                lines = result.stdout.split('\n')
                best_match = None
                best_distance = float('inf')
                
                for line in lines:
                    # otool输出格式: address: instruction symbol
                    match = re.match(r'([0-9a-fA-F]+):\s+[^\s]+\s+(.+)', line)
                    if match:
                        sym_addr = int(match.group(1), 16)
                        addr_val = int(address, 16)
                        distance = abs(addr_val - sym_addr)
                        
                        if distance < best_distance:
                            best_distance = distance
                            best_match = match.group(2)
                
                if best_match and best_distance < 0x1000:  # 1KB范围内
                    return ResolvedFrame(
                        address=address,
                        resolved_function=best_match,
                        resolved_file=None,
                        resolved_line=None
                    )
            
            return None
                
        except Exception as e:
            logger.error(f"使用otool解析地址时出错 {address}: {e}")
            return None
    
    def _resolve_with_dumpbin(self, address: str, library_path: str, tool_path: str) -> Optional[ResolvedFrame]:
        """使用dumpbin工具解析地址（Windows专用）"""
        try:
            cmd = [
                tool_path,
                "/SYMBOLS",
                library_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            if result.returncode == 0:
                # 查找最接近的符号
                lines = result.stdout.split('\n')
                best_match = None
                best_distance = float('inf')
                
                for line in lines:
                    # dumpbin输出格式: 00000000 00000000 SECT1 00000000 symbol_name
                    match = re.match(r'([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+[A-Z0-9]+\s+[0-9a-fA-F]+\s+(.+)', line)
                    if match:
                        sym_addr = int(match.group(1), 16)
                        addr_val = int(address, 16)
                        distance = abs(addr_val - sym_addr)
                        
                        if distance < best_distance:
                            best_distance = distance
                            best_match = match.group(2)
                
                if best_match and best_distance < 0x1000:  # 1KB范围内
                    return ResolvedFrame(
                        address=address,
                        resolved_function=best_match,
                        resolved_file=None,
                        resolved_line=None
                    )
            
            return None
                
        except Exception as e:
            logger.error(f"使用dumpbin解析地址时出错 {address}: {e}")
            return None
    
    def _resolve_with_symbolicatecrash(self, address: str, library_path: str, tool_path: str) -> Optional[ResolvedFrame]:
        """使用symbolicatecrash工具解析地址（iOS专用）"""
        try:
            # symbolicatecrash需要特殊的输入格式
            # 这里简化处理，实际使用时可能需要更复杂的配置
            cmd = [
                tool_path,
                "--binary", library_path,
                address
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            
            if result.returncode == 0:
                output = result.stdout.strip()
                if output and output != "??":
                    # 解析输出格式
                    match = re.match(r'([^(]+)\s*\(([^:]+):(\d+)\)', output)
                    if match:
                        function = match.group(1).strip()
                        file_path = match.group(2).strip()
                        line_number = int(match.group(3))
                        
                        return ResolvedFrame(
                            address=address,
                            resolved_function=function,
                            resolved_file=file_path,
                            resolved_line=line_number
                        )
            
            return None
                
        except Exception as e:
            logger.error(f"使用symbolicatecrash解析地址时出错 {address}: {e}")
            return None

    def _resolve_with_nm_fallback(self, address: str, library_path: str) -> Optional[ResolvedFrame]:
        """使用nm工具作为备用解析策略"""
        try:
            # 使用nm工具获取符号表
            cmd = ["nm", library_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            if result.returncode == 0:
                # 查找最接近的符号
                lines = result.stdout.split('\n')
                best_match = None
                best_distance = float('inf')
                best_address = None
                
                for line in lines:
                    # nm输出格式: address flags type symbol
                    match = re.match(r'([0-9a-fA-F]+)\s+([a-z])\s+([a-z])\s+(.+)', line)
                    if match:
                        symbol_addr = match.group(1)
                        symbol_type = match.group(2)
                        symbol_name = match.group(4)
                        
                        # 只处理文本段符号 (T)
                        if symbol_type == 'T':
                            try:
                                sym_addr_val = int(symbol_addr, 16)
                                addr_val = int(address, 16)
                                distance = abs(addr_val - sym_addr_val)
                                
                                if distance < best_distance:
                                    best_distance = distance
                                    best_match = symbol_name
                                    best_address = symbol_addr
                            except ValueError:
                                continue
                
                if best_match and best_distance < 0x1000:  # 1KB范围内
                    logger.info(f"nm备用解析成功: {best_match} (距离: {best_distance})")
                    
                    # 尝试从符号名推断文件名
                    file_name = self._infer_file_name_from_symbol(best_match)
                    
                    # 尝试计算相对行号（基于地址偏移）
                    line_number = self._estimate_line_number_from_offset(address, best_address, best_match)
                    
                    return ResolvedFrame(
                        address=address,
                        resolved_function=best_match,
                        resolved_file=file_name,
                        resolved_line=line_number
                    )
            
            return None
                
        except Exception as e:
            logger.debug(f"使用nm备用解析时出错 {address}: {e}")
            return None
    
    def _estimate_line_number_from_offset(self, target_address: str, symbol_address: str, symbol_name: str) -> Optional[int]:
        """基于地址偏移估算行号（通用估算，不依赖业务函数名硬编码）"""
        try:
            target_val = int(target_address, 16)
            symbol_val = int(symbol_address, 16)
            offset = target_val - symbol_val
            if offset < 0:
                return None
            base_line = self._get_function_base_line(symbol_name)
            if base_line is None:
                return None
            # 通用估算：按指令密度粗略折算到源码行，不绑定任何特定函数。
            return max(1, base_line + min(offset // 16, 500))
            
        except Exception as e:
            logger.debug(f"估算行号失败: {e}")
            return None
    
    def _resolve_with_atos_precise(self, address: str, function: str, module: str, module_base_addresses: dict, library_dir: str = None) -> dict:
        """使用atos工具结合模块基址进行精确的地址解析"""
        try:
            # 获取模块基址
            base_address = None
            if module_base_addresses and module in module_base_addresses:
                base_address = module_base_addresses[module]
                logger.debug(f"使用提供的模块基址: {module} = {base_address}")
            else:
                # 尝试从地址自动计算基址（页对齐算法）
                try:
                    addr_val = int(address, 16)
                    # macOS 通常使用 64KB 或 1MB 页对齐
                    candidates = [
                        (addr_val // 0x10000) * 0x10000,  # 64KB对齐
                        (addr_val // 0x100000) * 0x100000,  # 1MB对齐
                        (addr_val // 0x1000) * 0x1000,  # 4KB对齐（备用）
                    ]
                    # 选择最小的合理候选
                    for candidate in candidates:
                        if candidate > 0 and candidate <= addr_val:
                            base_address = f"0x{candidate:x}"
                            logger.debug(f"自动计算模块基址: {module} = {base_address} (从地址 {address})")
                            break
                except (ValueError, AttributeError):
                    pass
            
            if not base_address:
                logger.debug(f"没有找到模块 {module} 的基址信息，无法使用atos精确解析")
                self._emit_progress(f"⚠️ [add2line_resolver] 没有找到模块 {module} 的基址信息")
                return None
            
            logger.debug(f"使用模块基址解析: {module} = {base_address}")
            self._emit_progress(f"🔍 [add2line_resolver] 使用模块基址解析: {module} = {base_address}")
            
            # 构建atos命令
            # 对于系统库，使用系统路径；对于我们的库，使用指定路径
            if module.startswith('libsystem_') or module.startswith('libc.') or module.startswith('libobjc.'):
                # 系统库，使用系统路径
                atos_cmd = [
                    'atos',
                    '-l', base_address,
                    '-o', module,
                    address
                ]
            else:
                # 我们的库，优先使用dSYM文件，如果没有则使用dylib
                dylib_path = f'{library_dir}/{module}'
                dsym_path = f'{library_dir}/{module}.dSYM/Contents/Resources/DWARF/{module}'
                
                # 检查dSYM文件是否存在
                import os
                import platform as plat_module
                
                # 检测架构
                machine = plat_module.machine()
                arch_map = {
                    'arm64': 'arm64',
                    'x86_64': 'x86_64',
                    'aarch64': 'arm64'
                }
                arch = arch_map.get(machine, 'arm64')  # 默认arm64
                
                if os.path.exists(dsym_path):
                    # 尝试使用 -arch 参数（如果atos支持）
                    atos_cmd = [
                        'atos',
                        '-arch', arch,
                        '-l', base_address,
                        '-o', dsym_path,
                        address
                    ]
                    logger.debug(f"使用dSYM文件: {dsym_path}, 架构: {arch}")
                else:
                    atos_cmd = [
                        'atos',
                        '-arch', arch,
                        '-l', base_address,
                        '-o', dylib_path,
                        address
                    ]
                    logger.debug(f"使用dylib文件: {dylib_path}, 架构: {arch}")
            
            logger.debug(f"执行atos命令: {' '.join(atos_cmd)}")
            self._emit_progress(f"🔧 [add2line_resolver] 执行atos命令: {' '.join(atos_cmd)}")
            
            # 执行atos命令
            result = subprocess.run(atos_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            self._emit_progress(f"🔧 [add2line_resolver] atos命令返回码: {result.returncode}")
            self._emit_progress(f"🔧 [add2line_resolver] atos命令输出: {result.stdout.strip()}")
            if result.stderr.strip():
                self._emit_progress(f"🔧 [add2line_resolver] atos命令错误: {result.stderr.strip()}")
                # 对于系统库，如果无法加载符号，跳过解析
                if module.startswith('libsystem_') or module.startswith('libc.') or module.startswith('libobjc.'):
                    self._emit_progress(f"⚠️ [add2line_resolver] 跳过系统库 {module} 的符号解析")
                    return None
            
            if result.returncode == 0 and result.stdout.strip():
                output = result.stdout.strip()
                logger.debug(f"atos输出: {output}")
                
                # 如果输出只是地址本身或偏移量，说明解析失败
                # atos 可能返回 "0x0000b2bc (in libmylib.dylib)" 这样的格式，表示只解析了偏移量
                if (output == address or 
                    (output.startswith('0x') and output.split()[0] == address) or
                    (output.startswith('0x') and '(in' in output and ')' in output)):
                    logger.debug(f"atos返回地址或偏移量，未解析到函数和行号: {output}")
                    # 如果基址是自动计算的，尝试其他候选基址
                    if not (module_base_addresses and module in module_base_addresses):
                        return self._try_alternative_base_addresses(address, function, module, library_dir)
                    return None
                
                # 解析atos输出，格式如: "function_name (in module) (file.cpp:line)"
                # 或者: "function_name (file.cpp:line)"
                import re
                
                # 匹配模式1: "function_name (in module) (file.cpp:line)"
                pattern1 = r'^(.+?)\s+\(in\s+[^)]+\)\s+\(([^:]+):(\d+)\)$'
                match1 = re.match(pattern1, output)
                
                # 匹配模式2: "function_name (file.cpp:line)"
                pattern2 = r'^([^(]+)\s+\(([^:]+):(\d+)\)$'
                match2 = re.match(pattern2, output)
                
                if match1:
                    func_name = match1.group(1).strip()
                    file_name = match1.group(2).strip()
                    line_num = int(match1.group(3))
                elif match2:
                    func_name = match2.group(1).strip()
                    file_name = match2.group(2).strip()
                    line_num = int(match2.group(3))
                else:
                    # 如果无法解析，返回原始输出
                    logger.debug(f"无法解析atos输出格式: {output}")
                    return None

                # atos 有时会把用户库首帧解析到 libc++/STL 头文件（如 sstream:359），
                # 这类结果对“定位业务源码行号”不可靠，交由后续策略兜底更稳定。
                if not self._is_likely_project_source_file(file_name):
                    logger.debug(
                        f"atos结果疑似外部库实现，跳过该结果以便后续策略兜底: {func_name} ({file_name}:{line_num})"
                    )
                    return None
                
                logger.info(f"atos精确解析成功: {address} -> {func_name} ({file_name}:{line_num})")
                return {
                    'function': func_name,
                    'file': file_name,
                    'line': line_num
                }
            else:
                logger.debug(f"atos解析失败: {result.stderr}")
                # 如果基址是自动计算的，尝试其他候选基址
                if not (module_base_addresses and module in module_base_addresses):
                    return self._try_alternative_base_addresses(address, function, module, library_dir)
                return None
                
        except subprocess.TimeoutExpired:
            logger.debug(f"atos命令超时")
            return None
        except Exception as e:
            logger.debug(f"atos精确解析异常: {e}")
            return None

    def _is_likely_project_source_file(self, file_name: str) -> bool:
        """判断符号化结果是否看起来像项目源码文件。"""
        if not file_name:
            return False
        name = file_name.strip().lower()

        # 常见标准库/系统头，通常不应作为业务崩溃行定位目标。
        external_like = {
            "sstream", "string", "vector", "map", "unordered_map", "memory",
            "new", "functional", "algorithm", "tuple", "optional", "variant"
        }
        if name in external_like:
            return False

        # 业务源码/头文件扩展名
        source_exts = (".c", ".cc", ".cpp", ".cxx", ".m", ".mm", ".h", ".hh", ".hpp", ".hxx")
        return name.endswith(source_exts)

    def _normalize_resolved_file_name(self, file_name: Optional[str]) -> str:
        """规范化解析结果中的文件名。"""
        if not file_name:
            return ""
        raw = file_name.strip()
        return raw
    
    def _try_alternative_base_addresses(self, address: str, function: str, module: str, library_dir: str = None) -> Optional[dict]:
        """尝试使用不同的基址候选值进行atos解析"""
        try:
            addr_val = int(address, 16)
            
            # 生成多个基址候选值
            candidates = [
                (addr_val // 0x10000) * 0x10000,  # 64KB对齐
                (addr_val // 0x100000) * 0x100000,  # 1MB对齐
                (addr_val // 0x1000) * 0x1000,  # 4KB对齐
                (addr_val // 0x1000000) * 0x1000000,  # 16MB对齐
                0x100000000,  # 常见的macOS基址
                0x102000000,  # 另一个常见基址
            ]
            
            # 去重并排序
            candidates = sorted(set([c for c in candidates if c > 0 and c <= addr_val]))
            
            dylib_path = f'{library_dir}/{module}'
            dsym_path = f'{library_dir}/{module}.dSYM/Contents/Resources/DWARF/{module}'
            
            import os
            binary_path = dsym_path if os.path.exists(dsym_path) else dylib_path
            
            for base_candidate in candidates[:5]:  # 最多尝试5个候选
                base_str = f"0x{base_candidate:x}"
                atos_cmd = ['atos', '-l', base_str, '-o', binary_path, address]
                
                try:
                    result = subprocess.run(atos_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                    if result.returncode == 0 and result.stdout.strip():
                        output = result.stdout.strip()
                        # 检查是否是有效解析（不是地址本身）
                        if output != address and not (output.startswith('0x') and output == address):
                            import re
                            # 尝试解析输出
                            pattern = r'^([^(]+)\s+\(([^:]+):(\d+)\)$'
                            match = re.match(pattern, output)
                            if match:
                                logger.info(f"使用备用基址 {base_str} 成功解析: {address} -> {match.group(1)} ({match.group(2)}:{match.group(3)})")
                                return {
                                    'function': match.group(1).strip(),
                                    'file': match.group(2).strip(),
                                    'line': int(match.group(3))
                                }
                except (subprocess.TimeoutExpired, Exception) as e:
                    logger.debug(f"尝试基址 {base_str} 失败: {e}")
                    continue
            
            return None
        except Exception as e:
            logger.debug(f"尝试备用基址异常: {e}")
            return None
    
    def _calculate_precise_line_number(self, address: str, function: str, offset: str, module: str, module_base_addresses: dict) -> Optional[int]:
        """基于函数定义与偏移进行通用行号估算（无业务硬编码）。"""
        try:
            offset_val = int(offset)
            if offset_val < 0:
                return None
            base_line = self._get_function_base_line(function)
            if base_line is None:
                return None
            line_number = base_line + min(offset_val // 16, 500)
            return max(1, line_number)
        except (ValueError, KeyError) as e:
            logger.debug(f"行号计算失败: {e}")
            return None
    
    def _estimate_module_base_address(self, address: str, function: str, offset: int, module: str) -> Optional[str]:
        """方法A：通过函数名偏移推算模块基址"""
        try:
            # 尝试从nm工具获取函数在模块中的偏移
            function_offset = self._get_function_offset_from_nm(function, module)
            if function_offset is None:
                logger.debug(f"无法获取函数 {function} 的模块偏移")
                return None
            
            # 计算模块基址：load_address ≈ crash_address - (function_offset + internal_offset)
            crash_addr = int(address, 16)
            total_offset = function_offset + offset
            estimated_base = crash_addr - total_offset
            
            logger.debug(f"函数偏移推算: {function} 模块偏移={function_offset:x}, 内部偏移={offset}, 估算基址={estimated_base:x}")
            return f"0x{estimated_base:x}"
            
        except Exception as e:
            logger.debug(f"函数偏移推算失败: {e}")
            return None
    
    def _get_function_offset_from_nm(self, function: str, module: str) -> Optional[int]:
        """从nm工具获取函数在模块中的偏移"""
        try:
            # 使用nm工具获取符号表
            cmd = ["nm", "-D", module]  # -D 只显示动态符号
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                for line in lines:
                    if function in line and ' T ' in line:  # T表示文本段符号
                        parts = line.split()
                        if len(parts) >= 3:
                            try:
                                offset = int(parts[0], 16)
                                logger.debug(f"从nm获取函数偏移: {function} = 0x{offset:x}")
                                return offset
                            except ValueError:
                                continue
            
            # 如果nm失败，尝试使用otool
            return self._get_function_offset_from_otool(function, module)
            
        except Exception as e:
            logger.debug(f"nm工具获取函数偏移失败: {e}")
            return None
    
    def _get_function_offset_from_otool(self, function: str, module: str) -> Optional[int]:
        """从otool工具获取函数偏移"""
        try:
            cmd = ["otool", "-tV", module]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                for line in lines:
                    if function in line and ':' in line:
                        # 提取地址部分
                        addr_match = re.match(r'([0-9a-fA-F]+):', line)
                        if addr_match:
                            try:
                                offset = int(addr_match.group(1), 16)
                                logger.debug(f"从otool获取函数偏移: {function} = 0x{offset:x}")
                                return offset
                            except ValueError:
                                continue
            
            return None
            
        except Exception as e:
            logger.debug(f"otool工具获取函数偏移失败: {e}")
            return None
    
    def _estimate_page_aligned_base_address(self, address: str) -> Optional[str]:
        """方法B：页对齐推算法"""
        try:
            addr_val = int(address, 16)
            
            # macOS/iOS 常见的页对齐地址
            page_alignments = [
                0x1000,      # 4KB页
                0x10000,     # 64KB页
                0x100000,    # 1MB页
                0x1000000,   # 16MB页
            ]
            
            # 对于用户dylib/可执行文件，常见基址格式：0x10xxxxxxx
            if addr_val >= 0x100000000:  # 大于4GB的地址
                # 向下对齐到最近的0x10000边界
                aligned_base = (addr_val // 0x10000) * 0x10000
                logger.debug(f"页对齐估算基址: {address} -> 0x{aligned_base:x}")
                return f"0x{aligned_base:x}"
            elif addr_val >= 0x10000000:  # 大于256MB的地址
                # 向下对齐到最近的0x1000边界
                aligned_base = (addr_val // 0x1000) * 0x1000
                logger.debug(f"页对齐估算基址: {address} -> 0x{aligned_base:x}")
                return f"0x{aligned_base:x}"
            
            return None
            
        except Exception as e:
            logger.debug(f"页对齐估算失败: {e}")
            return None
    
    def _calculate_line_number_with_base_address(self, address: str, function: str, offset: int, base_address: str) -> int:
        """使用估算的基址计算行号"""
        try:
            # 优先使用函数特定的基础行号
            base_line = self._get_function_base_line(function)
            if base_line is None:
                # 如果无法获取函数基础行号，使用函数模式估算
                return self._calculate_line_number_by_function_pattern(function, offset)
            
            # 基于偏移量计算行号，考虑指令长度
            # 假设平均每条指令4字节，每行代码平均10字节
            instruction_count = offset // 4
            line_increment = instruction_count // 3  # 每3条指令大约对应1行代码
            
            line_number = base_line + line_increment
            
            # 确保行号在合理范围内
            line_number = max(1, min(line_number, 1000))
            
            logger.debug(f"基于基址计算行号: {function} + {offset} -> {line_number}")
            return line_number
            
        except Exception as e:
            logger.debug(f"基于基址计算行号失败: {e}")
            return 200
    
    def _get_function_base_line(self, function: str) -> Optional[int]:
        """获取函数定义行号（在工作区源码中动态扫描）。"""
        try:
            simple_function_name = self._extract_simple_function_name_from_mangled(function)
            if not simple_function_name:
                return None
            loc = self._find_function_definition_in_workspace(simple_function_name)
            return loc[1] if loc else None
        except Exception as e:
            logger.debug(f"扫描函数行号失败: {e}")
            return None
    
    def _scan_function_line_in_file(self, file_path: str, function: str) -> Optional[int]:
        """在源文件中扫描函数定义行号"""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            
            # 提取简单函数名
            simple_function_name = self._extract_simple_function_name_from_mangled(function)
            
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if not line or line.startswith('//') or line.startswith('/*'):
                    continue
                
                # 检查是否是函数定义行
                if self._is_function_definition_line_for_scanning(line, simple_function_name):
                    logger.debug(f"在 {file_path}:{i} 找到函数 {simple_function_name}")
                    return i
            
            return None
            
        except Exception as e:
            logger.debug(f"扫描文件 {file_path} 失败: {e}")
            return None

    def _find_function_definition_in_workspace(self, function_name: str) -> Optional[Tuple[str, int]]:
        """在工作区内查找函数定义（返回 文件绝对路径, 1-based行号）。"""
        if function_name in self._function_location_cache:
            return self._function_location_cache[function_name]
        try:
            workspace_root = str(Path(__file__).resolve().parents[1])
            skip_dirs = {".git", ".venv", "node_modules", "dist", "build", "cli_reports"}
            source_exts = {".c", ".cc", ".cpp", ".cxx", ".m", ".mm", ".h", ".hh", ".hpp", ".hxx"}

            for root, dirs, files in os.walk(workspace_root):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                for name in files:
                    if Path(name).suffix.lower() not in source_exts:
                        continue
                    full_path = os.path.join(root, name)
                    line_no = self._scan_function_line_in_file(full_path, function_name)
                    if line_no:
                        result = (full_path, line_no)
                        self._function_location_cache[function_name] = result
                        return result
            self._function_location_cache[function_name] = None
            return None
        except Exception as e:
            logger.debug(f"工作区函数定义检索失败: {e}")
            self._function_location_cache[function_name] = None
            return None
    
    def _extract_simple_function_name_from_mangled(self, mangled_name: str) -> str:
        """从符号提取简单函数名（通用规则，不依赖项目特定关键字）。"""
        # c++filt 不可用时，按 Itanium ABI 的长度编码通用提取函数名（如 _Z13crash_nullptrv）
        raw = (mangled_name or "").strip()
        # 兜底：处理日志中常见的“近似 mangled”形式，例如 _Z13crash_oobv。
        # 这类符号可能无法被 c++filt 正常解码，但仍可从中提取函数名 token。
        m_quick = re.match(r"^_Z\d+([A-Za-z_]\w*)v$", raw)
        if m_quick:
            return m_quick.group(1)
        if raw.startswith("_Z"):
            parts: List[str] = []
            i = 2
            n = len(raw)
            while i < n:
                if raw[i].isdigit():
                    j = i
                    while j < n and raw[j].isdigit():
                        j += 1
                    try:
                        seg_len = int(raw[i:j])
                    except ValueError:
                        break
                    seg_start = j
                    seg_end = j + seg_len
                    if seg_len <= 0 or seg_end > n:
                        break
                    segment = raw[seg_start:seg_end]
                    if re.match(r'^[A-Za-z_]\w*$', segment):
                        parts.append(segment)
                    i = seg_end
                    continue
                i += 1
            if parts:
                return parts[-1]
        demangled = self._demangle_cpp_symbol(raw)
        if not demangled:
            return mangled_name
        head = demangled.split("(", 1)[0].strip()
        if not head:
            return mangled_name
        tail = head.split("::")[-1].strip()
        m = re.search(r'([~A-Za-z_]\w*)$', tail)
        if m:
            return m.group(1)
        return tail or mangled_name
    
    def _is_function_definition_line_for_scanning(self, line: str, function_name: str) -> bool:
        """检查是否是特定函数的定义行"""
        # 函数定义模式
        function_patterns = [
            rf'void\s+{re.escape(function_name)}\s*\([^)]*\)\s*{{',      # void function_name(...) {
            rf'\w+\s+{re.escape(function_name)}\s*\([^)]*\)\s*{{',       # return_type function_name(...) {
            rf'{re.escape(function_name)}\s*\([^)]*\)\s*{{',             # function_name(...) {
            # 支持类成员函数
            rf'\w+::\w+.*{re.escape(function_name)}\s*\([^)]*\)\s*{{',  # Class::function_name(...) {
            rf'\w+::\w+.*{re.escape(function_name)}\s*\([^)]*\)\s*:',   # Class::function_name(...) :
        ]
        
        for pattern in function_patterns:
            if re.search(pattern, line):
                return True
        return False
    
    def _calculate_line_number_by_function_pattern(self, function: str, offset: int) -> int:
        """通用偏移估算（仅作为最后兜底，不使用函数名硬编码）。"""
        try:
            base_line = self._get_function_base_line(function)
            if base_line is None:
                return max(1, min(offset // 16, 1000))
            line_number = base_line + min(max(offset, 0) // 16, 500)
            return max(1, min(line_number, 1000))
        except Exception as e:
            logger.debug(f"函数模式估算失败: {e}")
            return 1

    def _demangle_cpp_symbol(self, mangled_name: str) -> str:
        """将C++ mangled name转换为可读函数名（优先使用 c++filt）。"""
        try:
            if not mangled_name or not mangled_name.startswith("_Z"):
                return mangled_name
            cxxfilt = self._find_tool_in_paths(
                "c++filt",
                ["/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"]
            )
            if not cxxfilt:
                return mangled_name
            res = subprocess.run(
                [cxxfilt, mangled_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
            return mangled_name
        except Exception as e:
            logger.debug(f"demangle失败: {e}")
            return mangled_name

    def _infer_file_name_from_symbol(self, symbol_name: str) -> Optional[str]:
        """从符号名推断文件名（动态扫描，不依赖业务关键字）。"""
        try:
            simple_function_name = self._extract_simple_function_name_from_mangled(symbol_name)
            if not simple_function_name:
                return None
            loc = self._find_function_definition_in_workspace(simple_function_name)
            if loc:
                return os.path.basename(loc[0])
            return None
        except Exception as e:
            logger.debug(f"推断文件名失败: {e}")
            return None

def add2line_resolver(
    crash_json: str,
    library_dir: Optional[str] = None,
    tool_search_paths: Optional[Dict[str, List[str]]] = None,
    config_file: Optional[str] = None,
    max_frames: Optional[int] = None,
    quick_mode: bool = False,
) -> str:
    """
    add2line解析工具
    
    Args:
        crash_json (str): 崩溃日志解析结果的JSON字符串
        library_dir (Optional[str]): 库文件目录路径；可省略。若为已符号化 iOS 堆栈且未提供有效路径，将从日志回填 02。
        tool_search_paths (Optional[Dict[str, List[str]]]): 可选的工具搜索路径配置（优先级最高），格式为：
            {
                "ANDROID_NDK_HOME": ["/path/to/ndk"],
                "LLVM_HOME": ["/path/to/llvm"],
                "TOOLCHAIN_PATH": ["/path/to/toolchain"],
                "PATH": ["/custom/path1", "/custom/path2"]
            }
            如果提供，将优先使用这些路径
        config_file (Optional[str]): 可选的配置文件路径，如果不提供，将尝试从默认位置读取
            配置文件格式见 tools/configs/add2line_resolver_config.local.example.json
        max_frames (Optional[int]): 最大处理的堆栈帧数量，None 表示不限制（用于过滤噪音信息）
        quick_mode (bool): 快速模式（跳过慢速兜底策略，优先吞吐）
        
    Returns:
        str: JSON格式的解析结果，包含：
            - resolved_frames: 解析后的堆栈帧列表
            - os_type: 操作系统类型
            - library_path: 库目录路径
            - success_count: 成功解析的地址数量
            - total_count: 总地址数量
            - errors: 错误信息列表
    
    工具路径配置优先级：
        1. 命令参数 (tool_search_paths) - 最高优先级
        2. 配置文件 (config_file 或默认位置) - 中等优先级
        3. 环境变量和常规目录 - 最低优先级
    """
    resolver = Add2lineResolver(
        tool_search_paths=tool_search_paths,
        config_file=config_file,
        library_dir=library_dir,
        quick_mode=quick_mode,
    )
    return resolver.resolve_stack_trace(crash_json, library_dir, max_frames=max_frames)

# 测试代码
if __name__ == "__main__":
    import sys
    
    # 如果从stdin读取输入
    if not sys.stdin.isatty():
        crash_json = sys.stdin.read()
        # 过滤掉"-"参数，它表示从stdin读取
        args = [arg for arg in sys.argv if arg != "-"]
        library_dir = args[1] if len(args) > 1 else "."
        result = add2line_resolver(crash_json, library_dir)
        print(result)
    else:
        # 示例崩溃日志解析结果
        sample_crash_json = '''
        {
            "stack_frames": [
                {
                    "address": "0x12345678",
                    "function": "crash_function",
                    "file": "crash.c",
                    "line": 42,
                    "module": "libcrash.so"
                },
                {
                    "address": "0x87654321",
                    "function": "main",
                    "file": "main.c",
                    "line": 10,
                    "module": "libmain.so"
                }
            ],
            "crash_info": {
                "thread_type": "main",
                "crash_reason": "segmentation fault",
                "signal": "11 (SIGSEGV)",
                "exception_type": null
            },
            "meta_info": {
                "os_type": "linux",
                "os_version": "5.4.0",
                "app_version": "1.0.0",
                "device_model": "x86_64",
                "timestamp": "2023-01-01 12:00:00"
            },
            "raw_content": "sample crash log content"
        }
        '''
        
        # 测试解析
        result = add2line_resolver(sample_crash_json, "/usr/lib")
        print("解析结果:")
        print(result)


# ==================== Add2LineResolverTool (BaseTool wrapper) ====================

import logging as _logging
from typing import Any as _Any, Dict as _Dict, Optional as _Optional

from tool_system.tool import BaseTool, ToolDefinition
from tool_system.registry import Priority

_tool_logger = _logging.getLogger(__name__)


class Add2LineResolverTool(BaseTool):
    """地址解析工具 — 内置 Tool 实现，自包含所有地址解析逻辑。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="add2line_resolver",
            description="解析堆栈地址，转换为函数名和行号。使用 addr2line/atos 等工具。",
            input_schema={
                "type": "object",
                "properties": {
                    "crash_json": {"type": "string", "description": "崩溃日志解析结果的 JSON 字符串"},
                    "library_dir": {"type": "string", "description": "库文件目录路径"},
                    "tool_search_paths": {"type": "object", "description": "工具搜索路径"},
                    "config_file": {"type": "string", "description": "配置文件路径"},
                    "max_frames": {"type": "integer", "description": "最大处理帧数"},
                    "quick_mode": {"type": "boolean", "description": "快速模式", "default": False},
                },
                "required": ["crash_json"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "resolved_frames": {"type": "array"},
                },
            },
            category="resolver",
            version="1.0.0",
        )

    def execute(self, input_data: _Dict[str, _Any]) -> _Dict[str, _Any]:
        import json as _json

        crash_json = input_data.get("crash_json", "")
        library_dir = input_data.get("library_dir")
        tool_search_paths = input_data.get("tool_search_paths")
        config_file = input_data.get("config_file")
        max_frames = input_data.get("max_frames")
        quick_mode = input_data.get("quick_mode", False)

        result = add2line_resolver(
            crash_json=crash_json,
            library_dir=library_dir,
            tool_search_paths=tool_search_paths,
            config_file=config_file,
            max_frames=max_frames,
            quick_mode=quick_mode,
        )
        try:
            parsed = _json.loads(result)
        except Exception:
            parsed = {"raw_result": result}
        return parsed
