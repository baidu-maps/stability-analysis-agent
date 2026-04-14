# 堆栈地址解析工具说明

本说明文档聚焦于 Agent 在**堆栈地址解析**阶段如何选择与使用不同工具，以及相关配置文件的用途。

## 1. 解析流程概览

当执行崩溃分析时，Agent 会把堆栈中的地址解析为 **函数名 / 文件路径 / 行号**。  
解析器会根据**当前平台**和**可用工具**选择最合适的解析工具（例如 `atos`、`llvm-addr2line`、`addr2line`、`gdb` 等）。

工具选择逻辑的核心来源是以下两个配置文件：

- `tools/configs/add2line_resolver_config.json`
- `tools/configs/add2line_resolver_config.local.example.json`

## 2. 配置文件说明

### 2.1 `add2line_resolver_config.json`（默认配置）

这是**默认配置文件**，用于描述各平台的：

- `preferred_tools`：解析工具优先级列表  
  - 例如 macOS：`["atos", "llvm-atos", "llvm-addr2line", "addr2line", "gdb", "otool"]`
- `tool_paths`：额外工具路径（默认空）
- `environment_vars`：依赖的环境变量键位（默认 `null`）

该文件可被版本控制，是**通用配置模板**。

### 2.2 `add2line_resolver_config.local.example.json`（本地示例）

这是**本地配置示例**，提供可替换的路径与环境变量写法，用于指导开发者在本机创建私有配置：

- 填写实际工具路径（如 `llvm-addr2line` 或 `atos` 所在目录）
- 填写实际环境变量值（如 `ANDROID_NDK_HOME`、`DEVELOPER_DIR`）

你可以基于该示例创建自己的本地配置文件（例如 `add2line_resolver_config.local.json`）。  
加载策略为：**命中 local 后不再读取 base（非 merge）**，并可避免把本地路径提交到仓库。

## 3. 工具选择机制（行为说明）

解析器会按以下顺序选择工具：

1. 根据当前平台读取 `preferred_tools` 列表
2. 结合 `tool_paths` 与系统 `PATH` 寻找可用工具
3. 选择第一个可用工具执行解析

常见情况举例：

- macOS：优先 `atos`，其次 `llvm-atos`，再到 `llvm-addr2line`
- Android：优先 `llvm-addr2line`，其次 `addr2line`、`gdb`、`ndk-stack`

## 4. 常见问题

- **解析失败**：通常是工具不可用或路径不正确，建议配置本地 `tool_paths` 与环境变量。
- **平台切换**：不同平台应维护各自的工具优先级，避免误用。

