#!/usr/bin/env bash
# Установка бота на чистый сервер (Ubuntu 22.04/24.04). Запускать от root:
#   bash install.sh
# Скрипт ставит Docker (если его нет), спрашивает настройки, создаёт .env
# и запускает бота. Повторный запуск ничего не ломает: готовый .env не трогается.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

ask() { # ask ПЕРЕМЕННАЯ "вопрос" [значение по умолчанию]
  local var="$1" prompt="$2" def="${3:-}" val=""
  if [ -n "$def" ]; then prompt="$prompt [$def]"; fi
  read -r -p "$prompt: " val
  printf -v "$var" '%s' "${val:-$def}"
}

if ! command -v docker >/dev/null 2>&1; then
  say "Ставлю Docker…"
  curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Не найден плагин docker compose. Установите его: apt-get install -y docker-compose-plugin"
  exit 1
fi

if [ -f .env ]; then
  say "Файл .env уже есть — настройки не меняю."
else
  say "Настройка. На каждый вопрос введите ответ и нажмите Enter."
  ask BOT_TOKEN "Токен бота от @BotFather"
  ask FOUNDER_TELEGRAM_ID "Ваш Telegram id (число, узнать у @userinfobot)"
  ask CHANNEL_URL "Ссылка на открытый канал, например https://t.me/sbaid (можно пропустить)"
  case "$FOUNDER_TELEGRAM_ID" in
    ''|*[!0-9]*) echo "Telegram id должен быть числом"; exit 1 ;;
  esac
  if [ -z "$BOT_TOKEN" ]; then echo "Токен бота обязателен"; exit 1; fi

  SERVER_IP="$(curl -fsS https://api.ipify.org || hostname -I | awk '{print $1}')"
  # sslip.io превращает IP в доменное имя, для которого выдаётся HTTPS-сертификат.
  # Свой домен можно вписать в .env позже (переменная DOMAIN).
  ask DOMAIN "Домен для уведомлений Robokassa" "${SERVER_IP//./-}.sslip.io"

  POSTGRES_PASSWORD="$(openssl rand -hex 24)"
  ENCRYPTION_KEY="$(openssl rand -base64 32 | tr '+/' '-_')"

  sed \
    -e "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" \
    -e "s|^FOUNDER_TELEGRAM_ID=.*|FOUNDER_TELEGRAM_ID=${FOUNDER_TELEGRAM_ID}|" \
    -e "s|^CHANNEL_URL=.*|CHANNEL_URL=${CHANNEL_URL}|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${POSTGRES_PASSWORD}|" \
    -e "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=${ENCRYPTION_KEY}|" \
    -e "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" \
    .env.example > .env
  chmod 600 .env
  say "Настройки сохранены в .env. Сделайте копию этого файла в надёжном месте:"
  echo "в нём ключ шифрования реквизитов, без него их не восстановить."
fi

say "Запускаю бота…"
docker compose up -d --build

DOMAIN_NOW="$(grep '^DOMAIN=' .env | cut -d= -f2-)"
say "Готово."
cat <<EOF
1. Напишите боту /start в Telegram и пройдите регистрацию.
2. Добавьте бота администратором в канал и в закрытую группу (если уже добавили
   больше суток назад — уберите и добавьте снова). На каждое добавление бот
   ответит вам «подключён». Больше ничего настраивать не нужно.
3. Адрес для уведомлений Robokassa (ResultURL, метод POST):
   https://${DOMAIN_NOW}/robokassa/result

Посмотреть журнал бота:  docker compose logs -f bot
Обновить бота:           bash update.sh
EOF
