# Tool System 架构概览

## 概述

Tool System 是 Stability Analysis Agent 的核心架构，采用 **Tool + Skill** 双层设计：

- **Tool（工具）** - 基础单元能力
- **Skill（技能）** - 问题类型解决方案

## 核心组件

### 1. Tool 接口

```python
from tools.core.tool_system import BaseTool, ToolDefinition

class MyTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="my_tool",
            description="我的工具",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            category="parser"
        )

    def execute(self, input_data):
        # 实现逻辑
        return {"result": "..."}
```

### 2. Skill 接口

```python
from tools.core.tool_system import BaseSkill, SkillDefinition, SkillContext

class MySkill(BaseSkill):
    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="my_skill",
            description="我的技能",
            problem_type="my_problem",
            required_tools=["tool1", "tool2"]
        )

    def solve(self, problem, context: SkillContext):
        # 调用工具
        result = context.execute_tool("tool1", {"input": "..."})

        # 调用 LLM（可选）
        if context.llm:
            response = context.call_llm("分析...")

        return {"status": "success", "result": result}
```

### 3. 注册中心

```python
from tools.core.tool_system import (
    ToolAndSkillRegistry,
    Priority,
    register_tool,
    register_skill
)

# 使用装饰器注册
@register_tool(priority=Priority.CUSTOM)
class MyTool(BaseTool):
    ...

@register_skill(priority=Priority.CUSTOM)
class MySkill(BaseSkill):
    ...
```

### 4. 执行器

```python
from tools.core.tool_system import (
    ConfigDrivenExecutor,
    SystemConfig,
    register_all_tools_and_skills
)

registry = ToolAndSkillRegistry()
register_all_tools_and_skills(registry)

config = SystemConfig(...)
executor = ConfigDrivenExecutor(registry, config, llm_adapter)

result = executor.execute_skill("skill_name", {"problem": "..."})
```

## 内置 Tool

| Tool 名称 | 类别 | 说明 |
|-----------|------|------|
| `crash_log_parser` | parser | 解析崩溃日志 |
| `add2line_resolver` | resolver | 符号化堆栈地址 |
| `code_content_provider` | provider | 提取代码上下文 |

## 内置 Skill

| Skill 名称 | 问题类型 | 说明 |
|-----------|---------|------|
| `ios_crash_analyze` | iOS 崩溃 | iOS 平台崩溃分析 |
| `android_crash_analyze` | Android 崩溃 | Android 平台崩溃分析 |
| `crash_analysis` | 通用崩溃 | 自动检测平台 |

## LLM 适配器

支持三种 LLM 调用方式：

| 适配器 | 说明 | 适用场景 |
|--------|------|---------|
| `DirectLLMAdapter` | 直接拼装提示词，一次调用 | 简单固定流程 |
| `LangChainLLMAdapter` | LangChain Agent | 需要灵活工具调用 |
| `LangGraphLLMAdapter` | LangGraph 图结构 | 复杂流程控制 |

## 配置驱动

通过 JSON 配置选择实现：

```json
{
  "tools": [
    {"name": "crash_log_parser", "enabled": true, "implementation": "MyCustomParser"}
  ],
  "skills": [
    {"name": "crash_analysis", "enabled": true, "implementation": "MyCustomSkill"}
  ],
  "llm": {
    "engine": "direct",
    "model": "glm-4"
  }
}
```

## 相关文档

- [ARCHITECTURE.md](./ARCHITECTURE.md) - 详细架构设计
- [IMPLEMENTATION.md](./IMPLEMENTATION.md) - 内置实现说明
- [EXTENSION.md](./EXTENSION.md) - 扩展指南