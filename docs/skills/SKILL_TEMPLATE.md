# Skill 模板

下面是一个最小可用的 Claude 兼容 Skill 模板。你可以把它当作第三方 skill 的起点，也可以直接通过 `sa-agent skill init` 生成。

## 目录结构

```text
my-skill/
├── SKILL.md
├── skill.json
├── README.md
├── reference.md
├── examples/
└── scripts/
```

## `SKILL.md`

```markdown
---
name: My Skill
description: Describe what this skill does and when to use it.
when_to_use: Use when ...
argument-hint: [issue-id]
disable-model-invocation: false
allowed-tools: Read Grep
---

## Goal

Write concise, actionable instructions here.

## Notes

- Keep the body short.
- Put long references into supporting files.
```

## `skill.json`

```json
{
  "id": "my-skill",
  "name": "My Skill",
  "command_name": "my-skill",
  "version": "0.1.0",
  "type": "prompt",
  "entrypoint": "prompt",
  "exports": [],
  "dependencies": [],
  "tags": ["stability-analysis-agent", "skill"]
}
```

## Workflow skill 示例

```json
{
  "id": "crash-analysis-skill",
  "name": "Crash Analysis Skill",
  "command_name": "crash-analysis",
  "version": "0.1.0",
  "type": "workflow",
  "entrypoint": "workflow:crash_analysis",
  "exports": [
    {
      "kind": "workflow",
      "ref": "my_package.my_skill:CrashAnalysisWorkflow",
      "name": "crash_analysis",
      "priority": "CUSTOM",
      "force_override": false,
      "enabled": true,
      "params": {}
    }
  ]
}
```

## 生成模板

```bash
sa-agent skill init my-skill ./my-skill
```

## 设计建议

- 让 `SKILL.md` 保持短小，只写加载后必须长期记住的内容。
- 长规则、样例、脚本说明放到 supporting files。
- 如果 skill 只是说明性知识，`entrypoint` 保持 `prompt` 即可。
- 如果 skill 需要真正执行逻辑，使用 `skill.json` 的 `exports` 和 `entrypoint` 连接到 Tool System。

