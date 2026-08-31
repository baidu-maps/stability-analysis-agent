# 文档目录

本文档是 Stability Analysis Agent 的文档索引。

## 目录结构

```
docs/
├── architecture/          # 架构设计
├── cli/                   # CLI 使用指南（含本地面板、Daemon）
├── testing/               # 测试分层、单元测试、AI 回归、Web/Daemon 契约
├── skills/                # Skill 系统
├── workflows/                # Workflow 系统
├── rag/                   # RAG 向量库
├── tools/                 # 工具链
└── crash_cases/           # 崩溃示例文档
```

## 仓库自带扩展（`extensions/`）

仓库根目录下的 `extensions/` 目录是**用户级与插件级 Tool / Workflow 扩展的
官方模板与自动发现入口**。`extensions/register_all()` 会在 `sa-agent`
启动时被调用，自动加载：

- 仓库级示例模板（`extensions/tools/example_tool.py`、
  `extensions/workflows/example_workflow.py`）
- 用户目录 `~/.config/stability-analysis-agent/extensions/`
- 工作区 `.stability-analysis-agent/extensions/`
- Python 入口点 `stability_analysis_agent.tools / workflows`

详见 [tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./tools/tool_system/TOOL_SYSTEM_EXTENSION.md)。



> 快速开始请直接阅读项目根目录 [README.md](../README.md)（[英文版](../README.en.md)）。

## 文档列表

### 架构

| 文档 | 说明 |
|------|------|
| [architecture/README.md](./architecture/README.md) | 系统架构 |
| [architecture/ARCHITECTURE_DIAGRAM.md](./architecture/ARCHITECTURE_DIAGRAM.md) | 架构图 |

### CLI

| 文档 | 说明 |
|------|------|
| [cli/CLI_GUIDE.md](./cli/CLI_GUIDE.md) | CLI 主指南 |
| [cli/CLI_COMMANDS_REFERENCE.md](./cli/CLI_COMMANDS_REFERENCE.md) | 参数参考 |
| [cli/DAEMON_SERVER_GUIDE.md](./cli/DAEMON_SERVER_GUIDE.md) | Daemon 指南 |
| [cli/WEB_UI_GUIDE.md](./cli/WEB_UI_GUIDE.md) | 本地面板（一键全流程修复、工作区、Skills） |

### 测试

| 文档 | 说明 |
|------|------|
| [testing/README.md](./testing/README.md) | 测试分层与提交/发布清单 |
| [testing/UNIT_TESTS.md](./testing/UNIT_TESTS.md) | `test/` 目录与按模块运行 |
| [testing/AI_REGRESSION.md](./testing/AI_REGRESSION.md) | AI 全流程代码回归（CLI / daemon 双入口） |
| [testing/WEB_DAEMON_TESTS.md](./testing/WEB_DAEMON_TESTS.md) | Web 壳与 Daemon HTTP 契约测试 |

### Workflow

| 文档 | 说明 |
|------|------|
| [workflows/WORKFLOWS.md](./workflows/WORKFLOWS.md) | Workflow 系统完整文档 |

### Skill

| 文档 | 说明 |
|------|------|
| [../stability-analysis-agent-skill/README.md](../stability-analysis-agent-skill/README.md) | **对外能力包**（给 Claude / Cursor 等外部 Agent） |
| [skills/README.md](./skills/README.md) | Skill 系统总览（sa-agent 扩展机制） |
| [skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./skills/CLOSE_LOOP_SKILL_TEMPLATES.md) | 闭环 Skill 模板 |
| [skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./skills/BUG_PLATFORM_FETCHER_TEMPLATE.md) | 缺陷管理平台拉取 Skill 模板 |
| [skills/SKILL_TEMPLATE.md](./skills/SKILL_TEMPLATE.md) | Skill 模板 |
| [skills/CLAUDE_COMPATIBILITY.md](./skills/CLAUDE_COMPATIBILITY.md) | Claude Skill 兼容说明 |

### 工具链

| 文档 | 说明 |
|------|------|
| [ROADMAP.md](./ROADMAP.md) | Roadmap 长版（与 README 中的里程碑表一致） |
| [tools/CRASH_LOG_FORMATS.md](./tools/CRASH_LOG_FORMATS.md) | 崩溃日志文件后缀与平台导出格式（英文） |
| [tools/CRASH_LOG_FORMATS.zh-CN.md](./tools/CRASH_LOG_FORMATS.zh-CN.md) | 崩溃日志格式与平台支持（中文） |
| [tools/addr2line/README.md](./tools/addr2line/README.md) | 地址解析工具 |
| [tools/ai_tip/README.md](./tools/ai_tip/README.md) | 提示词拼装逻辑 |
| [tools/llm/TEST_LLM_CONNECTION_GUIDE.md](./tools/llm/TEST_LLM_CONNECTION_GUIDE.md) | LLM 连接测试（见 [testing/README.md](./testing/README.md)） |
| [tools/tool_system/TOOL_SYSTEM_OVERVIEW.md](./tools/tool_system/TOOL_SYSTEM_OVERVIEW.md) | Tool System 架构 |
| [tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./tools/tool_system/TOOL_SYSTEM_EXTENSION.md) | 扩展指南 |

### RAG

| 文档 | 说明 |
|------|------|
| [rag/README.md](./rag/README.md) | RAG 向量库使用 |

### 示例

| 文档 | 说明 |
|------|------|
| [crash_cases/README.md](./crash_cases/README.md) | 崩溃示例总览导航 |
| [crash_cases/demo_basic/README.md](./crash_cases/demo_basic/README.md) | 基础崩溃示例 |
| [crash_cases/demo_multithread/README.md](./crash_cases/demo_multithread/README.md) | 多线程崩溃示例 |

## 相关链接

- [项目主页](../README.md)
- [贡献指南](../CONTRIBUTING.md)
