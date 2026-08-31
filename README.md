# Stability Analysis Agent

**一个面向 App 稳定性问题的开源 Agent 框架：通过可扩展的 Tool、Workflow、Skill 和 RAG 组件，把 Crash 日志中的地址、线程、寄存器、符号与源码组织成可验证的证据链，并驱动 AI 直接修复代码。**

**简体中文** | [English](./README.en.md)

[![PyPI](https://img.shields.io/pypi/v/stability-analysis-agent.svg)](https://pypi.org/project/stability-analysis-agent/)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](./LICENSE)

## 为什么稳定性问题需要专用 Agent

稳定性日志通常噪音大、地址多、上下文分散，通用 AI 编程工具往往需要人工整理后才能分析；专用 Agent 则负责把这些事故材料整理成可用的诊断证据。

| 稳定性问题 | 通用 AI 编程工具的困难 | 当前 Agent 提供的能力 |
|---|---|---|
| 日志噪音大，关键线程和调用栈不明显 | 手工筛选真正相关的崩溃信息 | 解析日志结构，识别异常类型、崩溃线程和关键调用栈 |
| Native 堆栈只有内存地址 | 无法根据地址直接判断函数和源码位置 | 结合匹配的符号文件完成地址符号化和源码定位 |
| 崩溃位置不一定是根因 | 容易根据表面堆栈生成猜测 | 结合故障地址、寄存器、调用关系等信息构建 Crash 证据链 |
| 日志、符号文件和源码彼此分离 | 需要手工整理和粘贴上下文 | 自动关联事故材料，提取相关源码上下文 |
| 同类问题反复发生 | 历史分析结果难以复用 | 通过规则和向量数据库沉淀经验，检索相似案例辅助分析 |

## 如何安装

### 推荐安装

要求 Python 3.9+，推荐 Python 3.10–3.12。默认安装包含完整的相似案例检索能力。

```bash
pip install stability-analysis-agent
```

中国大陆网络环境可以为 pip 命令追加镜像参数。安装失败、Python 版本、SSL 和 ML 依赖问题，见 [安装与依赖排错](./docs/cli/INSTALL_TROUBLESHOOTING.md)。

## 快速开始

### 运行内置 Demo

克隆仓库后启动交互式 CLI。选择“快速开始修复（推荐）”，按菜单提示配置大模型和堆栈地址解析工具，再输入内置 Demo 的日志、符号库和源码路径。这个流程会完成日志解析、符号化、证据链诊断、源码定位、AI 分析和源码修复：

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cd stability-analysis-agent
sa-agent
```

启动后会看到一级菜单，首次体验只需关注以下选项：

```text
请选择要执行的操作
❯ 1) 快速开始修复（推荐）
  2) 设置
  3) 帮助
  q) 退出
```

首次使用时按下面的路径操作：

```text
1) 快速开始修复（推荐）
  → 未配置模型时，按提示进入“大模型与路由设置”
  → 未检测到符号化工具时，按提示进入“配置堆栈地址解析工具”
  → 输入 Crash 日志、符号库目录和源码目录
  → 确认执行计划，开始完整分析和源码修复
```

内置 Demo 使用以下路径：

```text
Crash 日志：examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash
符号库目录：examples/crash_cases/demo_basic/lib/mac
源码目录：examples/crash_cases/demo_basic/code_dir
```

### 查看最终结果

Demo 中的空指针 Crash 会被定位并修复，源码从：

```cpp
int* p = nullptr;
*p = 42;
```

修改为：

```cpp
int* p = nullptr;
if (p != nullptr) {
    *p = 42;
} else {
    std::cerr << "错误: 尝试解引用空指针" << std::endl;
}
```

原文件会保留备份，代码修改可以通过 `git diff` 审查。

同一次运行还会生成面向开发者阅读的最终报告：

```text
reports/<timestamp>/final_output.md
```

报告主要回答：

- 发生了什么问题
- 根因是什么
- 为什么可以这样判断
- 哪些源码需要修改
- 采取了什么修复措施
- 还需要补充哪些材料

报告主要包含：

- `故障基本信息`：异常类型、信号、崩溃线程、平台和崩溃模块
- `三级根因定位`：从问题类别、触发机制到具体根因
- `证据链`：故障地址、符号化栈帧、源码证据、调用链和线程信息
- `置信度与证据等级`：当前判断的可信程度和依据
- `责任归属`：问题对应的模块、函数或代码责任范围
- `修复建议`：代码级修复和必要的防御性措施
- `需补充材料`：当前判断仍需要的日志、源码或运行信息
- `总结`：问题原因、修复结果和后续建议

## 支持的平台与能力边界

当前内置 Crash 分析链路覆盖 iOS、macOS、Android、鸿蒙、Linux 和 Windows，统一完成日志解析、堆栈符号化、证据分析、源码上下文提取和 AI 修复。不同平台通过对应的日志解析器和符号化工具接入，具体格式见 [Crash 日志格式说明](./docs/tools/CRASH_LOG_FORMATS.md)。

Agent 的核心边界不在于“能否读取某个平台的日志”，而在于是否存在可用的日志格式适配器、符号化工具和分析 Workflow。缺少这些适配时，第三方可以通过 Tool、Workflow 或 Skill 扩展，而不需要修改核心执行框架。

ANR、OOM、Jank、JavaScript/ArkTS 等问题已经具备专项分析组件；具体能力和扩展方式见 [诊断工具文档](./docs/tools/)、[Tool System 扩展指南](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md) 和 [Skill System](./docs/skills/README.md)。

## 遇到问题怎么办

- 使用问题、Bug 和功能建议：提交 [GitHub Issue](https://github.com/baidu-maps/stability-analysis-agent/issues)。
- 安装、CLI 使用和扩展问题：先看 [安装与依赖排错](./docs/cli/INSTALL_TROUBLESHOOTING.md)、[CLI 指南](./docs/cli/CLI_GUIDE.md) 和 [Tool System 扩展指南](./docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md)。
- 安全漏洞：请按 [安全策略](./SECURITY.md) 私下联系维护者，不要公开提交细节。
- 提交 Issue 时请附上版本、操作系统、运行命令、日志格式和脱敏后的报告，不要上传密钥或未脱敏的线上数据。

维护者：[@liuhong996](https://github.com/liuhong996) · [hong9988.dev@gmail.com](mailto:hong9988.dev@gmail.com)

项目版本和重要变更见 [GitHub Releases](https://github.com/baidu-maps/stability-analysis-agent/releases) 与 [CHANGELOG.md](./CHANGELOG.md)。项目采用 [Apache-2.0](./LICENSE)，贡献流程和 DCO 要求见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

欢迎提交 Issue、改进文档和代码。

## 文档地图

| 想了解什么 | 文档 |
|---|---|
| 安装失败、依赖和环境问题 | [docs/cli/INSTALL_TROUBLESHOOTING.md](./docs/cli/INSTALL_TROUBLESHOOTING.md) |
| 常见 CLI 用法 | [docs/cli/CLI_GUIDE.md](./docs/cli/CLI_GUIDE.md) |
| 全部 CLI 参数 | [docs/cli/CLI_COMMANDS_REFERENCE.md](./docs/cli/CLI_COMMANDS_REFERENCE.md) |
| 使用本地 Web 面板 | [docs/cli/WEB_UI_GUIDE.md](./docs/cli/WEB_UI_GUIDE.md) |
| Crash 日志格式 | [docs/tools/CRASH_LOG_FORMATS.md](./docs/tools/CRASH_LOG_FORMATS.md) |
| C++、ANR、JS、Jank 等诊断能力 | [docs/tools/](./docs/tools/) |
| Skill 和扩展机制 | [docs/skills/README.md](./docs/skills/README.md) |
| 系统架构 | [docs/architecture/README.md](./docs/architecture/README.md) |
| 测试方式 | [docs/testing/README.md](./docs/testing/README.md) |
| 后续规划 | [docs/ROADMAP.md](./docs/ROADMAP.md) |
| 许可证与贡献协议 | [LICENSE](./LICENSE) · [CONTRIBUTING.md](./CONTRIBUTING.md) |

## 从哪里继续

- 第一次使用：复制上面的 [Demo 命令](#先运行内置-demo)。
- 已有 Crash 日志：参考内置 Demo 的参数，替换为自己的日志、符号库和源码路径。
- 想接入团队流程：阅读 [Skill System](./docs/skills/README.md) 和 [CLI 指南](./docs/cli/CLI_GUIDE.md)。
- 想理解实现方式：阅读 [系统架构总览](./docs/architecture/README.md)。
