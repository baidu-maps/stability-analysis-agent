#!/bin/bash
set -e

# 兼容入口：保留历史命令 `sh mk/build.sh`，内部转调当前维护脚本 build-mac.sh。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/build-mac.sh" "$@"
