#!/usr/bin/env bash
# サーバ上の毎日のバックアップ（cron から実行）。SQLite のオンラインバックアップで一貫した複製を作る。
# サーバの外への複製は、研究室の PC で deploy/pull_backup.sh を毎日実行して取り込む（仕様 v1.2 §15.7）。
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/aisup}"
cd "$APP_DIR/data"
stamp=$(date +%Y%m%d_%H%M%S)
sqlite3 events.sqlite3 ".backup 'backups/daily_${stamp}.sqlite3'"
gzip -f "backups/daily_${stamp}.sqlite3"
[ -f allocation.csv ] && cp -p allocation.csv "backups/allocation_${stamp}.csv"
# 60日より古い日次バックアップを消す（参加者終了時のバックアップは残す）
find backups -name 'daily_*.sqlite3.gz' -mtime +60 -delete
echo "$(date -Is) backup ok: daily_${stamp}.sqlite3.gz"
