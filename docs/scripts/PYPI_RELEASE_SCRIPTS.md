# PyPI Release Scripts Guide

This document records the repository's PyPI release entry points, package URLs, and the standard release process.

## Package Pages

- PyPI: https://pypi.org/project/stability-analysis-agent/
- TestPyPI: https://test.pypi.org/project/stability-analysis-agent/

## Related Scripts

- `scripts/pypi_release/build_python_dist.sh`
  - Builds wheel/sdist into `output/pypi_dist/`
  - Runs `twine check` on generated artifacts
- `scripts/pypi_release/publish_pypi.sh`
  - Supports `--test` (TestPyPI) and `--prod` (PyPI)
  - Reuses `build_python_dist.sh` before upload

## Output Directory

All Python package artifacts are generated to:

```bash
output/pypi_dist/
```

Typical files:

- `stability_analysis_agent-<version>-py3-none-any.whl`
- `stability_analysis_agent-<version>.tar.gz`

## Authentication

Do not hardcode credentials in scripts or docs.

Use environment variables:

```bash
export TWINE_USERNAME="__token__"
export TWINE_PASSWORD="<pypi-token>"
```

## Standard Release Flow

### 1) Build and Validate Artifacts

```bash
bash scripts/pypi_release/build_python_dist.sh
```

### 2) Upload to TestPyPI

```bash
bash scripts/pypi_release/publish_pypi.sh --test
```

### 3) Install Verification from TestPyPI

```bash
python3 -m venv .venv_testpypi_verify
source .venv_testpypi_verify/bin/activate
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple "stability-analysis-agent[rag]==1.3.5"
sa-agent --help
```

Use the release version you just published (for example `1.2.0`) instead of hardcoding old versions.

### 4) Basic Runtime Verification (Post Install)

```bash
# 1) Config command should be available
sa-agent config path
sa-agent config doctor

# 2) Optional: initialize local config interactively
sa-agent config init

# 3) No-config path should still work with --scope gen_prompt_only
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
```

### 5) Upload to Production PyPI

```bash
bash scripts/pypi_release/publish_pypi.sh --prod
```

## Manual Fallback Upload

If script upload is blocked by local environment/network/proxy settings, run twine directly:

```bash
python3 -m twine upload --repository-url https://test.pypi.org/legacy/ output/pypi_dist/*
python3 -m twine upload --repository-url https://upload.pypi.org/legacy/ output/pypi_dist/*
```

## Common Issues

- `twine upload: error: the following arguments are required: dist`
  - Cause: no file paths were passed to `twine upload`
  - Fix: include `output/pypi_dist/*`
- `Missing '...whl' section from ~/.pypirc`
  - Cause: `--repository` was incorrectly set to a file path
  - Fix: use `--repository-url ...` or `--repository testpypi/pypi`
- `400 'CHANGELOG.md' is not a valid url`
  - Cause: non-URL metadata value in `project.urls`
  - Fix: ensure full URL strings in `pyproject.toml`

## Security Notes

- Never commit tokens to git
- Rotate tokens after accidental exposure
- Prefer project-scoped API tokens with minimum required permissions
- For GitHub Actions, prefer **Trusted Publishing (OIDC)** over long-lived tokens

## GitHub Actions（自动发布）

独立流水线：[`.github/workflows/publish-pypi.yml`](../../.github/workflows/publish-pypi.yml)（**不**写在 `ci.yml` 里）。

| 触发 | 行为 |
|------|------|
| 推送 tag `v*`（如 `v1.3.5`） | 跑确定性套件 → 构建 → 上传 **正式 PyPI** |
| Actions 页手动 `workflow_dispatch` | 可选 `testpypi` / `pypi`；可勾选跳过测试（紧急） |

**版本对齐**：tag 名去掉 `v` 前缀后必须与 `pyproject.toml` 的 `version` 一致，否则构建 job 失败。

### 一次性配置（Trusted Publishing）

1. 在 GitHub 仓库创建 Environments：`pypi`、`testpypi`（workflow 会引用同名 environment；可按需加审批保护）。
2. 打开 [PyPI publishing settings](https://pypi.org/manage/account/publishing/)（TestPyPI 对应 test.pypi.org）：
   - Owner / Repository：本仓库
   - Workflow name：`publish-pypi.yml`
   - Environment name：`pypi` 或 `testpypi`
3. 首次上传前建议先手动跑一次 `workflow_dispatch` → `testpypi` 验证。

### 推荐发版步骤

```bash
# 1) 改 pyproject.toml version，更新 CHANGELOG，合并到 main
# 2) 确认 CI 已绿
# 3) 打 tag 并推送（触发正式发布）
git tag v1.2.10
git push origin v1.2.10

# 或：先手动发到 TestPyPI
# GitHub → Actions → Publish PyPI → Run workflow → target=testpypi
```

本地脚本路径仍然可用（离线 / 无 Actions 时）：

```bash
bash scripts/pypi_release/publish_pypi.sh --test
bash scripts/pypi_release/publish_pypi.sh --prod
```
