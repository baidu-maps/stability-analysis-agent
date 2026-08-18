# JS/ArkTS Crash 诊断

`js_crash_diagnosis` 是建立在现有 Crash parser 输出之上的确定性诊断工具。它不会重新解析 Native Crash，也不会改变 `round_0/06_ai_prompt.md` 的组装逻辑。

## 输入

工具接受已有 `parse_result`，或者包含以下字段的兼容对象：

- `crash_info`
- `threads`
- `raw_content`
- `Reason / Error name / Error message / Error code` 的结构化字段

也可以分析 CLI 已生成的报告：

```bash
python3 cli/js_crash.py \
  reports/<run>/01_crash_log_parser.json \
  --output reports/<run>/04c_js_crash_diagnosis.json
```

## 诊断顺序

1. 优先读取 `Reason` 或 `Error name`。
2. 使用 `Error message` 正则细化三级故障模式。
3. 识别 ArkTS/JS 应用帧。
4. 从 Native 帧中识别 N-API、Ark runtime 等 HybridStack 桥接证据。
5. 输出 `confirmed / probable / preliminary` 状态、置信度和缺失证据。

当前内置模式覆盖 `TypeError`、`ReferenceError`、`SyntaxError`、`RangeError`、`URIError`、`OutOfMemoryError` 和 `BusinessError`。故障模式编号使用 `JSC-FM-*`，与 JS Heap 的 `JS-FM-*` 分开。

## Tool System

工具名：`js_crash_diagnosis`

```json
{
  "parse_result": {
    "crash_info": {},
    "threads": []
  }
}
```

主流程在解析结果像 JS/ArkTS 崩溃时会写入 `04h_js_crash_diagnosis.json` sidecar。需要进入 AI 上下文时，应通过现有 diagnosis module 的 `prompt_section_zh` 接口接入，而不是直接修改主 Prompt 骨架。

责任锚点取第一应用栈帧，跳过 `stateMgmt.js` / ArkUI / N-API 等框架帧。故障模式覆盖空值访问、装饰器/`super()`、ArrayBuffer detached、循环引用、N-API handle scope 和 OOM。

## 测试

```bash
python3 -B -m unittest test.tools.test_js_crash
```
