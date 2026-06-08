# demo_multithread 多线程崩溃示例

`demo_multithread` 用于验证多线程相关崩溃的解析与分析能力，覆盖竞态、死锁、原子操作失败等典型场景。

## 目录结构

```text
examples/crash_cases/demo_multithread/
├── demo_mtd1/   # 复杂数据损坏场景
├── demo_mtd2/   # 数据处理错误场景
├── demo_mtd3/   # 基础多线程崩溃场景
└── README.md    # 就地入口（跳转到本文件）
```

## 场景说明

### demo_mtd1

- 崩溃类型：`MultiThreadDataCorruption`
- 信号：`SIGSEGV`
- 特征：共享结构并发访问、无锁竞态、内存损坏

### demo_mtd2

- 崩溃类型：`DataProcessingError`
- 信号：`SIGSEGV`
- 特征：链表/指针处理错误、并发写入导致访问异常

### demo_mtd3

- 覆盖 4 类基础多线程问题：
  - `RaceCondition`
  - `Deadlock`（通常表现为 `SIGABRT`）
  - `AtomicFail`（通常表现为 `SIGSEGV`）
  - `DoubleLock`（通常表现为 `SIGSEGV`）

## 构建与运行

从仓库根目录执行：

```bash
# 按场景单独构建（若该场景含 mk/build.sh）
sh examples/crash_cases/demo_multithread/demo_mtd1/mk/build.sh
sh examples/crash_cases/demo_multithread/demo_mtd2/mk/build.sh
sh examples/crash_cases/demo_multithread/demo_mtd3/mk/build.sh
```

运行时请按各子目录说明执行对应二进制，日志通常输出到：

- `examples/crash_cases/demo_multithread/demo_mtd1/log/mac/`
- `examples/crash_cases/demo_multithread/demo_mtd2/log/mac/`
- `examples/crash_cases/demo_multithread/demo_mtd3/log/mac/`

## 用 CLI 分析示例日志

```bash
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_multithread/demo_mtd3/log/mac/AtomicFail_SIGSEGV_2026-04-08_10-56-54.crash \
  --library-dir examples/crash_cases/demo_multithread/demo_mtd3/lib
```

如果你只想验证工具链，可加 `--scope gen_prompt_only`。

## 相关文档

- `docs/crash_cases/demo_basic/README.md`
- `docs/cli/CLI_GUIDE.md`
