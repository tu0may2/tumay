#!/usr/bin/env bash
# Обновляет уже развёрнутый терминал из git-копии репозитория на сервере.
#
# В отличие от setup.sh (который берёт код из папки, залитой вручную через
# WinSCP), этот скрипт исходит из того, что репозиторий уже склонирован на
# сервере и свежие изменения получены через `git pull`. Он обновляет и
# backend (через setup.sh — тот же идемпотентный путь, что и раньше, база и
# .env не трогаются), и frontend (который setup.sh не знает, потому что
# исторически его заливали отдельно через WinSCP).
#
# Использование:
#   sudo bash deploy/update-from-git.sh /opt/treasury/src
#
# Если путь не указан, ищет репозиторий на два уровня выше себя.
set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
FRONTEND_TARGET="/opt/treasury/frontend"
SERVICE_USER="treasury"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash deploy/update-from-git.sh" >&2
  exit 1
fi

if [ ! -f "$REPO_DIR/backend/requirements.txt" ]; then
  echo "Не нахожу backend/requirements.txt в $REPO_DIR — укажите путь к репозиторию." >&2
  exit 1
fi

echo "==> Подтягиваю код из git ($REPO_DIR)"
git -C "$REPO_DIR" pull

echo "==> Обновляю backend (venv, зависимости, сервис)"
bash "$REPO_DIR/backend/deploy/setup.sh" "$REPO_DIR/backend"

echo "==> Обновляю frontend"
rsync -a --delete "$REPO_DIR/frontend/" "$FRONTEND_TARGET/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$FRONTEND_TARGET"

echo "==> Перезапускаю сервис"
systemctl restart treasury
sleep 2

if systemctl is-active --quiet treasury; then
  echo "==> Готово: systemctl status treasury"
else
  echo "==> Сервис НЕ запустился. Смотрите: journalctl -u treasury -n 50" >&2
  exit 1
fi
