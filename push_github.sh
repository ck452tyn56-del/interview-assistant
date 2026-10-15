#!/bin/bash
# Device-flow авторизация GitHub + создание приватного репозитория + push.
# Код подтверждения вводится на https://github.com/login/device (можно с iPhone).
cd "$(dirname "$0")"

CLIENT_ID="178c6fc778ccc68e1d6a"   # публичный OAuth client_id GitHub CLI
PROXY="socks5h://127.0.0.1:8081"

gh_curl() {
  # сначала напрямую, при неудаче — через туннель
  local resp
  resp=$(curl -s --max-time 15 "$@")
  if [ -z "$resp" ] || echo "$resp" | grep -q '"message":"Bad credentials"'; then
    resp=$(curl -s --max-time 20 --proxy "$PROXY" "$@")
  fi
  echo "$resp"
}

jget() { py -c "import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],''))" "$1"; }

echo "=== Шаг 1: запрашиваю код подтверждения..."
CODE_RESP=$(gh_curl -X POST https://github.com/login/device/code \
  -H "Accept: application/json" \
  -d "client_id=${CLIENT_ID}&scope=repo")
USER_CODE=$(echo "$CODE_RESP" | jget user_code)
DEVICE_CODE=$(echo "$CODE_RESP" | jget device_code)
INTERVAL=$(echo "$CODE_RESP" | jget interval); INTERVAL=${INTERVAL:-5}
if [ -z "$USER_CODE" ]; then echo "ОШИБКА получения кода: $CODE_RESP"; exit 1; fi
echo "=== КОД ПОДТВЕРЖДЕНИЯ: $USER_CODE"
echo "=== Введи его на https://github.com/login/device"
echo "=== Жду авторизацию (код действует ~15 минут)..."

TOKEN=""
for i in $(seq 1 170); do
  sleep "$INTERVAL"
  RESP=$(gh_curl -X POST https://github.com/login/oauth/access_token \
    -H "Accept: application/json" \
    -d "client_id=${CLIENT_ID}&device_code=${DEVICE_CODE}&grant_type=urn:ietf:params:oauth:grant-type:device_code")
  ERR=$(echo "$RESP" | jget error)
  if [ -z "$ERR" ]; then TOKEN=$(echo "$RESP" | jget access_token); break; fi
  if [ "$ERR" = "authorization_pending" ]; then continue; fi
  if [ "$ERR" = "slow_down" ]; then sleep 5; continue; fi
  echo "ОШИБКА авторизации: $ERR"; exit 1
done
if [ -z "$TOKEN" ]; then echo "Время ожидания истекло — скажи ассистенту, чтобы перезапустил."; exit 1; fi
echo "=== Авторизация получена!"

echo "=== Шаг 2: определяю аккаунт..."
LOGIN=$(gh_curl -H "Authorization: Bearer $TOKEN" https://api.github.com/user | jget login)
if [ -z "$LOGIN" ]; then echo "Не удалось получить логин"; exit 1; fi
echo "=== Аккаунт: $LOGIN"

echo "=== Шаг 3: создаю приватный репозиторий interview-assistant (если ещё нет)..."
CREATE_RESP=$(gh_curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/user/repos \
  -d '{"name":"interview-assistant","private":true,"description":"Голосовой IT-ассистент: Gemini Live, push-to-talk, выбор источника звука"}')
echo "$CREATE_RESP" | head -c 200; echo

echo "=== Шаг 4: push..."
git remote rename origin upstream 2>/dev/null
PUSH_URL="https://x-access-token:${TOKEN}@github.com/${LOGIN}/interview-assistant.git"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$PUSH_URL"
else
  git remote add origin "$PUSH_URL"
fi
git push -u origin master 2>&1
if [ $? -eq 0 ]; then
  echo "=== УСПЕХ: https://github.com/$LOGIN/interview-assistant (приватный)"
else
  echo "=== PUSH не удался, смотри вывод выше"
  exit 1
fi
