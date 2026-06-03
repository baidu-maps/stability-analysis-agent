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
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple "stability-analysis-agent[rag]==1.2.6"
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

# 3) No-config path should still work with --scope prompt_only
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope prompt_only
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

