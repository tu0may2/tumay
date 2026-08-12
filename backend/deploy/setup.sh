#!/usr/bin/env bash
# Разворачивает казначейский терминал на свежей Ubuntu-ВМ.
#
# Запускать на сервере от root (или через sudo) после того, как код терминала
# уже скопирован на ВМ. Скрипт идемпотентен: повторный запуск не ломает
# работающий сервис, только обновляет код и переустанавливает зависимости.
#
# Использование:
#   sudo bash deploy/setup.sh /путь/к/загруженной/папке/backend
#
# Если путь не указан, ищет backend/ рядом со скриптом.
set -euo pipefail

SOURCE_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TARGET_DIR="/opt/treasury/backend"
SERVICE_USER="treasury"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash deploy/setup.sh" >&2
  exit 1
fi

if [ ! -f "$SOURCE_DIR/requirements.txt" ]; then
  echo "Не нахожу requirements.txt в $SOURCE_DIR — укажите путь к папке backend." >&2
  exit 1
fi

echo "==> Устанавливаю системные пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx rsync >/dev/null

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "==> Завожу отдельного пользователя $SERVICE_USER"
  # Без права входа по паролю — сервис работает от него, а не человек
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Копирую код в $TARGET_DIR"
mkdir -p "$TARGET_DIR"
# --exclude сохраняет уже накопленную базу и .env при повторном запуске —
# иначе повторный деплой стирал бы портфель и пароль администратора
rsync -a --delete \
  --exclude 'treasury.db*' \
  --exclude '.env' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  "$SOURCE_DIR"/ "$TARGET_DIR"/

if [ ! -f "$TARGET_DIR/.env" ]; then
  echo "==> Первый запуск: создаю .env"
  GENERATED_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  cat > "$TARGET_DIR/.env" <<EOF
TREASURY_AUTH_ENABLED=true
TREASURY_ADMIN_PASSWORD=${GENERATED_PASSWORD}
EOF
  echo "    Пароль администратора: ${GENERATED_PASSWORD}"
  echo "    Он же записан в $TARGET_DIR/.env — смените на вкладке «Настройки» после первого входа."
fi

echo "==> Ставлю Python-окружение"
if [ ! -d "$TARGET_DIR/.venv" ]; then
  python3 -m venv "$TARGET_DIR/.venv"
fi
"$TARGET_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$TARGET_DIR/.venv/bin/pip" install --quiet -r "$TARGET_DIR/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$TARGET_DIR"

echo "==> Настраиваю systemd-сервис"
cp "$TARGET_DIR/deploy/treasury.service" /etc/systemd/system/treasury.service
systemctl daemon-reload
systemctl enable treasury >/dev/null
systemctl restart treasury

sleep 2
if systemctl is-active --quiet treasury; then
  echo "==> Сервис запущен: systemctl status treasury"
else
  echo "==> Сервис НЕ запустился. Смотрите: journalctl -u treasury -n 50" >&2
  exit 1
fi

echo
echo "Готово. Терминал слушает 127.0.0.1:8000."
echo "Дальше: настройте nginx (deploy/nginx-treasury.conf) — без него снаружи не достучаться."
