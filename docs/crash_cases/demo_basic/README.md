# 多平台崩溃测试演示项目

这是一个支持多平台的崩溃测试演示项目，包含桌面、移动和IoT平台的崩溃测试实现，能够自动生成包含堆栈地址的崩溃日志。

## 🏗️ 项目架构

### 核心设计理念

项目采用 **平台无关的通用库** + **平台特定的入口层** 的架构：

- **`common/`**: 平台无关的崩溃制造库，包含各种崩溃类型的实现
- **平台入口层**: 各平台特定的调用接口（desktop、android、ios、harmony）

### 目录结构

```
demo_basic/
├── code_dir/
│   ├── common/                    # 平台无关的通用逻辑
│   │   ├── include/my_lib.h      # 崩溃函数声明
│   │   └── src/my_lib.cpp        # 崩溃函数实现
│   │
│   ├── desktop/                   # 桌面平台入口
│   │   ├── main.cpp              # 命令行入口
│   │   └── CMakeLists.txt        # CMake构建配置
│   │
│   ├── android/                   # Android平台入口
│   │   ├── jni/
│   │   │   ├── jni_bridge.cpp    # JNI桥接层
│   │   │   └── CMakeLists.txt    # NDK构建配置
│   │   └── java/
│   │       └── com/example/crashdemo/
│   │           └── MainActivity.java  # Android UI
│   │
│   ├── ios/                       # iOS平台入口
│   │   ├── CrashTestBridge.mm    # Objective-C++桥接
│   │   └── CMakeLists.txt        # Xcode构建配置
│   │
│   └── harmony/                   # 鸿蒙平台入口
│       ├── NativeBridge.cpp      # 鸿蒙Native桥接
│       └── CMakeLists.txt        # 鸿蒙构建配置
│
├── mk/                           # 构建脚本目录
│   ├── build.sh                  # 兼容入口（内部转调 build-mac.sh）
│   ├── build-mac.sh              # macOS 主构建脚本
│   └── CMakeLists.txt            # CMake 构建入口
│
├── build/                        # 构建输出目录
├── lib/                          # 库文件目录
│   ├── libmylib.so              # 通用库
│   ├── android/                 # Android库
│   ├── ios/                     # iOS库
│   └── harmony/                 # 鸿蒙库
│
└── logs/                         # 日志目录
    ├── execution_log.txt        # 执行情况记录
    ├── mac/ linux/ windows/     # 桌面平台崩溃日志
    └── android/ ios/ harmony/   # 移动平台崩溃日志
```

## 🚀 支持的平台

### 桌面平台
- **macOS**: 使用g++编译，支持崩溃测试和日志收集
- **Linux**: 使用g++编译，支持崩溃测试和日志收集
- **Windows**: 使用g++编译，支持崩溃测试（需要手动运行）

### 移动平台
- **Android**: 使用NDK编译JNI库，Java UI界面
- **iOS**: 使用Xcode编译Objective-C++桥接库
- **鸿蒙**: 使用鸿蒙NDK编译Native桥接库

## 🛠️ 构建方法

### 1. 构建（推荐）

```bash
cd crash_cases/demo_basic
sh mk/build.sh
```

说明：
- `build.sh` 是兼容入口，内部会转调 `build-mac.sh`。
- 当前开源 demo 主要维护 macOS 构建路径。

## 🧪 崩溃类型

项目包含以下7种崩溃类型：

1. **NullPtr** - 空指针访问
2. **DanglingPtr** - 悬空指针访问
3. **OutOfBounds** - 数组越界访问
4. **DivZero** - 除零错误
5. **BadCast** - 错误类型转换
6. **StackOverflow** - 栈溢出
7. **Abort** - 主动终止

### ⚠️ 现代编译器优化影响

**重要说明**：由于现代编译器的安全优化特性，并非所有崩溃类型都能在所有平台上成功触发：

| 崩溃类型 | 触发成功率 | 原因分析 |
|---------|-----------|----------|
| **NullPtr** | ✅ 高 | 空指针访问通常能成功触发 SIGSEGV |
| **DanglingPtr** | ❌ 低 | 现代编译器会优化掉悬空指针访问 |
| **OutOfBounds** | ❌ 低 | 编译器会进行边界检查优化 |
| **DivZero** | ❌ 低 | 编译器会优化掉明显的除零操作 |
| **BadCast** | ❌ 低 | 类型转换会被编译器优化 |
| **StackOverflow** | ⚠️ 部分 | 可能触发但无法生成日志（栈空间不足） |
| **Abort** | ✅ 高 | 主动终止总是能成功触发 SIGABRT |

**实际测试结果**：
- **macOS**: 通常只有 2-3 种崩溃类型能成功触发
- **Linux**: 类似 macOS，受编译器优化影响
- **移动平台**: 由于沙盒限制，可能触发率更低

## 📱 平台特定使用

### 桌面平台

```bash
# 运行特定崩溃测试
./build/crash_test 1  # NullPtr
./build/crash_test 6  # StackOverflow
./build/crash_test 7  # Abort
```

### Android平台

1. 将生成的JNI库集成到Android Studio项目
2. 在Java代码中调用：
```java
MainActivity.runCrashTest(1);  // NullPtr
```

### iOS平台

1. 将生成的库文件集成到Xcode项目
2. 在Swift/Objective-C代码中调用：
```swift
runCrashTest(1)  // NullPtr
```

### 鸿蒙平台

1. 将生成的库文件集成到DevEco Studio项目
2. 在JS/ETS代码中调用：
```javascript
runCrashTest(1)  // NullPtr
```

## 📊 崩溃日志系统

### 🎯 核心特性

项目实现了完整的崩溃日志系统，能够：

1. **自动捕获堆栈地址**: 使用 `backtrace()` 和 `backtrace_symbols()` 函数
2. **信号处理**: 捕获 SIGSEGV、SIGBUS、SIGFPE、SIGILL、SIGABRT 等信号
3. **多平台支持**: 每个平台生成独立的崩溃日志
4. **详细堆栈信息**: 包含函数名、地址偏移、库名等信息

### 📝 崩溃日志格式

每个崩溃日志包含以下信息：

```
=== 崩溃报告 ===
时间: 2025-08-25_20-25-24
平台: mac
崩溃类型: NullPtr_SIGSEGV
进程ID: 92673
崩溃地址: 0x1040c28cc

=== 堆栈跟踪 ===
#0 0x1040c28cc 0   libmylib.so                         0x00000001040c28cc _Z19capture_stack_traceRKNSt3__112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEEE + 76
#1 0x1040c37cc 1   libmylib.so                         0x00000001040c37cc _Z14signal_handleriP9__siginfoPv + 440
#2 0x19bca6e04 2   libsystem_platform.dylib            0x000000019bca6e04 _sigtramp + 56
#3 0x1040c397c 3   libmylib.so                         0x00000001040c397c _Z13crash_nullptrv + 84
#4 0x10409627c 4   crash_test                          0x000000010409627c main + 476
#5 0x19b8f0274 5   dyld                                0x000000019b8f0274 start + 2840

=== 系统信息 ===
编译时间: Aug 25 2025 20:25:22
编译器: Apple LLVM 16.0.0 (clang-1600.0.26.6)
```

### 🔍 日志生成机制

**桌面平台（Mac/Linux/Windows）**：
- 使用信号处理器（`sigaction`）自动捕获崩溃
- 崩溃时自动生成日志，无需手动调用
- 支持 SIGSEGV、SIGBUS、SIGFPE、SIGILL、SIGABRT 等信号

**移动平台（Android/iOS/鸿蒙）**：
- 主动调用 `capture_stack_trace` 记录开始状态
- 信号处理器调用 `capture_stack_trace` 记录崩溃信息
- 提供更详细的调试上下文信息

### ⚠️ 特殊崩溃类型说明

**StackOverflow（栈溢出）**：
- 可能触发崩溃但无法生成日志
- 原因：栈空间不足，信号处理器无法正常工作
- 系统级保护会直接终止进程，不经过用户空间处理

**现代编译器优化**：
- 悬空指针、数组越界、除零等操作可能被编译器优化掉
- 这是现代 C++ 编译器的安全特性
- 使用 `-O0` 编译选项可以减少优化影响

### 📁 日志文件组织

```
logs/
├── execution_log.txt                # 执行情况记录
├── mac/                             # macOS崩溃日志
│   ├── NullPtr_2025-08-25_20-25-24.crash
│   ├── NullPtr_SIGSEGV_2025-08-25_20-25-24.crash
│   ├── StackOverflow_2025-08-25_20-25-25.crash
│   └── ...
├── linux/                           # Linux崩溃日志
├── windows/                         # Windows崩溃日志
├── android/                         # Android崩溃日志
├── ios/                             # iOS崩溃日志
└── harmony/                         # 鸿蒙崩溃日志
```

### 🔍 崩溃日志分析

可直接用 `ls logs/mac/` 与 `head`/`cat` 查看日志内容，或使用仓库主 CLI 进行解析与分析。

输出示例：
```
📊 崩溃日志报告
==================
📈 总体统计:
总崩溃日志数量: 9

📱 按平台统计:
mac: 9 个日志
linux: 0 个日志
...

🚨 按崩溃类型统计:
NullPtr: 2 个日志
DanglingPtr: 1 个日志
...

📍 堆栈地址分析:
堆栈帧数量统计:
  Abort_SIGABRT_2025-08-25_20-25-26.crash: 8 个堆栈帧
  ...

常见堆栈地址模式:
  0x19b8f0274: 出现 9 次
  0x19bca6e04: 出现 2 次
  ...
```

### ✅ 用 Stability Analysis Agent 测试 mac 崩溃日志（推荐）

当你已经在 `logs/mac/` 里有 crash 日志（例如 `_SIGSEGV_*.crash`、`_SIGABRT_*.crash`），可以直接用仓库根目录下的 CLI 做回归测试。

#### 你需要准备的“三件套”

- **crash 日志**：`crash_cases/demo_basic/logs/mac/<某个>.crash`
- **符号/库目录**：`crash_cases/demo_basic/lib/mac`
- **代码根目录**：`crash_cases/demo_basic/code_dir`

> 说明：如果你不确定要选哪个日志，可先 `ls crash_cases/demo_basic/logs/mac/*.crash`，再挑一个 `.crash` 进行分析。

#### 1) 完整分析（默认）

```bash
cd /path/to/stability-analysis-agent/main

python3 tools/cli/main.py \
  --crash-log-file crash_cases/demo_basic/logs/mac/_SIGSEGV_2025-09-10_18-06-22.crash \
  --library-dir crash_cases/demo_basic/lib/mac \
  --code-roots crash_cases/demo_basic/code_dir
```

#### 2) 无密钥/快速回归：仅生成提示词（跑完整工具链但不调 LLM）

```bash
python3 tools/cli/main.py \
  --crash-log-file crash_cases/demo_basic/logs/mac/_SIGSEGV_2025-09-10_18-06-22.crash \
  --library-dir crash_cases/demo_basic/lib/mac \
  --code-roots crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
```

#### 3) 更快：只做解析 + 地址解析（此模式不需要 code_roots / 代码根目录）

```bash
python3 tools/cli/main.py \
  --crash-log-file crash_cases/demo_basic/logs/mac/_SIGSEGV_2025-09-10_18-06-22.crash \
  --library-dir crash_cases/demo_basic/lib/mac \
  --scope parse_stack_only
```

#### 4) 高频调试：先起 daemon，再让 CLI 走 daemon（可选）

```bash
# 终端 A：启动 daemon
python3 tools/daemon/server.py --host 127.0.0.1 --port 8765
```

```bash
# 终端 B：CLI 通过 daemon 执行（daemon 不可用会自动回退本地直跑）
python3 tools/cli/main.py --daemon http://127.0.0.1:8765 \
  --crash-log-file crash_cases/demo_basic/logs/mac/_SIGSEGV_2025-09-10_18-06-22.crash \
  --library-dir crash_cases/demo_basic/lib/mac \
  --code-roots crash_cases/demo_basic/code_dir
```

### 🎯 堆栈地址信息

每个崩溃日志包含详细的堆栈地址信息：

- **地址**: 16进制内存地址（如 `0x1040c28cc`）
- **帧号**: 堆栈帧序号（如 `#0`, `#1`）
- **库名**: 包含该地址的库文件（如 `libmylib.so`）
- **函数名**: 符号化的函数名（如 `_Z19capture_stack_trace...`）
- **偏移**: 函数内的地址偏移（如 `+ 76`）

## 🔧 开发环境要求

### 通用要求
- g++ 编译器（支持C++17）
- CMake 3.10+
- sh shell

### 平台特定要求

#### Android
- Android NDK
- Android Studio（可选，用于UI开发）

#### iOS
- Xcode
- iOS SDK

#### 鸿蒙
- 鸿蒙NDK
- DevEco Studio（可选，用于UI开发）

## 🎯 扩展指南

### 添加新的崩溃类型

1. 在 `common/include/my_lib.h` 中添加枚举值
2. 在 `common/src/my_lib.cpp` 中实现崩溃函数
3. 在所有平台的桥接文件中添加调用
4. 更新构建脚本中的枚举映射

### 添加新平台

1. 在 `code_dir/` 下创建新平台目录
2. 创建平台特定的桥接文件
3. 创建对应的CMakeLists.txt
4. 创建平台构建脚本
5. 更新主README文档

## 🐛 故障排除

### 常见问题

1. **编译失败**: 检查编译器版本和C++17支持
2. **链接失败**: 检查库文件路径和依赖关系
3. **平台检测错误**: 检查 `uname -s` 输出
4. **权限问题**: 确保脚本有执行权限
5. **堆栈信息缺失**: 确保使用 `-g -O0 -rdynamic` 编译选项
6. **崩溃测试未触发**: 现代编译器优化可能导致部分崩溃类型无法触发
7. **StackOverflow 无日志**: 栈溢出时信号处理器无法正常工作，这是正常现象

### 崩溃测试结果分析

**典型测试结果**：
- **成功触发的崩溃类型**: 通常只有 2-3 种（NullPtr、Abort 等）
- **无法触发的崩溃类型**: 4-5 种（DanglingPtr、OutOfBounds、DivZero、BadCast 等）
- **部分触发的崩溃类型**: StackOverflow（崩溃但无日志）

**这是正常现象**，原因：
1. 现代编译器的安全优化
2. 操作系统的保护机制
3. 硬件级别的内存保护

### 调试技巧

1. 查看构建日志：`sh mk/build.sh 2>&1 | tee build.log`
2. 检查库文件：`ls -la lib/`
3. 验证崩溃测试：`./build/crash_test 1`
4. 查看崩溃日志：`cat logs/crash_log.txt`
5. 分析崩溃日志：使用仓库主 CLI（`cli/main.py`）或直接查看 `logs/mac/*.crash`

## 📄 许可证

本仓库软件采用 **Apache License 2.0**；贡献需遵循 **DCO**。详见仓库根目录的 `LICENSE`、`NOTICE` 与 `CONTRIBUTING.md`。
