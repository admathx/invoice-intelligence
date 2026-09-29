#!/bin/sh
# Nightly database backups, run by the `backup` service in
# docker-compose.prod.yml (it uses the Postgres image, for a pg_dump that
# matches the server).
#
# Once a day, from BACKUP_HOUR_UTC (default 07:00), writes
# /backups/invoice-YYYYMMDD-HHMM.dump, checks it can be read back, and
# deletes dumps older than BACKUP_KEEP_DAYS (default 14). A fresh deployment
# with no backup yet gets one straight away. The scheduler warns if they stop
# coming, and copies them off the server when storage is S3 (app/backups.py).
#
# ONCE=1 runs a single pass and exits (for trying it by hand).
set -eu

HOUR=${BACKUP_HOUR_UTC:-7}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
DIR=/backups
mkdir -p "$DIR"

backup_now() {
  file="$DIR/invoice-$(date -u +%Y%m%d-%H%M).dump"
  # Written under another name and only renamed once it reads back, so a
  # dump cut short (disk full, a restart) never looks like a good one.
  if pg_dump -h "${PGHOST:-postgres}" -U invoice -Fc invoice_intelligence > "$file.partial" \
    && pg_restore --list "$file.partial" > /dev/null; then
    mv "$file.partial" "$file"
    echo "$(date -u +%FT%TZ) backup written: $file ($(du -h "$file" | cut -f1))"
  else
    rm -f "$file.partial"
    echo "$(date -u +%FT%TZ) backup FAILED" >&2
  fi
  find "$DIR" -name 'invoice-*.dump' -mtime +"$KEEP_DAYS" -delete
}

while true; do
  today=$(date -u +%Y%m%d)
  hour=$(date -u +%H | sed 's/^0//')
  if ! ls "$DIR"/invoice-*.dump > /dev/null 2>&1; then
    backup_now
  elif [ "${hour:-0}" -ge "$HOUR" ] && ! ls "$DIR"/invoice-"$today"-*.dump > /dev/null 2>&1; then
    backup_now
  fi
  [ "${ONCE:-}" = "1" ] && exit 0
  sleep 600
done
