# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Changed

- **配置目录**：示例与本地配置从 `tools/configs/` 迁至仓库根目录 `configs/`。`agent_config.local.json` / `add2line_resolver_config.local.json` 继续被 `.gitignore` 忽略，勿提交密钥。
- **LLM 配置加载**：`STABILITY_AGENT_CONFIG_DIR`（若设置）优先；否则开源源码树读 `<仓库根>/configs/agent_config.local.json`；安装后的 CLI 读 `~/.config/stability-analysis-agent/agent_config.local.json`。闭源工作区通过入口自动将 `STABILITY_AGENT_CONFIG_DIR` 指向其 `configs/`。

## [1.2.8] - 2026-07-15

### Added

- **Bug Platform Fetcher Skill 模板**：新增内置 `--preset bug-platform-fetcher`，对应一级菜单「4) 根据缺陷管理平台自动修复（基于 bug-platform-fetcher-skill）」。预设只生成空骨架（`SKILL.md` + `skill.json`），**绝不调用任何具体平台 API**（iCafe / Jira / WorkTile / 飞书 / 自建系统都由下游团队在自己仓库里实现）。详见 `docs/skills/BUG_PLATFORM_FETCHER_TEMPLATE.md`。
- **一级菜单调整**：把闭源 `bd-sa-agent` 的「输入 iCafe 编号自动修复」抽象为通用「根据缺陷管理平台自动修复」，并将其放到一级菜单"快速开始修复（推荐）"正下方（数字键 4）。这与一键"退出置顶"、设置 / 帮助置末的约定一致。
- **CLI 文档**：`docs/cli/INTERACTIVE_CLI_DESIGN.md` 与 `docs/cli/CLI_GUIDE.md` 已同步增补新菜单项及其与闭源版的边界（"开源仓库**不**带任何具体平台 API 调用"）。
- **测试**：`test/skill_system/test_skill_system.py::test_bug_platform_fetcher_preset_is_generic_template` 断言该预设元数据与生成的 `SKILL.md` 中**绝不包含** `icafe-cli` / `uuap.baidu` / `bcebos` / `baidu-int.com` / `UGate` 等内网 API 痕迹，防止以后被人无意中塞进具体平台实现。

### Changed

- 顶部一级菜单文案统一：「快速开始分析」→「快速开始修复」、「再次进行上一次分析」→「再次进行上一次修复」（与闭源版用语对齐）。

## [1.2.7] - 2026-07-15

### Added

- **`extensions/` 扩展框架**：仓库自带 `extensions/tools/example_tool.py` 与 `extensions/workflows/example_workflow.py` 两个可运行模板，覆盖 `@register_tool` / `@register_workflow` 装饰器、`ToolDefinition` / `WorkflowDefinition`、以及 `WorkflowContext.execute_tool()` 等接入方式；`extensions.register_all()` 在 CLI 启动时被自动调用，逐级扫描仓库自带扩展、`~/.config/stability-analysis-agent/extensions/`、`<cwd>/.stability-analysis-agent/extensions/`、`STABILITY_AGENT_EXT_DIRS` 追加目录，以及 Python 入口点 `stability_analysis_agent.tools` / `stability_analysis_agent.workflows`。详见 `docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md`。
- **设置菜单新增缓存与自检**：增加「查看本地缓存（cli_reports 占用）」与「清理本地缓存（cli_reports）」两个菜单项，背后由新模块 `cli/report_paths.py` 提供（`format_bytes / summarize_cli_reports / clear_cli_reports / print_cli_reports_overview`），可按"全部 / 仅最近 N 份"清理 `cli_reports/`。
- **`cli/upgrade.run_upgrade_check_interactive`**：设置菜单中的「检查更新（升级 sa-agent 到最新版）」接入公网 PyPI 公开发布渠道，探测 `pip / pipx / 编辑模式 / 预编译二进制` 安装方式，推荐与具体环境匹配的升级命令，可一键 `pip install -U "stability-analysis-agent[rag]"`。

### Documentation

- `docs/tools/tool_system/TOOL_SYSTEM_EXTENSION.md`：补充「仓库级示例 `extensions/`」、「用户级扩展发现」、「Python 入口点发布扩展包」三个段落，及设置菜单新动作的说明。
- `docs/README.md`：在文档目录结构下新增「仓库自带扩展（`extensions/`）」段。
- `README.md` / `README.zh-CN.md`：同步 Skill System / 闭环 Skill 模板章节的扩展机制。
- 新增 `test/cli/test_report_paths.py`：覆盖 `format_bytes` 单位换算、`summarize / clear_cli_reports` 在正常、缺失目录、`preview_limit=1` 下的行为，以及 example 模板与用户级扩展自动发现。

## [1.2.6] - 2026-06-03

### Changed

- **PyPI 依赖**：核心包不再捆绑 RAG/ML 栈；向量检索与相似案例能力改为可选 extra `pip install "stability-analysis-agent[rag]"`（版本区间与 `requirements-rag.txt` 对齐）。
- **打包**：wheel/sdist 纳入 `services` 子包；核心依赖新增 `ripgrep`。
- **Interactive CLI（符号化工具）**：从「设置 → 配置堆栈地址解析工具」完成手动路径或自动写入后，仅保留一次「按回车继续」，避免与内层保存成功面板重复。

## [1.2.5] - 2026-05-14

### Changed

- **Interactive CLI（堆栈符号化配置）**：移除「从 shell 读取环境变量 KEY 并写入配置」的向导菜单；手动配置改为 **「手动设置符号化工具绝对路径」**（可输入可执行文件或其所在目录）。在需要符号化的 **快速开始** 流程中，会先静默尝试与 **自动获取** 相同的配置写入，减少重复操作。文档与 `add2line_resolver_config.local.example.json` 已同步说明 `tool_paths` / `environment_vars` 含义。
- **Interactive CLI（大模型文案）**：菜单与检测摘要中面向用户的 **provider** 统一改为 **厂商** 等中文表述（配置 JSON 字段名 `active_provider` / `providers` 仍保持不变）。
- **Documentation**：`README.md` / `README.zh-CN.md`、`docs/cli/CLI_GUIDE.md`、`docs/cli/INTERACTIVE_CLI_DESIGN.md` 与 `tools/configs/agent_config.local.example.json` 中面向用户的 LLM 说明已与「厂商」用语对齐（英文 README 使用 *vendor*；脚本参数 `--provider` 等接口名不变）。
- **Interactive CLI（大模型向导）**：交互提示将裸写 `base_url` 改为「接口请求地址」，并在说明中保留 JSON 字段名 `base_url` 以便对照配置文件。
- **LLM 示例配置**：`tools/configs/agent_config.local.example.json` 移除仅占位的 `your_*` 自定义厂商条目，避免用户复制到 `agent_config.local.json` 后在厂商菜单中出现无关项；说明与 JSON 模板见 `docs/cli/CLI_GUIDE.md`。

## [1.2.2] - 2026-05-07

### Added

- **`cli.api`**: stable programmatic entry points for embedding and enterprise wrappers (`build_parser`, `execute_analysis`, `collect_interactive_run_state`, `interactive_state_to_argv`, `parse_analysis_args`, `run_from_interactive_state`, `run_cli_main`).
- **`bd-sa-agent` / closed-workspace note**: downstream packages should avoid shipping a top-level `cli` package that shadows this library’s `cli` module.
- **Enterprise LLM menu (opt-in)**: when env `STABILITY_AGENT_BD_ENTERPRISE` is set to `1`/`true`/`yes`/`on`, the interactive “配置大模型” flow adds **「配置百度内部API-KEY」** (under “重新设置”, and also when LLM is not yet configured). Default `sa-agent` behavior is unchanged.

### Changed

- Interactive CLI: streamlined confirmations, optional LLM connectivity check, Ctrl+C handling, and related UX tweaks.
- Renamed public API: `execute_analysis`, `collect_interactive_run_state` (formerly private `_execute_analysis`, `_collect_interactive_run_state`).

### Fixed

- LLM `base_url` handling when configured with a trailing `/chat/completions` path (avoids duplicated path segments with OpenAI-compatible clients).
