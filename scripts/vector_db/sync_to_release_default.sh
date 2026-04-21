#!/usr/bin/env bash
#
# 将当前项目根目录下的向量数据库（默认 ./vector_db）
# 同步为发布用默认向量库（默认 ./vector_db_default）。
#
# 使用方式（在仓库根目录执行）:
#   bash scripts/vector_db/sync_to_release_default.sh
#
# 可选环境变量：
#   CURRENT_DB_DIR   当前运行时库目录（默认: $ROOT_DIR/vector_db）
#   RELEASE_DB_DIR   发布默认库目录（默认: $ROOT_DIR/vector_db_default）
#   SNAPSHOT_PATH    中间快照路径（默认: $ROOT_DIR/scripts/vector_db/vector_db_snapshot_latest.json）
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CURRENT_DB_DIR="${CURRENT_DB_DIR:-$ROOT_DIR/vector_db}"
RELEASE_DB_DIR="${RELEASE_DB_DIR:-$ROOT_DIR/vector_db_default}"
SNAPSHOT_PATH="${SNAPSHOT_PATH:-$ROOT_DIR/scripts/vector_db/vector_db_snapshot_latest.json}"
MANAGE_SCRIPT="$ROOT_DIR/scripts/vector_db/manage_vector_db.py"

echo "==> 仓库根目录: $ROOT_DIR"
echo "==> 当前向量库: $CURRENT_DB_DIR"
echo "==> 发布向量库: $RELEASE_DB_DIR"
echo "==> 快照路径  : $SNAPSHOT_PATH"

if [[ ! -f "$MANAGE_SCRIPT" ]]; then
  echo "错误: 管理脚本不存在: $MANAGE_SCRIPT" >&2
  exit 1
fi

if [[ ! -d "$CURRENT_DB_DIR" ]]; then
  echo "错误: 当前向量库目录不存在: $CURRENT_DB_DIR" >&2
  echo "请先初始化向量数据库（例如执行 rag/init_vector_db_data.py 或 CLI init 命令）。" >&2
  exit 1
fi

if [[ -f "$ROOT_DIR/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.venv/bin/activate"
fi

cd "$ROOT_DIR"

echo "==> 1) 导出当前向量库快照到: $SNAPSHOT_PATH"
python3 "$MANAGE_SCRIPT" --db-path "$CURRENT_DB_DIR" --output-file "$SNAPSHOT_PATH" export --no-timestamp

if [[ ! -f "$SNAPSHOT_PATH" ]]; then
  echo "错误: 导出快照失败，未找到文件: $SNAPSHOT_PATH" >&2
  exit 1
fi

echo "==> 2) 准备发布用向量库目录: $RELEASE_DB_DIR"
mkdir -p "$RELEASE_DB_DIR"

echo "==> 3) 清空发布用向量库中的旧数据"
python3 "$MANAGE_SCRIPT" --db-path "$RELEASE_DB_DIR" clear

echo "==> 4) 将当前快照导入发布用向量库"
python3 "$MANAGE_SCRIPT" --db-path "$RELEASE_DB_DIR" import "$SNAPSHOT_PATH"

echo "==> 同步完成。发布向量库已与当前库对齐。"
