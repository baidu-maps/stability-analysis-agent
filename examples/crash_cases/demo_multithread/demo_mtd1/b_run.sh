#!/bin/bash

# 多线程数据损坏演示脚本
echo "=== 多线程数据损坏演示程序 ==="
echo "这个程序故意制造复杂的多线程竞态条件来触发内存损坏..."

# 设置错误时退出
set -e

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

echo "项目根目录: $PROJECT_ROOT"

# 创建构建目录
echo "创建构建目录..."
mkdir -p "$PROJECT_ROOT/build"
mkdir -p "$PROJECT_ROOT/log/mac"

# 进入构建目录
cd "$PROJECT_ROOT/build"

# 清理之前的构建
echo "清理之前的构建..."
rm -rf *

# 检查必要的工具
echo "检查构建工具..."

# 检查CMake
if ! command -v cmake &> /dev/null; then
    echo "错误: 未找到CMake命令"
    echo "请安装CMake:"
    echo "  - 使用Homebrew: brew install cmake"
    echo "  - 或从官网下载: https://cmake.org/download/"
    exit 1
fi

# 检查C++编译器
if ! command -v g++ &> /dev/null && ! command -v clang++ &> /dev/null; then
    echo "错误: 未找到C++编译器"
    echo "请安装Xcode命令行工具:"
    echo "  xcode-select --install"
    exit 1
fi

# 检查make
if ! command -v make &> /dev/null; then
    echo "错误: 未找到make命令"
    echo "请安装Xcode命令行工具:"
    echo "  xcode-select --install"
    exit 1
fi

echo "所有必要的构建工具已找到"

# 配置CMake项目
echo "配置CMake项目..."
cmake -DCMAKE_BUILD_TYPE=Debug \
      -DCMAKE_CXX_FLAGS_DEBUG="-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector" \
      -DCMAKE_C_FLAGS_DEBUG="-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector" \
      -DCMAKE_EXE_LINKER_FLAGS="-rdynamic -pthread" \
      -DCMAKE_SHARED_LINKER_FLAGS="-rdynamic -pthread" \
      "$PROJECT_ROOT"

# 编译项目
echo "编译项目..."
make -j$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

echo "编译完成！"

# 生成dSYM文件
echo "生成调试符号文件..."
if command -v dsymutil &> /dev/null; then
    # 为库文件生成dSYM
    if [ -f "$PROJECT_ROOT/lib/libmylib.dylib" ]; then
        echo "为库文件生成dSYM..."
        dsymutil "$PROJECT_ROOT/lib/libmylib.dylib" -o "$PROJECT_ROOT/lib/libmylib.dylib.dSYM"
    fi
    
    # 为可执行文件生成dSYM
    if [ -f "$PROJECT_ROOT/build/mtd_crash_test" ]; then
        echo "为可执行文件生成dSYM..."
        dsymutil "$PROJECT_ROOT/build/mtd_crash_test" -o "$PROJECT_ROOT/build/mtd_crash_test.dSYM"
    fi
else
    echo "警告: dsymutil不可用，跳过dSYM生成"
fi

# 检查可执行文件和库文件是否生成
EXEC_PATH="$PROJECT_ROOT/build/mtd_crash_test"
LIB_PATH="$PROJECT_ROOT/lib/libmylib.dylib"

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

# 确保目标日志目录存在
echo "准备运行多线程数据损坏测试..."
mkdir -p "$PROJECT_ROOT/log/mac"

# 运行程序
echo "开始运行多线程数据损坏测试..."
echo "注意: 这个程序故意制造内存损坏，可能会崩溃并生成崩溃日志"
echo ""

# 切换到项目根目录运行程序
cd "$PROJECT_ROOT"

# 运行程序并捕获结果
if "$EXEC_PATH" > /dev/null 2>&1; then
    echo "程序正常结束（未发生崩溃）"
else
    echo "程序发生崩溃（这是预期的）"
fi

# 检查生成的崩溃日志
echo ""
echo "检查生成的崩溃日志..."

# 显示生成的日志文件
echo "生成的crash日志文件:"
ls -la "$PROJECT_ROOT/log/mac/" | grep "MultiThreadDataCorruption" || echo "无多线程数据损坏崩溃日志"

# 显示最新的崩溃日志内容
LATEST_CRASH_LOG=$(ls -t "$PROJECT_ROOT/log/mac/"*MultiThreadDataCorruption*.crash 2>/dev/null | head -1)
if [ -n "$LATEST_CRASH_LOG" ]; then
    echo ""
    echo "最新的崩溃日志内容:"
    echo "===================="
    cat "$LATEST_CRASH_LOG"
    echo "===================="
fi

echo ""
echo "=== 构建和运行完成 ==="
echo "可执行文件位置: $EXEC_PATH"
echo "库文件位置: $LIB_PATH"
echo "崩溃日志位置: $PROJECT_ROOT/log/mac/"

# 显示程序说明
echo ""
echo "=== 程序说明 ==="
echo "这个程序故意制造了以下复杂的内存损坏场景："
echo "1. 多线程竞态条件：8个工作线程同时操作共享数据结构"
echo "2. 悬空指针：在删除节点时故意延迟释放内存"
echo "3. 数组越界：故意不检查边界就访问数组元素"
echo "4. 内存损坏：使用reinterpret_cast强制类型转换"
echo "5. 内存压力：持续分配和释放大量内存"
echo "6. 无锁访问：故意在关键区域不加锁"
echo ""
echo "这些技术使得程序很难被现代编译器和操作系统优化掉，"
echo "能够稳定地触发内存损坏并生成崩溃日志。"
