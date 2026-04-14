cmake_minimum_required(VERSION 3.10)
project(CrashTest)

set(CMAKE_CXX_STANDARD 17)

# 设置common库的路径
set(COMMON_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../common)
set(COMMON_INCLUDE_DIR ${COMMON_DIR}/include)
set(COMMON_SRC_DIR ${COMMON_DIR}/src)

# 添加common库
add_library(mylib SHARED 
    ${COMMON_SRC_DIR}/my_lib.cpp
)

# 设置common库的属性
set_target_properties(mylib PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/../../lib/mac
    OUTPUT_NAME "mylib"
    PREFIX "lib"
    SUFFIX ".dylib"
    INCLUDE_DIRECTORIES ${COMMON_INCLUDE_DIR}
)

# 添加desktop可执行文件
add_executable(crash_test main.cpp)

# 链接common库
target_link_libraries(crash_test PRIVATE mylib)

# 设置包含目录
target_include_directories(crash_test PRIVATE ${COMMON_INCLUDE_DIR})
