# 测试指南（Testing）

本目录汇总 Stability Analysis Agent 的测试分层、常用命令与发布前检查清单。测试代码仍在仓库根目录 `test/`；此处是**文档索引**。

## 分层概览

| 层级 | 目录 / 入口 | 是否调用真实 LLM | 用途 |
|------|-------------|------------------|------|
| **单元 / 契约** | `test/agent_py_tool/`、`test/cli/`、`test/daemon/`、`test/skill_system/`、`test/tool_system/`、`test/web/` | 否 | 解析、工具链、协议、Web/Daemon 壳层 |
| **LLM 连通性** | `test/llm/` | 是（可选） | 验证各厂商 API 与配置 |
| **AI 全流程回归** | `test/ai_regression/` + `scripts/run_ai_regression.py` | 是 | 真实 Agent 跑通后比对落盘源码与期望 patch |
| **集成 / 模拟** | `test/test_vscode_ai_agent_integration.py`、`test/llm/test_vscode_simulation.py` | 可跳过 | IDE / 流式路径冒烟 |

设计原则：

- **确定性测试**覆盖可重复逻辑（解析、报告路径、daemon 参数透传、Web DOM 契约）。
- **AI 回归**只断言最终改码结果，不把 LLM 原文当作黄金标准。
- **Web 壳**不重复跑 Crash Agent；业务正确性由 CLI / daemon 回归链路覆盖。

## 提交前（推荐）

不调用真实 LLM，约 1 分钟内完成：

```bash
python3 -B -m unittest \
  test.ai_regression.test_runner \
  test.cli.test_report_paths \
  test.cli.test_vector_db_commit_prompt \
  test.rag.test_case_writer \
  test.daemon.test_build_cli_cmd \
  test.daemon.test_skills_api \
  test.daemon.test_run_lifecycle \
  test.daemon.test_vector_db_commit_api \
  test.daemon.test_web_preferences \
  test.skill_system.test_installed_skills_runtime \
  test.web.test_web_contract \
  test.tools.test_diagnosis_infrastructure \
  test.tools.test_cpp_crash \
  test.tools.test_appfreeze \
  test.tools.test_js_crash \
  test.tools.test_js_heap \
  test.tools.test_jank_analysis \
  test.tools.test_api_fault
```

详见 [UNIT_TESTS.md](./UNIT_TESTS.md)、[WEB_DAEMON_TESTS.md](./WEB_DAEMON_TESTS.md)。

## GitHub Actions

| Workflow | 触发 | 内容 |
|----------|------|------|
| [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) | push / PR → `main`/`master` | 安装冒烟 + **提交前确定性套件** + 工具链 spot check；PR 跑 Python 3.10/3.12，主干跑 3.9–3.12 |
| [`.github/workflows/ai-regression.yml`](../../.github/workflows/ai-regression.yml) | 手动 `workflow_dispatch`，或给 PR 打上 `ai-regression` label | 真实 LLM 全流程改码回归（需仓库 Secrets：`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `WENXIN_API_KEY` 之一） |
| [`.github/workflows/publish-pypi.yml`](../../.github/workflows/publish-pypi.yml) | tag `v*`，或手动选择 TestPyPI/PyPI | 确定性门禁 → `scripts/pypi_release` 构建 → Trusted Publishing 上传（与 `ci.yml` 分离） |

本地复现 CI 确定性步骤：与上方「提交前」命令相同，并额外可跑 `test/agent_py_tool` 里列出的 spot check 文件。PyPI 发版细节见 [../scripts/PYPI_RELEASE_SCRIPTS.md](../scripts/PYPI_RELEASE_SCRIPTS.md)。

## 发布前（含 AI）

```bash
# 1) 确定性套件（同上）

# 2) CLI 入口 AI 回归（默认）
python3 scripts/run_ai_regression.py \
  --case test/ai_regression/cases/demo_basic_nullptr.json

# 3) 若变更了 daemon / Web 壳，追加 daemon 入口
python3 scripts/run_ai_regression.py \
  --case test/ai_regression/cases/demo_basic_nullptr.json \
  --entrypoint daemon
```

详见 [AI_REGRESSION.md](./AI_REGRESSION.md)。

## 子文档

| 文档 | 说明 |
|------|------|
| [UNIT_TESTS.md](./UNIT_TESTS.md) | `test/` 目录结构与按模块运行方式 |
| [AI_REGRESSION.md](./AI_REGRESSION.md) | AI 全流程代码回归（Case、期望 patch、双入口） |
| [WEB_DAEMON_TESTS.md](./WEB_DAEMON_TESTS.md) | 本地面板 + Daemon HTTP 契约测试 |
| [../tools/llm/TEST_LLM_CONNECTION_GUIDE.md](../tools/llm/TEST_LLM_CONNECTION_GUIDE.md) | LLM 连接与厂商探测 |

## 与本地面板的关系

开源 **Web 壳**（`web/` + `daemon/server.py`）通过 `POST /runs` 拉起与 CLI 相同的子进程。相关测试：

- `test/web/test_web_contract.py` — 静态资源与前端调用的 API 端点存在性
- `test/daemon/test_*.py` — RunRequest 透传、Skills API、偏好设置、任务生命周期
- `test/ai_regression` + `--entrypoint daemon` — 端到端验证「面板 → daemon → CLI → 改码」

面板使用说明：[../cli/WEB_UI_GUIDE.md](../cli/WEB_UI_GUIDE.md)。
