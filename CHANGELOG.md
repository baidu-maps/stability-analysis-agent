# Changelog

All notable changes to this project are documented in this file.

## [1.2.2] - 2026-05-07

### Added

- **`cli.api`**: stable programmatic entry points for embedding and enterprise wrappers (`build_parser`, `execute_analysis`, `collect_interactive_run_state`, `interactive_state_to_argv`, `parse_analysis_args`, `run_from_interactive_state`, `run_cli_main`).
- **`bd-sa-agent` / closed-workspace note**: downstream packages should avoid shipping a top-level `cli` package that shadows this library’s `cli` module.

### Changed

- Interactive CLI: streamlined confirmations, optional LLM connectivity check, Ctrl+C handling, and related UX tweaks.
- Renamed public API: `execute_analysis`, `collect_interactive_run_state` (formerly private `_execute_analysis`, `_collect_interactive_run_state`).

### Fixed

- LLM `base_url` handling when configured with a trailing `/chat/completions` path (avoids duplicated path segments with OpenAI-compatible clients).
