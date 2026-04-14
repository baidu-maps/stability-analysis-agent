#!/usr/bin/env bash
# build_cli_mac.sh — 将 Stability Analysis Agent CLI 打包成 macOS 单文件二进制
#
# 用法:
#   bash cli/build_cli_mac.sh              # 使用 pyproject.toml 中的版本号
#   bash cli/build_cli_mac.sh v1.2.0       # 指定版本号（覆盖 pyproject.toml）
#
# 产物目录: releases/stability_analyzer_cli/<VERSION>-mac-<ARCH>/
#   StabilityAnalyzer        — 可执行二进制（PyInstaller onefile）
#   configs/                 — 公共配置模板
#   CLI_RELEASE_NOTES_<V>.md — 本次发布说明（首次需人工补充正文）
#
# 发布流程:
#   1. 执行本脚本生成产物
#   2. 压缩产物目录: zip -r StabilityAnalyzer-<VERSION>-mac.zip releases/...
#   3. 在 GitHub 创建 Release / Tag，将压缩包作为 Asset 上传
#   详见: docs/dev/CLI_RELEASE_GUIDE.md
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# 路径 & 版本
# --------------------------------------------------------------------------- #
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_NAME="StabilityAnalyzer"

# 从 pyproject.toml 读取默认版本；可用第一个参数覆盖
DEFAULT_VERSION="v$(python3 - <<'EOF'
import os
import re
try:
    root_dir = os.environ.get("ROOT_DIR", ".")
    pyproject_path = os.path.join(root_dir, "pyproject.toml")
    with open(pyproject_path, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"\s*$', content, re.MULTILINE)
    print(match.group(1) if match else "0.1.0")
except Exception:
    print("0.1.0")
EOF
)"
VERSION="${1:-$DEFAULT_VERSION}"
# 确保版本带 v 前缀，方便与 git tag 对齐
[[ "$VERSION" == v* ]] || VERSION="v${VERSION}"

# 目标架构：arm64 / x86_64（取运行时架构）
ARCH="$(uname -m)"   # arm64 or x86_64
OUT_DIR="$ROOT_DIR/releases/stability_analyzer_cli/${VERSION}-mac-${ARCH}"

echo "==> repo   : $ROOT_DIR"
echo "==> version: $VERSION"
echo "==> arch   : $ARCH"
echo "==> output : $OUT_DIR"

# --------------------------------------------------------------------------- #
# 1. 激活虚拟环境（如果存在）
# --------------------------------------------------------------------------- #
if [[ -f "$ROOT_DIR/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.venv/bin/activate"
fi

# --------------------------------------------------------------------------- #
# 2. 安装/升级构建依赖
# --------------------------------------------------------------------------- #
python3 -m pip install -U pip --quiet
python3 -m pip install pyinstaller --quiet

# 安装项目本体依赖（跳过百度内网 index，回退到 PyPI）
if [[ -f "$ROOT_DIR/requirements.txt" ]]; then
  # 去掉内网 index-url / trusted-host 行，避免在开源环境报错
  CLEAN_REQ=$(mktemp)
  grep -v '^\s*--index-url\|^\s*--trusted-host' "$ROOT_DIR/requirements.txt" > "$CLEAN_REQ" || true
  python3 -m pip install -r "$CLEAN_REQ" --quiet
  rm -f "$CLEAN_REQ"
fi

# --------------------------------------------------------------------------- #
# 3. 清理旧构建产物
# --------------------------------------------------------------------------- #
rm -rf "$ROOT_DIR/build" "$ROOT_DIR/dist" "$ROOT_DIR/${BIN_NAME}.spec"

# --------------------------------------------------------------------------- #
# 4. PyInstaller 打包
#
# hidden-import 说明：
#   - chromadb.*        : RAG 向量库，动态加载，PyInstaller 无法自动探测
#   - hnswlib / posthog : chromadb 的运行时依赖
#   - tree_sitter*      : 代码解析，含 C 扩展
# --------------------------------------------------------------------------- #
cd "$ROOT_DIR"
pyinstaller \
  --name "$BIN_NAME" \
  --onefile \
  --clean \
  --hidden-import hnswlib \
  --hidden-import posthog \
  --hidden-import chromadb.telemetry.product.posthog \
  --hidden-import chromadb.api.segment \
  --hidden-import chromadb.segment.impl.metadata.sqlite \
  --hidden-import chromadb.segment.impl.vector.local_hnsw \
  --hidden-import chromadb.segment.impl.vector.local_persistent_hnsw \
  --hidden-import chromadb.migrations \
  --hidden-import chromadb.migrations.embeddings_queue \
  --hidden-import chromadb.migrations.metadb \
  --hidden-import chromadb.migrations.sysdb \
  --hidden-import tree_sitter \
  --hidden-import tree_sitter_languages \
  --collect-submodules chromadb.db.impl \
  --collect-submodules chromadb.migrations \
  --collect-data     chromadb.migrations \
  --collect-submodules chromadb.segment.impl \
  --collect-submodules chromadb.execution.executor \
  --collect-submodules chromadb.ingest.impl \
  --collect-submodules chromadb.telemetry.product \
  --collect-submodules chromadb.quota \
  --collect-submodules chromadb.rate_limit \
  --collect-all tree_sitter \
  --collect-all tree_sitter_languages \
  --collect-binaries hnswlib \
  "cli/main.py"

# --------------------------------------------------------------------------- #
# 5. 组装发布目录
# --------------------------------------------------------------------------- #
mkdir -p "$OUT_DIR/configs"

cp "$ROOT_DIR/dist/$BIN_NAME" "$OUT_DIR/$BIN_NAME"

# --------------------------------------------------------------------------- #
# 6. macOS 代码签名（可选）
#
# 设置 CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAM_ID)" 启用签名。
# 设置 NOTARIZE=1 且配置 APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / TEAM_ID 启用公证。
# --------------------------------------------------------------------------- #
if [[ "$(uname)" == "Darwin" ]]; then
  SIGN_IDENTITY="${CODESIGN_IDENTITY:-}"
  if [[ -z "$SIGN_IDENTITY" ]]; then
    SIGN_IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
      | grep "Developer ID Application" | head -1 \
      | sed -E 's/.*"([^"]+)".*/\1/') || true
  fi
  if [[ -n "$SIGN_IDENTITY" ]]; then
    echo "==> signing with: $SIGN_IDENTITY"
    codesign --force --timestamp --options runtime --sign "$SIGN_IDENTITY" "$OUT_DIR/$BIN_NAME"
    if [[ "${NOTARIZE:-0}" == "1" ]]; then
      if [[ -n "${APPLE_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" && -n "${TEAM_ID:-}" ]]; then
        echo "==> notarizing..."
        NOTARIZE_ZIP="$OUT_DIR/${BIN_NAME}.zip"
        (cd "$OUT_DIR" && zip -q "$NOTARIZE_ZIP" "$BIN_NAME")
        if xcrun notarytool submit "$NOTARIZE_ZIP" \
            --apple-id "$APPLE_ID" \
            --password "$APPLE_APP_SPECIFIC_PASSWORD" \
            --team-id "$TEAM_ID" \
            --wait 2>/dev/null; then
          echo "==> notarization complete"
        else
          echo "==> notarization failed — check APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / TEAM_ID"
        fi
        rm -f "$NOTARIZE_ZIP"
      else
        echo "==> skipping notarization (set NOTARIZE=1 + APPLE_ID + APPLE_APP_SPECIFIC_PASSWORD + TEAM_ID)"
      fi
    fi
  else
    echo "==> no Developer ID cert found — binary unsigned"
    echo "    users may need: xattr -d com.apple.quarantine $BIN_NAME"
    xattr -cr "$OUT_DIR/$BIN_NAME" 2>/dev/null || true
  fi
fi

# --------------------------------------------------------------------------- #
# 7. 复制配置模板
#    仅复制公共配置，不覆盖用户已有的 *.local.json
# --------------------------------------------------------------------------- #
CONFIG_SRC_DIR="$ROOT_DIR/agent"
if [[ -f "$CONFIG_SRC_DIR/agent_config.json" ]]; then
  cp "$CONFIG_SRC_DIR/agent_config.json" "$OUT_DIR/configs/agent_config.json"
fi
if [[ -f "$CONFIG_SRC_DIR/agent_config.local.example.json" ]]; then
  cp "$CONFIG_SRC_DIR/agent_config.local.example.json" "$OUT_DIR/configs/agent_config.local.example.json"
fi

# --------------------------------------------------------------------------- #
# 8. 生成 Release Notes 模板（若对应版本文件不存在则创建）
# --------------------------------------------------------------------------- #
NOTES_FILE="$OUT_DIR/CLI_RELEASE_NOTES_${VERSION}.md"
if [[ ! -f "$NOTES_FILE" ]]; then
  BUILD_DATE="$(date '+%Y-%m-%d')"
  cat > "$NOTES_FILE" <<NOTES
# Stability Analysis Agent CLI ${VERSION} — Release Notes

**Release date**: ${BUILD_DATE}
**Platform**: macOS (${ARCH})
**Binary**: \`StabilityAnalyzer\`

## What's New

<!-- TODO: 填写本次版本的主要变更 -->

## Bug Fixes

<!-- TODO: 填写修复的问题 -->

## Installation

\`\`\`bash
# 解压后赋予执行权限
chmod +x StabilityAnalyzer

# macOS Gatekeeper 提示（无签名时）
xattr -d com.apple.quarantine StabilityAnalyzer

# 运行
./StabilityAnalyzer --help
\`\`\`

## Configuration

复制并编辑配置文件:

\`\`\`bash
cp configs/agent_config.local.example.json configs/agent_config.local.json
# 填入 LLM API Key（OpenAI / DeepSeek / 文心等）
\`\`\`

## Known Issues

<!-- TODO: 已知问题 -->
NOTES
  echo "==> created release notes template: $NOTES_FILE"
fi

# --------------------------------------------------------------------------- #
# 9. 打印汇总
# --------------------------------------------------------------------------- #
echo ""
echo "==> Build complete!"
echo "    binary : $OUT_DIR/$BIN_NAME"
echo "    size   : $(du -sh "$OUT_DIR/$BIN_NAME" | cut -f1)"
echo ""
echo "Next steps:"
echo "  1. Edit $NOTES_FILE"
echo "  2. zip -r StabilityAnalyzer-${VERSION}-mac-${ARCH}.zip \\"
echo "       releases/stability_analyzer_cli/${VERSION}-mac-${ARCH}/"
echo "  3. gh release create ${VERSION} StabilityAnalyzer-${VERSION}-mac-${ARCH}.zip \\"
echo "       --title 'Stability Analysis Agent ${VERSION}' --notes-file $NOTES_FILE"
