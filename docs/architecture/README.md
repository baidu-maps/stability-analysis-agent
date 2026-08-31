# 系统架构总览

本文件是架构主文档，说明技术架构、数据流与目录结构。

## 系统目标

- 核心能力只实现一次（解析、符号化、代码上下文、AI 分析、RAG）
- 多入口复用同一核心（CLI / daemon / 本地面板 `web/`）
- 配置与执行解耦（Tool System + ConfigDrivenExecutor）

## 分层架构

- 应用层：CLI、daemon（托管 `web/` 静态页 + HTTP API）
- 编排层：Tool System / Workflow / Executor
- 工具层：`crash_log_parser`、`add2line_resolver`、`code_content_provider`
- AI 层：Direct / LangChain / LangGraph 适配器
- 记忆层：规则 + 向量 + 元数据（RAG）

## 核心流程

标准分析链路：

1. 解析崩溃日志
2. 地址符号化
3. 提取代码上下文
4. RAG 检索增强（默认安装）
5. 生成 AI 分析结论

统一 CLI 入口为 `cli/main.py`，通过 `execute_workflow("crash_analysis", problem)` 执行。

## 目录约定

- `agent/`：AI Agent 实现
- `cli/`：命令行入口
- `daemon/`：本地 HTTP 服务（Run API、Skills API、`web_preferences`）
- `web/`：本地面板静态资源
- `workflows/`：Workflow 定义
- `tool_system/`：注册表、配置、执行器、LLM 适配器
- `tools/`：具体工具实现
- `rag/`：RAG 相关实现
- `examples/`：demo crash cases
- `test/`：测试集（文档见 [docs/testing/README.md](../testing/README.md)）

## 关键设计取舍

- 配置优先：支持显式 `--config`，否则按默认配置与本地覆盖加载
- 依赖可降级：LLM 或向量库不可用时，工具链仍可独立运行
- 单一权威入口：减少历史兼容入口带来的维护成本
