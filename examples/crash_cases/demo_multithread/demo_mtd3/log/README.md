# Crash Logs 目录说明

本目录包含用于测试 Stability Analysis Agent 的多线程崩溃场景日志。

## 目录结构

```
logs/mac/
├── AtomicFail_SIGSEGV_*.crash     # CAS操作失败崩溃
├── Deadlock_SIGABRT_*.crash        # 死锁崩溃
└── DoubleLock_SIGSEGV_*.crash      # 竞态条件崩溃
```

## 崩溃场景与日志文件对应表

| case_id | 崩溃类型 | 信号 | 触发代码位置 | 说明 |
|---------|---------|------|-------------|------|
| 1 | RaceCondition | - | `mylib.cpp:race_condition_*()` | 读写竞态，测试期间未触发崩溃 |
| 2 | Deadlock | SIGABRT | `mylib.cpp:trigger_deadlock()` | 死锁导致abort()调用 |
| 3 | AtomicFail | SIGSEGV | `mylib.cpp:cas_operation()` | CAS失败后访问无效内存 |
| 4 | DoubleLock | SIGSEGV | `mylib.cpp:double_lock_operation()` | 竞态条件导致数据不一致 |

## 触发方式

```bash
cd demo_multithread/demo_mtd3
DYLD_LIBRARY_PATH=./lib ./build/mtd3_crash_test <case_id>
```

| case_id | 崩溃类型 |
|--------|---------|
| 1 | RaceCondition (读写竞态) |
| 2 | Deadlock (死锁) |
| 3 | AtomicFail (CAS操作失败) |
| 4 | DoubleLock (竞态条件) |

## 日志格式说明

崩溃日志采用自定义格式，包含以下部分：

```
=== 多线程崩溃报告 ===
时间: YYYY-MM-DD_HH-MM-SS
平台: mac
崩溃类型: CRASH_TYPE_SIGNAL
进程ID: PID
崩溃地址: 0xADDRESS

=== 模块基址信息 ===
libmylib.dylib基址: 0x...
libsystem_pthread.dylib基址: 0x...

=== 堆栈跟踪 ===
#N  ADDRESS  INDEX  LIBRARY  SYMBOL

=== 系统信息 ===
编译时间: DATE
编译器: COMPILER
```