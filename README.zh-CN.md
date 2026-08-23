# Stability Analysis Agent

**🐛 App 一崩或一卡，sa-agent 先把日志变成证据链，再把证据变成修复。**  
面向 **App 稳定性** 的开源修复框架：确定性工具链优先（寄存器 · ANR · 内存 · 业务路径），LLM 补丁其次。**Crash 自动修复已可用**；ANR / OOM / 卡死分析已跑在同一条流水线上。



[English](./README.md) | **简体中文**

**维护状态：** 持续维护中 · **最新版 [v1.3.2](https://pypi.org/project/stability-analysis-agent/1.3.2/)**（Daemon HTTP 与 CLI 参数对齐、栈帧默认裁剪）· 有实质性改动时发布 [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases)（活跃期大约每月一档，非日历 SLA）· 详见 [CHANGELOG.md](./CHANGELOG.md)

---

### 项目定位

`Stability Analysis Agent` 是一个**面向 App 稳定性问题的修复框架**。
Crash、ANR、OOM、卡死、内存压力、watchdog kill 这类问题，都会逐步成为这个框架里的正式分析（并最终修复）对象。

它**不是**一个“通用 Prompt 工具”。Agent 会读取真实崩溃 / AppFreeze / ANR 日志，调用原生工具链（`addr2line` / `atos`），先构建**确定性证据链**（PC → 符号 → 可选反汇编 → 寄存器 → ANR 热点 / EventHandler → 内存线索 → 崩溃前业务路径），再对 Crash 场景生成 patch 并本地落盘（含备份）。验证、打包、上线交给闭环 Skill。

它**也不会**声称每一类稳定性问题都已完成自动改码。
**Crash 自动修复已可生产使用。** ANR / Freeze / 内存压力 / 时序诊断已经以结构化报告（`04a`–`04e`）落地并注入提示词——这些类别的专用自动改码工作流仍在同一框架内按类成熟，而不是另起一个 “v2 产品”。

## 为什么不用 AI 编程工具


|                                         | Cursor / Copilot / Claude Code | Stability Analysis Agent                          |
| --------------------------------------- | ------------------------------ | ------------------------------------------------- |
| **对崩溃 / ANR 日志做了什么**                    | 基本当普通文本读，顶多给分析建议               | **工具优先**：解析 → 符号化 → 证据罗盘 →（Crash）patch + 本地落盘     |
| **原生工具链（**`addr2line` **/** `atos`**）** | 很难真正接入                         | 一等公民，地址在进入 LLM 之前就完成解析                            |
| **寄存器 / fault 地址 / near-null**          | 靠模型猜                           | 确定性寄存器与故障模式诊断（`04a`）                              |
| **ANR / AppFreeze / 卡死**                | 粘贴 traces 碰运气                  | 专用 ANR workflow：热点栈、EventHandler 队列、IPC 提示（`04c`） |
| **“用户当时在干什么？”**                         | 手工翻 logcat                     | 崩溃前时序 + 业务路径抽取（`04e`）                             |
| **知识沉淀**                                | 跨会话无状态                         | RAG 规则表 + 向量数据库，模式持续积累                            |
| **多步推理**                                | 主要靠单轮 Prompt                   | LangGraph 状态机，可按需补充上下文并重调工具                       |
| **工单系统打通**                              | 没有现成支持                         | `bug-platform-fetcher` Skill 可对接任意工单系统            |
| **修复后自动验证**                             | ❌ 往往停在“看起来像是对的”                | ✅ `automation-testing` Skill 预置可接入你的测试执行器         |
| **自动发布**                                | ❌                              | ✅ `cicd-pipeline` Skill 预置可接入构建、签名与发布             |
| **端到端自动修复闭环**                           | ❌                              | ✅ 工单 → 自动修复 → 验证 → 发布，整条链路可串起来                    |
| **可扩展性**                                | 主要只能改 Prompt                   | Tool + Workflow + Skill，并支持 `extensions/` 本地插件    |


> 本仓库里“自动修复”的**精确边界**：
> `解析 → 符号化 → 读源码 → 生成 patch → 本地落盘（含备份）`。之后 Agent **把控制权交回**：交给你，或交给闭环上的其它 Skill（验证 / 打包）。它不会自己合并到 `main`，不会自己开 PR，也不会绕过 Code Review。



### 今天你已经能拿到什么


| 层次                          | 状态       | 你可以跑什么                                                 |
| --------------------------- | -------- | ------------------------------------------------------ |
| **Crash 自动修复**              | ✅ GA     | 空指针、abort、双重释放、竞态、栈溢出… → patch + 落盘                    |
| **Crash 证据诊断**              | ✅ GA     | 寄存器、maps、可选 PC 反汇编、证据罗盘（`04a`）                         |
| **ANR / AppFreeze / 卡死分析**  | ✅ GA（分析） | 自动路由 `anr_freeze_analysis`；热点 + EventHandler（`04c`）    |
| **内存压力 / OOM 线索**           | ✅ GA（旁路） | 日志侧 RSS/PSS/heap 线索 + 故障模式匹配（`04d`）；heap-diff 自动修复仍在规划 |
| **崩溃前业务路径**                 | ✅ GA（旁路） | logcat / HiLog / ASI 时序 → 生命周期与点击路径（`04e`）             |
| **ANR / OOM / Freeze 自动改码** | 🚧 打磨中   | 同一框架，按问题类逐步补齐 patch 工作流                                |


[完整 Roadmap →](#roadmap)

## 修复闭环

从 `v1.2.8` 开始，仓库内置了三个 Skill 预置，用来搭起 Crash 自动修复的**闭环骨架**。它们不是彼此孤立的功能点，而是前后衔接的一条流水线。

```
                 ┌────────────────────  自 动 修 复 闭 环  ─────────────────────┐
                 │                                                              │
                 │                                                              │
   ① 拉取工单          ② 自动修复             ③ 自动验证              ④ 自动打包    │
   bug-platform-        sa-agent              automation-             cicd-    │
   fetcher-skill        (Direct /             testing-skill          pipeline- │
   (你的实现)           LangChain /           (你的测试运行器)        skill      │
                        LangGraph)                                                  │
                                                                                  │
       ⬇                  ⬇                    ⬇                      ⬇    │
   工单号           →   解析 + 符号化    →   测试 / 冒烟         →   构建        │
   crash 日志          读源码上下文         回归检验             发布           │
   库文件目录           patch + 自动落盘    通过 / 失败           产物           │
                       (含备份)                                                    │
                                                                                  │
                 │  每一步都能独立运行。把它们串起来才形成真正的"修复闭环"——     │
                 │  当前由人工 / CI 串，未来由工作流自动串。框架是开放的接口。      │
                 │                                                              │
                 └──────────────────────────────────────────────────────────────┘
```



### 按你的目标选择入口


| 你想…                                 | 直接去                                                              |
| ----------------------------------- | ---------------------------------------------------------------- |
| 自动修复一份已经拿到手的崩溃日志                    | [快速开始 — 60 秒](#快速开始)                                             |
| 无 so / 无 API Key 诊断 ANR / AppFreeze | `--scope parse_stack_only` → 见 [证据驱动诊断](#证据驱动诊断开发者真正留下来的理由)      |
| 从工单系统拉取缺陷并准备分析输入                    | 使用 `bug-platform-fetcher` Skill 拉取数据，再交给 `sa-agent` 主流程          |
| 用项目自带测试验证修复结果                       | 一级菜单里的 `自动验证修复结果`（基于 `automation-testing`）                       |
| 把修好的产物推到 CI / 发布链路                  | 一级菜单里的 `自动生成修复后的新包`（基于 `cicd-pipeline`）                          |
| 把整个闭环串成一个团队专用 Skill                 | [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md) |




### 把闭环跑起来的基础命令

```bash
# ① 把 bug 从工单系统拉下来
sa-agent skill init bug-platform-fetcher-skill ./bug-platform-fetcher-skill \
                   --preset bug-platform-fetcher
sa-agent skill install ./bug-platform-fetcher-skill

# ③ 验证修复
sa-agent skill init automation-testing-skill ./automation-testing-skill \
                   --preset automation-testing
sa-agent skill install ./automation-testing-skill

# ④ 发布产物
sa-agent skill init cicd-pipeline-skill ./cicd-pipeline-skill \
                   --preset cicd-pipeline
sa-agent skill install ./cicd-pipeline-skill

# 通过 sa-agent skill CLI 端到端运行
sa-agent skill run bug-platform-fetcher-skill --input '{"ticket_id":"MY-123"}' --json
sa-agent --crash-log <log> --library-dir <dir> --code-root <dir>      # ② 自动修复
sa-agent skill run automation-testing-skill --input '{"build":{...}}' --json
sa-agent skill run cicd-pipeline-skill --input '{"artifact":{...}}'    --json
```

这些预置本质上只是脚手架：默认只生成 `SKILL.md` + `skill.json`，具体的平台逻辑、测试命令、发布流程需要你按项目实际情况补进去。详见 [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md) 与 [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md)。

### 你下一步可能想看的

- 🙋 **“我只想先自动修一份崩溃日志”** → [快速开始](#快速开始)，60 秒上手；诊断本身**无需 LLM Key**。
- 🔬 **“我只要寄存器 / ANR / 业务路径，不调大模型”** → [证据驱动诊断](#证据驱动诊断开发者真正留下来的理由)。
- 🖥 **“我想用浏览器一键跑全流程修复（本地开发）”** → [本地面板](#本地面板)。
- 🛠 **“我想把** `sa-agent` **接进自己的工具 / IDE / CI”** → [Python API](#python-api) + [Daemon 模式](#daemon-模式)。
- 🧩 **“我想扩展 Agent，加自己的 Tool / Workflow / Skill”** → [给开发者](#给开发者--四种贡献路径)。



## 快速开始



### 环境要求

- 二进制使用：无需 Python 运行时
- **Python 版本**：最低 **3.9**；**推荐 3.10–3.12**（依赖与 CI 主要在此区间验证）
  - 仅核心能力（解析 + 符号化 + 不调用 LLM）：3.9+ 通常可用
  - 含 `[rag]`（torch / transformers 等）：建议 **3.10–3.12**；3.9 可能遇到 ML 栈组合问题
  - macOS 建议优先使用 **Homebrew / pyenv** 安装的 Python，避免官方安装包未配置 CA 导致 SSL 失败
- （可选）符号化工具：`atos`（macOS 自带）或 `addr2line`（Linux，来自 binutils）



### 安装并启动（推荐）

**方式 A —** `pip`**（venv 或系统环境）**

```bash
# 安装（中国大陆可加 -i https://pypi.tuna.tsinghua.edu.cn/simple）
pip install stability-analysis-agent

# 含向量库 / 相似案例 RAG（推荐完整体验）
pip install "stability-analysis-agent[rag]"

# 进入交互向导
sa-agent
```

**方式 B —** `pipx`**（隔离 CLI，不污染全局 site-packages）**

```bash
# 先安装 pipx：https://pipx.pypa.io/
pipx install stability-analysis-agent
# 或含 RAG（体积较大、首次安装较慢）
pipx install "stability-analysis-agent[rag]"

sa-agent --help
```

**方式 C — 预编译二进制**：见下方「使用预编译 CLI 二进制」。

安装排错（Python 版本、SSL、pipx、`transformers`/`nn` 报错等）见 [docs/cli/INSTALL_TROUBLESHOOTING.md](./docs/cli/INSTALL_TROUBLESHOOTING.md)。

> 交互体验参考 Claude CLI：支持上下键菜单、分组式“设置 / 帮助”、可返回的操作路径，以及关键步骤确认。一级菜单围绕“快速开始分析 / 再次分析 / 闭环相关 Skill / 设置 / 帮助”展开，常见操作基本都能一两次按键完成。



### Demo：自动修一份 Crash（60 秒）

使用内置 Demo 可以快速体验“终端交互 + 自动修复完整链路”：

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
sa-agent
```

在向导中选择 `快速开始分析（推荐）`，然后输入：

```text
crash_log   -> examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash
library_dir -> examples/crash_cases/demo_basic/lib/mac
code_root   -> examples/crash_cases/demo_basic/code_dir
```

CLI 会先输出执行计划，再自动执行。AI 模式下它会解析、符号化、读源码上下文、生成 patch、本地落盘（含备份）。要自动修复自己的崩溃日志同样使用 `sa-agent` 交互输入路径即可。输出位于 `./reports/<timestamp>/`。

> 🎥 想继续体验闭环？先跑完上面的 demo，再回到一级菜单，继续试试 `自动验证修复结果` 和 `自动生成修复后的新包`。

### 本地面板

面向**已有本机符号库与源码路径**的开发者的浏览器壳，用于一键全流程修复；开源版内部工具，不是面向地图 SDK 客户的最终「仅上传日志」交付形态。

```bash
python3 daemon/server.py --host 127.0.0.1 --port 8765
open http://127.0.0.1:8765/
```

| 区域 | 作用 |
|------|------|
| **主区** | 粘贴崩溃日志路径或全文 →「运行全流程修复」（`scope=full`、自动改码，报告与 CLI 相同） |
| **侧栏 · 工作区** | 保存 `library_dir`、`code_roots` 到 `~/.config/stability-analysis-agent/web_preferences.json` |
| **侧栏 · 已安装 Skills** | 列出 `~/.config/.../skills` 下已安装项；开关启用/关闭；本地路径安装 |
| **改码成功后** | 可选「写入向量知识库」（`POST /runs/<id>/vector-db/commit`）；CLI 同样会确认（默认不写） |

`gen_prompt_only`、`parse_stack_only` 等参数扫描请用 CLI。详见 [本地面板指南](./docs/cli/WEB_UI_GUIDE.md)、[Daemon 服务指南](./docs/cli/DAEMON_SERVER_GUIDE.md)。

## 核心特性


| 特性 | 说明 |
| --- | --- |
| **端到端自动修复闭环** | Crash → 自动修复（解析 + 符号化 + patch + 落盘）→ 验证 → 上线，可与闭环 Skill 串起来 |
| **三级故障模式库** | 68 条规则，L1→L2→L3 分类（故障类型 → 触发机制 → 具体原因）；在 LLM 之前做确定性匹配 |
| **五级证据分级（Tier 1–5）** | 每个结论标注置信度：检测器报告(HIGH) > 寄存器+地址(HIGH) > 多栈特征(MEDIUM) > 单一特征(LOW) > 推测(LOW) |
| **信号子码语义解读** | SEGV_MAPERR、SEGV_ACCERR、BUS_ADRALN、FPE_INTDIV、ILL_ILLOPC 等 20+ 子码在 parse 阶段即输出根因提示 |
| **崩溃地址模式识别** | 接近零 → 空指针；0x6b6b → UAF（释放后填充）；0xDEADBEEF → 调试毒值；栈/堆区域分类 |
| **寄存器关联分析** | 提取 ARM64/ARM32/x86_64 寄存器 dump；检测 NULL 寄存器、UAF 模式、与崩溃地址的关联 |
| **调用栈分层** | 区分崩溃帧 / 首个非运行时帧 / 首个应用帧 —— 避免把系统帧误判为业务根因 |
| **选择性知识加载** | 模块→知识域路由（14 条映射）；RAG 只搜索相关模式，减少噪声 |
| **确定性前置分析** | `空指针` / `abort` / `除零` / `栈溢出` / `ASan 报告` 以 100% 置信度在 LLM 前确认 |
| **责任归属判定** | 按平台路径规则（Android/iOS/HarmonyOS/macOS/Linux）分类 application / system / vendor / third_party |
| **业务流水分析** | 崩溃前 logcat/HiLog/syslog → 操作路径推断（lifecycle → network → database → user_action → 崩溃） |
| **EventHandler + Binder 链路** | ANR 队列深度分析 + IPC 调用图遍历 + 死锁环检测 |
| **采样栈热点统计** | 函数出现频次、阻塞指标检测（mutex/futex/IO）、重复调用模式发现 |
| **可选反汇编** | `llvm-objdump` / `objdump` 封装 —— PC 附近指令、访存方向、涉及寄存器（仅在提供二进制时触发） |
| **结构化报告 Schema** | 强制 LLM 输出 7 段式结构：故障信息 → 三级根因 → 证据链 → 置信度 → 责任归属 → 修复建议 → 补充材料 |
| **Crash + ANR 同一 CLI** | 按 `log_kind` 自动路由：Crash → `crash_analysis`；AppFreeze / ANR → `anr_freeze_analysis`；混合场景按置信度决定主/辅轨 |
| **地址符号化** | `addr2line` / `atos` 在 LLM 之前就把地址变成函数名 + 行号 |
| **结构化日志解析** | iOS / Android / macOS / Linux / Windows / Harmony；Crash · ANR · OOM · Freeze 分类 |
| **RAG 知识库** | 规则表（快速路径）+ 向量检索（ChromaDB）；改码成功后**可选**写入本地向量库（CLI 确认 / Web 按钮，默认不自动写） |
| **Tool + Workflow + Skill** | 可插拔工具/工作流 + Claude 风格 Skill + `extensions/` 落地插件 |
| **对外 Agent 能力包** | 教 Claude Code / Cursor 正确调用 `sa-agent` |
| **多种接入方式** | CLI、HTTP Daemon（SSE）、本地面板、Python API |




### 证据驱动诊断（开发者真正留下来的理由）

丢一份日志，拿到的是**结构化报告**，不是一大段模型散文。即便只用
`--scope parse_stack_only`、**没有 so、没有 LLM Key**，也能产出可行动的 JSON：


| 报告 | 回答什么问题 |
| --- | --- |
| `01` 解析 | 信号 + **子码语义**、线程、`log_kind`（crash / app_freeze / anr_trace / oom…）、**地址模式分析** |
| `02` maps | 有则给出内存映射 / 模块布局 |
| `03` 符号化 | 函数 + 文件:行（无 `.so` 时干净跳过）、**调用栈分层**（崩溃帧 / 非运行时 / 应用帧） |
| `04a` **崩溃诊断** | **三级根因**（L1→L2→L3）、故障模式、**寄存器**、near-null、可选**反汇编**、**证据罗盘**、**确定性事实**、**证据等级（Tier 1–5）** |
| `04c` **ANR / Freeze** | 栈热点、**EventHandler** 队列（含鸿蒙 AppFreeze dump）、**Binder/IPC 链路**（死锁检测）、阻塞指标 |
| `04d` **内存压力** | RSS/PSS/heap/FD 线索 + 泄漏模式关键词（旁路 / `--force-memory-analysis`） |
| `04e` **业务路径** | 崩溃前 logcat / HiLog / ASI 时序 → **业务操作路径推断**（*用户当时在干什么*） |
| `04c`/`04f`/`04g`/`04h` **专项旁路** | Native 特征提示与栈分层 · AppFreeze Binder/系统噪声门禁 · API 错误码知识 · JS/ArkTS 故障模式 |
| `05` RAG | **故障模式库匹配**（68 条规则）+ 证据等级 + 知识域路由 + 相似模式 |
| `06` / `07` | 结构化 7 段式报告（启用 LLM 时）：故障信息 → 三级根因 → 证据链 → 置信度 → 责任归属 → 修复 → 补充材料 |


```bash
# 只要 Crash 证据 —— 不用 code-root，不用 API Key
sa-agent --crash-log ./app.crash --library-dir ./lib --scope parse_stack_only

# 鸿蒙 AppFreeze / Android ANR —— 可以没有 .so
sa-agent --crash-log ./appfreeze.txt --scope parse_stack_only

# 在富日志上强制内存 / 时序旁路
sa-agent --crash-log ./crashInfos.txt --scope parse_stack_only \
  --force-memory-analysis --force-timeline-analysis
```

设计说明：[docs/architecture/fault_mode_library.md](./docs/architecture/fault_mode_library.md) ·
报告编号：[docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md)。

## 架构

```
                  ┌──────────┐   ┌──────────┐   ┌──────────┐
                  │   CLI    │   │  Daemon  │   │  Python  │
                  │          │   │  (HTTP)  │   │   API    │
                  └────┬─────┘   └────┬─────┘   └────┬─────┘
                       │              │              │
                       └──────────────┼──────────────┘
                                      │
                            ┌─────────▼─────────┐
                            │ Tool + Workflow + │
                            │     Skill        │
                            └─────────┬─────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
   ┌────────────┐            ┌────────────┐            ┌────────────┐
   │  崩溃日志   │            │   地址     │            │   代码     │
   │   解析器    │            │  符号化器   │            │  提取器    │
   └────────────┘            └────────────┘            └────────────┘
                                      │
                            ┌─────────▼─────────────────────┐
                            │  sa-agent 自动修复核心           │
                            │  ┌─────────────────────────┐  │
                            │  │ Direct / LangChain /     │  │
                            │  │ LangGraph 引擎            │  │
                            │  └──────────┬──────────────┘  │
                            │             │                  │
                            │        ┌────▼─────┐            │
                            │        │   RAG    │            │
                            │        │ 规则 +   │            │
                            │        │ 向量检索 │            │
                            │        └────┬─────┘            │
                            │             │                  │
                            │        ┌────▼─────┐            │
                            │        │   LLM    │ (仅自动    │
                            │        │  patch   │  修复时)   │
                            │        └──────────┘            │
                            └───────────────────────────────────┘

   ┌─── Skill 系统（插件与闭环预置） ───────────────────────────────────┐
   │                                                                  │
   │  bug-platform-fetcher ──▶ automation-testing ──▶ cicd-pipeline    │
   │       (① 拉取)                  (③ 验证)               (④ 上线)   │
   │                                                                  │
   │  cli/main.py → SkillManager → SkillRuntime → extensions/         │
   └──────────────────────────────────────────────────────────────────┘
```

**诊断 + 自动修复流水线：**

```
Crash / ANR / AppFreeze 日志
        │
        ▼
   解析 (01) ──log_kind──▶ crash_analysis  或  anr_freeze_analysis
        │          │
        │          ├─ 信号子码语义（SEGV_MAPERR → UAF/越界提示）
        │          └─ 崩溃地址模式（接近零 / 0x6b / 毒值）
        │
        ▼
   Maps (02) → 符号化 (03) → 调用栈分层
        │                     （崩溃帧 / 非运行时 / 应用帧）
        │
        ├─▶ 04a 崩溃证据  （寄存器 · 故障分析 · 反汇编 · 证据罗盘）
        │       ├─ 确定性分析器（空指针 / abort / SIGFPE = 100% 事实）
        │       ├─ 三级故障模式匹配（68 规则: L1→L2→L3）
        │       ├─ 证据分级（Tier 1–5, HIGH/MEDIUM/LOW）
        │       └─ 责任归属（应用 / 系统 / 厂商 / 三方）
        │
        ├─▶ 04c ANR       （热点 · EventHandler 队列 · Binder/IPC 死锁检测）
        ├─▶ 04d 内存      （压力 / OOM 线索 · 泄漏模式）     [旁路]
        └─▶ 04e 时序      （业务操作路径推断: logcat/HiLog）  [旁路]
        │
        ▼
   源码上下文 (04b) → RAG (05: 选择性知识路由) → LLM (06: 7 段式报告) → 落盘 (07)
                              ▲
                              └── 请求更多上下文（context_loop）
```

> 详细架构图请参阅 [docs/architecture/ARCHITECTURE_DIAGRAM.md](./docs/architecture/ARCHITECTURE_DIAGRAM.md)。



## Skill 系统（sa-agent 运行时扩展）

`skill_system/` 包与 `sa-agent skill …` 子命令，为 `sa-agent` 提供了一层**可插拔的运行时扩展机制**（与上面提到的“对外能力包”不是一回事）。一个 Skill 本质上是一个目录，里面至少包含 Claude 风格的 `SKILL.md`，也可以附带机器可读的 `skill.json`。安装后，Skill 可以在启动时被自动发现，也可以被渲染为提示词片段，或者桥接到现有的 **Tool / Workflow** 运行时中。

### CLI 子命令

```bash
# 发现、列出、查看
sa-agent skill list [--skill-dir PATH]… [--json]
sa-agent skill show <name> [--json]

# 校验
sa-agent skill lint <path-to-skill-dir> [--json]

# 安装 / 卸载（目录或 .zip）
sa-agent skill install <source-dir-or.zip> [--target-root PATH] [--overwrite]
sa-agent skill uninstall <name> [--target-root PATH]

# 生成新 skill 模板（Claude 风格 prompt 或 workflow / tool / plugin）
sa-agent skill init <name> <target-dir> [--type prompt|workflow|tool|plugin] \
                   [--preset automation-testing|cicd-pipeline|bug-platform-fetcher]

# 运行：渲染提示词，或调用导出的 workflow / tool
sa-agent skill run <name> [args…] [--input path/to/input.json] [--json]
```



### 闭环 Skill 预置（Closed-Loop Presets）

三个 `--preset` 模板覆盖了“取 → 修 → 验 → 包”这条链路：


| 预置                     | 用途                       | 适用节点                                                       |
| ---------------------- | ------------------------ | ---------------------------------------------------------- |
| `bug-platform-fetcher` | 按工单号拉取对应的 crash 日志与调试库文件 | 自动修复**之前**，为 `sa-agent` 准备 `crash_log` / `library_dir` 等输入 |
| `automation-testing`   | 跑自动化测试 / 冒烟 / 回归验证已修复的产物 | Agent 修复完成并落盘之后                                            |
| `cicd-pipeline`        | 打包、构建、发布或交接已验证的修复产物      | 修复验证通过之后                                                   |


```bash
sa-agent skill init bug-platform-fetcher-skill ./bug-platform-fetcher-skill --preset bug-platform-fetcher
sa-agent skill init automation-testing-skill  ./automation-testing-skill  --preset automation-testing
sa-agent skill init cicd-pipeline-skill      ./cicd-pipeline-skill      --preset cicd-pipeline

sa-agent skill install ./bug-platform-fetcher-skill
sa-agent skill install ./automation-testing-skill
sa-agent skill install ./cicd-pipeline-skill
```

在交互式 `sa-agent` 向导中，验证与打包这两个闭环预置已经作为一级菜单入口展示；每项都会给出推荐的 `init` / `install` 命令，以及当前安装状态（`sa-agent skill show …`）。工单拉取场景则更适合通过 `sa-agent skill ...` 命令单独管理和调用。

> 这些预置在开源仓库里**都只是空骨架**。本项目**不会内置**任何具体平台（iCafe / Jira / WorkTile / 飞书 / 自建系统）的 API 对接。真正的平台实现，需要你通过 Skill 包自行扩展，详见 [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md) 与 [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md)。



### 发现目录与安装目录

- 默认安装根目录：`~/.config/stability-analysis-agent/skills`（可用 `--skill-home` 或 `STABILITY_AGENT_SKILL_HOME` 覆盖）。
- 启动时 `sa-agent` 还会扫描这些目录里的 skill：
  - `~/.claude/skills`
  - `./.claude/skills`（当前工作目录）
  - `<repo>/.claude/skills`
  - 通过 `--skill-dir` 或环境变量 `STABILITY_AGENT_SKILL_DIRS`（列表分隔符）追加的任意额外目录。
- 支持的安装包格式：skill **目录**或顶层就是 skill 目录的 `.zip` 压缩包。



### Skill 到 Tool / Workflow 的桥接

`skill.json` 声明 `entrypoint` 与 `exports` 数组：


| `entrypoint`      | 运行时行为                                                                        |
| ----------------- | ---------------------------------------------------------------------------- |
| `prompt`          | 渲染 `SKILL.md`，自动替换 `$ARGUMENTS` / `$SKILL_NAME` / `$SKILL_DIR` 等占位符，返回提示词字符串 |
| `workflow:<name>` | 调用通过 `exports.kind = workflow` 注册的工作流                                        |
| `tool:<name>`     | 调用通过 `exports.kind = tool` 注册的工具                                             |


带执行入口的 Skill 通过 `exports` 将自身注册回 `tool_system` 注册表，从而被现有执行器（`ConfigDrivenExecutor` / LangGraph 路由）调用：

```json
{
  "id": "crash-analysis-skill",
  "command_name": "crash-analysis",
  "type": "workflow",
  "entrypoint": "workflow:crash_analysis",
  "exports": [
    {
      "kind": "workflow",
      "ref": "my_package.my_skill:CrashAnalysisWorkflow",
      "name": "crash_analysis",
      "priority": "CUSTOM",
      "force_override": false,
      "enabled": true
    }
  ]
}
```

端到端示例（安装 → 校验 → 运行）见 [docs/skills/README.md](./docs/skills/README.md) 与 [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md)。

## 给开发者 — 四种贡献路径

如果你写过常见开发工具的插件，那么给 `sa-agent` 扩展能力也会很顺手。下面四条路径，基本覆盖了团队最常见的接入方式。


| 你想…                        | 起点                                                                                                                         | 你会提交                                                                                                                      |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **包装内部符号表服务**（Tool）        | `[extensions/tools/example_tool.py](./extensions/tools/example_tool.py)`                                                   | 编写一个 `BaseTool` 子类，并用 `@register_tool(priority=…)` 注册；`sa-agent` 会从 `~/.config/stability-analysis-agent/extensions/` 自动加载 |
| **打通团队的工单系统**（Skill）       | 预置 `bug-platform-fetcher` + [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md) | 编写一个返回 `{crash_log, library_dir, ticket_id, ...}` JSON 的 Skill，可对接 Jira / WorkTile / 飞书 / 自建系统                            |
| **接入项目自带的测试运行器**（Skill 预置） | 预置 `automation-testing` + [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md)         | 把项目里的测试命令封装成 Skill，并输出“通过 / 失败”等结果                                                                                        |
| **替换或扩展自动修复核心**（Workflow）  | [docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md)                       | 编写一个 `BaseWorkflow` 子类，并用 `@register_workflow(priority=Priority.CUSTOM)` 注册；同样放在插件目录中即可，无需额外 wrapper                      |


> 这四条路径遵循的是同一套扩展约定：把 `.py` 文件或目录放到 `sa-agent` 的扫描位置即可生效。没有额外 SDK，没有注册中心，也不需要 fork 主仓库。
> 分支 / DCO / 签名约定见 [CONTRIBUTING.md](./CONTRIBUTING.md)。



## 在 Claude / Cursor 等外部 Agent 中使用

如果你已经在使用 **Claude Code**、**Cursor** 等 AI 编程工具，可以安装仓库自带的对外能力包，让外部 Agent 知道该如何正确调用这套工具链（例如符号化、结构化报告、`--scope` 等），而不是靠猜命令或只粘贴原始日志来分析。

这与 `sa-agent skill install`（给 sa-agent 运行时安装扩展）**不是一回事**。能力包位于 `[stability-analysis-agent-skill/](./stability-analysis-agent-skill/)`，需复制到**外部 Agent 自己的** skill 目录。

**步骤 1 — 安装 Python 包**（提供 `sa-agent` 命令）：

```bash
pip install stability-analysis-agent
# 或：pipx install stability-analysis-agent
```

**步骤 2 — 安装能力包**到外部 Agent：

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cp -R stability-analysis-agent/stability-analysis-agent-skill ~/.claude/skills/stability-analysis-agent
```

**Cursor**（项目级示例）：

```bash
mkdir -p .cursor/skills
cp -R stability-analysis-agent/stability-analysis-agent-skill .cursor/skills/stability-analysis-agent
```

安装完成后，你就可以让外部 Agent“使用 Stability Analysis Agent 自动修复 crash”。它应该能够给出合适的 `sa-agent` 命令、选择正确的 `--scope`，并读取 `reports/<timestamp>/` 下生成的报告。


| 资源                                                                                             | 说明                                                 |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| [SKILL.md](./stability-analysis-agent-skill/SKILL.md)                                          | 外部 Agent 主入口                                       |
| [examples.md](./stability-analysis-agent-skill/examples.md)                                    | 可复制命令示例                                            |
| [reference.md](./stability-analysis-agent-skill/reference.md)                                  | 参数、报告、配置路径                                         |
| [docs/skills/README.md](./docs/skills/README.md)                                               | sa-agent Skill System（运行时扩展机制）                     |
| [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md)       | `automation-testing` / `cicd-pipeline` 闭环 Skill 模板 |
| [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md) | `bug-platform-fetcher` 模板                          |


> **没有 LLM Key？** 能力包内说明可使用 `--scope gen_prompt_only` — 完整解析 + 符号化 + 代码上下文 + 提示词文件，不调用 LLM。（自动修复本身需要 LLM；`gen_prompt_only` 模式跳过 LLM，仅输出结构化分析。）



## Roadmap

这个项目会持续公开推进。下面这张表既是当前状态，也反映了接下来要补齐的能力方向。

**发版节奏：** 项目**持续维护**。有一批有意义的修复或功能时，会推 PyPI / GitHub Release——树在动的时候大约**每月一档**，空窗期会安静一些；**不承诺固定日历日**。进度以 [`CHANGELOG.md`](./CHANGELOG.md) 与 [Releases](https://github.com/baidu-maps/stability-analysis-agent/releases) 为准；PR 协作预期见 [`CONTRIBUTING.md`](./CONTRIBUTING.md)。


| 里程碑                                                                            | 状态       | 首次发布                   |
| ------------------------------------------------------------------------------ | -------- | ---------------------- |
| **框架**                                                                         |          |                        |
| Tool + Workflow 注册框架                                                           | ✅ GA     | v1.1                   |
| Python API / Daemon 模式                                                         | ✅ GA     | v1.2.2                 |
| RAG `[rag]` extra（ChromaDB + sentence-transformers）                            | ✅ GA     | v1.2.6                 |
| `extensions/` 插件自动发现 + Tool / Workflow 示例                                      | ✅ GA     | v1.2.7                 |
| **自动修复核心**                                                                     |          |                        |
| Crash 自动修复（解析 + 符号化 + patch + 落盘）                                              | ✅ GA     | v1.0（核心）→ v1.2.8（闭环预置） |
| `bug-platform-fetcher` **/** `automation-testing` **/** `cicd-pipeline` **预置** | ✅ GA     | v1.2.8                 |
| **同一框架，新稳定性类**                                                                 |          |                        |
| ANR / AppFreeze / 卡死**分析**（热点、EventHandler、IPC）                                | ✅ GA     | v1.3.0                 |
| 内存压力 / OOM **线索**（日志侧 `04d`）                                                   | ✅ GA（旁路） | v1.3.0                 |
| 崩溃前业务路径 / 时序（`04e`）                                                            | ✅ GA（旁路） | v1.3.0                 |
| Crash 证据罗盘 + 寄存器 + 可选反汇编（`04a`）                                                | ✅ GA     | v1.3.0                 |
| ANR / Freeze **自动改码**（patch 落盘）                                                | 🚧 打磨中   | 后续小版本                  |
| OOM / 内存 **自动改码**（heap snapshot diff）                                          | 📋 计划中   | 后续小版本                  |
| **社区预置**                                                                       |          |                        |
| engine-build、iOS-XCUITest、Hypium 等 Skill 预置                                    | 🎯 社区驱动  | 接受 PR                  |
| Sentry、Bugsnag、Azure DevOps、Linear、Jira Cloud、飞书 等缺陷管理平台                       | 🎯 社区驱动  | 接受 PR                  |


长版 Roadmap（待办设计、RFC、设计笔记）见 [docs/ROADMAP.md](./docs/ROADMAP.md)。

## 兼容平台与运行时


| 层              | 支持范围                                                                                                                                                        |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **当前范围（自动修复）** | Crash —— 空指针、除零、abort、双重释放、死锁 / 竞态 / 原子操作失败、栈溢出…                                                                                                            |
| **当前范围（分析）**   | ANR / AppFreeze / 卡死 · 内存压力 / OOM 线索 · 崩溃前业务路径 · 寄存器 / 反汇编证据                                                                                                |
| **计划中（自动改码）**  | ANR patch · heap-diff OOM 修复 · 更深的 Freeze 自动修复                                                                                                              |
| **操作系统**       | macOS · iOS · Android · Harmony · Linux · Windows                                                                                                           |
| **崩溃日志格式**     | Apple `.crash` · Android logcat / tombstone · Harmony `Stacktrace:` · native `#NN pc` · Sentry / Firebase Crashlytics / Bugsnag / Bugly / 自建 APM 等的 JSON 导出 |
| **Python**     | 3.9 · 3.10 · 3.11 · 3.12                                                                                                                                    |
| **大模型服务商**     | 任意 OpenAI 兼容端点：OpenAI · DeepSeek · 文心 / ERNIE · GLM · 通义千问 · llama.cpp · vLLM                                                                               |
| **符号化工具**      | `addr2line`（Linux）· `atos`（macOS）· DWARF `.dSYM`                                                                                                            |
| **外部 Agent**   | Claude Code · Cursor · 任何支持 `~/.claude/skills/` 的 Agent                                                                                                     |


完整列表与新增 adapter 的方法：[docs/tools/CRASH_LOG_FORMATS.zh-CN.md](./docs/tools/CRASH_LOG_FORMATS.zh-CN.md)

## 其它方式（高级）



### 以 Python 集成（可编程接口）

自 **v1.2.4** 起，PyPI 包提供了稳定的 `[cli/api.py](./cli/api.py)` 模块，其中包括 `execute_analysis`、`build_parser`、`collect_interactive_run_state`、`interactive_state_to_argv`、`run_from_interactive_state`、`run_cli_main` 等接口，便于企业封装层或自动化脚本在进程内直接调用与 `sa-agent` 相同的分析链路，而不必依赖 `subprocess`。变更说明见 `[CHANGELOG.md](./CHANGELOG.md)`。

如需在代码中扩展 Skill 系统，公开接口可由 `[skill_system/](./skill_system/)` 直接导入：

```python
from skill_system import (
    SkillManager, SkillRuntime,
    load_skill_bundle, parse_skill_directory,
    available_skill_presets, write_skill_scaffold,
)

manager = SkillManager()        # 使用默认发现目录
runtime = SkillRuntime(manager)

# 直接生成空模板
write_skill_scaffold("./my-skill", "my-skill", preset="automation-testing")

# 渲染提示词类 Skill
prompt = runtime.render("my-skill", arguments="issue-123 json").prompt

# 以 JSON 输入调用 workflow / tool 类 Skill
result = runtime.execute("crash-analysis-skill", input_payload={
    "crash_log": "...", "library_dir": "./lib", "code_root": "./code"
})
```



### 使用预编译 CLI 二进制（无需 Python）

从 [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases) 下载最新二进制。压缩包与目录名随版本变化，请以实际 Release 文件名为准：

```bash
unzip StabilityAnalyzer-v1.2.4-mac-arm64.zip
cd output/cli_release/stability_analyzer_cli/v1.2.4-mac-arm64
./StabilityAnalyzer
```



### 开发者源码安装

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
pip install -e .
sa-agent
```

> `pip install -e .` 主要用于开发场景，同时也会暴露本地 `sa-agent` 命令。



### CLI 参数说明


| 参数                | 必须  | 说明                                                                                            |
| ----------------- | --- | --------------------------------------------------------------------------------------------- |
| `--crash-log`     | 是   | 崩溃日志文件路径（不限后缀，按内容识别格式，见 [崩溃日志格式说明](./docs/tools/CRASH_LOG_FORMATS.zh-CN.md)）                  |
| `--library-dir`   | 是*  | 库文件目录，包含 `.dylib`/`.so` 及调试符号（`.dSYM`）                                                        |
| `--code-root`     | 否   | 源码根目录，用于读崩溃点代码上下文                                                                             |
| `--scope <value>` | 否   | Agent 执行流程范围（默认 `full`），取值 `full` / `gen_prompt_only` / `parse_stack_only` / `parse_log_only` |
| `--daemon <url>`  | 否   | 委托给运行中的 Daemon 实例                                                                             |


 使用 `--scope parse_log_only` 时不需要。

### `--scope` 取值说明


| 取值                 | 行为                                                                                  |
| ------------------ | ----------------------------------------------------------------------------------- |
| `full`（默认）         | 解析 + maps + 符号化 + 诊断族（`04a` + 条件 `04c`/`04d`/`04e`）+ 源码 + LLM 自动修复。                 |
| `gen_prompt_only`  | 同上到提示词为止，不调用 LLM。                                                                   |
| `parse_stack_only` | 解析 + maps + 符号化 + 诊断（`04a` / ANR `04c` …）。无需 `--code-root` / LLM。适合无 so 的 ANR dump。 |
| `parse_log_only`   | 仅解析（`01`，含 `log_kind`）。                                                             |




### 支持的崩溃日志文件与平台导出

**文件后缀：** 不做白名单限制 — `.crash`、`.txt`、`.log`、`.json` 或无后缀均可，关键看**文件内容**是否匹配已知格式；也支持 `--crash-log -` 从 stdin 读取。RTF 导出会先转为纯文本。

**文本类（示例）：** Apple `.crash`、iOS 卡顿/Mach 导出、Android logcat/tombstone、Harmony `Stacktrace:` / `Tid:` dump、native 文本栈 `#NN pc 0x地址 /path/lib.so`。

**JSON 类导出：**


| 平台 / 形态                                                                                           | `01` 报告中的 `log_format`         |
| ------------------------------------------------------------------------------------------------- | ------------------------------ |
| Harmony 崩溃平台（`crashDiagnosis:` / `crashDiagnsis:` + JSON，含 `body.stacks[].call_stack` 的 `#NN pc`） | `harmony_crash_diagnosis_json` |
| [Sentry](https://sentry.io/) 事件 JSON                                                              | `sentry_event_json`            |
| [Firebase Crashlytics](https://firebase.google.com/docs/crashlytics) 事件 JSON                      | `firebase_crashlytics_json`    |
| [Bugsnag](https://www.bugsnag.com/) 事件 JSON                                                       | `bugsnag_event_json`           |
| Bugly / 友盟 / 自建 APM 等（`frames` / `stack_frames` 常见字段）                                             | `generic_json_stack_export`    |


完整列表、解析器优先级与扩展方式：**[docs/tools/CRASH_LOG_FORMATS.zh-CN.md](./docs/tools/CRASH_LOG_FORMATS.zh-CN.md)**

## Daemon 模式

Daemon 提供**流式输出（SSE）**、**进程复用**（免冷启动）、**任务取消**，并托管**本地面板**，适合 IDE 集成、浏览器壳与高频率分析：

```bash
# 启动 Daemon
sa-agent --daemon-server --host 127.0.0.1 --port 8765
# 或：python3 daemon/server.py

# 打开本地面板
open http://127.0.0.1:8765/

# 通过 Daemon 分析（CLI）
sa-agent --daemon http://127.0.0.1:8765 \
  --crash-log <崩溃日志> --library-dir <库目录> --code-root <源码目录>
```

> 完整 HTTP API 见 [Daemon 服务指南](./docs/cli/DAEMON_SERVER_GUIDE.md)；面板说明见 [本地面板指南](./docs/cli/WEB_UI_GUIDE.md)。



## Python API

```python
from tool_system import (
    ToolAndWorkflowRegistry, SystemConfig, WorkflowConfig,
    ConfigDrivenExecutor, register_all_tools_and_workflows
)

registry = ToolAndWorkflowRegistry()
register_all_tools_and_workflows(registry)

config = SystemConfig(
    workflows=[WorkflowConfig(name="crash_analysis", enabled=True)]
)
executor = ConfigDrivenExecutor(registry, config, llm_adapter=None)

result = executor.execute_workflow("crash_analysis", {
    "crash_log": open("crash.crash").read(),
    "library_dir": "./lib",
    "code_root": "./code"
})
print(result)
```



## 配置 LLM 与符号化工具

推荐通过交互向导配置：

```bash
sa-agent
```

进入后，在 `设置` 中选择 `配置大模型` 或 `配置堆栈地址解析工具`，流程会自动检测当前环境并给出引导。堆栈符号化向导支持 **“自动获取”** 和 **“手动设置符号化工具绝对路径”** 两种方式（可填写可执行文件路径，也可填写工具所在目录）；当你选择“快速开始分析”且流程需要符号化时，CLI 还会先静默尝试一次与“自动获取”相同的配置写入，以减少重复操作。

默认本地配置目录（安装后的 CLI）：

```bash
~/.config/stability-analysis-agent/
```

- `agent_config.local.json`：配置大模型 **厂商 / 密钥 / 模型**（对应 `llm_config.active_provider` 与 `llm_config.providers`）
- `add2line_resolver_config.local.json`：配置符号化工具搜索路径（`tool_paths` 为工具所在目录；可选 `environment_vars` 为 NDK/LLVM 等安装根，常由自动获取写入）

仓库内模板在 [`configs/`](./configs/)（如 `agent_config.local.example.json`）。可编辑安装时：若设置了 `STABILITY_AGENT_CONFIG_DIR` 则优先该目录，否则读 `<仓库根>/configs/agent_config.local.json`。请勿把带真实密钥的 `*.local.json` 提交进仓库。

若你偏好手动编辑，也可直接修改以上配置文件。

### 高级：add2line 配置路径覆盖

可通过环境变量显式指定 add2line 配置文件路径：

```bash
export STABILITY_AGENT_ADD2LINE_CONFIG_FILE="/绝对路径/add2line_resolver_config.local.json"
```



## 项目结构

```
stability-analysis-agent/
├── agent/              # 自动修复核心引擎（LangGraph 状态机）
├── cli/                # CLI 入口
├── daemon/             # HTTP Daemon（流式、SSE、托管 Web UI、web 偏好设置）
├── web/                # 本地面板静态资源（一键全流程修复 + 工作区 + Skills）
├── tools/              # 工具实现（解析器、符号化、代码提取）
│   └── configs/        # 配置模板
├── tool_system/        # Tool + Workflow 注册与调度框架
├── extensions/         # 用户级 Tool / Workflow 插件目录（自动发现）
│   ├── tools/          #   Tool 示例（extensions/tools/example_tool.py）
│   └── workflows/      #   Workflow 示例（extensions/workflows/example_workflow.py）
├── skill_system/       # Skill 发现、安装、运行时桥接（CLI 子命令）
│   ├── cli.py          # `sa-agent skill …` argparse 子解析器
│   ├── manager.py      # SkillManager：发现 / 安装 / 校验 / 注册
│   ├── runtime.py      # SkillRuntime：渲染 prompt / 执行 workflow / 执行 tool
│   ├── templates.py    # `available_skill_presets()`（3 个预置）+ 模板生成器
│   ├── models.py       # SkillBundle / SkillExport / SkillRunResult 数据类
│   └── parser.py       # `SKILL.md` + `skill.json` 解析器
├── workflows/          # Workflow 定义（Crash 自动修复）
├── rag/                # RAG：规则存储 + 向量索引（ChromaDB）+ 元数据
├── prompts/            # LLM 自动修复提示词模板
├── protocol/           # 统一请求/响应协议
├── examples/           # 内置崩溃案例
│   └── crash_cases/
│       ├── demo_basic/         # NullPtr、DivZero、Abort、DoubleFree 等
│       └── demo_multithread/   # 竞态条件、死锁、原子操作失败等
├── test/               # 测试套件（cli / daemon / web / rag / ai_regression / …）
├── .github/workflows/  # CI、可选 AI 回归、PyPI 发布
├── .devcontainer/      # Codespaces / VS Code Dev Container（轻量，默认不含完整 [rag]）
├── stability-analysis-agent-skill/  # 对外 Agent 能力包（Claude / Cursor 等）
└── docs/               # 文档
```



## 文档导航


| 主题                         | 链接                                                                                                   |
| -------------------------- | ---------------------------------------------------------------------------------------------------- |
| CLI 使用指南                   | [docs/cli/CLI_GUIDE.md](./docs/cli/CLI_GUIDE.md)                                                     |
| CLI 参数参考                   | [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md)                           |
| Daemon 服务指南                | [docs/cli/DAEMON_SERVER_GUIDE.md](./docs/cli/DAEMON_SERVER_GUIDE.md)                                 |
| 本地面板                       | [docs/cli/WEB_UI_GUIDE.md](./docs/cli/WEB_UI_GUIDE.md)                                               |
| 测试（单元 / AI 回归 / Web·Daemon / CI） | [docs/testing/README.md](./docs/testing/README.md)                                                   |
| PyPI 发布（脚本 + GitHub Actions）       | [docs/scripts/PYPI_RELEASE_SCRIPTS.md](./docs/scripts/PYPI_RELEASE_SCRIPTS.md)                       |
| Codespaces / Dev Container           | [.devcontainer/README.md](./.devcontainer/README.md)                                                 |
| 对外 Agent 能力包               | [stability-analysis-agent-skill/](./stability-analysis-agent-skill/)                                 |
| Skill 系统（sa-agent 运行时）     | [docs/skills/README.md](./docs/skills/README.md)                                                     |
| 闭环 Skill 模板                | [docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md](./docs/skills/CLOSE_LOOP_SKILL_TEMPLATES.md)             |
| 缺陷平台拉取模板                   | [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md)       |
| Skill 模板参考                 | [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md)                                     |
| AI 代码回归（重定向）              | [docs/testing/AI_REGRESSION.md](./docs/testing/AI_REGRESSION.md)                                     |
| Roadmap 长版                 | [docs/ROADMAP.md](./docs/ROADMAP.md)                                                                 |
| 系统架构                       | [docs/architecture/README.md](./docs/architecture/README.md)                                         |
| 架构图                        | [docs/architecture/ARCHITECTURE_DIAGRAM.md](./docs/architecture/ARCHITECTURE_DIAGRAM.md)             |
| 故障模式 · 证据链 · ANR / 内存 / 时序 | [docs/architecture/fault_mode_library.md](./docs/architecture/fault_mode_library.md)                 |
| Tool System 概览             | [docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md](./docs/tools/tool_system/TOOL_SYSTEM_OVERVIEW.md)   |
| 工具扩展指南                     | [docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md) |
| Workflow 系统                | [docs/workflows/WORKFLOWS.md](./docs/workflows/WORKFLOWS.md)                                         |
| RAG 向量数据库                  | [docs/rag/README.md](./docs/rag/README.md)                                                           |
| 崩溃示例                       | [docs/crash_cases/README.md](./docs/crash_cases/README.md)                                           |
| 崩溃日志格式与平台支持                | [docs/tools/CRASH_LOG_FORMATS.zh-CN.md](./docs/tools/CRASH_LOG_FORMATS.zh-CN.md)                     |




## 测试

完整说明：**[docs/testing/README.md](./docs/testing/README.md)**（单元测试、AI 回归、Web/Daemon 契约、GitHub Actions）。

**提交前（不调用 LLM）** — 与 [`.github/workflows/ci.yml`](./.github/workflows/ci.yml) 同一套：

```bash
python3 -B -m unittest \
  test.ai_regression.test_runner \
  test.cli.test_report_paths \
  test.cli.test_vector_db_commit_prompt \
  test.rag.test_case_writer \
  test.daemon.test_build_cli_cmd \
  test.daemon.test_skills_api \
  test.daemon.test_run_lifecycle \
  test.daemon.test_vector_db_commit_api \
  test.daemon.test_web_preferences \
  test.skill_system.test_installed_skills_runtime \
  test.web.test_web_contract
```

**GitHub Actions：**

| Workflow | 触发时机 | 内容 |
|----------|----------|------|
| [CI](./.github/workflows/ci.yml) | PR / push 到 `main`/`master` | 确定性套件（Python matrix）+ 工具链抽查 |
| [AI Regression](./.github/workflows/ai-regression.yml) | 手动运行，或 PR 打上 `ai-regression` label | 真实 LLM 改码回归（需 API Secret） |
| [Publish PyPI](./.github/workflows/publish-pypi.yml) | tag `v*`，或手动 | 构建 + Trusted Publishing 上传（先过确定性门禁） |

**Codespaces：** [`.devcontainer/`](./.devcontainer/) — 创建时执行 `pip install -e ".[test]"`；在 GitHub 上 **Code → Codespaces** 打开即可。

**抽查：**

```bash
python3 test/tool_system/test_regression.py
python3 test/skill_system/test_skill_system.py
python3 test/llm/test_llm_connection.py --provider openai   # 需 API Key
```

**发布（真实 LLM、代码回归）：**

```bash
python3 scripts/run_ai_regression.py --case test/ai_regression/cases/demo_basic_nullptr.json
# 若改动 daemon / Web 壳，追加：
python3 scripts/run_ai_regression.py --case test/ai_regression/cases/demo_basic_nullptr.json --entrypoint daemon
```

另见 [docs/testing/AI_REGRESSION.md](./docs/testing/AI_REGRESSION.md)、[docs/testing/WEB_DAEMON_TESTS.md](./docs/testing/WEB_DAEMON_TESTS.md)。



## 常见问题

**Q：“自动修复”到底修到什么程度？Agent 会替我合并** `main`**、直接上线吗？**
不会。本仓库里“自动修复”的范围是：`解析 → 符号化 → 读源码 → 生成 patch → 本地落盘（含备份）`。从这一步开始，控制权会回到你手上，或者交给闭环上的其它 Skill（例如验证、打包）。Agent 不会自己开 PR、合并 `main`，也不会绕过 Code Review。这里的自动修复闭环是开放接口，不是无人值守系统。（同样的边界说明也见 [为什么不用 AI 编程工具](#为什么不用-ai-编程工具)。）

**Q：符号化失败？**
确保 `--library-dir` 包含二进制文件（`.dylib` / `.so`）及其调试符号（`.dSYM` 目录或 DWARF 信息）。交互式 CLI 中可在 `设置 → 配置堆栈地址解析工具` 使用 **自动获取**，或 **手动设置符号化工具绝对路径**（可执行文件或工具所在目录）；亦可编辑 `~/.config/stability-analysis-agent/add2line_resolver_config.local.json`（参见 `configs/add2line_resolver_config.local.example.json`）。

**Q：LLM 步骤失败，或者我没有 LLM Key，**`sa-agent` **还能用吗？**
可以。用 `--scope gen_prompt_only` 生成可复用提示词，或用 `--scope parse_stack_only` 只拿诊断 JSON（`04a` / `04c` / …）——**完全不需要 API Key**。结构化报告本身就能贴进聊天或交给 reviewer。

**Q：代码上下文读取为空？**
确保 `--code-root` 指向的源码目录包含符号化堆栈中引用的文件。

**Q：现在支持 ANR / OOM / 卡死吗？**
**分析：已经支持。** AppFreeze / Android ANR traces 会自动走 ANR workflow（`04c`）；内存压力与崩溃前时序是旁路（`04d` / `04e`）。这些类别的**自动改码 patch** 仍在打磨——今天 GA 的是 Crash 自动修复。详见 [今天你已经能拿到什么](#今天你已经能拿到什么) 与 [Roadmap](#roadmap)。

**Q：如何在 Claude Code 或 Cursor 里使用？**
先安装 Python 包（`pip install stability-analysis-agent`），再将 `[stability-analysis-agent-skill/](./stability-analysis-agent-skill/)` 复制到外部 Agent 的 skill 目录（例如 `~/.claude/skills/stability-analysis-agent`）。详见上文 [在 Claude / Cursor 等外部 Agent 中使用](#在-claude--cursor-等外部-agent-中使用)。

**Q：如何给** `sa-agent` **增加自己的 Skill（例如自定义验证步骤、CI 流程、工单系统）？**
可以先用 `sa-agent skill init <name> ./<dir> --preset bug-platform-fetcher|automation-testing|cicd-pipeline` 生成闭环骨架，也可以用 `sa-agent skill init <name> ./<dir>` 生成空白的 prompt / workflow / tool Skill。补齐逻辑后，通过 `sa-agent skill install ./<dir>` 安装，再用 `sa-agent skill list` / `sa-agent skill show <name>` 验证。如果希望把它真正接入执行器，只需要在 `skill.json` 中声明 `entrypoint` 与 `exports`。详见上文 [Skill 系统](#skill-系统sa-agent-运行时扩展) 与 [docs/skills/SKILL_TEMPLATE.md](./docs/skills/SKILL_TEMPLATE.md)。

**Q：**`sa-agent` **是否开箱即用就会调用 Jira / iCafe / WorkTile / 飞书 这类平台 API？**
**不会**。`bug-platform-fetcher` Skill 预置只是一个**空骨架**：它定义了接口契约，例如需要下载什么、返回什么 JSON 结构，但**不包含**任何具体平台代码。真正的对接逻辑应该放在你们自己的私有包或内部仓库里，详见 [docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md](./docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md)。

**Q：**`sa-agent` **与** `bd-sa-agent` **是什么关系？**
本仓库（`stability-analysis-agent`）是开源核心，包含框架、Crash 自动修复能力，以及 Skill 扩展机制。`bd-sa-agent` 则是企业级闭源包装，会接入内部 LLM 提供方、内部工单系统后端，以及打包好的二进制 release。Skill 系统的意义就在于：这类企业接入可以**建立在开源核心之上扩展**，而不必长期 fork 主仓库。

## 贡献

欢迎贡献代码！提交前请阅读 [CONTRIBUTING.md](./CONTRIBUTING.md)。

```bash
# 所有提交需包含 DCO 签名
git commit -s -m "feat: 描述你的改动"
```

最容易上手的第一份 PR 通常是：

- 一个新的 **bug-platform-fetcher** 适配（你们团队在用的工单系统），放在 `extensions/bug-platform/<vendor>-fetcher/` 目录下；如果通用性广，欢迎提 PR 上游。
- 一个新的 **automation-testing** 预置（pytest / XCTest / GTest / Hypium / adb shell）。
- 一个新的 **崩溃日志格式** 适配（你们内部某 APM），放在 `tools/crash_log_parser/` 下，见 [docs/tools/CRASH_LOG_FORMATS.zh-CN.md](./docs/tools/CRASH_LOG_FORMATS.zh-CN.md)。
- 一类新的**稳定性问题自动修复**（ANR / OOM / Freeze）—— 最有野心的一份 PR，详见 [Roadmap](#roadmap)。



## 许可证

[Apache License 2.0](./LICENSE)

## 联系方式


| 渠道            | 链接                                                                            |
| ------------- | ----------------------------------------------------------------------------- |
| GitHub Issues | [提交 Bug 或功能建议](https://github.com/baidu-maps/stability-analysis-agent/issues) |
| 邮箱            | [hong9988.dev@gmail.com](mailto:hong9988.dev@gmail.com)                       |


**维护者：**


| 姓名      | GitHub                                       | 邮箱                                                      |
| ------- | -------------------------------------------- | ------------------------------------------------------- |
| liuhong | [@liuhong996](https://github.com/liuhong996) | [hong9988.dev@gmail.com](mailto:hong9988.dev@gmail.com) |


---

如果这个项目帮你自动修过哪怕一次 crash，欢迎点个 **Star** 支持一下。  
📣 **Star** 之后，**另请开一个 Issue** 告诉我们你最想加深的是 ANR/OOM 自动改码、哪家 APM adapter，还是哪个工单 Skill（Sentry、Bugsnag、Azure DevOps、Linear、Jira Cloud、飞书、自建……）—— 这些信号会决定 [roadmap](./docs/ROADMAP.md) 的下一步。
