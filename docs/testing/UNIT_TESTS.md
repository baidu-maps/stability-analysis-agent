# 单元与契约测试

测试代码位于仓库根目录 `test/`。多数用例基于标准库 `unittest`，可直接 `python3 -m unittest` 或 `python3 test/.../test_*.py` 运行。

## 目录结构

```
test/
├── agent_py_tool/     # 工具链、工作流、诊断模块（体量最大）
├── ai_regression/     # AI 全流程回归（见 AI_REGRESSION.md）
├── cli/                 # CLI 辅助逻辑（报告路径、迁移等）
├── daemon/              # HTTP daemon：CLI 命令拼装、Skills API、偏好、任务生命周期
├── llm/                 # LLM 连接探测（需 API Key，见 TEST_LLM_CONNECTION_GUIDE）
├── skill_system/        # Skill 解析、安装、运行时注册
├── tool_system/         # Tool System 回归与路由
└── web/                 # 本地面板静态契约（HTML/JS 与 DOM id）
```

## 按模块运行

### Tool / 诊断（`test/agent_py_tool/`）

```bash
cd test/agent_py_tool
python3 test_code_content_provider.py
python3 test_vector_db.py
python3 test_evidence_compass.py
# … 同目录下其它 test_*.py
```

覆盖：崩溃解析、符号化、代码上下文、`04a`–`04e` 诊断、apply fixes、repo_search 等。

### CLI（`test/cli/`）

```bash
python3 -m unittest test.cli.test_report_paths test.cli.test_vector_db_commit_prompt -v
```

覆盖：`reports/` 默认目录、`cli_reports` 迁移、修复后向量库确认与 `--save-to-vector-db` 旗标。

### RAG / 向量库写库（`test/rag/`）

```bash
python3 -m unittest test.rag.test_case_writer -v
```

覆盖：`case_writer` 从报告构建 pattern、`commit_from_report_dir`、`vector_store_config` local/remote 桩。

### Daemon（`test/daemon/`）

```bash
python3 -m unittest discover -s test/daemon -v
```

| 文件 | 覆盖点 |
|------|--------|
| `test_build_cli_cmd.py` | `RunRequest` → `cli/main.py` 参数透传（含 Web 非交互写库旗标） |
| `test_vector_db_commit_api.py` | `POST /runs/<id>/vector-db/commit`、`report_dir` |
| `test_skills_api.py` | `GET/POST /skills*`、仅已安装列表 |
| `test_web_preferences.py` | `web_preferences.json` 工作区、skill 开关、`vector_db` |
| `test_run_lifecycle.py` | 任务提交、SSE、取消（轻量） |

### Skill System（`test/skill_system/`）

```bash
python3 test/skill_system/test_skill_system.py
python3 -m unittest test.skill_system.test_installed_skills_runtime -v
```

### Tool System（`test/tool_system/`）

```bash
python3 test/tool_system/test_regression.py
```

### Web 壳（`test/web/`）

```bash
python3 -m unittest test.web.test_web_contract -v
```

不启动浏览器；校验 `index.html` / `app.js` 引用的 DOM id 与 daemon 端点字符串一致。

## Tool System 全量回归

```bash
python3 test/tool_system/test_regression.py
```

## LLM 测试（可选，需密钥）

```bash
cd test/llm
python3 test_llm_connection.py --all
```

配置说明：[../tools/llm/TEST_LLM_CONNECTION_GUIDE.md](../tools/llm/TEST_LLM_CONNECTION_GUIDE.md)。

## IDE / 流式集成

```bash
# 30s 超时
AI_STABILITY_TEST_TIMEOUT_SECONDS=30 python3 test/test_vscode_ai_agent_integration.py

# 快速模式（跳过 AI 分析）
AI_STABILITY_TEST_FAST=1 python3 test/llm/test_vscode_simulation.py
```

## 开发建议

- 新功能优先补**确定性**单测；避免在 `agent_py_tool` 里默认调真实 LLM。
- 改 `protocol/models.py`、`daemon/server.py` 的 RunRequest 时，同步更新 `test/daemon/test_build_cli_cmd.py`。
- 改 `web/` 前端时，同步更新 `test/web/test_web_contract.py`。
- 改自动改码主路径时，考虑更新 `test/ai_regression/expected/` 中的期望 patch。

索引：[README.md](./README.md)
