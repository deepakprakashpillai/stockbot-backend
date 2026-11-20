# Model 2 historical research

Model 2 is an additional developer-owned algorithm, key
`model2_overnight_iron_condor`, code `1.0.0-model2-iron-condor`, engine
`1.0.0-model2-overnight-clock`. It uses preserved four-leg opening prices,
one chronological overnight clock and three independent exclusion policies.
No broker orders or live-capital controls are included.

## Installation and activation

`MODEL2_ENABLED=false` is the default. Apply migration `20261117_16`, install
compatible API/workers and the frontend pinned to REST 0.1.3 / WebSocket 1.3.0,
and complete the release checks before deployed activation. Set the switch true,
then run `python -m app.cli.sync_algorithm_registry`. Synchronization is
idempotent and creates the primary 200-point and alternative 150-point immutable
versions. Approved editable parameters are width 150/200, slippage 0/1/2 percent,
and the experimental VIX filter, OFF by default. Capital is a fixed run input.

`deploy/compose.yaml` passes `MODEL2_ENABLED` to both API and worker, defaulting
to false. Setting it in the environment file therefore controls the actual
container configuration; recreate the API to apply a changed flag.

On rollback, disable new submissions with `MODEL2_ENABLED=false`; synchronize
the catalog if it must hide Model 2. Saved groups remain readable and already
saved work retains its execution implementation. Do not delete records or
downgrade a database containing Model 2 groups/four-leg trades; the migration
explicitly refuses that destructive downgrade. Restore only a verified matched
database/files snapshot to a separate drill target.

## Preserving input data

Use `python -m scripts.build_model2_bundle --help` to build a native v3 manifest.
Every component has a source reference, checksum and explicit timestamp
semantics. Spot, VIX, CE/PE prices, contracts and sessions/expiries are required;
futures are optional. Official settlement and halt evidence are optional and
their absence is explicit. Contract ticks are in INR, with provider conversion
recorded at import. Never reinterpret completed-candle timestamps as opens.

Place the preserved bundle inside `STORAGE_ROOT`, then register it with:

```sh
python -m scripts.register_model2_bundle --bundle-dir /storage/new-bundle --name "NIFTY Model 2"
```

Registration verifies sources and preserves a dataset version. A later settlement
source creates a new bundle/version and replay; it never rewrites past outcomes.
`scripts.qualify_model2_sample` preserves and replays one declared January 2026
real V6 cycle at width 200 and 2% slippage, without searching for profitable
parameters or editing source files. Its report is a data/execution qualification,
not strategy validation.

## Requests and saved evidence

`POST /api/v1/research-scenario-groups/preflight` reviews entry data without
looking ahead and removing missing exits. Acknowledgement is bound to the exact
coverage digest. `POST /research-scenario-groups` atomically saves the immutable
group, three children and first delivery intent. `Idempotency-Key` prevents
duplicate research requests; reuse with different inputs returns a conflict.
Each child starts with fresh risk counters. The next child's durable delivery
intent is committed with terminal finalization of its predecessor.

Read group status and same-cycle comparison through `/research-scenario-groups`;
read each child's `/cycles`, `/option-positions`, `/risk-ledger`, `/assessment`
and chronological `/portfolio-journal`. The journal has a cursor bound to its
run and execution attempt; pending overnight states remain intact. Legacy
`/journal` returns a structured 409 directing Model 2 callers to this endpoint,
preserving the original three-state journal response schema. Positions contain
exactly four typed legs. The
legacy two-leg `/trades` representation returns a structured 409 for this mode.
Default history includes only the primary child; the group exposes the others.
Group cancellation retains open evidence without fabricating an exit.

PostgreSQL is authoritative. Redis delivers jobs and refresh notifications.
Delivery ignores Celery result-store subscriptions because outcomes already live
in PostgreSQL; an outage retains the pending intent and retries with the same
job identity. An expired worker lease creates a new fenced replay attempt,
retaining the earlier journal/cycle records. Old attempts cannot finalize or
schedule children. Reads default to the active attempt; its predecessors remain
available through the attempts/journal endpoints.

`backtest.portfolio_updated` uses the existing versioned WebSocket envelope.
Notifications follow committed evidence. REST polling/replay restores screens
after a disconnect; messages are not the sole result store.

## Money and assessment

Priced trades, closed-trade equity and costs use exact stored decimals in INR.
Excluded outcomes remain unknown. Assumed maximum loss, assumed zero and halted
contributions are separate risk-ledger entries, never returns. Reliability uses
excluded/opened including exclusions, with strictly more than 2% unreliable.
Halted/cancelled scopes are incomplete. No opened trades means insufficient data.

Section 12 diagnostics preserve zero/partial months and years, development and
known-exposed later dates. Evaluation requires 2% slippage. Random-day control
remains unassessed, sufficient unseen history is not inferred, and paper/live
qualification is deferred. Overall statuses cannot grant live approval.

The existing export-job endpoints build ZIP or constant-memory XLSX evidence for
all three terminal children separately. ZIP includes a row-count/checksum
manifest; oversized Excel cells use indexed continuation rows. Export reads use
a separate heartbeat transaction so renewing the lease cannot close a streaming
database cursor. No scenario totals are combined.

## Acceptance evidence

See the workspace's `docs/audits/2026-11-18-model-2-vps-acceptance/README.md` for
isolated Hostinger VPS migration, worker/Redis recovery, matched restore,
disablement and bounded sample capacity evidence. A one-off verified backup is
also on the Mac; recurring backups and numerical capacity acceptance remain open.
The 17 November report preserves earlier local results. Browser acceptance and
production activation remain open, as do the independent correction/cleanup gates.
