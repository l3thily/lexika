#!/bin/sh
# Сборка словарей из Obsidian (.english) → коммит/пуш в приватный GitHub → выкладка на VPS-88.
#   https://lexika.88-218-121-40.sslip.io
set -e
cd "$(dirname "$0")"
python3 build_data.py
git add -A
git commit -qm "${1:-Обновление}" || true
git push -q origin main
ssh vps2 'mkdir -p /opt/lexika/data /var/www/lexika'
scp -q index.html static/* vps2:/var/www/lexika/
scp -q data/*.json vps2:/opt/lexika/data/
scp -q server/server.py vps2:/opt/lexika/
ssh vps2 'systemctl restart lexika'
sleep 1
curl -s -o /dev/null -w "site: %{http_code}  " https://lexika.88-218-121-40.sslip.io/
curl -s https://lexika.88-218-121-40.sslip.io/api/health; echo
