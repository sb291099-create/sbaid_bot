#!/bin/sh
# Ежедневная резервная копия базы. Старые копии бот сам не удаляет:
# настройте ротацию на сервере (например, logrotate) или чистите папку вручную.
set -eu
while true; do
  pg_dump -h db -U torgi torgi | gzip > "/backups/torgi_$(date +%F).sql.gz"
  sleep 86400
done
