# stability-analysis-agent-skill

本仓库 **Stability Analysis Agent** 的对外能力导出包，供 **Claude Code、Cursor** 等外部 Agent 安装使用。

## 这是什么

| 是 | 不是 |
|----|------|
| 教外部 Agent 如何安装并调用 `sa-agent` | `sa-agent skill install` 的安装目录 |
| Claude 兼容的 `SKILL.md` 能力包 | 本 Agent 运行时已加载的 skill 注册表 |
| 可随仓库版本一起维护的使用说明 | `output/` 构建产物 |

## 目录结构

```text
stability-analysis-agent-skill/
├── SKILL.md        # 主入口（外部 Agent 读取）
├── reference.md    # 参数、报告、配置详解
├── examples.md     # 可复制命令
└── README.md       # 本说明（给人看）
```

## 如何使用

### 1. 安装 Python 包（必须）

外部 Agent 按 [SKILL.md](./SKILL.md) 安装 `stability-analysis-agent`（PyPI / pipx / 源码 / 二进制）。

### 2. 安装本 Skill 到外部 Agent

```bash
# Claude Code（用户级）
cp -R stability-analysis-agent-skill ~/.claude/skills/stability-analysis-agent

# Cursor（项目级示例）
mkdir -p .cursor/skills
cp -R stability-analysis-agent-skill .cursor/skills/stability-analysis-agent
```

### 3. 验证

在 Claude/Cursor 中询问：「用 stability-analysis-agent 分析 demo 崩溃日志」，Agent 应能给出 `sa-agent` 命令并说明 `--scope`。

## 相关文档

- Skill System 框架（sa-agent 扩展机制）：[docs/skills/README.md](../docs/skills/README.md)
- CLI 完整参数：[docs/cli/CLI_COMMANDS_REFERENCE.md](../docs/cli/CLI_COMMANDS_REFERENCE.md)
