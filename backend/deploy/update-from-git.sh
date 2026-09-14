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
# Это единственная команда, которая нужна для обновления: unit-файл,
# daemon-reload и перезапуск делает setup.sh, который вызывается отсюда.
# Руками остаётся только конфиг nginx — о нём скрипт напомнит в конце.
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
# Перезапуск после этого не нужен: и страница, и отпечаток версии ассетов
# читаются с диска на каждый запрос. Сервис уже перезапущен внутри setup.sh —
# вместе с новым unit-файлом и daemon-reload

if ! systemctl is-active --quiet treasury; then
  echo "==> Сервис НЕ запустился. Смотрите: journalctl -u treasury -n 50" >&2
  exit 1
fi

# ----------------------------------------------------------------------
# Что осталось сделать руками
# ----------------------------------------------------------------------
# Конфиг nginx скрипт не трогает намеренно: там живут пути к сертификатам,
# которые прописал certbot, и перезапись стёрла бы их. Но промолчать про
# него тоже нельзя — человек уверен, что обновился целиком, а терминал
# продолжает отвечать по открытому http.
NGINX_CONF=""
for candidate in /etc/nginx/sites-enabled/treasury /etc/nginx/conf.d/treasury.conf; do
  [ -f "$candidate" ] && NGINX_CONF="$candidate" && break
done

echo
echo "==> Готово. Терминал обновлён и перезапущен."

if [ -z "$NGINX_CONF" ]; then
  echo "    Конфиг nginx не найден — проверьте, как терминал отдаётся наружу."
elif ! grep -q "listen 443" "$NGINX_CONF"; then
  echo
  echo "    ВНИМАНИЕ: в $NGINX_CONF нет блока HTTPS."
  echo "    Значит пароль и токен сессии ходят по сети открытым текстом."
  echo "    Получить сертификат:  sudo certbot --nginx -d ваш-домен.ru"
  echo "    Либо возьмите готовый блок из deploy/nginx-treasury.conf."
elif ! grep -qE "return 30[12] https://" "$NGINX_CONF"; then
  echo
  echo "    ВНИМАНИЕ: HTTPS настроен, но с http на него не перенаправляет."
  echo "    Терминал остаётся доступен по открытому каналу."
  echo "    Блок перенаправления — в deploy/nginx-treasury.conf."
else
  echo "    HTTPS настроен, с http идёт перенаправление — всё на месте."
fi

echo
echo "    Состояние сервиса: systemctl status treasury"
