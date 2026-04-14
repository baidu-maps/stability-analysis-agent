#!/bin/bash
# 构建脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "=== 开始构建 demo_mtd3 ==="

# 创建构建目录
mkdir -p build
cd build

# 运行cmake
cmake ..

# 编译
make -j$(sysctl -n hw.ncpu)

echo "=== 构建完成 ==="
echo "可执行文件: build/mtd3_crash_test"
echo "库文件: lib/libmylib.dylib"