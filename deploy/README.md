# Развёртывание Cappi Core

Ставится на тот же сервер, где живёт CappiX — Hetzner, доступ `ssh cappix-prod`.
Отдельный сервер заводить незачем: бот занимает десятки мегабайт, а Caddy там
уже умеет выдавать сертификаты.

## Что нужно до начала

1. **DNS**: запись `core.cappi.ua` → тот же IP, что у `cappix.cappi.ua`.
2. **Доступ** к серверу с той машины, откуда деплоим.

## Установка

```bash
ssh cappix-prod

# код
git clone git@github.com:hrgcappi-collab/Cappi-Core.git /opt/cappi-core/repo
mkdir -p /opt/cappi-core/state

# конфиг: скопировать api.env.example и заполнить
cp /opt/cappi-core/repo/api.env.example /opt/cappi-core/state/api.env
chmod 600 /opt/cappi-core/state/api.env
nano /opt/cappi-core/state/api.env

# запуск
cd /opt/cappi-core/repo
docker compose -f deploy/docker-compose.yml up -d --build
```

Затем дописать `deploy/Caddyfile.snippet` в `infra/prod/Caddyfile` проекта
CappiX и перезагрузить Caddy:

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## Проверка

```bash
curl https://core.cappi.ua/health
# {"ok": true, "service": "cappi-core"}
```

Этот же адрес отдать разработчику Джамшута:

```
CORE_WEBHOOK_URL=https://core.cappi.ua/webhook/zones
CORE_WEBHOOK_TOKEN=<из api.env>
```

## Обновление

```bash
ssh cappix-prod
cd /opt/cappi-core/repo && git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

## Почему состояние на хосте

`/opt/cappi-core/state` монтируется в контейнер как `~/.cappi`. Там лежат
конфиг с доступами, план, журнал смен цен, очередь проверок и события зон.
Если держать это внутри образа, пересборка стирала бы историю — а журнал
цен нужен именно на длинной дистанции.

## Часовой пояс

`TZ=Europe/Kyiv` задан осознанно: по нему считается, когда слать отчёт в
22:00, и какой день считать сегодняшним. На сервере в UTC отчёт уходил бы
в час ночи, а «сегодня» менялось бы посреди вечерней смены.
