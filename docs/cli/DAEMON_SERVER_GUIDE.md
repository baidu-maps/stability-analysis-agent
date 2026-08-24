# Daemon 服务指南（daemon/server.py）

本地 HTTP daemon，提供任务生命周期管理与 SSE 流式事件推送，同时内置 Tool System 直连端点。

## 启动

```bash
# 默认监听 127.0.0.1:8765
python3 daemon/server.py

# 自定义地址和端口
python3 daemon/server.py --host 0.0.0.0 --port 8765 --max-workers 2 --max-queue 32 \
  --shutdown-wait 90 --run-ttl 21600 --event-queue-max 256
```

启动后输出：
```
daemon listening on http://127.0.0.1:8765 (protocol=1)
  Web UI:         http://127.0.0.1:8765/
  Run API:         POST /runs  GET /runs  GET /runs/<id>  GET /runs/<id>/events  POST /runs/<id>/cancel
                   POST /runs/<id>/vector-db/commit
  Skills API:      GET /skills  GET /skills/<name>
                   POST /skills/install  POST /skills/lint  POST /skills/uninstall
  Tool System API: POST /tool-system/analyze  GET /tool-system/tools  GET /tool-system/workflows
```

浏览器打开 `/` 即可使用本地面板（详见 [WEB_UI_GUIDE.md](./WEB_UI_GUIDE.md)）。

---

## API 端点

### 公共

#### `GET /health`
健康检查。数字员工探活用此接口。

**响应：**
```json
{
  "ok": true,
  "service": "stability-analysis-agent",
  "protocol_version": "1",
  "pid": 12345,
  "web_ui": true,
  "queued": 0,
  "running": 1,
  "max_workers": 2,
  "max_queue": 32,
  "run_timeout_sec": 0,
  "shutting_down": false,
  "runs_retained": 3
}
```

并发：`--max-workers`（或环境变量 `STABILITY_AGENT_DAEMON_MAX_WORKERS`，默认 2）限制同时运行的 CLI 子进程；`--max-queue`（`STABILITY_AGENT_DAEMON_MAX_QUEUE`，默认 32）限制等待中的任务。队列满时 `POST /runs` 返回 **429** `queue_full`。`--run-timeout` 为 **0** 表示不限时；**非 0** 表示该秒数后终止 CLI 子进程。

优雅停机：`SIGTERM`/`SIGINT` 后 `POST /runs` 返回 **503** `shutting_down`，并取消 queued/running 任务，最多等待 `--shutdown-wait` 秒（默认 90，`STABILITY_AGENT_DAEMON_SHUTDOWN_WAIT_SEC`）。systemd 的 `TimeoutStopSec` 必须大于该值。

内存：每任务 SSE 队列默认最多 256 条（满则丢最旧，`--event-queue-max` / `STABILITY_AGENT_DAEMON_EVENT_QUEUE_MAX`）；结束态任务默认保留 6 小时后从内存淘汰（`--run-ttl` / `STABILITY_AGENT_DAEMON_RUN_TTL_SEC`，`0` 表示不淘汰）。淘汰后旧 `run_id` 会 404，应重新提交。

---

### Web UI（静态页）

| 路径 | 说明 |
|------|------|
| `GET /` / `GET /index.html` | 本地面板 |
| `GET /app.js` | 前端逻辑 |
| `GET /styles.css` | 样式 |

文件来自仓库根目录 `web/`。

---

### Run API（子进程执行模式）

daemon 将任务分发给 `cli/main.py` 子进程执行，通过 SSE 流式推送事件。

数字员工常用短路径（与 `/runs/...` 等价）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 探活（含 `package` / `package_version`） |
| POST | `/runs` | 提交任务；可选头 `Idempotency-Key` 或 body `idempotency_key`（TTL 2 小时） |
| GET | `/status/{id}` | 查询状态 |
| GET | `/result/{id}` | 取分析结果；`?format=summary` 附加 `00_run_summary.json` |
| POST | `/cancel/{id}` | 取消任务 |

#### `POST /runs`
提交一个分析任务。任务先进入 `queued`，有空闲 worker 后变为 `running`。队列已满时返回 **429**：

```json
{ "error": "queue_full", "queued": 32, "max_queue": 32 }
```

进程正在停机时返回 **503**：

```json
{ "error": "shutting_down", "message": "服务正在停机，请稍后重新提交分析任务" }
```

成功时：

**请求体（RunRequest）：**
```json
{
  "crash_log": "/path/to/crash.crash",
  "crash_log_content": "...",
  "crash_log_dir": "/path/to/logs",
  "library_dir": "/path/to/lib",
  "code_roots": ["/path/to/src"],
  "config": null,
  "output_format": "markdown",
  "engine": "direct",
  "scope": "full",
  "prompt_mode": "fix",
  "agent_loop": null,
  "max_agent_rounds": null,
  "max_context_requests_per_round": null,
  "streaming": null,
  "apply_ai_fixes": true,
  "backup_original_sources": true,
  "force_disassembly": false,
  "force_anr_analysis": false,
  "force_memory_analysis": false,
  "force_timeline_analysis": false,
  "native_leak_dir": null,
  "native_leak_trace_db": null,
  "llm_mode": null,
  "llm_profile": null,
  "include_memory_in_05": false,
  "vector_db_path": null,
  "vector_db_max_results": null,
  "vector_db_record_usage": false,
  "rule_confidence_threshold": null,
  "use_ctags_index": false,
  "plugin_modules": null,
  "max_sibling_member_functions": null,
  "max_stack_frames_symbol_enrich": null,
  "max_stack_frames_in_prompt": null,
  "max_shared_var_related_functions": null,
  "min_key_read_related_functions": null,
  "code_context_timeout_sec": null,
  "find_source_timeout_sec": null
}
```
- `crash_log` / `crash_log_content` / `crash_log_dir`：三选一；`crash_log_dir` 优先；`crash_log_content` 通过 stdin 传入（`--crash-log-file -`）
- **省略字段 = 与直接跑 CLI 的 argparse 默认值相同**（daemon 对照 `build_parser().parse_args([])`，默认值不拼进子进程命令行）。例如不传 `max_agent_rounds` 时 CLI 仍为 `0`（随 `prompt_mode`：analysis=3，其它=1）；不传 `streaming` 时沿用 provider 配置。
- 显式传值才会覆盖：`max_agent_rounds: 1` 会加上 `--max-agent-rounds 1`；`streaming: false` 会加上 `--no-streaming`。
- `engine`：`direct`（默认）/ `langchain` / `langgraph`（旧值 `sequential` 会映射为 `direct`）
- `output_format`：`markdown`（默认）/ `json` / `text`
- `scope`：`full`（默认）/ `gen_prompt_only` / `parse_stack_only` / `parse_log_only`，控制 Agent 执行流程范围（详见 [CLI 参考](./CLI_COMMANDS_REFERENCE.md#--scope-取值)）
- `prompt_mode`：`fix`（默认）/ `analysis`，控制 `06_ai_prompt.md` / LLM 输入偏补丁输出还是偏证据分析；不控制是否自动应用修复（详见 [CLI 参考](./CLI_COMMANDS_REFERENCE.md#--prompt-mode-取值)）
- `agent_loop`：`null`（省略或传 `null` 时随 `prompt_mode`：`analysis`→`context_loop`，其它→`single`）/ `single` / `context_loop`，控制是否允许模型请求补充函数源码后继续多轮分析；独立于 `engine`（详见 [CLI 参考](./CLI_COMMANDS_REFERENCE.md#--agent-loop-取值)）
- `apply_ai_fixes` / `backup_original_sources`：默认 `true`；为 `false` 时分别传 `--no-apply-ai-fixes` / `--no-backup-original-sources`
- daemon 对 `scope=full` 且 `apply_ai_fixes=true` 的请求自动创建 detached Git worktree，CLI 只修改该 run 的隔离目录，不会直接回写请求中的源码目录。`code_roots` 必须位于 Git 仓库内，且对应范围不能有未提交修改；非 Git 或 dirty code root 会使 run 以 `workspace_error` 结束。
- worktree 默认保留在系统临时目录的 `stability-analysis-agent/worktrees/<run_id>/` 下，便于检查和继续处理；可通过 `STABILITY_AGENT_WORKTREE_DIR` 指定管理根目录。
- `force_*` / `native_leak_*` / `llm_mode` / `llm_profile` / `include_memory_in_05` / 向量库与 04b 超时裁剪字段 / `max_stack_frames_symbol_enrich` / `max_stack_frames_in_prompt`：透传对应 CLI 旗标（仅当与 CLI 默认不同）
- `consultation` / `prompt` / `model`：兼容旧客户端，**不会**转成 CLI 参数（当前 CLI 无 `--consultation` / `--model`）
- 未知字段会被忽略（`run_request_from_dict`）

**响应：**
```json
{ "run_id": "20260413-153012-a1b2c3d4", "status": "queued" }
```

---

#### `GET /runs`
列出内存中的任务，并附带调度器计数（字段与 `/health` 相同：`queued` / `running` / `max_workers` / `max_queue` / `run_timeout_sec`）。

**响应：**
```json
{
  "runs": [{ "run_id": "...", "status": "queued" }],
  "queued": 1,
  "running": 1,
  "max_workers": 2,
  "max_queue": 32,
  "run_timeout_sec": 0
}
```

---

#### `GET /runs/<run_id>` / `GET /status/<run_id>`
查询任务状态。`/status/{id}` 是给外部调用方的短路径。

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
  "output_format": "markdown",
  "report_dir": "/path/to/reports/20260812_...",
  "workspace_dir": "/tmp/stability-analysis-agent/worktrees/<run_id>",
  "original_code_roots": ["/path/to/src"],
  "isolated_code_roots": ["/tmp/.../src"],
  "workspace_manifest": "/path/to/report/09_ai_fix_workspace.json",
  "patch_path": "/path/to/report/09_ai_fix.patch"
}
```
`status` 取值：`queued` / `running` / `done` / `error` / `canceled`  
`progress`：阶段文案；对外另有 `progress_percent`（0–100，可 null）。  
`error`：失败/取消时的中文摘要，可直接转述。  
`workspace_dir`：自动改码隔离 worktree；任务未改码时经常为 `null`，不是必有字段。  
`report_dir`：从 CLI stderr 行 `report 已保存到:` 解析，供 Web 写向量库使用。  
`patch_path`：存在代码变化时生成的 Git patch；没有变化时为 `null`。  
任务表在内存中，daemon 重启后旧 run_id 全部 404，应重新提交。  
对 running 任务 POST `/cancel` 后可能仍返回 `status: running`，需再查 `/status`。

---

#### `GET /runs/<run_id>/events`
SSE 流式订阅任务事件（`text/event-stream`）。队列为**单消费者**，不要多端同时订阅；轮询 `/status` 已足够。

**事件格式：**
```
data: {"run_id": "...", "type": "stdout", "data": {"chunk": "..."}, "ts": 1718000002.0}
```

常见事件类型：

| type | 说明 |
|------|------|
| `run_started` | 任务开始执行 |
| `workspace_prepared` | Git worktree 已创建，包含原始与隔离后的 code roots |
| `workspace_error` | worktree 创建失败，任务不会回写原始源码 |
| `process_spawn` | 子进程已启动，含命令行 |
| `stdout` | 子进程标准输出块（`data.chunk`） |
| `stderr` | 子进程标准错误行（`data.line`） |
| `run_finished` | 任务结束（`data.status` / `data.exit_code`） |
| `run_canceled` | 任务已取消 |
| `artifact_written` | 产物已落盘（`data.path`） |
| `workspace_artifacts_written` | worktree 清单与 patch 已写入报告目录 |
| `keepalive` | 保活心跳（每 1 秒一次） |

---

#### `GET /runs/<run_id>/result` / `GET /result/<run_id>`
获取任务最终结果（任务完成后可用）。响应含 `output`（全文）和 `report`（去掉前言的 Markdown）。

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

#### `POST /runs/<run_id>/cancel` / `POST /cancel/<run_id>`
取消排队中的任务（不启动 CLI），或终止正在运行的 CLI 子进程。

**响应：**
```json
{ "run_id": "...", "status": "canceled" }
```

---

#### `POST /runs/<run_id>/vector-db/commit`
在 **改码成功** 后，将本次报告目录中的案例写入本地向量知识库（用户确认路径；Web 面板「写入」按钮调用此端点）。

**前提：**
- 任务已结束（`status` 为 `done` 或 `error`）
- `report_dir` 已解析且目录存在
- `08_apply_ai_fixes.json` 存在且 `success=true`

**请求体：** 空 JSON `{}` 即可。

**成功响应（200）：**
```json
{
  "ok": true,
  "pattern_id": "pattern_abc123...",
  "vector_db_path": "/Users/you/.config/stability-analysis-agent/vector_db",
  "summary": { "signal": "SIGSEGV", "fix_files": ["foo.cpp"] }
}
```

**常见错误：**
| HTTP | 说明 |
|------|------|
| `404` | `run_not_found` |
| `409` | `run_not_finished` |
| `400` | `report_dir_missing` 或报告不符合写库条件（`skipped`） |
| `501` | `vector_db.mode=remote` 尚未实现 |
| `500` | RAG 运行时不可用或其它内部错误 |

写库配置来自 `web_preferences.json` 的 `vector_db` 段（`mode`、`local_path`）。成功后会在报告目录落盘 `09_vector_db_commit.json`。

Daemon 拉起 CLI 时固定附加 `--no-interactive --no-save-to-vector-db`，避免子进程内二次确认。

---

### Skills API

进程内调用 `skill_system.SkillManager`（与 `sa-agent skill` 对齐）。详见 [WEB_UI_GUIDE.md](./WEB_UI_GUIDE.md) 与 [Skill System](../skills/README.md)。

#### `GET /skills`
列出**已安装**技能（`~/.config/stability-analysis-agent/skills`；不含仅 discovery 路径下的 skill）。

**响应：** `{ "skills": [ /* SkillSummary + enabled */ ] }`

#### `GET /skills/<name>`
技能详情（summary / frontmatter / package / body）。

#### `POST /skills/install`
```json
{ "source": "/path/to/skill-or.zip", "overwrite": false }
```
成功返回 `SkillInstallResult`；目标已存在且未 overwrite → `409`。

#### `POST /skills/lint`
```json
{ "source": "/path/to/skill" }
```
**响应：** `{ "issues": [ { "level", "message", "path" } ] }`

#### `POST /skills/uninstall`
```json
{ "name": "my-skill" }
```

---

### Web 偏好 API（本地面板）

工作区路径与 Skill 开关；数据文件 `~/.config/stability-analysis-agent/web_preferences.json`。

#### `GET /web/preferences`
返回 `{ "workspace": { "library_dir", "code_roots" }, "disabled_skills": [...], "vector_db": { "mode", "local_path", ... } }`。

#### `POST /web/preferences`
- 更新工作区：`{ "workspace": { "library_dir", "code_roots" } }`
- 切换 Skill：`{ "skill": "<command_name>", "enabled": true|false }`
- 更新向量库：`{ "vector_db": { "mode": "local", "local_path": "/path/to/vector_db" } }`（`remote` 仅配置预留）

禁用列表通过环境变量 `STABILITY_AGENT_DISABLED_SKILLS` 传入 CLI 子进程，分析时跳过对应 skill 注册。

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

- [Local Web UI](./WEB_UI_GUIDE.md)
- [CLI 使用指南](./CLI_GUIDE.md)
- [Tool System 概览](../tools/tool_system/TOOL_SYSTEM_OVERVIEW.md)
- [协议模型](../tools/tool_system/TOOL_SYSTEM_OVERVIEW.md)
