# Model 2 specification foundation

This directory freezes the historical-research source set for specification
v1.4 and the Phase 36A source/compatibility baseline. It is not an API contract,
registered strategy, executable overnight engine or evidence of profitability.

`specification-v1.4.json` is a detached snapshot of the code-owned fixed rules,
approved presets, ordered source hashes and rule-to-acceptance-case map. Its
`specification_sha256` identifies the canonical compact JSON specification;
`specification-v1.4.sha256` identifies the bytes of the pretty-printed wrapper.
These are intentionally different hashes. `implementation_status_at_freeze`
records the stage at which this artifact was captured, not current catalog
availability. Runtime version and readiness belong to later implementation.

`sources/` preserves exact copies of the original Word document, Round 2 text
and lifecycle addendum. Source precedence is lifecycle, Round 2, original.
The old rules in the original Word file are not rewritten. These copies let
CI verify provenance without access to the parent workspace or chat attachments.

`tests/fixtures/model2/acceptance_cases_v1.json` contains 50 expected scenarios
for later executable tests, linked to 17 rule groups. Its
`engine_assertions_pending = true` is deliberate: provenance/case coverage does
not demonstrate an implemented engine. Current parameter validation tests are
executable; future selection/feed/overnight/scenario tests will consume these
vectors as their respective slices are implemented.

`tests/fixtures/model2/legacy_compatibility_v1.json` was captured before Model 2
runtime changes from backend commit
`ee81c3fa04c578ae655e2c3209499a70484410af`. It pins the six existing algorithm code
identities, their defaults/templates, schemas/rules/documentation, decisions
on three fixed sessions and Platform Demo execution. Tests project these
identities, so adding a new algorithm never requires rewriting old fingerprints.
Do not automatically refresh the baseline to make a changed old strategy pass.

The archived 36A snapshot keeps its original `null` denominator and
`NOT_ASSESSED_POLICY_PENDING` status. The current approved revision below
supersedes that pending rule without changing the archived bytes. The VIX filter
is explicitly experimental, defaults OFF, and Model 3/live trading are excluded.

From the backend root, run:

```sh
rtk proxy .venv/bin/python -m pytest tests/test_model2_foundation.py tests/test_model2_compatibility_baseline.py
```

The [shared Phase 36 technical plan](../../../docs/implementation/36-model-2-overnight-iron-condor.md)
lives in the parent workspace. Use its gates before runtime work.

## Approved revision and components added in 36C

`specification-v1.4-r1.json` freezes internal revision `2026-11-17-r1` and
supersedes the original specification digest. Its ordered sources prepend
`sources/approved-reliability-2026-11-17.txt` (SHA-256
`51f2b3668ea3c08b08e43e0e97fbde70df27cc0db24b2d116b6e3636913f06ca`).
Current canonical specification digest:
`755096fd04fc4fda345673e8b08685cedf3f5d82db5b8f98c7dce6c222c188fa`.
The `.sha256` companion identifies wrapper bytes, as for the original artifact.

The approved denominator is opened cycles including excluded positions;
pre-entry skips are excluded. Strictly more than 2% is unreliable; no opened
cycles means insufficient sample. Assumed exclusion losses book on expiry date
and affect only risk controls. Later settlement evidence requires a new dataset
and replay. The current rules live in `approved_specification.py`; the original
`specification.py` stays an archival 36A baseline.

`tests/fixtures/model2/acceptance_cases_r1.json` preserves 50 revised future
engine vectors separately from v1. Pure selection, pricing, risk-control and
data-gate tests are executable in `tests/model2/test_strategy.py` and
`tests/model2/test_risk_controls.py`. The chronological engine and scenario
orchestration were delivered separately in 36D below. No catalog, database, worker or REST/WebSocket contract
is activated by these components. See the
[36C evidence](../../../docs/audits/2026-11-17-model-2-strategy/README.md).

## Overnight clock and scenarios added in 36D

`app/domain/model2/execution.py` defines immutable request, event, cycle and
summary records. `clock.py::iter_replay` streams evidence in simulated market
order; `scenarios.py` collects results or replays the three policies sequentially
with independent risk counters. Domain results retain dataset/range/parameters,
capital, calendar version/hash, specification digest, algorithm code identity
`1.0.0-model2-iron-condor` and engine `1.0.0-model2-overnight-clock`.
These identities are not registered in the platform catalog yet.

Callers snapshot an explicit settlement-data checkpoint for each ready cycle.
Official data is queried only at that time after the final scheduled exit open;
its values cannot be used before source availability. Checkpoints cannot cross
the next cycle decision and retroactively change its risk state. This is an
input-availability assumption, not a new trading parameter or a guessed official
publication time. New evidence requires a new immutable snapshot/replay.

`tests/fixtures/model2/clock_goldens_v1.json` freezes independently declared
milestones for time exit and overnight target-to-gap-stop paths. The 82 executable
clock tests also cover entry retries, emergency evidence, late fills, intrinsic
settlement, exclusion, risk pauses, cancellation and divergent scenario paths.
Native complete two-session CSV/Parquet fixtures verify both timestamp conventions.
The older acceptance packs remain unchanged; later persistence/API assertions
are still pending. See the
[36D evidence](../../../docs/audits/2026-11-17-model-2-overnight-engine/README.md).

## Market-data layer added in 36B

`market-bundle-v3.schema.json` and `calendar-v1.schema.json` snapshot the new
backend-only data contracts. They do not change REST/WebSocket contracts or
alter the original v1/v2 bundle readers. The new Model 2 feed requires spot,
VIX, contracts, an explicit session/weekly-expiry calendar and both CE/PE price
series. Futures are optional. Official settlement and exchange-halt components
are optional and carry explicit provenance/availability; their absence remains
observable and cannot be filled with ordinary spot prices or guessed halts.

Price components are CSV or Parquet with a `timestamp` string using explicit
`+05:30`, OHLC and, for options, volume. Each component declares `interval_start`
or `interval_end`. An end-stamped 15:25 row provides its open at 15:20 and its
closing fields/volume at 15:25. Zero option prices/volume remain in source data;
the later strategy applies positive-price/volume fill-validity checks. Native
contracts require `contract_id`, `symbol`, `expiry`, integer `strike`, `CE`/`PE`
`option_type`, integer `lot_size` and explicitly rupee-denominated `tick_size_inr`.

The calendar declares covered dates, ordered normal/special sessions, expected
weekly expiries and their source. Normal sessions use 09:15–15:30. Missing
sessions within declared coverage are the source's closure assertions, so the
source must be qualified; a checksum alone does not establish exchange truth.
Unsupported dates fail. A new immutable calendar can add qualified years;
the reader does not invent dates or change an older calendar in place.

To snapshot already-prepared files, create a JSON list of declarations such as:

```json
[
  {"kind":"spot","path":"spot.csv","timestamp_semantics":"interval_start","source_reference":"Preserved provider export"},
  {"kind":"india_vix","path":"vix.parquet","timestamp_semantics":"interval_end","source_reference":"Preserved provider export"},
  {"kind":"contracts","path":"contracts.json","source_reference":"Preserved contract master"},
  {"kind":"calendar","path":"calendar.json","source_reference":"Qualified calendar source"},
  {"kind":"options","contract_id":"CALL_ID","path":"call.parquet","timestamp_semantics":"interval_end","source_reference":"Preserved provider export"},
  {"kind":"options","contract_id":"PUT_ID","path":"put.parquet","timestamp_semantics":"interval_end","source_reference":"Preserved provider export"}
]
```

Paths are relative to the supplied bundle directory and must stay inside it.
The CLI calculates checksums, validates file contents, requires both call/put
series and refuses a changed manifest. Missing particular contracts or their
price series remain evidence for later cycle preflight; no strike substitution
or dataset registration happens in this command.

```sh
rtk proxy .venv/bin/python -m scripts.build_model2_bundle --bundle-dir /path/to/prepared/bundle --components /path/to/components.json
```

Run the fixed, read-only V6 sample verification with:

```sh
rtk proxy .venv/bin/python -m scripts.verify_model2_market_feed --output /tmp/model2-feed-sample.json
```

The sample's four hand-declared contracts and current-platform calendar test
data access, not strategy performance or full-history qualification. Raw V6
five-paise ticks are explicitly converted for that fixed sample; the native
reader does not apply a unit heuristic. The temporary v3 bundle is removed.
