# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

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
