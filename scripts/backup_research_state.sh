#!/bin/sh
set -eu

usage() {
  echo "Usage: DATABASE_URL=... STORAGE_ROOT=... $0 --output /absolute/path/research-state.tar.gz --confirm QUIESCED_WRITES" >&2
  exit 2
}

output=""
confirm=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) [ "$#" -ge 2 ] || usage; output="$2"; shift 2 ;;
    --confirm) [ "$#" -ge 2 ] || usage; confirm="$2"; shift 2 ;;
    *) usage ;;
  esac
done

[ -n "${DATABASE_URL:-}" ] || usage
[ -d "${STORAGE_ROOT:-}" ] || usage
[ "$confirm" = "QUIESCED_WRITES" ] || usage
case "$output" in /*.tar.gz) ;; *) usage ;; esac
[ -d "$(dirname "$output")" ] || usage
[ ! -e "$output" ] || { echo "Refusing to overwrite an existing backup." >&2; exit 2; }
storage_real=$(cd "$STORAGE_ROOT" && pwd -P)
output_parent_real=$(cd "$(dirname "$output")" && pwd -P)
case "$output_parent_real/" in
  "$storage_real/"*) echo "Backup output must be outside the storage root." >&2; exit 2 ;;
esac
if [ -n "$(find "$STORAGE_ROOT" -type l -print -quit)" ]; then
  echo "Storage contains a symbolic link; refusing an incomplete file checksum manifest." >&2
  exit 2
fi

umask 077
stage=$(mktemp -d "$(dirname "$output")/.stockbot-backup.XXXXXX")
temporary="$output.tmp.$$"
cleanup() {
  rm -rf "$stage" "$temporary"
}
trap cleanup EXIT HUP INT TERM

"$(dirname "$0")/backup_postgres.sh" --output "$stage/database.dump"
cp -a "$STORAGE_ROOT" "$stage/storage"
{
  echo "format=stockbot.research-state.v1"
  echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "consistency=api_and_worker_writes_quiesced_by_operator"
} > "$stage/manifest.txt"
(
  cd "$stage"
  find database.dump storage manifest.txt -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)
COPYFILE_DISABLE=1 tar -czf "$temporary" -C "$stage" database.dump storage manifest.txt SHA256SUMS
mv "$temporary" "$output"
echo "Matched database and market-file backup created: $output"
