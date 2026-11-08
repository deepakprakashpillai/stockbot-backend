#!/usr/bin/env bash
set -euo pipefail

# Restricted SSH entry point: only checked GitHub image tags can be deployed.
read -r component revision extra <<< "${SSH_ORIGINAL_COMMAND:-}"
[[ -z "${extra:-}" && "$revision" =~ ^[0-9a-f]{40}$ ]]
[[ "$component" == backend || "$component" == frontend ]]
exec 9>/opt/stockbot/deploy.lock
flock -n 9 || { echo 'Another StockBOT deployment is in progress.' >&2; exit 1; }

env_file=/opt/stockbot/research.env
compose_file=/opt/stockbot/backend/deploy/compose.yaml
test -r "$env_file" && test -r "$compose_file"
compose=(docker compose --env-file "$env_file" -f "$compose_file")

if [[ "$component" == backend ]]; then
  export BACKEND_IMAGE="ghcr.io/deepakprakashpillai/stockbot-backend:sha-$revision"
  "${compose[@]}" pull api
  # Let an active task finish before replacing the worker. New requests are paused.
  "${compose[@]}" stop api
  "${compose[@]}" stop -t 600 worker
  "${compose[@]}" up -d --wait postgres redis
  "${compose[@]}" run --rm --no-deps api alembic upgrade head
  "${compose[@]}" run --rm --no-deps api python -m app.cli.sync_algorithm_registry
  "${compose[@]}" up -d --no-deps api worker
  curl --fail --silent --show-error --max-time 5 --retry 15 --retry-delay 2 --retry-all-errors http://127.0.0.1:18080/ready
  image_key=BACKEND_IMAGE
  image_value=$BACKEND_IMAGE
else
  export FRONTEND_IMAGE="ghcr.io/deepakprakashpillai/stockbot-frontend:sha-$revision"
  "${compose[@]}" pull frontend
  "${compose[@]}" up -d --no-deps frontend
  curl --fail --silent --show-error --output /dev/null --max-time 5 --retry 15 --retry-delay 2 --retry-all-errors http://127.0.0.1:18081/
  image_key=FRONTEND_IMAGE
  image_value=$FRONTEND_IMAGE
fi

# Keep secrets in the server-only environment file, and remember the deployed image.
temporary=$(mktemp /opt/stockbot/research.env.XXXXXX)
chmod 600 "$temporary"
trap 'rm -f "$temporary"' EXIT
awk -v key="$image_key" -v image="$image_value" 'index($0, key "=") == 1 { print key "=" image; next } { print }' "$env_file" > "$temporary"
mv -f "$temporary" "$env_file"
echo "Deployed $component $revision"
