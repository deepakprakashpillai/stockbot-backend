#!/bin/sh
set -eu

usage() {
  echo "Usage: RESTORE_DATABASE_URL=... $0 --input /absolute/path/research-state.tar.gz --storage-target /empty/path --confirm RESTORE_ISOLATED_DRILL" >&2
  exit 2
}

input=""
target=""
confirm=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --input) [ "$#" -ge 2 ] || usage; input="$2"; shift 2 ;;
    --storage-target) [ "$#" -ge 2 ] || usage; target="$2"; shift 2 ;;
    --confirm) [ "$#" -ge 2 ] || usage; confirm="$2"; shift 2 ;;
    *) usage ;;
  esac
done

[ -n "${RESTORE_DATABASE_URL:-}" ] || usage
[ -f "$input" ] || usage
[ "$confirm" = "RESTORE_ISOLATED_DRILL" ] || usage
case "$target" in /*) ;; *) usage ;; esac
[ ! -e "$target" ] || { echo "Refusing to replace an existing storage target." >&2; exit 2; }
[ -d "$(dirname "$target")" ] || usage

umask 077
stage=$(mktemp -d "$(dirname "$target")/.stockbot-restore.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
python3 - "$input" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

seen = set()
required = {"database.dump", "manifest.txt", "SHA256SUMS", "storage"}
with tarfile.open(sys.argv[1], "r:gz") as archive:
    for member in archive:
        name = member.name.rstrip("/")
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or ".." in path.parts
            or name in seen
            or not (name in required or name.startswith("storage/"))
            or not (member.isfile() or member.isdir())
            or (name == "storage" and not member.isdir())
            or (name in required - {"storage"} and not member.isfile())
        ):
            raise SystemExit(f"Unsafe or unexpected backup member: {member.name}")
        seen.add(name)
if not required <= seen:
    raise SystemExit("Backup is missing required members.")
PY
tar -xzf "$input" -C "$stage"
(
  cd "$stage"
  [ "$(head -n 1 manifest.txt)" = "format=stockbot.research-state.v1" ]
  sha256sum -c SHA256SUMS >/dev/null
)
"$(dirname "$0")/restore_postgres.sh" --input "$stage/database.dump" --confirm RESTORE_ISOLATED_DRILL
mv "$stage/storage" "$target"
echo "Matched database and market-file restore drill completed: $target"
