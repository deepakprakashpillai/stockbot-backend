# MVP Release Checklist and Version Policy

## Release identity

- Record backend Git commit/tag, frontend Git commit/tag, image digests, and UTC
  deployment time.
- Use independent semantic versions for frontend and backend. Patch releases
  are compatible fixes, minor releases are additive contracts/features, and
  major releases may break a published contract.
- Record the OpenAPI and WebSocket SHA-256 files shipped with the backend and
  the exact snapshots pinned by the frontend.
- Do not mutate an algorithm code version, parameter version, dataset version,
  or completed run. Behavior changes require a new identifiable version.

## Before deployment

- All Phase 0-7 acceptance tests remain green.
- `uv lock --check`, Ruff, mypy, pytest/coverage, migration upgrade/downgrade,
  contract snapshot verification, and dependency audit pass.
- No unresolved critical security finding or committed secret exists.
- Production access key, cookie/TLS settings, database/Redis URLs, CORS origin,
  storage, and backup destination are supplied by the deployment environment.
- PostgreSQL backup and dataset-volume backup have verified checksums.
- Release owner confirms live trading remains disabled and inaccessible.

## Staging-like rehearsal from scratch

1. Start a clean isolated Compose project; never remove the normal project's
   volumes to simulate cleanliness.
2. Build backend and frontend releases independently.
3. Apply migrations and synchronize the algorithm registry.
4. Verify `/health`, `/ready`, security headers, authentication, rate limits,
   and no secrets in browser bundles or logs.
5. Upload the preserved NIFTY candle dataset and confirm its immutable checksum.
6. Create an immutable platform-demo parameter version.
7. Run an identical request twice and verify deterministic results/snapshots.
8. Verify real-time progress, disconnect/reconnect replay, terminal REST state,
   results, comparison, filters, JSON export, and CSV export. For one known
   multi-month completed run, export the exact Trade, Day, and Month research
   views and reconcile their stored rows, URL filters, immutable metadata, and
   `DEMO_UNITS` safeguards with the screen.
9. Confirm every result/export says synthetic, uses `DEMO_UNITS`, and reports
   `financial_pnl_available = false`; there must be no currency symbol or
   strategy-performance claim.
10. Exercise API, worker, Celery Redis, real-time Redis, PostgreSQL, and browser
    restart scenarios using the operations matrix.
11. Demonstrate stalled-run dry-run and recovery on a deliberately isolated
    test run.
12. Restore the backup into a separate `*_restore_drill` database and compare
    migration, dataset, version, and run counts.
13. Run the capacity benchmark and save its JSON result with the release record.
14. Complete keyboard, focus, labels, contrast, 375px/200%-zoom tables/charts,
    empty/error, warning, cancellation, and timezone QA.
15. If completed runs predate the analytics tables, run the bounded analytics
    backfill, record its progress/duration, rerun one batch to prove
    idempotence, and retain the reconciliation evidence with the release.

## Go/no-go and rollback

Go only when every release gate has evidence and an owner. A failed readiness,
restore, contract, deterministic replay, synthetic-label, or security gate is a
no-go.

Rollback frontend and backend independently only to contract-compatible
versions. Prefer a forward database fix; if a downgrade is unavoidable, stop
writers, take a new backup, inspect the migration downgrade, and confirm no
newer-schema data will be lost. After rollback, rerun readiness, authentication,
one durable result read, WebSocket replay, and synthetic-label checks.

Archive the checklist, test reports, benchmark JSON, backup/restore evidence,
known limitations, and release identifiers together. Never archive access keys,
cookies, tickets, database URLs, or provider credentials.
