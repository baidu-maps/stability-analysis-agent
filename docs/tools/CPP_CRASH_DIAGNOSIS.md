# C/C++ Crash 诊断

`cpp_crash_diagnosis` 建立在现有 Native Crash parser、符号化和地址模式分析之上，提供证据优先的结构化诊断。它不重新解析日志，也不替换当前 Crash Agent 的自动修复主流程。

## CLI

```bash
python3 cli/cpp_crash.py \
  reports/<run>/01_crash_log_parser.json \
  --output reports/<run>/04c_cpp_crash_diagnosis.json
```

## 诊断内容

- Signal、signal code、fault address
- 寄存器、memory-near 和 Native 栈证据
- 复用 `address_pattern_analyzer` 的空指针、低地址和 poison 地址判断
- Signal → si_code 三级分类（参照华为 cppcrash `fault_mode.md`）
- 调用栈分层：崩溃帧 / 第一非运行时调用方 / 第一应用帧
- 历史特征提示：JS OOM、跨线程 env、ASCII 踩踏、libuv、sqlite BUS、GWP-ASan、Scudo/jemalloc 堆 abort、未捕获异常
- `CPP-FM-*` 故障模式：空指针、野指针/Use-after-free、权限错误、abort/assert、堆分配器损坏（CPP-FM-15）、N-API 边界、检测器报告等
- 证据等级：detector > register > address > pattern
- `confirmed / probable / preliminary` 诊断状态
- 缺失证据和后续检查：ASan/HWASan、BuildID/ABI、反汇编、源码条件核对
- 分层修复建议：`direct_fix`、`defensive_fix`、`verification`

## Tool System

工具名：`cpp_crash_diagnosis`。输入可以是现有 `parse_result`，也可以是包含 `crash_info`、`registers`、`threads`、`raw_content` 的对象。

建议将结果保存为 `04c_cpp_crash_diagnosis.json` sidecar，再由现有 diagnosis module 选择性生成 `prompt_section_zh`。不应把整份参考知识库或原始日志重复拼入主 Prompt。

## 测试

```bash
python3 -B -m unittest test.tools.test_cpp_crash
```
