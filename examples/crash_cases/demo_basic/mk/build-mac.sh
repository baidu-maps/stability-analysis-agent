#!/bin/bash

# 设置错误时退出
set -e

echo "=== Stability Analysis Agent - Mac Build Script ==="
echo "开始构建mac平台版本..."

# 执行bash_profile以设置环境变量
#echo "正在加载环境配置..."
#source ~/.bash_profile

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "脚本目录: $SCRIPT_DIR"
echo "项目根目录: $PROJECT_ROOT"

# 创建必要的目录
echo "创建构建目录..."
mkdir -p "$PROJECT_ROOT/build"
mkdir -p "$PROJECT_ROOT/lib/mac"
mkdir -p "$PROJECT_ROOT/logs/mac"

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

# 函数：增强crash日志，添加符号解析
enhance_crash_log() {
    local crash_file="$1"
    local executable_path="$2"
    local library_path="$3"
    
    # 检查对应的dSYM文件
    local executable_dsym="${executable_path}.dSYM"
    local library_dsym="${library_path}.dSYM"
    
    if [ ! -f "$crash_file" ]; then
        return
    fi
    
    echo "增强crash日志: $crash_file"
    
    # 创建增强版本的日志文件
    local enhanced_file="${crash_file%.crash}_enhanced.crash"
    
    # 检查addr2line是否可用
    if ! command -v addr2line &> /dev/null; then
        echo "警告: addr2line不可用，跳过符号解析"
        return
    fi
    
    # 解析堆栈地址
    {
        echo "=== 增强崩溃报告 ==="
        echo "原始文件: $crash_file"
        echo "解析时间: $(date)"
        echo ""
        
        # 读取原始文件并解析地址
        while IFS= read -r line; do
            if [[ $line =~ ^#[0-9]+\ +0x[0-9a-f]+\ + ]]; then
                # 这是一个堆栈帧行，提取地址
                local frame_num=$(echo "$line" | sed -n 's/^#\([0-9]*\).*/\1/p')
                local address=$(echo "$line" | sed -n 's/.*0x\([0-9a-f]*\).*/\1/p')
                local module=$(echo "$line" | awk '{print $3}')
                
                echo "$line"
                
                # 根据模块选择可执行文件进行符号解析
                local target_file=""
                local target_dsym=""
                if [[ "$module" == "crash_test" ]]; then
                    target_file="$executable_path"
                    target_dsym="$executable_dsym"
                elif [[ "$module" == "libmylib.dylib" ]]; then
                    target_file="$library_path"
                    target_dsym="$library_dsym"
                fi
                
                # 特殊处理：如果是第一个堆栈帧（崩溃地址），标记为崩溃点
                if [ "$frame_num" = "0" ]; then
                    echo "    -> [崩溃点] $line"
                else
                    echo "$line"
                fi
                
                if [ -n "$target_file" ] && [ -f "$target_file" ]; then
                    # 优先使用dSYM文件进行符号解析
                    local symbol_info=""
                    if [ -d "$target_dsym" ]; then
                        # 使用dSYM文件
                        symbol_info=$(xcrun dwarfdump --lookup "$address" "$target_dsym" 2>/dev/null | grep -E "(DW_TAG_subprogram|DW_AT_name|DW_AT_decl_file|DW_AT_decl_line)" | head -10)
                    else
                        # 回退到addr2line
                        symbol_info=$(addr2line -e "$target_file" -f -C "0x$address" 2>/dev/null)
                    fi
                    
                    if [ $? -eq 0 ] && [ -n "$symbol_info" ]; then
                        if [ -d "$target_dsym" ]; then
                            echo "    -> 使用dSYM解析: $symbol_info"
                        else
                            echo "    -> 函数: $(echo "$symbol_info" | head -1)"
                            echo "    -> 位置: $(echo "$symbol_info" | tail -1)"
                        fi
                    fi
                fi
            else
                echo "$line"
            fi
        done < "$crash_file"
        
        echo ""
        echo "=== 符号解析信息 ==="
        echo "可执行文件: $executable_path"
        echo "库文件: $library_path"
        echo "可执行文件dSYM: $executable_dsym"
        echo "库文件dSYM: $library_dsym"
        echo "解析工具: dwarfdump (dSYM) / addr2line (回退)"
        
    } > "$enhanced_file"
    
    echo "✅ 增强日志已保存: $enhanced_file"
}

# 配置CMake项目，启用调试符号
echo "配置CMake项目..."
cmake -DCMAKE_BUILD_TYPE=Debug \
      -DCMAKE_CXX_FLAGS_DEBUG="-g -O0 -rdynamic" \
      -DCMAKE_C_FLAGS_DEBUG="-g -O0 -rdynamic" \
      -DCMAKE_EXE_LINKER_FLAGS="-rdynamic" \
      -DCMAKE_SHARED_LINKER_FLAGS="-rdynamic" \
      -DCMAKE_BUILD_WITH_INSTALL_RPATH=TRUE \
      -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=TRUE \
      "$SCRIPT_DIR"

# 编译项目
echo "编译项目..."
make -j$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

echo "编译完成！"

# 生成dSYM文件
echo "生成调试符号文件..."
if command -v dsymutil &> /dev/null; then
    # 为库文件生成dSYM
    if [ -f "$PROJECT_ROOT/lib/mac/libmylib.dylib" ]; then
        echo "为库文件生成dSYM..."
        dsymutil "$PROJECT_ROOT/lib/mac/libmylib.dylib" -o "$PROJECT_ROOT/lib/mac/libmylib.dylib.dSYM"
    fi
    
    # 为可执行文件生成dSYM
    if [ -f "$PROJECT_ROOT/build/desktop/crash_test" ]; then
        echo "为可执行文件生成dSYM..."
        dsymutil "$PROJECT_ROOT/build/desktop/crash_test" -o "$PROJECT_ROOT/build/desktop/crash_test.dSYM"
    fi
else
    echo "警告: dsymutil不可用，跳过dSYM生成"
fi

# 检查库文件是否生成
LIB_PATH="$PROJECT_ROOT/lib/mac/libmylib.dylib"
if [ ! -f "$LIB_PATH" ]; then
    echo "错误: 库文件未生成: $LIB_PATH"
    exit 1
fi

echo "库文件已生成: $LIB_PATH"

# 检查可执行文件是否生成
EXEC_PATH="$PROJECT_ROOT/build/desktop/crash_test"
if [ ! -f "$EXEC_PATH" ]; then
    echo "错误: 可执行文件未生成: $EXEC_PATH"
    exit 1
fi

echo "可执行文件已生成: $EXEC_PATH"

# 确保目标日志目录存在
echo "准备生成crash日志..."
mkdir -p "$PROJECT_ROOT/logs/mac"

# 定义crash类型数组
crash_types=(
    "1"  # NullPtr
    "2"  # DanglingPtr
    "3"  # OutOfBounds
    "4"  # DivZero
    "5"  # BadCast
    "6"  # StackOverflow
    "7"  # Abort
    "8"  # SigBus
    "9"  # SigIll
    "10" # DoubleFree
    "11" # NullFuncPtr
)

# 为每种crash类型生成日志
for crash_id in "${crash_types[@]}"; do
    echo "执行crash测试 $crash_id..."
    
    # 切换到项目根目录运行crash测试，确保日志输出到正确位置
    cd "$PROJECT_ROOT"
    if "$EXEC_PATH" "$crash_id" > /dev/null 2>&1; then
        echo "警告: crash测试 $crash_id 没有产生预期的崩溃"
    else
        echo "crash测试 $crash_id 执行完成"
    fi
    cd "$PROJECT_ROOT/build"
done

# 增强所有生成的crash日志
echo "增强crash日志..."
for crash_file in "$PROJECT_ROOT/logs/mac"/*.crash; do
    if [ -f "$crash_file" ]; then
        enhance_crash_log "$crash_file" "$EXEC_PATH" "$LIB_PATH"
    fi
done

# 不再从 DiagnosticReports 复制 .ips：与本项目自定义 .crash 格式不一致，
# Stability Analysis Agent 标准输入为 logs/mac 下由 signal_handler 生成的 .crash。
# 若需系统报告，请用户自行从 ~/Library/Logs/DiagnosticReports 打开。
echo "跳过复制系统 .ips（与 tools 解析链路不一致，避免污染 logs/mac）"



echo "=== 构建完成 ==="
echo "库文件位置: $LIB_PATH"
echo "库文件dSYM位置: ${LIB_PATH}.dSYM"
echo "可执行文件位置: $EXEC_PATH"
echo "可执行文件dSYM位置: ${EXEC_PATH}.dSYM"
echo "Crash日志位置: $PROJECT_ROOT/logs/mac/"

# 显示生成的日志文件
echo ""
echo "生成的crash日志文件:"
ls -la "$PROJECT_ROOT/logs/mac/"

echo ""
echo "增强日志文件 (包含符号解析):"
ls -la "$PROJECT_ROOT/logs/mac/" | grep -E '_enhanced\.crash$' || echo "无增强日志文件"

echo ""
echo "构建脚本执行完成！"
