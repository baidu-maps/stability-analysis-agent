# 堆栈地址解析工具说明

本说明文档聚焦于 Agent 在**堆栈地址解析**阶段如何选择与使用不同工具，以及相关配置文件的用途。

## 1. 解析流程概览

当执行崩溃分析时，Agent 会把堆栈中的地址解析为 **函数名 / 文件路径 / 行号**。  
解析器会根据**当前平台**和**可用工具**选择最合适的解析工具（例如 `atos`、`llvm-addr2line`、`addr2line`、`gdb` 等）。

配置与示例以 **`add2line_resolver_config.local.json`** 为主（见下文加载顺序）；仓库内提供示例：

- `configs/add2line_resolver_config.local.example.json`

## 2. 配置文件说明

### 2.1 生效的本地配置 `add2line_resolver_config.local.json`

解析器默认按候选路径**依次查找**该文件名（先找到者生效），例如：

- 当前工作目录下 `configs/add2line_resolver_config.local.json`（便于发布产物同目录配置）
- 仓库内 `configs/add2line_resolver_config.local.json`
- 用户目录 `~/.config/stability-analysis-agent/add2line_resolver_config.local.json`

也可通过环境变量 **`STABILITY_AGENT_ADD2LINE_CONFIG_FILE`** 指定单一绝对路径，覆盖上述候选列表。

文件内典型字段：

- **`preferred_tools`**（可选）：该平台下解析工具优先级列表。若未在 local 中声明，解析器仍可按内置默认顺序探测。
- **`tool_paths`**：额外搜索路径，为 **字符串数组**；每项必须是**目录**的绝对路径（目录内需能直接找到 `llvm-addr2line`、`addr2line`、`atos` 等之一）。交互式 CLI 的「手动设置符号化工具绝对路径」允许用户输入**可执行文件的绝对路径**，保存时会写入其**父目录**到 `tool_paths`。
- **`environment_vars`**（可选）：键为环境变量名，值为**工具链安装根目录**（如 `ANDROID_NDK_HOME` 指向 NDK 根路径）。解析器对已知键名会推导其下的 `bin` 等子路径。该块常由 CLI **「自动获取」**或**快速开始**前的静默自动写入生成；高级用户也可手编 JSON 维护。

示例与字段注释见：`configs/add2line_resolver_config.local.example.json`。

### 2.2 与交互式 CLI 的关系

- **设置 → 配置堆栈地址解析工具**：先展示符号化工具检测；向导内为 **「自动获取（推荐）」** 与 **「手动设置符号化工具绝对路径」**（可填可执行文件或含该工具的目录），不再提供「从 shell 读取环境变量 KEY」的独立菜单。
- **快速开始修复**：当当前 Agent 流程需要符号化时，会在阻断用户前**静默尝试**与「自动获取」相同的写入逻辑（若有可写入的 env / IDE 路径），减少重复配置。

## 3. 工具选择机制（行为说明）

解析器会按以下顺序选择工具：

1. 根据当前平台读取 `preferred_tools` 列表（若配置中有）
2. 结合 `tool_paths`、配置中的 `environment_vars` 所推导路径、以及系统 `PATH` 等寻找可用工具
3. 选择第一个可用工具执行解析

常见情况举例：

- macOS：优先 `atos`，其次 `llvm-atos`，再到 `llvm-addr2line`
- Android：优先 `llvm-addr2line`，其次 `addr2line`、`gdb`、`ndk-stack`
- Windows：优先 `llvm-symbolizer.exe`，其次 `llvm-addr2line.exe`；PE/COFF 文件的源码行号依赖匹配的 PDB

### 3.1 Windows PE/PDB

Windows 原生栈符号化建议准备同一构建产出的 `.exe` / `.dll` 和 `.pdb`。将它们放入 `--library-dir` 指定的目录，或通过 CLI 的“配置堆栈地址解析工具”设置包含 `llvm-symbolizer.exe` 的目录。解析器调用 LLVM 的 PE 目标模式：

```text
llvm-symbolizer.exe --obj=C:\symbols\demo.exe --demangle 0x140001234
```

如果日志中的地址是模块内偏移而不是进程虚拟地址，应先确认崩溃采集器的地址语义；错误的模块基址或 ASLR 处理会导致 PDB 无法命中。没有匹配 PDB 时，Agent 仍可保留日志中的函数名和偏移，但不能保证源码文件、行号和后续自动修复质量。

## 4. 常见问题

- **解析失败**：通常是工具不可用或路径不正确；优先使用 **「自动获取」**，或手动指定 **符号化工具绝对路径**（可执行文件或其所在目录），或直接编辑 `add2line_resolver_config.local.json` 中的 `tool_paths` / `environment_vars`。
- **平台切换**：不同平台应维护各自的工具优先级与路径，避免误用。
