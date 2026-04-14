cmake_minimum_required(VERSION 3.10)
project(MultiThreadDataCorruption)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 设置编译选项，确保能够触发崩溃
set(CMAKE_CXX_FLAGS_DEBUG "-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector")
set(CMAKE_C_FLAGS_DEBUG "-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector")

# 设置链接选项
set(CMAKE_EXE_LINKER_FLAGS "-rdynamic -pthread")
set(CMAKE_SHARED_LINKER_FLAGS "-rdynamic -pthread")

# 设置包含目录
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/src)

# 创建共享库
add_library(mylib SHARED src/mylib.cpp)

# 设置库的输出目录
set_target_properties(mylib PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/lib
    OUTPUT_NAME "mylib"
    PREFIX "lib"
    SUFFIX ".dylib"
)

# 添加可执行文件
add_executable(mtd_crash_test src/main.cpp)

# 链接库和pthread
target_link_libraries(mtd_crash_test mylib pthread)

# 设置输出目录
set_target_properties(mtd_crash_test PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
)

# 添加编译定义
target_compile_definitions(mtd_crash_test PRIVATE
    _REENTRANT
    _THREAD_SAFE
)

target_compile_definitions(mylib PRIVATE
    _REENTRANT
    _THREAD_SAFE
)

# 添加编译选项
target_compile_options(mtd_crash_test PRIVATE
    -Wall
    -Wextra
    -Wno-unused-parameter
    -Wno-unused-variable
    -Wno-unused-function
)

target_compile_options(mylib PRIVATE
    -Wall
    -Wextra
    -Wno-unused-parameter
    -Wno-unused-variable
    -Wno-unused-function
)

# 针对不同平台的特殊设置
if(APPLE)
    target_compile_options(mtd_crash_test PRIVATE
        -fno-omit-frame-pointer
        -fno-inline
    )
    target_compile_options(mylib PRIVATE
        -fno-omit-frame-pointer
        -fno-inline
    )
elseif(UNIX)
    target_compile_options(mtd_crash_test PRIVATE
        -fno-omit-frame-pointer
        -fno-inline
    )
    target_compile_options(mylib PRIVATE
        -fno-omit-frame-pointer
        -fno-inline
    )
endif()
