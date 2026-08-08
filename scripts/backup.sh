#!/usr/bin/env bash
# Lilies 数据备份：SQLite 热备（VACUUM INTO 顺带压实）+ 事件冷文件 + 构建转录。
# 用法：scripts/backup.sh [数据目录] [备份目录]
set -euo pipefail

DATA_DIR="${1:-data}"
BACKUP_ROOT="${2:-backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$STAMP"
mkdir -p "$DEST"

DB="$DATA_DIR/agent_platform.db"
if [ -f "$DB" ]; then
  # VACUUM INTO：在线一致性快照，同时把已删除事件占用的空间压实掉
  sqlite3 "$DB" "VACUUM INTO '$DEST/agent_platform.db'"
  echo "DB 快照: $(du -h "$DEST/agent_platform.db" | cut -f1)（原库 $(du -h "$DB" | cut -f1)）"
fi

for DIR in events build_transcripts secrets; do
  if [ -d "$DATA_DIR/$DIR" ]; then
    tar -czf "$DEST/$DIR.tar.gz" -C "$DATA_DIR" "$DIR"
    echo "$DIR: $(du -h "$DEST/$DIR.tar.gz" | cut -f1)"
  fi
done

echo "备份完成 → $DEST"
