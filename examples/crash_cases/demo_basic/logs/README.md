# Crash Logs 目录说明

本目录包含用于测试 Stability Analysis Agent 的各种崩溃场景日志。

## 目录结构

```
logs/mac/
├── _SIGSEGV_2025-08-25_22-39-33.crash  # 空指针崩溃 (NullPtr)
├── DanglingPtr_2026-04-07_11-09-30.crash   # 悬空指针崩溃
├── OutOfBounds_2026-04-07_11-09-31.crash   # 数组越界崩溃
├── DivZero_2026-04-07_11-09-32.crash       # 除零崩溃
├── BadCast_2026-04-07_11-09-33.crash       # 错误类型转换崩溃（历史样例）
├── StackOverflow_2026-04-07_11-09-34.crash # 栈溢出崩溃
├── Abort_2026-04-07_11-09-35.crash         # 主动终止崩溃
├── NullPtr_SIGSEGV_2026-04-08_10-43-08.crash
├── SigBus_SIGBUS_2026-04-08_10-43-08.crash
├── SigIll_SIGILL_2026-04-08_10-43-08.crash
├── DoubleFree_SIGABRT_2026-04-08_10-43-08.crash
└── NullFuncPtr_SIGSEGV_2026-04-08_10-43-08.crash
```

## 崩溃场景与日志文件对应表

| 日志文件 | 崩溃类型 | CrashType ID | 触发信号 | 触发代码位置 | 说明 |
|---------|---------|---------------|----------|--------------|------|
| `_SIGSEGV_2025-08-25_22-39-33.crash` | NullPtr | 1 | SIGSEGV | `my_lib.cpp:crash_nullptr()` | 空指针解引用，访问 nullptr 导致段错误 |
| `DanglingPtr_2026-04-07_11-09-30.crash` | DanglingPtr | 2 | SIGSEGV | `my_lib.cpp:crash_dangling()` | 悬空指针访问，delete 后继续访问已释放的内存 |
| `OutOfBounds_2026-04-07_11-09-31.crash` | OutOfBounds | 3 | SIGSEGV | `my_lib.cpp:crash_oob()` | 数组越界访问，vector 下标超出实际大小 |
| `DivZero_2026-04-07_11-09-32.crash` | DivZero | 4 | SIGFPE | `my_lib.cpp:crash_divzero()` | 除零操作，整数除以零导致浮点异常 |
| `BadCast_2026-04-07_11-09-33.crash` / `BadCast_SIGSEGV_*.crash` | BadCast | 5 | SIGSEGV | `my_lib.cpp:crash_bad_cast()` | 错误类型转换后未判空直接解引用（稳定触发） |
| `StackOverflow_2026-04-07_11-09-34.crash` | StackOverflow | 6 | SIGSEGV | `my_lib.cpp:crash_stackoverflow()` | 栈溢出，无限递归导致栈空间耗尽 |
| `Abort_2026-04-07_11-09-35.crash` | Abort | 7 | SIGABRT | `my_lib.cpp:crash_abort()` | 主动调用 abort() 终止进程 |
| `SigBus_SIGBUS_2026-04-08_10-43-08.crash` | SigBus | 8 | SIGBUS | `my_lib.cpp:crash_sigbus()` | 主动触发 SIGBUS，用于总线错误类场景回归 |
| `SigIll_SIGILL_2026-04-08_10-43-08.crash` | SigIll | 9 | SIGILL | `my_lib.cpp:crash_sigill()` | 主动触发 SIGILL，用于非法指令类场景回归 |
| `DoubleFree_SIGABRT_2026-04-08_10-43-08.crash` | DoubleFree | 10 | SIGABRT | `my_lib.cpp:crash_double_free()` | 重复释放同一堆内存（double free）导致进程中止 |
| `NullFuncPtr_SIGSEGV_2026-04-08_10-43-08.crash` | NullFuncPtr | 11 | SIGSEGV | `my_lib.cpp:crash_null_func_ptr()` | 调用空函数指针触发段错误 |

## 触发方式

使用 `crash_test` 可执行文件通过命令行参数指定崩溃类型：

```bash
cd build/desktop
DYLD_LIBRARY_PATH=../../lib/mac ./crash_test <case_id>
```

| case_id | 崩溃类型 |
|--------|---------|
| 1 | NullPtr (空指针) |
| 2 | DanglingPtr (悬空指针) |
| 3 | OutOfBounds (数组越界) |
| 4 | DivZero (除零) |
| 5 | BadCast (错误类型转换) |
| 6 | StackOverflow (栈溢出) |
| 7 | Abort (主动终止) |
| 8 | SigBus (总线错误) |
| 9 | SigIll (非法指令) |
| 10 | DoubleFree (重复释放) |
| 11 | NullFuncPtr (空函数指针调用) |

## 日志格式说明

本目录仅保存 `.crash` 格式的日志文件，这是 Stability Analysis Agent 的标准输入格式。

`.ips` 格式是 macOS 系统生成的原始崩溃报告，由于格式不兼容，已删除。

崩溃日志采用自定义格式，包含以下部分：

```
=== 崩溃报告 ===
时间: YYYY-MM-DD_HH-MM-SS
平台: mac
崩溃类型: _SIGNAL_NAME
进程ID: PID
崩溃地址: 0xADDRESS

=== 堆栈跟踪 ===
#N  ADDRESS  INDEX  LIBRARY  SYMBOL

=== 系统信息 ===
编译时间: DATE
编译器: COMPILER
```