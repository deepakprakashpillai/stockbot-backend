# StockBOT MVP Operations Runbook

This runbook covers the platform-demo MVP. It does not enable the deferred Bull
Call Spread brain, real option fills, broker orders, financial P&L, or live
trading.

## Supported release boundary

- Python 3.12, PostgreSQL 17, Redis 7, FastAPI, and Celery 5.
- REST API `/api/v1` with the checked-in `contracts/openapi.json` snapshot.
- WebSocket contract `stockbot.websocket.v1`, snapshot version `1.0.0`.
- Dataset schema `nifty_index_candles_v1`, five-minute Asia/Kolkata candles.
- Algorithm code versions `1.0.0-platform-demo` and the non-production
  `0.1.0-signal-research` research implementation.
- Only `PLATFORM_DEMO` produces complete synthetic result records. Its values
  are `DEMO_UNITS`, not money, and `financial_pnl_available` is always false.

## Clean setup and migration

1. Copy `.env.example` to `.env`, replace the access key, and keep secrets out
   of the frontend and source control.
2. Run `docker compose config` and confirm PostgreSQL and Redis have no public
   host ports.
3. Start dependencies and application processes with
   `docker compose up --build -d`.
4. Apply migrations with `docker compose exec api alembic upgrade head`.
5. Seed developer-owned algorithms with
   `docker compose exec api python -m app.cli.sync_algorithm_registry`.
6. Require both `/health` (process) and `/ready` (PostgreSQL, Celery Redis, and
   real-time Redis) to pass before accepting use.

Forward migrations are the normal production path. Downgrades are tested in CI
to catch reversible-schema mistakes, but a deployed release should be rolled
forward unless the release owner has reviewed the downgrade and confirmed no
new data would be lost. Back up before every migration.

## Stalled-run inspection and recovery

A RUNNING run is stalled only after its database lease expires. A PENDING run
is considered a lost delivery after 15 minutes by default. Cancellation-marked
and terminal runs are never selected.
Input preparation renews its lease every 15 seconds through a separate database
session while it verifies the saved market files. If ownership changes, that
worker stops before writing results.

Inspection is read-only and is always the first step:

```bash
docker compose exec api python scripts/recover_stalled_runs.py
docker compose exec api python scripts/recover_stalled_runs.py --run-id RUN_UUID
```

After checking the worker is healthy and only the intended runs appear, apply:

```bash
docker compose exec api python scripts/recover_stalled_runs.py \
  --run-id RUN_UUID --apply --confirm RECOVER_STALLED_RUNS
```

Recovery locks and re-checks the row, clears the obsolete lease, records a
durable `BACKTEST_ADMIN_REQUEUED` event, and resets the PostgreSQL job-delivery
intent to PENDING in the same commit. It then invokes the normal dispatcher.
If Redis is unavailable, that intent remains PENDING for the regular dispatcher
to retry. Re-execution replaces generated signals, trades, equity, and metrics
from the immutable run snapshot. Never recover while an unexpired lease owner
may still be working.

## Backup and isolated restoration drill

Quiesce API and worker writes first, then create one archive containing the
PostgreSQL dump and the matching dataset storage tree. Use an encrypted,
operator-controlled destination outside the storage tree. The archive includes
per-file SHA-256 checksums and refuses to overwrite an existing backup:

```bash
DATABASE_URL='postgresql+psycopg://...' STORAGE_ROOT=/absolute/data \
  scripts/backup_research_state.sh --output /absolute/secure/stockbot-YYYYMMDD.tar.gz \
  --confirm QUIESCED_WRITES
```

For an isolated drill, create an empty database whose name ends exactly in
`_restore_drill` and choose a storage target that does not exist. The restore
script checks archive member types and checksums before restoring either part:

```bash
RESTORE_DATABASE_URL='postgresql+psycopg://.../stockbot_restore_drill' \
  scripts/restore_research_state_drill.sh \
  --input /absolute/secure/stockbot-YYYYMMDD.tar.gz \
  --storage-target /absolute/isolated/stockbot-data \
  --confirm RESTORE_ISOLATED_DRILL
```

The database-only tools remain available when the storage volume is backed up
by another documented, matching snapshot mechanism:

```bash
DATABASE_URL='postgresql+psycopg://...' \
  scripts/backup_postgres.sh --output /absolute/secure/stockbot-YYYYMMDD.dump
```

For a restoration drill, create an empty database whose name ends exactly in
`_restore_drill`. The restore script refuses every other target name and needs
typed confirmation. Never point it at the active database.

```bash
RESTORE_DATABASE_URL='postgresql+psycopg://.../stockbot_restore_drill' \
  scripts/restore_postgres.sh --input /absolute/secure/stockbot-YYYYMMDD.dump \
  --confirm RESTORE_ISOLATED_DRILL
```

Record the archive checksum, start/end time, migration version printed by the
script, run count, and operator. In the restored environment compare
dataset/version/run counts, verify stored `storage_key` paths, and open one
completed result before calling a production restore drill complete.

## Restart and recovery matrix

| Failure | Expected behavior | Operator verification |
|---|---|---|
| API restart | PostgreSQL runs remain intact; browser reconnects and resumes durable events | `/ready`, browser status, terminal REST snapshot |
| Worker crash | Active lease expires; task redelivery or explicit recovery can continue idempotently | worker logs, expired lease dry-run, one terminal event |
| Celery Redis restart | API/database remain authoritative; queued delivery may need recovery | Redis health, worker reconnect, stale PENDING dry-run |
| Real-time Redis restart | No business data loss; clients reconnect and replay PostgreSQL events | WebSocket reconnect and sequence continuity |
| PostgreSQL restart | readiness fails and commands stop safely until the database returns | `/health` stays live, `/ready` fails then recovers |
| Browser refresh/network loss | one new ticket/socket resumes from last durable sequence | no duplicate visible events, final REST refresh |

Do not delete Redis/PostgreSQL volumes during a restart exercise. Stop one
service at a time, note timestamps, restore it, and verify readiness before the
next exercise.

## Capacity baseline

Run the deterministic engine-only baseline on the release host:

```bash
uv run python scripts/benchmark_platform_demo.py
```

The representative fixture is 18,000 candles, roughly the preserved MVP data
scale. The conservative gate is at least 10,000 candles/second and at most 128
MiB peak Python allocation for three repeated evaluations. The tool also proves
decision determinism with a SHA-256 digest. This is an engine regression gate,
not a full API/database load test and not evidence of trading performance.

The 2026-08-05 local release rehearsal passed at 187,681 candles/second with a
16.19 MiB peak allocation. All three repetitions produced decision digest
`e4f81c1f3e7ab0eddb0c568ac322467ee48a1ff3675e457cf3738fed6937aba4`.
Hardware and load affect timing, so each release environment must record its own
result instead of treating this measurement as a universal capacity promise.

For an MVP demonstration, support one active run per worker and a small number
of browser sessions. Re-measure before increasing concurrency, input size, or
WebSocket fanout. Capture API/worker/container metrics during the clean release
journey; do not infer production capacity from the engine-only number.

## Dataset upload-session cleanup and profiles

Phase 13 keeps uploaded CSV bytes in a server-private staging area until the
user explicitly commits the preview. Expire abandoned sessions in bounded,
idempotent batches; the command marks the durable row first and then removes
the staged bytes, so a retry tolerates an already-missing file.

```bash
docker compose exec api python scripts/cleanup_dataset_upload_sessions.py --limit 100
```

After deploying the immutable profile column to an environment with earlier
valid or warning dataset versions, populate missing profiles in bounded,
idempotent batches:

```bash
docker compose exec api python scripts/backfill_dataset_profiles.py --limit 100
```

Neither command changes original immutable dataset bytes, validation status, or
research data. Investigate storage permission failures before retrying.

## Completed-run analytics backfill

Phase 12 exports read only the derived analytics stored for a completed run.
After deploying the analytics schema to an environment with earlier completed
runs, rebuild a bounded idempotent batch before exposing those exports:

```bash
docker compose exec api python scripts/backfill_completed_run_analytics.py --limit 100
```

The command logs start, per-run progress, completion duration, and failures. It
reads immutable run snapshots and synthetic trades, then replaces only derived
analytics rows. Re-running the same batch is safe; verify one known multi-month
run against its Day, Month, and Trade exports before considering the backfill
complete.

## Troubleshooting

- `/health` fails: the API process is down or cannot serve HTTP.
- `/health` passes and `/ready` fails: inspect the named PostgreSQL/Redis check;
  do not accept new runs until it recovers.
- Run remains PENDING: confirm a worker is connected, then use the dry-run
  recovery command after the age threshold.
- Run remains RUNNING: do not requeue until `lease_expires_at` is in the past.
- WebSocket is degraded: results remain available over REST; restart only the
  real-time Redis service and verify durable replay.
- Dataset checksum mismatch: restore the exact versioned file from storage
  backup; never replace metadata or silently edit the CSV.
- Contract mismatch: deploy a frontend that pins the backend OpenAPI and
  WebSocket snapshots, or roll the backend forward to a compatible release.

Operational logs must not contain access keys, session tokens, WebSocket
tickets, database URLs, or dataset contents. Preserve request/run/task/event IDs
for diagnosis.

## Known limitations

- Authentication is the documented single-principal MVP access-key/session
  boundary, not a multi-user identity system.
- Local filesystem dataset storage and synchronous exports are bounded MVP
  choices; object storage and asynchronous large exports remain post-MVP.
- No transactional outbox exists. PostgreSQL events are durable, while a Redis
  notification can be missed and recovered by replay.
- No realistic options P&L is available because executable option bid/ask data
  and contract metadata are absent.
- Bull Call Spread rules and score-model conflicts remain partner-owned and
  unresolved. No broker SDK, order route, live capital, or live trading exists.

## 2026-08-05 restoration evidence

The custom archive catalog verified successfully and restored into the isolated
`stockbot_restore_drill` database at Alembic revision `20260805_05`. Active and
restored counts matched exactly: 4 datasets, 4 dataset versions, 5 algorithm
versions, and 4 backtest runs. The isolated database and temporary archive were
removed after comparison. This proves the PostgreSQL drill procedure; a release
owner must still include and verify the separate dataset-storage backup.
