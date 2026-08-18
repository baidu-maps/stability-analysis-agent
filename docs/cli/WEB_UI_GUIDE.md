# Local Web UI Guide

开源 **本地面板**：面向本地开发者的一键 `full` 全流程修复壳（需本机 `library_dir` + `code_roots`）。参数调试、诊断-only 等请用 CLI。

测试说明：[docs/testing/WEB_DAEMON_TESTS.md](../testing/WEB_DAEMON_TESTS.md)

## Quick start

```bash
python3 daemon/server.py --host 127.0.0.1 --port 8765
open http://127.0.0.1:8765/
```

## 页面结构

| 区域 | 作用 |
|------|------|
| **主区** | 一个输入框 + 模板 chips；点击「运行全流程修复」 |
| **侧栏 · 工作区** | 一次配置 `library_dir` + `code_roots`，保存后复用 |
| **侧栏 · 已安装 Skills** | 仅列出 `~/.config/stability-analysis-agent/skills` 下已安装项；开关启用/关闭；本地路径安装 |

## 主区输入

支持三种自动识别：

1. **单行路径** → `crash_log`（如 `examples/.../foo.crash`）
2. **以 `/` 结尾的路径** → `crash_log_dir`（批量）
3. **多行或含崩溃特征文本** → `crash_log_content`（粘贴日志）

模板 chips：Demo NullPtr、Demo 目录、路径模板、粘贴提示。

## 固定流水线（无需在网页选参数）

Web 提交时固定等价于：

```text
scope=full
prompt_mode=fix
agent_loop=context_loop
apply_ai_fixes=true
backup_original_sources=true
engine=direct
```

需要试 `gen_prompt_only` / `parse_stack_only` 等，请用 CLI。

## 结果与报告

- 进度条：解析 → 符号化 → 诊断 → AI → 改码
- 报告目录：`reports/<timestamp>_analysis_full_.../`（与 CLI 相同）
- 证据罗盘：`04a_crash_diagnosis.json` → `evidence_compass`
- 详细日志在「详细日志」折叠区

### 修复后写入向量库

改码成功（`08_apply_ai_fixes.success=true`）后，结果区会出现 **写入向量知识库** 卡片：

- **写入**：`POST /runs/<run_id>/vector-db/commit`，成功后显示 `pattern_id`
- **暂不**：关闭卡片，不写库

侧栏 **向量知识库** 显示当前部署模式与本地路径（来自 `web_preferences.json` 的 `vector_db` 段）。Daemon 拉起的 CLI 子进程不会弹写库确认；仅通过上述按钮写库。

默认向量库目录：`~/.config/stability-analysis-agent/vector_db`（可在 preferences 中配置 `vector_db.local_path`）。详见 [RAG 指南](../rag/README.md)。

## API

| 用途 | 端点 |
|------|------|
| 工作区 + skill 开关 + 向量库配置 | `GET/POST /web/preferences` |
| 运行任务 | `POST /runs` + SSE `/runs/<id>/events` |
| 修复后写向量库 | `POST /runs/<id>/vector-db/commit` |
| Skills | `GET /skills`, `POST /skills/install`, … |

工作区与 skill 开关保存在 `~/.config/stability-analysis-agent/web_preferences.json`。

`GET /skills` 仅返回**已安装** skill。关闭的 skill 会写入 `disabled_skills`；daemon 启动 CLI 子进程时通过环境变量 `STABILITY_AGENT_DISABLED_SKILLS` 透传，分析运行时会跳过对应 skill 的 tool/workflow 注册。

## 与 CLI 分工

- **Web**：本地开发、一键 `full` 修复（固定流水线参数）
- **CLI**：参数扫描、诊断-only、回归、脚本化

测试：[WEB_DAEMON_TESTS.md](../testing/WEB_DAEMON_TESTS.md)。详见 [DAEMON_SERVER_GUIDE.md](./DAEMON_SERVER_GUIDE.md)。
