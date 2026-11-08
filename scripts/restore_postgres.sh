#!/bin/sh
set -eu

usage() {
  echo "Usage: RESTORE_DATABASE_URL=... $0 --input /absolute/path/stockbot.dump --confirm RESTORE_ISOLATED_DRILL" >&2
  exit 2
}

input=""
confirm=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --input) [ "$#" -ge 2 ] || usage; input="$2"; shift 2 ;;
    --confirm) [ "$#" -ge 2 ] || usage; confirm="$2"; shift 2 ;;
    *) usage ;;
  esac
done

[ -n "${RESTORE_DATABASE_URL:-}" ] || { echo "RESTORE_DATABASE_URL is required." >&2; exit 2; }
[ -f "$input" ] || { echo "Backup input does not exist." >&2; exit 2; }
[ "$confirm" = "RESTORE_ISOLATED_DRILL" ] || { echo "Typed confirmation is required." >&2; exit 2; }

database_url=$(printf '%s' "$RESTORE_DATABASE_URL" | sed 's#^postgresql+psycopg://#postgresql://#')
database_name=$(printf '%s' "$database_url" | sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')
case "$database_name" in
  *_restore_drill) ;;
  *) echo "Refusing restore: target database name must end with _restore_drill." >&2; exit 2 ;;
esac

pg_restore --list "$input" >/dev/null
pg_restore --exit-on-error --single-transaction --dbname="$database_url" --clean --if-exists --no-owner --no-acl "$input"
psql "$database_url" -v ON_ERROR_STOP=1 -Atc "SELECT version_num FROM alembic_version;"
psql "$database_url" -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM backtest_runs;"
echo "Isolated restoration drill completed: $database_name"
