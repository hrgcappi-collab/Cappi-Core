#!/bin/sh
# Выкатка Cappi Core: тесты → rsync → docker compose. Красный тест — стоп.
#
# Раньше тесты перед выкаткой запускались по договорённости. Скрипт делает
# договорённость обязательной: без зелёного selftest, теста приказа и
# прав ничего не уезжает.
set -e
cd "$(dirname "$0")/.."

if pgrep -f "Cappi-Core/bot.py" >/dev/null 2>&1; then
    echo "локальный bot.py запущен — второй поллер поделит сообщения с сервером"; exit 1
fi
for t in tests/приказ.py tests/права.py tests/непонятые.py selftest.py; do
    echo "== $t"; python3 "$t" >/tmp/выкатка.log 2>&1 || { tail -15 /tmp/выкатка.log; echo "❌ $t красный — не выкатываю"; exit 1; }
    tail -1 /tmp/выкатка.log
done

rsync -az --delete --exclude '.git' --exclude '__pycache__' --exclude 'menu.json' \
      --exclude '*.log' --exclude 'data' --exclude 'выгрузка' --exclude 'аналитика' \
      ./ cappi-bot:~/app/cappi-core/
ssh cappi-bot 'cd ~/app/cappi-core && docker compose -f deploy/docker-compose.yml up -d --build 2>&1 | tail -1 \
  && sleep 5 && docker logs cappi-core --tail 3 && [ "$(docker ps -q -f name=^cappi-core$ | wc -l)" = 1 ]'
echo "✅ выкачено"
