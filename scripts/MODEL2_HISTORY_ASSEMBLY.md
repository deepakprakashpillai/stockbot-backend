# Assemble the historical Model 2 bundle

`assemble_model2_history_bundle.py` assembles declared acquisitions after the
download finishes. It does not download data, read credentials, place orders,
change algorithms or register anything on the website.

Run from the backend repository, after confirming the acquisition is complete:

```sh
uv run python -m scripts.assemble_model2_history_bundle \
  --acquisition-root /absolute/path/to/options-2023-2026 \
  --calendar-dir /absolute/path/to/options-2023-2026/calendar-source-v2 \
  --output-dir /absolute/path/to/new-native-bundle \
  --spot-csv /absolute/path/to/nifty_spot_5min_2023-11-16_to_2026-11-16.csv \
  --vix-csv /absolute/path/to/india_vix_5min_2023-11-16_to_2026-11-16.csv \
  --reuse-bundle-root /absolute/path/to/upstox_nifty_derivatives_v6
```

The output directory must not exist. The command checks exact coverage of all
request identities from the downloader's month-split plan before creating output.
`COMPLETE`, `EMPTY` and `PARTIAL_INVALID` are completed acquisition states; a failed
or unfinished request prevents assembly. Every raw response, normalized CSV and
contract capture must match its saved checksum and request declaration.

Fresh chunks are grouped by actual expired instrument identity, then merged in
timestamp order without shifting their source-start timestamps. Numerically
identical duplicate observations collapse and are counted; conflicting values at
one timestamp fail assembly. Invalid raw rows remain available in evidence and
their reason counts remain visible. No replacement prices or missing candles are
created. An empty selected contract remains in metadata but has no price component.

Reused V6 files must match the declared immutable source manifest, its contract
master and exact expiry/strike/type path. They are copied byte for byte and keep
`interval_end` semantics; the native feed interprets that declaration. Fresh series
keep `interval_start` semantics. The assembler refuses mixed source semantics for
one contract. Historical lot sizes are preserved. Provider `tick_size` is converted
from paise to rupees by dividing by 100, matching the existing provider loader.

The bundle contains complete source spot/VIX CSVs, a sourced explicit calendar,
selected contracts, available option series and `bundle_qualification.json`. The
qualification distinguishes the requested three-year range from actual available
option expiry/observation ranges, unconfirmed early cycles, fully empty selected
expiries, invalid source-row reasons and per-contract coverage. The 150/200-point
wing selection declarations are research inputs; assembly does not choose a
configuration by its performance or produce strategy P&L.

`evidence/` contains only explicitly whitelisted market-data captures and reports,
the acquisition plans, calendar sources/audit and reused bundle metadata. It never
copies a whole source project or credential directory. Its checksum index is
pinned in every native component's provenance, so the evidence is tied to the
immutable manifest. Source market-history README/download reports are included
when available; they describe the original source, not a guarantee that option
coverage spans all of those dates.

Finally, the existing `build_model2_bundle` validates all components and writes the
native v3 manifest. A failed validation leaves an unregistered output directory
for inspection; it does not publish a valid manifest or overwrite an older bundle.
Registration, website descriptions and disclosure of coverage warnings remain a
separate operator step. Candles are OHLC pricing proxies, not executable bid/ask.

Focused verification:

```sh
uv run pytest tests/model2/test_history_bundle_assembly.py tests/model2/test_calendar_builder.py
uv run ruff check scripts/assemble_model2_history_bundle.py tests/model2/test_history_bundle_assembly.py
uv run mypy --follow-imports=silent scripts/assemble_model2_history_bundle.py
```
