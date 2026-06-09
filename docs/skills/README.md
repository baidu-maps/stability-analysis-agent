# Skill System

Skill 是 Stability Analysis Agent 的对外扩展单元，负责把第三方能力包装成可发现、可安装、可运行的资产。

## 分层关系

- `Tool`：原子能力，例如解析、符号化、检索。
- `Workflow`：问题级编排，例如 crash 分析、ANR 分析。
- `Skill`：可安装扩展包，负责分发、发现、模板、权限和可执行入口。

## 兼容范围

本仓库的 Skill System 兼容 Claude 风格的 `SKILL.md`：

- 支持 `SKILL.md` 作为主入口
- 支持 YAML frontmatter
- 支持 `description`、`when_to_use`、`argument-hint`、`disable-model-invocation`、`allowed-tools`
- 支持 supporting files 目录（`scripts/`、`examples/`、`templates/`、`reference.md` 等）
- 支持目录级安装与发现

同时增加了本项目自己的机器可读清单 `skill.json`，用于：

- 声明 `entrypoint`
- 声明 `exports`
- 声明版本与依赖
- 让 skill 可以注册 Tool / Workflow 到现有执行内核

## CLI

```bash
# 列出已发现技能
sa-agent skill list

# 查看技能详情
sa-agent skill show <name>

# 安装一个 Claude 风格 skill 目录
sa-agent skill install /path/to/skill-dir

# 校验 skill
sa-agent skill lint /path/to/skill-dir

# 生成 skill 模板
sa-agent skill init my-skill ./my-skill
```

## 安装目录

默认安装目录是：

```text
~/.config/stability-analysis-agent/skills
```

同时支持发现这些目录中的 skill：

- `~/.config/stability-analysis-agent/skills`
- `~/.claude/skills`
- 当前工作目录下的 `.claude/skills`
- 仓库根目录下的 `.claude/skills`
- 通过 `STABILITY_AGENT_SKILL_DIRS` 显式追加的目录

## 对现有系统的映射

Skill 安装后可以通过导出进入现有 Tool System：

- `exports.kind = tool` -> 注册到 `tool_system` 的工具表
- `exports.kind = workflow` -> 注册到 `tool_system` 的工作流表
- `entrypoint = prompt` -> 作为提示词技能渲染并输出
- `entrypoint = workflow:<name>` -> 直接执行导出的工作流

## 相关文档

- [Skill 模板](./SKILL_TEMPLATE.md)
- [Claude 兼容说明](./CLAUDE_COMPATIBILITY.md)
- [Tool System 概览](../tools/tool_system/TOOL_SYSTEM_OVERVIEW.md)
