#ifndef MY_LIB_H
#define MY_LIB_H

#pragma once
#include <string>

// 崩溃类型枚举
enum class CrashType {
    NullPtr = 1,
    DanglingPtr = 2,
    OutOfBounds = 3,
    DivZero = 4,
    BadCast = 5,
    StackOverflow = 6,
    Abort = 7,
    SigBus = 8,
    SigIll = 9,
    DoubleFree = 10,
    NullFuncPtr = 11
};

// 崩溃函数声明
void crash_nullptr();
void crash_dangling();
void crash_oob();
void crash_divzero();
void crash_bad_cast();
void crash_stackoverflow();
void crash_abort();
void crash_sigbus();
void crash_sigill();
void crash_double_free();
void crash_null_func_ptr();

// 工具函数
std::string crash_type_name(CrashType type);

// 堆栈捕获函数
void setup_crash_handler();
void capture_stack_trace(const std::string& crash_type);
void set_current_crash_type(const std::string& crash_type);
std::string get_platform_name();

#endif // MY_LIB_H
