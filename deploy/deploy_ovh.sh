#!/usr/bin/env bash
set -euo pipefail

archive=${1:?Release archive is required}
revision=${2:?Git revision is required}
nginx_source=${3:-}
target=/opt/cdg-ia-sync-test
staging="/tmp/cdg-ia-sync-test-${revision}"
backup_tmp="/tmp/cdg-ia-sync-test-backup-${revision}.tgz"
backup_dir="/opt/cdg-ia-sync-test-backups"

cleanup() {
  rm -rf -- "$staging" "$archive"
  sudo rm -f -- "$backup_tmp"
}
trap cleanup EXIT

test -f "$archive"
rm -rf -- "$staging"
mkdir -p -- "$staging"
tar -xzf "$archive" -C "$staging"

sudo install -d -m 755 "$target" "$backup_dir"

if sudo test -n "$(sudo find "$target" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)"; then
  sudo tar     --exclude='./.env'     --exclude='./runtime'     -czf "$backup_tmp"     -C "$target" .
  sudo mv "$backup_tmp" "$backup_dir/backup-before-${revision}.tgz"
fi

sudo install -d -m 700 "$target/runtime"
sudo rsync -a --delete   --exclude='.env'   --exclude='/runtime/'   "$staging/" "$target/"

if ! sudo test -f "$target/.env"; then
  sudo tee "$target/.env" >/dev/null <<'EOF'
MVSEP_API_TOKEN=
MVSEP_API_BASE=https://mvsep.com/api
EOF
  sudo chmod 600 "$target/.env"
fi

cd "$target"
sudo docker compose -p cdg-ia-sync-test up -d --build --remove-orphans
sudo docker compose -p cdg-ia-sync-test ps

health="$(
  curl --fail --silent --show-error     --retry 24 --retry-all-errors --retry-delay 5 --max-time 10     http://127.0.0.1:8097/api/health
)"
printf '%s\n' "$health"

if [ -n "$nginx_source" ]; then
  test -f "$nginx_source"

  nginx_match="$(
    sudo sh -c "grep -Rsl 'server_name panel\.kitkaraoke\.com' /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | head -n 1" || true
  )"
  if [ -z "$nginx_match" ] && sudo test -e /etc/nginx/sites-available/panel.kitkaraoke.com; then
    nginx_match=/etc/nginx/sites-available/panel.kitkaraoke.com
  fi
  if [ -z "$nginx_match" ]; then
    echo "No se encontró la configuración Nginx activa de panel.kitkaraoke.com" >&2
    exit 1
  fi

  nginx_target="$(sudo readlink -f "$nginx_match")"
  nginx_backup="${nginx_target}.backup-cdg-ia-${revision}"
  sudo cp -a "$nginx_target" "$nginx_backup"
  sudo install -m 644 "$nginx_source" "$nginx_target"

  if ! sudo nginx -t; then
    echo "Nginx inválido; restaurando respaldo." >&2
    sudo cp -a "$nginx_backup" "$nginx_target"
    sudo nginx -t
    exit 1
  fi

  sudo systemctl reload nginx
fi

printf '\nCDG_IA_SYNC_TEST deployment %s completed.\n' "$revision"
