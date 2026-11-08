#!/bin/sh
set -eu

usage() {
  echo "Usage: DATABASE_URL=... $0 --output /absolute/path/stockbot.dump [--overwrite --confirm OVERWRITE_BACKUP]" >&2
  exit 2
}

output=""
overwrite="false"
confirm=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) [ "$#" -ge 2 ] || usage; output="$2"; shift 2 ;;
    --overwrite) overwrite="true"; shift ;;
    --confirm) [ "$#" -ge 2 ] || usage; confirm="$2"; shift 2 ;;
    *) usage ;;
  esac
done

[ -n "${DATABASE_URL:-}" ] || { echo "DATABASE_URL is required." >&2; exit 2; }
[ -n "$output" ] || usage
case "$output" in /*.dump) ;; *) echo "Output must be an absolute .dump path." >&2; exit 2 ;; esac
[ -d "$(dirname "$output")" ] || { echo "Output directory does not exist." >&2; exit 2; }
if [ -e "$output" ] && { [ "$overwrite" != "true" ] || [ "$confirm" != "OVERWRITE_BACKUP" ]; }; then
  echo "Refusing to overwrite an existing backup without --overwrite --confirm OVERWRITE_BACKUP." >&2
  exit 2
fi

database_url=$(printf '%s' "$DATABASE_URL" | sed 's#^postgresql+psycopg://#postgresql://#')
umask 077
pg_dump --dbname="$database_url" --format=custom --no-owner --no-acl --file="$output"
pg_restore --list "$output" >/dev/null
echo "Backup created and catalog verified: $output"
