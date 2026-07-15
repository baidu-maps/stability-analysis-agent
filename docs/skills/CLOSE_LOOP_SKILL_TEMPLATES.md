# 闭环 Skill 模板

本文档定义两个面向“修复闭环”的空模板 Skill。它们的目标不是直接承载企业定制逻辑，而是给第三方开发者一个通用起点。

## 适用范围

- `自动验证修复结果`
- `自动生成修复后的新包`

这两个菜单入口对应两个可安装的 Skill 预置：

- `automation-testing-skill`
- `cicd-pipeline-skill`

## 生成方式

```bash
sa-agent skill init automation-testing-skill ./automation-testing-skill --preset automation-testing
sa-agent skill init cicd-pipeline-skill ./cicd-pipeline-skill --preset cicd-pipeline
```

生成后再通过下面命令安装或发现：

```bash
sa-agent skill install ./automation-testing-skill
sa-agent skill install ./cicd-pipeline-skill
sa-agent skill list
sa-agent skill show automation-testing-skill
sa-agent skill show cicd-pipeline-skill
```

## 运行时参与位置

- `automation-testing-skill`：适合放在“修复候选已经生成，进入自动验证”的分支中。
- `cicd-pipeline-skill`：适合放在“修复结果验证通过，进入打包/发布”的分支中。

当前版本里，这两个 Skill 菜单只负责引导与模板生成，不会自动混入崩溃分析主提示词。

## 模板骨架

### `automation-testing-skill`

```markdown
---
name: Automation Testing Skill
description: Validate a fix by running automated tests, smoke checks, or regression checks.
when_to_use: Use when a repaired feature needs automated verification before merge or release.
disable-model-invocation: true
allowed-tools:
  - shell
context: inline
---

## Purpose

## Inputs

## Workflow

## Outputs
```

### `cicd-pipeline-skill`

```markdown
---
name: CICD Pipeline Skill
description: Package, build, and publish a repaired artifact through a CI/CD pipeline.
when_to_use: Use when a verified fix needs to be packaged, signed, uploaded, or handed off.
disable-model-invocation: true
allowed-tools:
  - shell
context: inline
---

## Purpose

## Inputs

## Workflow

## Outputs
```

## 开发建议

- 保持 `SKILL.md` 只放最稳定、最核心的步骤。
- 具体命令、平台差异、脚本、样例日志放到 supporting files。
- 当 Skill 逐步成熟后，再考虑把它升级为 workflow/tool 入口。
