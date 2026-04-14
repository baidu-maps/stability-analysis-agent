# Skill 系统

Skill（技能）是 Stability Analysis Agent 中用于解决特定问题类型的**解决方案抽象**。它封装了完整的崩溃分析流程，包括崩溃日志解析、堆栈地址符号化、代码上下文提取和 LLM 分析。

## 什么是 Skill？

Skill（技能）是面向特定问题类型的完整解决方案：

| 维度 | Tool | Skill |
|------|------|-------|
| **定位** | 基础单元能力 | 问题类型解决方案 |
| **粒度** | 原子操作 | 多个 Tool 组合 |
| **抽象级别** | 底层能力 | 面向业务场景 |
| **调用者** | Agent/LLM | 用户/业务层 |

### Tool 示例
- `crash_log_parser` - 解析崩溃日志
- `add2line_resolver` - 符号化地址
- `code_content_provider` - 提取代码上下文

### Skill 示例
- `ios_crash_analyze` - iOS 崩溃分析
- `android_crash_analyze` - Android 崩溃分析
- `crash_analysis` - 通用崩溃分析

## 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                      用户层                                 │
│  CLI / Daemon                                              │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│                    ConfigDrivenExecutor                     │
│  - 根据配置选择 Tool/Skill 实现                              │
│  - 管理 LLM 适配器                                          │
│  - 注入 SkillContext                                        │
└─────────────────────────┬───────────────────────────────────┘
                          │
    ┌─────────────────────┴─────────────────────┐
    ▼                                           ▼
┌──────────────┐                        ┌──────────────┐
│    Tool      │                        │    Skill     │
│  (基础能力)   │                        │ (解决方案)    │
└──────────────┘                        └──────────────┘
```

### BaseSkill 抽象类

```python
from tools.core.tool_system import BaseSkill, SkillDefinition, SkillContext

class MySkill(BaseSkill):
    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="my_skill",
            description="我的自定义技能",
            problem_type="my_problem_type",
            required_tools=["tool1", "tool2"]
        )

    def solve(self, problem: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        # 实现分析逻辑
        pass
```

### SkillContext 上下文

SkillContext 提供了 Skill 执行过程中需要的能力：

- **context.llm** - LLM 适配器（用于 AI 分析）
- **context.tools** - 工具注册表（调用底层工具）
- **context.config** - 配置信息

典型用法：

```python
def solve(self, problem, context):
    # 调用工具
    parse_result = context.execute_tool("crash_log_parser", {
        "log_content": problem["crash_log"]
    })

    # 调用 LLM（可选）
    if context.llm:
        response = context.call_llm("分析这个崩溃...")
        return {"analysis": response.content}

    return {"parse_result": parse_result}
```

### 执行流程

```
用户请求
    │
    ▼
ConfigDrivenExecutor.execute_skill("crash_analysis", problem)
    │
    ├─ 根据配置选择 Skill 实现
    │
    ├─ 创建 SkillContext (注入 LLM 适配器)
    │
    ├─ 调用 Skill.solve()
    │   │
    │   ├─ 调用 Tool: crash_log_parser
    │   ├─ 调用 Tool: add2line_resolver
    │   ├─ 调用 Tool: code_content_provider
    │   └─ 调用 LLM: 生成分析报告
    │
    └─ 返回结果
```

### 注册中心

支持覆盖替换的注册机制：

```python
from tools.core.tool_system import Priority

# 注册自定义 Skill
registry.register(
    "my_skill",
    MySkill(),
    priority=Priority.CUSTOM,  # 更高的优先级
    force_override=True,        # 强制覆盖内置
    is_tool=False              # 这是 Skill
)
```

优先级：
- `Priority.BUILTIN` (100) - 内置实现
- `Priority.EXTENSION` (200) - 扩展实现
- `Priority.CUSTOM` (300) - 自定义实现（最高）

## 内置 Skill

| Skill 名称 | 问题类型 | 平台 | 版本 |
|-----------|---------|------|------|
| `ios_crash_analyze` | iOS 崩溃 | iOS | 1.0.0 |
| `android_crash_analyze` | Android 崩溃 | Android | 1.0.0 |
| `crash_analysis` | 通用崩溃 | 自动检测 | 1.0.0 |

### iOS Crash Analyze Skill

解析 iOS 崩溃日志，提取 SIGSEGV/SIGABRT 等信号，使用 atos 符号化地址，提取 Objective-C/Swift 源代码上下文。

### Android Crash Analyze Skill

解析 Android 崩溃日志，提取 Java 异常/Native 崩溃，使用 addr2line/llvm-addr2line 符号化，提取 Java/Kotlin/C++ 源代码上下文。

### Generic Crash Analysis Skill

自动检测崩溃日志的平台类型：
- **iOS** - 包含 `SIGSEGV`, `SIGABRT`, `Swift` 等关键词
- **Android** - 包含 `java.lang`, `Native crash`, `ANR` 等关键词
- **其他** - 使用通用分析逻辑

### 使用示例

```python
result = executor.execute_skill("crash_analysis", {
    "crash_log": "...",      # 崩溃日志内容
    "library_dir": "...",     # .dylib/.so 目录
    "code_root": "...",      # 源代码目录
})
```

### 返回结果格式

```json
{
  "status": "success",
  "platform": "ios",
  "skill": "ios_crash_analyze",
  "parse_result": { ... },
  "resolved_stack": { ... },
  "code_context": { ... },
  "analysis": "..."
}
```

## 扩展指南

### 创建自定义 Skill

#### 步骤 1：继承 BaseSkill

```python
from tools.core.tool_system import (
    BaseSkill,
    SkillDefinition,
    SkillContext
)

class MyCustomSkill(BaseSkill):
    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="my_custom_skill",
            description="我的自定义技能描述",
            problem_type="my_problem_type",
            required_tools=["tool1", "tool2"],
            version="1.0.0"
        )

    def solve(self, problem: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        # 1. 获取输入
        crash_log = problem.get("crash_log", "")

        # 2. 调用工具
        tool_result = context.execute_tool("tool_name", {
            "input": "..."
        })

        # 3. 调用 LLM（可选）
        if context.llm:
            llm_response = context.call_llm("分析这个...")
            llm_result = llm_response.content

        # 4. 返回结果
        return {
            "status": "success",
            "result": tool_result
        }
```

#### 步骤 2：注册 Skill

**方式 1：使用装饰器**

```python
from tools.core.tool_system import register_skill, Priority

@register_skill(priority=Priority.CUSTOM, force_override=False)
class MyCustomSkill(BaseSkill):
    # ...
```

**方式 2：手动注册**

```python
from tools.core.tool_system import ToolAndSkillRegistry, Priority

registry = ToolAndSkillRegistry()
registry.register(
    "my_custom_skill",
    MyCustomSkill(),
    priority=Priority.CUSTOM,
    force_override=False,
    is_tool=False
)
```

**方式 3：配置文件驱动**

```json
{
  "skills": [
    {
      "name": "crash_analysis",
      "enabled": true,
      "implementation": "MyCustomSkill"
    }
  ]
}
```

#### 步骤 3：使用

```python
from tools.core.tool_system import (
    ConfigDrivenExecutor,
    SystemConfig, SkillConfig,
    register_all_tools_and_skills
)

# 注册所有内置 + 自定义
registry = ToolAndSkillRegistry()
register_all_tools_and_skills(registry)
registry.register("my_custom_skill", MyCustomSkill(), priority=Priority.CUSTOM)

# 创建配置
config = SystemConfig(
    skills=[SkillConfig(name="my_custom_skill", enabled=True)]
)

# 执行
executor = ConfigDrivenExecutor(registry, config)
result = executor.execute_skill("my_custom_skill", {
    "crash_log": "..."
})
```

### 覆盖内置 Skill

如果想替换内置的 Skill，使用 `force_override=True`：

```python
class MyCustomCrashAnalysis(BaseSkill):
    @property
    def definition(self):
        return SkillDefinition(
            name="crash_analysis",
            description="自定义崩溃分析",
            problem_type="crash_analysis",
            required_tools=[...]
        )

# 覆盖内置
registry.register(
    "crash_analysis",
    MyCustomCrashAnalysis(),
    priority=Priority.CUSTOM,
    force_override=True,
    is_tool=False
)
```

## 配置驱动

通过 JSON 配置选择使用哪个实现：

```json
{
  "skills": [
    {
      "name": "crash_analysis",
      "enabled": true,
      "implementation": "MyCustomSkill",
      "params": {}
    }
  ]
}
```

## 快速开始

```python
from tools.core.tool_system import (
    ToolAndSkillRegistry,
    SystemConfig, SkillConfig,
    ConfigDrivenExecutor,
    register_all_tools_and_skills,
)

# 创建注册表
registry = ToolAndSkillRegistry()
register_all_tools_and_skills(registry)

# 创建配置
config = SystemConfig(
    skills=[SkillConfig(name="crash_analysis", enabled=True)]
)

# 执行分析
executor = ConfigDrivenExecutor(registry, config, llm_adapter=None)
result = executor.execute_skill("crash_analysis", {
    "crash_log": "...",
    "library_dir": "...",
    "code_root": "..."
})
```

## 扩展点

1. **新增 Skill** - 创建新的问题类型解决方案
2. **替换 Tool** - 用自定义实现替换内置工具
3. **替换 Skill** - 用自定义实现替换内置技能
4. **自定义 LLM 适配器** - 支持新的 LLM 提供商
