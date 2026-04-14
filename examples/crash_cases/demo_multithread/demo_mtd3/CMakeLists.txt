cmake_minimum_required(VERSION 3.10)
project(MultiThreadCrash3)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 设置编译选项
set(CMAKE_CXX_FLAGS_DEBUG "-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector")
set(CMAKE_C_FLAGS_DEBUG "-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector")

set(CMAKE_EXE_LINKER_FLAGS "-rdynamic -pthread")
set(CMAKE_SHARED_LINKER_FLAGS "-rdynamic -pthread")

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/src)

# 创建共享库
add_library(mylib SHARED src/mylib.cpp)

set_target_properties(mylib PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/lib
    OUTPUT_NAME "mylib"
    PREFIX "lib"
    SUFFIX ".dylib"
)

# 可执行文件
add_executable(mtd3_crash_test src/main.cpp)
target_link_libraries(mtd3_crash_test mylib pthread)

set_target_properties(mtd3_crash_test PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
)

target_compile_definitions(mtd3_crash_test PRIVATE
    _REENTRANT
    _THREAD_SAFE
)

target_compile_definitions(mylib PRIVATE
    _REENTRANT
    _THREAD_SAFE
)

target_compile_options(mtd3_crash_test PRIVATE
    -Wall -Wextra -Wno-unused-parameter -Wno-unused-variable -Wno-unused-function
    -fno-omit-frame-pointer -fno-inline
)

target_compile_options(mylib PRIVATE
    -Wall -Wextra -Wno-unused-parameter -Wno-unused-variable -Wno-unused-function
    -fno-omit-frame-pointer -fno-inline
)