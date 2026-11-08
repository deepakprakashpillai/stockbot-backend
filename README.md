# StockBOT Backend

The clean backend foundation for StockBOT's reproducible research platform.
It provides configuration, PostgreSQL and Alembic wiring, Redis and Celery
worker wiring, health/readiness checks, structured request IDs, and a versioned
OpenAPI contract, immutable dataset ingestion, the developer-owned algorithm
registry, asynchronous backtests, Bull Call Spread V1 estimated historical
option-spread persistence, resumable WebSocket delivery, filtered run history,
compatible-run comparison, and safe JSON/CSV exports. Live trading and
executable bid/ask option execution remain deferred.

Shared architecture and phase plans are in [`../docs/`](../docs/README.md).
This folder is an independent Git repository; run all backend commands here.

## Local development

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --all-groups
uv run uvicorn app.main:app --reload
```

Copy `.env.example` to `.env` to override local connection values. The default
development values expect PostgreSQL and Redis on localhost.

## Foundation endpoints

- `GET /health` confirms the API process is alive and does not call dependencies.
- `GET /ready` checks PostgreSQL, the Celery Redis broker, and the real-time Redis bus.
- `GET /api/v1/system` exposes the service name, API version, and environment.

Open the generated API documentation at <http://127.0.0.1:8000/docs>.

## Database and workers

Run migrations explicitly before starting services:

```bash
uv run alembic upgrade head
uv run python -m app.cli.sync_algorithm_registry
uv run celery -A app.workers.celery_app:celery_app worker --loglevel=INFO
```

The worker exposes diagnostics, asynchronous dataset validation, durable
`PLATFORM_DEMO` backtests, and `OPTION_SPREAD_SIMULATION` Bull Call Spread V1
backtests. The latter use a registered derivatives bundle and option candle
closes with configurable adverse slippage, so their INR outcomes are historical
estimates rather than executable bid/ask fills. It does not place orders or run
live trading work.

## Local MVP session and real-time connection

The browser exchanges `STOCKBOT_LOCAL_ACCESS_KEY` once through
`POST /api/v1/auth/session`. The API keeps the resulting opaque session in the
real-time Redis database and sets an HttpOnly, SameSite=Strict cookie. An
authenticated browser can then request a 60-second, single-use WebSocket ticket
from `POST /api/v1/realtime/tickets` and connect to `/api/v1/ws?ticket=...`.
Change the development access key before any shared deployment; startup rejects
the unchanged default in production.

PostgreSQL `run_events` remain authoritative. Redis channels under
`stockbot:realtime:*` only notify gateways that committed events are available,
and are isolated from Celery's broker and result databases.

## Results, comparison, and exports

`GET /api/v1/backtests` is the durable run catalog. It supports status,
dataset, algorithm, version, mode, selected-range, and creation-date filters;
cursor pagination is ordered newest first and `total_count` always describes
the full filtered result set.

`POST /api/v1/backtests/comparisons/preflight` validates a selection before
`POST /api/v1/backtests/comparisons` returns immutable inputs, parameter and
assumption differences, and stored metrics. It blocks non-completed runs,
mixed modes, and runs using different dataset versions or date ranges. Option
spread comparisons carry their estimated-pricing limitation.

`GET /api/v1/backtests/{run_id}/exports/{summary|trades|signals}` returns JSON
by default or CSV with `?format=csv`. Every export embeds the immutable run
snapshot and the explicit `synthetic = true`, `DEMO_UNITS`, and
`financial_pnl_available = false` boundary. Synchronous collection exports are
capped at 10,000 rows instead of silently truncating data.

`GET /api/v1/backtests/{run_id}/signals/{signal_id}/detail` returns the
versioned, decision-time evidence for one signal, including ordered
documentation-step checks, exact immutable inputs, parameter snapshot,
previous/next signal identity, and an explicitly linked synthetic trade when
one exists. Signals created before diagnostic persistence return limited raw
evidence instead of a newly inferred explanation.

## Docker Compose

```bash
docker compose up --build
docker compose exec api alembic upgrade head
docker compose run --rm api python -m app.cli.sync_algorithm_registry
```

Compose exposes only the API on `127.0.0.1:8000`; PostgreSQL and Redis remain
inside the Compose network.

## Verification

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run coverage run -m pytest
uv run coverage report
uv run python scripts/export_openapi.py
shasum -a 256 contracts/openapi.json
```

Operational recovery, PostgreSQL backup/restore drills, restart behavior, and
the deterministic capacity baseline are documented in
[`docs/operations.md`](docs/operations.md). Use the auditable release gate in
[`docs/release-checklist.md`](docs/release-checklist.md) for every MVP build.

`contracts/openapi.json`, `contracts/openapi.sha256`,
`contracts/websocket-v1.json`, and `contracts/websocket-v1.sha256` are the
versioned backend contracts for the separate `stockBOT-frontend` repository.
