#!/usr/bin/env bash
# 本番サーバの構築（Ubuntu 22.04/24.04、arm64/amd64。Oracle Cloud Always Free の Ampere A1 を想定。仕様 v1.2 §15.7）
# 使い方（サーバ上で、リポジトリを /opt/aisup に置いてから）：
#   sudo bash deploy/setup_server.sh
# 事前に deploy/.env を作る（deploy/env.example を参照）。DNS の A レコードをこのサーバのIPに向けておく。
# Oracle Cloud ではコンソールで VCN のセキュリティ・リストに 80/443 の受信を許可しておくこと（README 参照）。
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/aisup}"
cd "$APP_DIR"
[ -f deploy/.env ] || { echo "deploy/.env がありません（deploy/env.example をコピーして作成）"; exit 1; }

echo "== Docker"
if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl gnupg sqlite3 rsync
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker

echo "== ファイアウォール（OCI の Ubuntu イメージは iptables で 80/443 を閉じている）"
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp --dport "$port" -j ACCEPT
done
if command -v netfilter-persistent >/dev/null; then netfilter-persistent save; fi

echo "== データとバックアップのディレクトリ"
mkdir -p data/backups
chmod 700 data

echo "== イメージの作成と起動"
docker compose --env-file deploy/.env --profile build build sandbox-image
docker compose --env-file deploy/.env up -d --build app caddy

echo "== 毎日のバックアップ（3:30）"
CRON_LINE="30 3 * * * root $APP_DIR/deploy/backup_daily.sh >> $APP_DIR/data/backups/backup.log 2>&1"
echo "$CRON_LINE" > /etc/cron.d/aisup-backup
chmod 644 /etc/cron.d/aisup-backup

echo "== 確認"
sleep 5
docker compose --env-file deploy/.env ps
DOMAIN=$(grep '^DOMAIN=' deploy/.env | cut -d= -f2)
echo "死活監視の URL: https://$DOMAIN/healthz （外部の監視サービスに登録する）"
echo "割付表がなければ作る: docker compose --env-file deploy/.env exec app python -m server.allocation --blocks 10 --seed <種> --out data/allocation.csv"
