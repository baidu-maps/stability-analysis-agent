# Web 壳与 Daemon 测试

开源本地面板（`web/`）由 `daemon/server.py` 托管静态资源，并通过 HTTP API 拉起 CLI 子进程。本节说明相关测试范围与维护约定。

## 架构（测试视角）

```
浏览器 (web/index.html + app.js)
    │  GET /  GET /web/preferences  POST /runs  GET /skills …
    ▼
daemon/server.py (Handler)
    │  subprocess: cli/main.py + RunRequest 旗标
    │  env: STABILITY_AGENT_DISABLED_SKILLS（来自 web_preferences）
    ▼
reports/<timestamp>_.../   （与直接 CLI 相同）
```

## 静态契约：`test/web/test_web_contract.py`

- `index.html` 引用的 `/styles.css`、`/app.js` 存在
- `app.js` 中 `$("id")` 均在 HTML 中有对应元素
- 前端包含对 `/health`、`/runs`、`/events`、`/cancel`、`/result`、`/vector-db/commit` 的调用
- 向量库写库卡片 DOM（`vectorDbCommit` 等）存在
- Demo 路径指向 `examples/crash_cases/demo_basic/`

**不**启动 daemon、**不**跑 E2E 浏览器自动化。

```bash
python3 -m unittest test.web.test_web_contract -v
```

## Daemon HTTP：`test/daemon/`

```bash
python3 -m unittest discover -s test/daemon -v
```

| 模块 | 验证内容 |
|------|----------|
| `test_build_cli_cmd.py` | Web 固定流水线字段（`scope=full`、`apply_ai_fixes` 等）映射到 CLI；含 `--no-interactive` / `--no-save-to-vector-db` |
| `test_vector_db_commit_api.py` | `POST /runs/<id>/vector-db/commit`、`report_dir` 解析 |
| `test_skills_api.py` | 安装/列表/详情；`GET /skills` 仅返回**已安装** skill |
| `test_web_preferences.py` | `~/.config/.../web_preferences.json` 工作区、`disabled_skills`、`vector_db` |
| `test_run_lifecycle.py` | 任务队列、SSE 事件、取消 |

本地临时目录测试可通过 `STABILITY_AGENT_SKILL_HOME`、`STABILITY_AGENT_WEB_PREFS_FILE` 覆盖路径。

## Skill 运行时注册：`test/skill_system/test_installed_skills_runtime.py`

验证分析前 `cli/main.py` 仅注册**已安装且未禁用**的 skill 导出（与 Web 开关一致）。

## 端到端（含真实 LLM）

Web 壳本身不测 LLM 输出。发布前对 daemon 链路跑 AI 回归：

```bash
python3 scripts/run_ai_regression.py \
  --case test/ai_regression/cases/demo_basic_nullptr.json \
  --entrypoint daemon
```

## 修改清单

| 你改了… | 应更新… |
|---------|---------|
| `web/index.html` / `app.js` | `test/web/test_web_contract.py` |
| `protocol/models.py` RunRequest | `test/daemon/test_build_cli_cmd.py` |
| Skills / preferences API | `test/daemon/test_skills_api.py`、`test_web_preferences.py` |
| 改码主逻辑 | `test/ai_regression/expected/` + AI 回归 |

相关用户文档：[../cli/WEB_UI_GUIDE.md](../cli/WEB_UI_GUIDE.md)、[../cli/DAEMON_SERVER_GUIDE.md](../cli/DAEMON_SERVER_GUIDE.md)。

索引：[README.md](./README.md)
