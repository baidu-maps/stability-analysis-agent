# AI 全流程代码回归

该框架运行**真实** Crash Agent 全流程（`scope=full`、自动改码），并以最终落盘源码是否匹配期望 patch 作为结论。不校验 LLM 自然语言输出。

## 数据归属

| 路径 | 作用 |
|------|------|
| `examples/crash_cases/<case>/` | 日志、符号库、待修复源码（唯一数据源） |
| `test/ai_regression/cases/` | Case 定义：输入路径、允许修改文件、运行参数 |
| `test/ai_regression/expected/` | 期望 patch（非第二份完整源码树） |
| `test/ai_regression/results/<batch>/` | 批次结果（机器可读 + 失败 diff） |

Agent 仅修改运行时**临时副本**，不会改写 `examples/`。

## 运行

### CLI 入口（默认发布回归）

```bash
python3 scripts/run_ai_regression.py \
  --case test/ai_regression/cases/demo_basic_nullptr.json
```

### Daemon 入口（Web 壳同源链路）

与浏览器面板相同：`daemon` 接收请求后子进程执行 `cli/main.py`。

```bash
python3 scripts/run_ai_regression.py \
  --case test/ai_regression/cases/demo_basic_nullptr.json \
  --entrypoint daemon
```

两种入口**共用同一套 Case 与期望 patch**，不维护重复用例。`result.json` / `batch_summary.json` 含 `entrypoint` 字段便于区分。

## 通过条件

1. `cli/main.py` 全流程成功退出。
2. `applied_ai_fixes.success=true`。
3. 实际修改文件集合等于期望 patch 的修改文件集合。
4. 未修改 `allowed_changed_files` 之外的文件。
5. 归一化行尾与尾随空白后，最终源码与期望一致。

## 产物说明

- CLI 完整报告仍写入标准目录：`reports/<timestamp>_analysis_<scope>_.../`
- 回归框架在 `test/ai_regression/results/<batch>/` 仅保留精简物：
  - `batch_summary.json` — 批次统计
  - `<case>_<attempt>/result.json` — 单次状态、报告目录、文件比较
  - `<case>_<attempt>/<case>.diff` — **仅失败时**保存源码差异

`--keep-workspace` 可在结果目录保留 Agent 修改后的临时源码，便于排查。

## 批量运行

```bash
python3 scripts/run_ai_regression.py \
  --suite test/ai_regression/cases \
  --repetitions 3 \
  --stop-on-failure
```

## 单元测试（模拟 Agent，无 LLM）

```bash
python3 -m unittest test.ai_regression.test_runner -v
```

验证：正确修复、错误代码、越界修改三种比较器行为。

## 建议执行频率

| 时机 | 测试 |
|------|------|
| 每次提交 | [提交前确定性套件](./README.md) |
| Python 包发布 | 确定性 + 默认 `cli` AI 回归 |
| Web / daemon 发布 | 确定性 + `cli` AI 回归 + `--entrypoint daemon` |

索引：[README.md](./README.md)
