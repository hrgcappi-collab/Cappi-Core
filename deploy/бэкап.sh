#!/bin/sh
# Ночная копия состояния бота: ~/app/cappi-core/data → ~/backup/cappi-core.
# Держим 14 копий. Ставится в crontab пользователя botdev:
#   15 4 * * * ~/app/cappi-core/deploy/бэкап.sh
# Только своё: чужие каталоги сервера скрипт не трогает.
set -e
SRC=~/app/cappi-core/data
DST=~/backup/cappi-core
mkdir -p "$DST"
tar -czf "$DST/data-$(date +%Y%m%d-%H%M).tgz" -C "$(dirname $SRC)" data
ls -1t "$DST"/data-*.tgz | tail -n +15 | xargs -r rm --
