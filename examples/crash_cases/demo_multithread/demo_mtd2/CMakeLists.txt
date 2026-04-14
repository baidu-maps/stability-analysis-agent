cmake_minimum_required(VERSION 3.10)
project(DataProcessingSystem)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(CMAKE_CXX_FLAGS_DEBUG "-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector")
set(CMAKE_C_FLAGS_DEBUG "-g -O0 -rdynamic -fno-optimize-sibling-calls -fno-stack-protector")

set(CMAKE_EXE_LINKER_FLAGS "-rdynamic -pthread")
set(CMAKE_SHARED_LINKER_FLAGS "-rdynamic -pthread")

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/src)

add_library(datamanager SHARED src/mylib.cpp)

set_target_properties(datamanager PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/lib
    OUTPUT_NAME "datamanager"
    PREFIX "lib"
    SUFFIX ".dylib"
)

add_executable(data_processor src/main.cpp)

target_link_libraries(data_processor datamanager pthread)

set_target_properties(data_processor PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
)

target_compile_definitions(data_processor PRIVATE
    _REENTRANT
    _THREAD_SAFE
)

target_compile_definitions(datamanager PRIVATE
    _REENTRANT
    _THREAD_SAFE
)

target_compile_options(data_processor PRIVATE
    -Wall
    -Wextra
    -Wno-unused-parameter
    -Wno-unused-variable
    -Wno-unused-function
)

target_compile_options(datamanager PRIVATE
    -Wall
    -Wextra
    -Wno-unused-parameter
    -Wno-unused-variable
    -Wno-unused-function
)

if(APPLE)
    target_compile_options(data_processor PRIVATE
        -fno-omit-frame-pointer
        -fno-inline
    )
    target_compile_options(datamanager PRIVATE
        -fno-omit-frame-pointer
        -fno-inline
    )
elseif(UNIX)
    target_compile_options(data_processor PRIVATE
        -fno-omit-frame-pointer
        -fno-inline
    )
    target_compile_options(datamanager PRIVATE
        -fno-omit-frame-pointer
        -fno-inline
    )
endif()
