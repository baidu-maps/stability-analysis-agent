# Bug Platform Fetcher Skill 模板

本文档定义 `bug-platform-fetcher` 这个面向"工单号 → 崩溃上下文"的空模板 Skill。

本仓库**只提供模板**，不会预置任何具体平台（Jira / iCafe / WorkTile / 飞书多维表格 / BugZilla / 自建系统）的接入实现。下游团队需要按自身平台 API 自己填充：`平台拉取` 与 `附件下载` 都由 Skill 编写者决定。

## 适用范围

- 一级菜单「4) 根据缺陷管理平台自动修复（基于 bug-platform-fetcher-skill）」

这一菜单入口对应一个可安装的 Skill 预置：

- `bug-platform-fetcher-skill`

## 生成方式

```bash
sa-agent skill init bug-platform-fetcher-skill ./bug-platform-fetcher-skill --preset bug-platform-fetcher
```

生成后再通过下面命令安装或发现：

```bash
sa-agent skill install ./bug-platform-fetcher-skill
sa-agent skill list
sa-agent skill show bug-platform-fetcher-skill
```

## 与闭源版的边界

闭源版 `bd-sa-agent` 的 `4) 输入 iCafe 编号自动修复` 在迁移时**只迁移菜单交互模式与 Skill 模板**，以下内容**不会出现在开源仓库**：

- 任何调用 iCafe / iPipe / UGate / `icafe-cli` 的代码；
- 任何公司内网 REST 接口的端点、字段名、Token 缓存逻辑；
- 任何包含 `*.baidu-int.com` / `*.bj.bcebos.com` / `uuap.baidu.com` 字样的 URL。

这些都被认为是**企业内部实现**，请团队在自己的私有仓库里实现。

## 模板骨架

```markdown
---
name: Bug Platform Fetcher Skill
description: 根据缺陷管理平台（Jira / iCafe / WorkTile / 自建系统等）编号，拉取工单详情并下载崩溃日志与对应调试库文件，为 sa-agent 标准分析流程提供 crash_log 与 library_dir 路径。
when_to_use: Use when a fix should be driven directly by a ticket ID rather than manual selection of crash_log / library_dir / code-root paths.
disable-model-invocation: true
allowed-tools:
  - shell
  - http
context: inline
---

## Purpose

把「工单号 → crash 上下文」的拉取抽象为可替换的 Skill。本模板只是骨架，需要开发者按自家平台 API 自行实现以下步骤。

## Inputs

- Ticket ID（工单 / 缺陷单编号）
- Platform auth token / cookie（通过环境变量读取，不写入 skill）
- Optional：下载目录前缀

## Workflow

1. 校验 ticket_id 格式
2. 调用平台 API 拉取工单详情
3. 识别唯一的崩溃日志附件（其它类型附件：截图、视频暂不处理）
4. 下载对应的调试库（.dSYM / .so / .pdb）到 library_dir
5. 解析 build_id / branch / platform 字段
6. 输出 JSON：{crash_log, library_dir, ticket_id, build_id, branch, platform}

## Outputs

- crash_log 绝对路径
- library_dir 目录路径
- ticket_id / build_id / branch / platform 元数据
```

## 接入 sa-agent 的两种方式

按本仓库现有 Skill 系统，`bug-platform-fetcher-skill` 可走两条接入路径之一。

### 路径 A：tool skill（推荐）

在 `skill.json` 中声明 entrypoint 与 export：

```json
{
  "id": "bug-platform-fetcher-skill",
  "command_name": "bug-platform-fetcher",
  "type": "tool",
  "entrypoint": "tool:bug_platform_fetcher",
  "exports": [
    {
      "kind": "tool",
      "ref": "my_pkg.bug_platform.tool:BugPlatformFetcherTool",
      "name": "bug_platform_fetcher",
      "priority": "CUSTOM",
      "force_override": false,
      "enabled": true
    }
  ]
}
```

然后在 CLI 或 daemon 里：

```bash
sa-agent skill run bug-platform-fetcher-skill \
  --input '{"ticket_id": "MY-123"}' --json
# → 返回 JSON {crash_log, library_dir, ticket_id, ...}
```

### 路径 B：prompt skill（最简单）

如果只想要"提示词输出路径建议"（人工事后拉取文件再回填），可以让 Skill 输出 prompt：

```bash
sa-agent skill run bug-platform-fetcher-skill --prompt-only
```

详细 Skill 编写规范见 [SKILL_TEMPLATE.md](./SKILL_TEMPLATE.md)。

## 开发建议

- 保持 `SKILL.md` 只放最稳定、最核心的步骤。
- 把平台相关的 HTTP/RPC 调用放到 `scripts/` 或同目录的 Python 包里。
- **绝不要在 SKILL 正文里写明任何公司内部 API URL、Token、或字段名**，把它们放在你自己仓库里的 `my_pkg/bug_platform/` 里。
- 建议把错误信息与状态码翻译成稳定的 Skill 内部约定（如 `{"status": "auth_required"}`），让调用方不用关心不同平台的细节。
