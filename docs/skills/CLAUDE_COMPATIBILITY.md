# Claude Skill 兼容说明

本项目的 Skill System 目标之一，是尽量兼容 Claude Code 常见的 skill 组织方式，尤其是目录级 `SKILL.md` 包。

## 支持的字段

| 字段 | Claude 语义 | 本项目语义 |
|------|------------|------------|
| `name` | skill 显示名 | 显示名，若不填则回退目录名 |
| `description` | 自动触发描述 | 发现 / 列表 / lint 使用 |
| `when_to_use` | 额外触发说明 | 发现 / 列表 / lint 使用 |
| `argument-hint` | 参数提示 | CLI `skill run` 可展示 |
| `disable-model-invocation` | 禁止自动触发 | 作为 prompt-only/manual skill 标记 |
| `allowed-tools` | 允许调用的工具 | 作为权限声明保留，后续可接入权限层 |
| `context` | inline / fork 等 | 作为元数据保留 |

## 支持的目录结构

```text
skill-name/
├── SKILL.md
├── reference.md
├── scripts/
├── examples/
└── templates/
```

## 兼容策略

- `SKILL.md` 是主入口。
- `skill.json` 是本项目新增的机器可读清单，不会破坏 Claude 风格 skill。
- 仅有 `SKILL.md` 的 skill 可以直接安装和发现。
- 有 `skill.json` 的 skill 可以进一步注册 Tool / Workflow 到本项目运行时。

## 迁移建议

- 纯 prompt skill：直接复制目录即可。
- Claude 的手工触发 skill：建议将 `disable-model-invocation: true` 视为手动运行。
- Claude 的 plugin 级 skill：建议通过 `skill.json` 的 `exports` 映射到本项目的 registry。

