# Historical Model 2 bundle calendar

`build_model2_calendar.py` creates an explicit calendar component for a native
Model 2 v3 bundle. It does not change the application's shared calendar, strategy
rules, approved parameters or research approval state. It makes no network calls.

The adjacent `model2_calendar_sources_2023_2026.v1.json` records official NSE
holiday circulars, exceptional openings, the deferred April 2025 expiry proposal
and the actual September 2025 Thursday-to-Tuesday change. Its qualified range is
15 November 2023 through 16 November 2026. Holiday shifts use announced sessions,
not gaps inferred from price rows. A Muhurat opening on a holiday does not restore
the regular expiry to that holiday. Monthly-expiry weeks remain opportunities.

Example (from the backend repository):

```sh
python scripts/build_model2_calendar.py \
  --spot-csv /absolute/path/to/spot.csv \
  --start-date 2023-11-16 --end-date 2026-11-16 \
  --expiry-evidence /absolute/path/to/expiry-evidence.json \
  --output-dir /absolute/path/to/new-calendar-directory
```

The output directory must not exist. Successful session reconciliation writes
`calendar.json` and `calendar_qualification.json`. If observed dates disagree with
official sessions, only the audit is written and the command exits unsuccessfully.
The audit records missing regular candle openings and extra observations separately;
these do not redefine calendar hours. Session qualification is not a claim that
every option contract or candle needed for backtesting exists.

The provider evidence format is:

```json
{
  "schema_version": "stockbot.model2-expiry-evidence.v1",
  "source_reference": "Provider endpoint and capture reference",
  "queried_expiries": [{
    "requested_expiry": "2025-10-28",
    "status": "AVAILABLE",
    "response_sha256": "64 hexadecimal characters from the captured response",
    "contracts": [{
      "expiry": "2025-10-28",
      "instrument_type": "CE",
      "instrument_key": "provider contract identity",
      "strike_price": 25000,
      "lot_size": 75,
      "tick_size": 5
    }]
  }]
}
```

`AVAILABLE`, `EMPTY` and `ERROR` are distinct. Each response must have a hash.
Only matching expiry metadata with both CE and PE confirms an expiry; the
provider's `weekly` flag is not used to discard monthly-expiry weeks. This still
does not confirm required strikes or prices. All sourced scheduled cycles remain
in the calendar regardless of provider availability, so unavailable history cannot
silently disappear from the opportunity count. Unqueried dates remain UNQUERIED,
not EMPTY. The audit separately lists any queried dates outside the sourced schedule.

The calendar includes one sourced session before the requested start where
available. This is calendar context and does not assert price coverage. For the
three-year source starting 16 November 2023, the first expiry's entry is 15 November
and is explicitly `OUTSIDE_COMPLETE_CYCLE_RANGE` for a run beginning on the 16th.
The 8 November 2026 Muhurat opening is retained as SPECIAL; therefore the 9 November
expiry has a SPECIAL entry session and is skipped by the existing Model 2 engine.
Exceptional full-hours weekend openings are also labelled SPECIAL, consistent
with the existing February 2026 calendar treatment. Changing that policy requires
an explicit calendar-policy decision.

The source archive and audit may span all three years even when matching option
metadata is available only for a later subset. Name and describe the registered
bundle accordingly; do not describe the entire three-year range as option-ready.
Keep the source facts and qualification audit alongside the immutable bundle for
review, and include their official references in the calendar component's source
reference. The regular bundle builder verifies the resulting component checksum.
