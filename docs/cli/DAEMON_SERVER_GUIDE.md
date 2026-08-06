# Daemon 服务指南（daemon/server.py）

本地 HTTP daemon，提供任务生命周期管理与 SSE 流式事件推送，同时内置 Tool System 直连端点。

## 启动

```bash
# 默认监听 127.0.0.1:8765
python3 daemon/server.py

# 自定义地址和端口
python3 daemon/server.py --host 0.0.0.0 --port 8765
```

启动后输出：
```
daemon listening on http://127.0.0.1:8765 (protocol=1)
  Run API:         POST /runs  GET /runs/<id>  GET /runs/<id>/events  POST /runs/<id>/cancel
  Tool System API: POST /tool-system/analyze  GET /tool-system/tools  GET /tool-system/workflows
```

---

## API 端点

### 公共

#### `GET /health`
健康检查。

**响应：**
```json
{ "ok": true, "protocol_version": "1", "pid": 12345 }
```

---

### Run API（子进程执行模式）

daemon 将任务分发给 `cli/main.py` 子进程执行，通过 SSE 流式推送事件。

#### `POST /runs`
提交一个分析任务。

**请求体（RunRequest）：**
```json
{
  "crash_log": "/path/to/crash.crash",
  "crash_log_content": "...",
  "library_dir": "/path/to/lib",
  "code_roots": ["/path/to/src"],
  "output_format": "markdown",
  "engine": "direct",
  "scope": "full",
  "prompt_mode": "analysis",
  "agent_loop": null,
  "max_agent_rounds": 1,
  "max_context_requests_per_round": 5,
  "streaming": false
}
```
- `crash_log` 与 `crash_log_content` 二选一；`crash_log_content` 通过 stdin 传入
- `engine`：`direct`（默认）/ `langchain` / `langgraph`
- `output_format`：`markdown`（默认）/ `json` / `text`
- `scope`：`full`（默认）/ `gen_prompt_only` / `parse_stack_only` / `parse_log_only`，控制 Agent 执行流程范围（详见 [CLI 参考](./CLI_COMMANDS_REFERENCE.md#--scope-取值)）
- `prompt_mode`：`analysis`（默认）/ `fix`，控制 `06_ai_prompt.md` / LLM 输入偏证据分析还是偏补丁输出；不控制是否自动应用修复（详见 [CLI 参考](./CLI_COMMANDS_REFERENCE.md#--prompt-mode-取值)）
- `agent_loop`：`null`（省略或传 `null` 时随 `prompt_mode`：`analysis`→`context_loop`，其它→`single`）/ `single` / `context_loop`，控制是否允许模型请求补充函数源码后继续多轮分析；独立于 `engine`（详见 [CLI 参考](./CLI_COMMANDS_REFERENCE.md#--agent-loop-取值)）

**响应：**
```json
{ "run_id": "20260413-153012-a1b2c3d4" }
```

---

#### `GET /runs/<run_id>`
查询任务状态。

**响应：**
```json
{
  "run_id": "...",
  "status": "running",
  "created_at": 1718000000.0,
  "started_at": 1718000001.0,
  "finished_at": null,
  "exit_code": null,
  "error": null,
  "output_format": "markdown"
}
```
`status` 取值：`queued` / `running` / `done` / `error` / `canceled`

---

#### `GET /runs/<run_id>/events`
SSE 流式订阅任务事件（`text/event-stream`）。

**事件格式：**
```
data: {"run_id": "...", "type": "stdout", "data": {"chunk": "..."}, "ts": 1718000002.0}
```

常见事件类型：

| type | 说明 |
|------|------|
| `run_started` | 任务开始执行 |
| `process_spawn` | 子进程已启动，含命令行 |
| `stdout` | 子进程标准输出块（`data.chunk`） |
| `stderr` | 子进程标准错误行（`data.line`） |
| `run_finished` | 任务结束（`data.status` / `data.exit_code`） |
| `run_canceled` | 任务已取消 |
| `artifact_written` | 产物已落盘（`data.path`） |
| `keepalive` | 保活心跳（每 1 秒一次） |

---

#### `GET /runs/<run_id>/result`
获取任务最终结果（任务完成后可用）。

**响应（RunResult）：**
```json
{
  "run_id": "...",
  "status": "done",
  "output_format": "markdown",
  "output": "# 崩溃分析摘要\n...",
  "error": null
}
```
任务仍在运行时返回 `202 Accepted` + `{"status": "running"}`。

---

#### `POST /runs/<run_id>/cancel`
取消正在运行的任务。

**响应：**
```json
{ "run_id": "...", "status": "canceled" }
```

---

### Tool System API（进程内直连模式）

直接在 daemon 进程内通过 `ConfigDrivenExecutor` 调用 Tool/Workflow，无需子进程，适合轻量调用。

> **延迟初始化**：首次调用任意 `/tool-system/*` 端点时才初始化 `ConfigDrivenExecutor`。

#### `POST /tool-system/analyze`
直接执行 `crash_analysis` Workflow（同步）。

**请求体：**
```json
{
  "crash_log": "...",
  "library_dir": "/path/to/lib",
  "code_roots": ["/path/to/src"]
}
```

**响应：** Workflow 返回的 JSON 结果。

---

#### `GET /tool-system/tools`
列出所有已注册的 Tool。

**响应：** `ConfigDrivenExecutor.list_active()` 的返回值。

---

#### `GET /tool-system/workflows`
列出所有已注册的 Workflow。

**响应：** `ConfigDrivenExecutor.list_active()` 的返回值。

---

## Run API vs Tool System API

| 维度 | Run API | Tool System API |
|------|---------|-----------------|
| 执行方式 | 子进程（`cli/main.py`） | 进程内直连 |
| 流式支持 | ✅ SSE 事件流 | ❌ 同步返回 |
| 取消支持 | ✅ | ❌ |
| 功能完整性 | 与 CLI 完全一致 | 仅 Tool/Workflow 层 |
| 适用场景 | 完整分析、长耗时任务 | 轻量调用、快速测试 |

---

## 相关文档

- [CLI 使用指南](./CLI_GUIDE.md)
- [Tool System 概览](../tools/tool_system/TOOL_SYSTEM_OVERVIEW.md)
- [协议模型](../tools/tool_system/TOOL_SYSTEM_OVERVIEW.md)
