# Stability Analysis Agent 架构图

本文档展示系统架构设计。

## 整体架构图

```mermaid
graph TB
    subgraph "Shell 层（交互入口）"
        direction TB
        S1[CLI<br/>权威入口<br/>脚本化/可回归]
        S2[Daemon<br/>性能与复用层<br/>HTTP/SSE/流式]
        S3[未来壳<br/>Web/CI/Bot/JetBrains]
    end

    subgraph "Protocol 层（统一协议）"
        direction TB
        P1[RunRequest<br/>统一输入协议]
        P2[RunEvent<br/>流式事件协议]
        P3[RunResult<br/>结果协议]
    end

    subgraph "Core 层（核心能力中心）"
        direction TB
        C1[Agent 引擎<br/>LangGraph/Sequential]
        C2[Analyzers<br/>解析/定位/上下文]
        C3[RAG<br/>向量检索增强]
        C4[Report<br/>报告生成]
    end

    subgraph "数据层（外部服务）"
        direction TB
        D1[Vector Database<br/>ChromaDB<br/>知识库]
        D2[LLM Service<br/>百度千帆/OpenAI<br/>智能分析]
    end

    S1 -->|POST /runs| P1
    S2 -->|接收请求| P1
    S3 -->|后续支持| P1

    P1 -->|解析请求| C2
    P2 -->|流式事件| S1
    P2 -->|SSE| S2

    C1 -->|调用| C2
    C2 -->|调用| C3
    C3 -->|检索| D1
    C1 -->|分析| D2
    C4 -->|生成报告| P3

    D1 -->|返回规则| C3
    D2 -->|返回分析| C1
```

## 分层说明

### Shell 层
- **CLI**：权威入口，脚本化回归测试
- **Daemon**：HTTP 服务，支持流式输出、任务复用
- **未来壳**：Web/CI/Bot/JetBrains 等

### Protocol 层
- **RunRequest**：统一输入协议
- **RunEvent**：流式事件协议（SSE）
- **RunResult**：结果协议

### Core 层
- **Agent 引擎**：LangGraph/Sequential 工作流
- **Analyzers**：解析器、地址解析、代码上下文
- **RAG**：向量检索增强
- **Report**：报告生成

### 数据层
- **Vector Database**：ChromaDB 知识库
- **LLM Service**：OpenAI/DeepSeek/百度千帆等

## 数据流

```
用户请求 (CLI/Daemon)
    │
    ▼
RunRequest (Protocol)
    │
    ▼
Core 分析流程
    │
    ├─→ crash_log_parser
    ├─→ add2line_resolver
    ├─→ code_content_provider
    ├─→ RAG 检索
    └─→ LLM 分析
    │
    ▼
RunResult / RunEvent
    │
    ▼
返回结果
```

## 与闭源工作区关系

开源项目与闭源工作区（`map_sdk_crash_agent/`）共享 Core 层和工具链，闭源工作区可以在此基础上添加：
- 私有崩溃案例
- 自定义配置
- 私有 UI 层（如有）
