# API Fault 诊断

`api_fault_diagnosis` 是统一的错误码/API/BusinessError 诊断入口，借鉴华为 `apifault-analysis` 的环境发现、知识库匹配和两阶段诊断设计，并复用当前 Agent 的 JS Crash、C++ Crash、AppFreeze 和 RAG 能力。

## CLI

```bash
python3 cli/api_fault.py \
  --error-code 5400105 \
  --error-name BusinessError \
  --message "media service died" \
  --api AVPlayer \
  --project-root examples/crash_cases/demo_basic/code_dir
```

也可以输入 JSON：

```json
{"error_code": "5400105", "error_name": "BusinessError", "message": "media service died", "api": "AVPlayer"}
```

## 输出

- 错误码原值、十进制归一值和格式
- 模块候选及证据
- 结构化知识匹配
- 项目 API 使用点
- `confirmed / probable / preliminary` 诊断状态
- 缺失证据和下一步问题
- `direct_fix`、`defensive_fix`、`verification` 修复建议

当前内置知识覆盖华为 multimedia player 错误码 `5400101`–`5400107` / `5411001`–`5411002`，以及 permission、parameter、network、database 和重复 reset 状态机。条目注册到 `tools.diagnosis.knowledge.default_registry`。不把原始 Markdown 知识库拼进 Prompt。

## Tool System

工具名：`api_fault_diagnosis`。

## 报告 sidecar

当 CLI 的 `parse_result` 或运行请求中存在 API 错误字段时，可保存为：

```text
04g_api_fault_diagnosis.json
```

API Fault 低置信度时只输出诊断和补证建议，默认不进入自动代码修复。

## 测试

```bash
python3 -B -m unittest test.tools.test_api_fault
```
