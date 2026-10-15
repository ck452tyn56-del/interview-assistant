#!/bin/bash
# Супервизор: перезапускает ассистента, если процесс умер НЕ закрытием окна.
# Пользовательское закрытие (код 0) — не перезапускает.
cd "$(dirname "$0")"
while true; do
  py fast_assistant.py
  CODE=$?
  echo "[SUPERVISOR] процесс завершился с кодом $CODE"
  if [ $CODE -eq 42 ]; then
    echo "[SUPERVISOR] другой экземпляр уже работает — выходим без перезапуска"
    break
  fi
  if [ -f .user_closed ]; then
    rm -f .user_closed
    echo "[SUPERVISOR] окно закрыл пользователь — выходим"
    break
  fi
  echo "[SUPERVISOR] падение, перезапуск через 2с..."
  sleep 2
done
