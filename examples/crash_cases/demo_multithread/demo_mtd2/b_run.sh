#!/bin/bash

echo "=== 数据处理系统构建脚本 ==="
echo "正在构建数据处理引擎..."

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

echo "项目根目录: $PROJECT_ROOT"

echo "创建构建目录..."
mkdir -p "$PROJECT_ROOT/build"
mkdir -p "$PROJECT_ROOT/log/mac"

cd "$PROJECT_ROOT/build"

echo "清理之前的构建..."
rm -rf *

echo "检查构建工具..."

if ! command -v cmake &> /dev/null; then
    echo "错误: 未找到CMake命令"
    echo "请安装CMake:"
    echo "  - 使用Homebrew: brew install cmake"
    echo "  - 或从官网下载: https://cmake.org/download/"
    exit 1
fi

if ! command -v g++ &> /dev/null && ! command -v clang++ &> /dev/null; then
    echo "错误: 未找到C++编译器"
    echo "请安装Xcode命令行工具:"
    echo "  xcode-select --install"
    exit 1
fi

if ! command -v make &> /dev/null; then
    echo "错误: 未找到make命令"
    echo "请安装Xcode命令行工具:"
    echo "  xcode-select --install"
    exit 1
fi

echo "所有必要的构建工具已找到"

echo "配置CMake项目..."
cmake -DCMAKE_BUILD_TYPE=Debug \
      -DCMAKE_CXX_FLAGS_DEBUG="-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector" \
      -DCMAKE_C_FLAGS_DEBUG="-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector" \
      -DCMAKE_EXE_LINKER_FLAGS="-rdynamic -pthread" \
      -DCMAKE_SHARED_LINKER_FLAGS="-rdynamic -pthread" \
      "$PROJECT_ROOT"

echo "编译项目..."
make -j$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

echo "编译完成！"

echo "生成调试符号文件..."
if command -v dsymutil &> /dev/null; then
    if [ -f "$PROJECT_ROOT/lib/libdatamanager.dylib" ]; then
        echo "为库文件生成dSYM..."
        dsymutil "$PROJECT_ROOT/lib/libdatamanager.dylib" -o "$PROJECT_ROOT/lib/libdatamanager.dylib.dSYM"
    fi
    
    if [ -f "$PROJECT_ROOT/build/data_processor" ]; then
        echo "为可执行文件生成dSYM..."
        dsymutil "$PROJECT_ROOT/build/data_processor" -o "$PROJECT_ROOT/build/data_processor.dSYM"
    fi
else
    echo "警告: dsymutil不可用，跳过dSYM生成"
fi

EXEC_PATH="$PROJECT_ROOT/build/data_processor"
LIB_PATH="$PROJECT_ROOT/lib/libdatamanager.dylib"

if [ ! -f "$EXEC_PATH" ]; then
    echo "错误: 可执行文件未生成: $EXEC_PATH"
    exit 1
fi

if [ ! -f "$LIB_PATH" ]; then
    echo "错误: 库文件未生成: $LIB_PATH"
    exit 1
fi

echo "可执行文件已生成: $EXEC_PATH"
echo "库文件已生成: $LIB_PATH"

echo "准备运行数据处理系统..."
mkdir -p "$PROJECT_ROOT/log/mac"

echo "开始运行数据处理系统..."
echo "注意: 系统正在处理大量数据，可能会遇到性能问题"
echo ""

cd "$PROJECT_ROOT"

if "$EXEC_PATH" > /dev/null 2>&1; then
    echo "数据处理系统正常结束"
else
    echo "数据处理系统遇到错误（这是可能的）"
fi

echo ""
echo "检查生成的错误日志..."

echo "生成的错误日志文件:"
ls -la "$PROJECT_ROOT/log/mac/" | grep "DataProcessingError" || echo "无数据处理错误日志"

LATEST_CRASH_LOG=$(ls -t "$PROJECT_ROOT/log/mac/"*DataProcessingError*.crash 2>/dev/null | head -1)
if [ -n "$LATEST_CRASH_LOG" ]; then
    echo ""
    echo "最新的错误日志内容:"
    echo "===================="
    cat "$LATEST_CRASH_LOG"
    echo "===================="
fi

echo ""
echo "=== 构建和运行完成 ==="
echo "可执行文件位置: $EXEC_PATH"
echo "库文件位置: $LIB_PATH"
echo "错误日志位置: $PROJECT_ROOT/log/mac/"

echo ""
echo "=== 系统说明 ==="
echo "这个数据处理系统具有以下特点："
echo "1. 多线程数据处理：8个工作线程同时处理数据"
echo "2. 内存管理：自动清理和优化内存使用"
echo "3. 数据同步：实时同步和更新数据"
echo "4. 错误处理：完善的错误日志和崩溃处理"
echo "5. 性能优化：针对大数据量处理进行优化"
echo ""
echo "系统设计用于处理大量并发数据，"
echo "能够稳定地处理各种数据操作并记录错误信息。"
