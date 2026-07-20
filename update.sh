#!/usr/bin/env bash
# Обновление watcher на сервере из GitHub: git pull + перезапуск службы.
# Запуск на сервере:  bash /opt/homework-watcher/update.sh
set -e
cd /opt/homework-watcher
echo "→ Тяну изменения из GitHub..."
git pull --rebase
echo "→ Перезапускаю службу..."
systemctl restart homework-watcher
sleep 2
echo "✅ Обновлено и перезапущено:"
systemctl status homework-watcher --no-pager | head -5
