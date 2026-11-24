#!/usr/bin/env bash
# 研究室の PC で毎日実行し、サーバのバックアップを取り込む（サーバ外への保管。仕様 v1.2 §15.7）。
#   使い方： deploy/pull_backup.sh ubuntu@aisup.example.jp ~/aisup_backups
#   macOS なら launchd、Linux なら cron に登録する（README 参照）。SSH 鍵でログインできること。
set -euo pipefail
REMOTE="${1:?ssh先（例 ubuntu@host）}"
DEST="${2:-$HOME/aisup_backups}"
APP_DIR="${APP_DIR:-/opt/aisup}"
mkdir -p "$DEST"
rsync -az --ignore-existing "$REMOTE:$APP_DIR/data/backups/" "$DEST/"
latest=$(ls -t "$DEST"/daily_*.sqlite3.gz 2>/dev/null | head -1 || true)
if [ -n "$latest" ]; then
  # 取り込んだバックアップが壊れていないか確認する
  tmp=$(mktemp)
  gunzip -c "$latest" > "$tmp"
  n=$(sqlite3 "$tmp" "select count(*) from events;")
  rm -f "$tmp"
  echo "$(date -Iseconds) pulled; latest=$(basename "$latest") events=$n"
else
  echo "$(date -Iseconds) pulled; no daily backup yet"
fi
