from __future__ import annotations

import csv
import fcntl
import json
import sys
from datetime import date

import httpx
import pytest

from scripts.download_option_candles import (
    CandleRequest,
    DownloadClient,
    DownloadError,
    acquire,
    load_plan,
    main,
    normalize,
)


def contract() -> dict:
    return {
        "instrument_key": "NSE_FO|40491|06-01-2026",
        "expiry": "2026-01-06",
        "instrument_type": "CE",
        "strike_price": 26400,
        "lot_size": 65,
        "tick_size": 5,
    }


def request() -> CandleRequest:
    return CandleRequest(contract(), date(2026, 1, 5), date(2026, 1, 6))


def payload(rows=None) -> dict:
    return {
        "status": "success",
        "data": {
            "candles": rows
            if rows is not None
            else [
                ["2026-01-05T09:15:00+05:30", 100, 104, 98, 102, 5, 10],
                ["2026-01-05T09:20:00+05:30", 102, 105, 100, 101, 0, 11],
            ]
        },
    }


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def client(handler, *, retries=0, clock=None):
    clock = clock or Clock()
    return DownloadClient(
        "private-test-credential",
        retries=retries,
        transport=httpx.MockTransport(handler),
        clock=clock.clock,
        sleep=clock.sleep,
    )


def test_plan_clamps_to_expiry_splits_months_and_excludes_secret_fields(tmp_path):
    metadata = {
        **contract(),
        "access_token": "never-write-this",
        "expiry": "2026-02-06",
        "instrument_key": "NSE_FO|40491|06-02-2026",
    }
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "requests": [
                    {"contract": metadata, "from_date": "2026-01-30", "to_date": "2026-03-10"}
                ]
            }
        )
    )
    plan = load_plan(path)
    assert [(r.from_date, r.to_date) for r in plan] == [
        (date(2026, 1, 30), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 6)),
    ]
    assert all("access_token" not in r.contract for r in plan)
    assert all(r.instrument_key == metadata["instrument_key"] for r in plan)


def test_normalization_keeps_open_timestamp_prices_zero_volume_and_missing_oi():
    rows = payload()["data"]["candles"]
    rows.append(["2026-01-05T09:25:00+05:30", "101.25", 103, 100, "102.50", 7])
    rows.append(rows[0].copy())
    normalized, invalid, duplicates = normalize(payload(list(reversed(rows))), request())
    assert normalized[0] == ["2026-01-05T09:15:00+05:30", "100", "104", "98", "102", "5", "10"]
    assert normalized[1][5] == "0"
    assert normalized[2][1] == "101.25" and normalized[2][4] == "102.50"
    assert normalized[2][6] == ""
    assert not invalid and duplicates == 1


@pytest.mark.parametrize(
    "change",
    [
        {"expiry": "2026-01-07"},
        {"lot_size": 2.5},
        {"tick_size": -5},
        {"name": {"unexpected": "nested"}},
    ],
)
def test_invalid_contract_metadata_is_rejected_before_requests(tmp_path, change):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "requests": [
                    {
                        "contract": {**contract(), **change},
                        "from_date": "2026-01-05",
                        "to_date": "2026-01-06",
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError):
        load_plan(plan)


def test_missing_open_interest_is_not_collapsed_with_reported_zero():
    first = ["2026-01-05T09:15:00+05:30", 100, 104, 98, 102, 5]
    with pytest.raises(DownloadError, match="Conflicting duplicate"):
        normalize(payload([first, [*first, 0]]), request())


def test_conflicting_duplicate_is_failure_and_raw_response_is_retained(tmp_path):
    rows = payload()["data"]["candles"]
    rows.append([rows[0][0], 100, 104, 98, 103, 5, 10])
    http = client(lambda _: httpx.Response(200, json=payload(rows)))
    report = acquire([request()], tmp_path, http)
    saved = report["requests"][0]
    assert saved["status"] == "FAILED" and "Conflicting duplicate" in saved["error"]
    from pathlib import Path

    assert json.loads((Path(saved["path"]) / "raw.json").read_text()) == payload(rows)
    assert not (tmp_path / "requests" / request().identity).exists()


def test_invalid_rows_are_reported_without_filling_or_discarding_raw_data(tmp_path):
    rows = payload()["data"]["candles"]
    rows.extend(
        [
            ["2026-01-05T09:22:00+05:30", 1, 2, 1, 2, 3, 4],
            ["2026-01-05T09:25:00+05:30", 0, 2, 0, 2, 3, 4],
            ["2026-01-07T09:15:00+05:30", 1, 2, 1, 2, 3, 4],
        ]
    )
    http = client(lambda _: httpx.Response(200, json=payload(rows)))
    report = acquire([request()], tmp_path, http)
    saved = report["requests"][0]
    assert saved["status"] == "PARTIAL_INVALID" and saved["row_count"] == 2
    assert {r["reason"] for r in saved["invalid_bars"]} == {
        "off_grid_timestamp",
        "invalid_price_or_activity",
        "outside_requested_range",
    }
    folder = tmp_path / "requests" / request().identity
    assert json.loads((folder / "raw.json").read_text()) == payload(rows)
    with (folder / "candles.csv").open() as source:
        assert len(list(csv.DictReader(source))) == 2


def test_resume_does_not_call_api_and_rejects_changed_saved_artifacts(tmp_path):
    calls = []

    def handler(incoming):
        calls.append(incoming)
        assert incoming.method == "GET" and incoming.url.host == "api.upstox.com"
        assert "%7C06-01-2026/5minute/2026-01-06/2026-01-05" in str(incoming.url)
        return httpx.Response(200, json=payload())

    http = client(handler)
    first = acquire([request()], tmp_path, http)
    second = acquire([request()], tmp_path, client(handler))
    assert len(calls) == 1
    assert not first["requests"][0]["resumed"] and second["requests"][0]["resumed"]
    assert second["api_calls_this_run"] == 0
    assert "private-test-credential" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    (tmp_path / "requests" / request().identity / "candles.csv").write_text("tampered")
    with pytest.raises(DownloadError, match="checksum"):
        acquire([request()], tmp_path, client(handler))


def test_empty_response_is_an_explicit_resumable_gap(tmp_path):
    http = client(lambda _: httpx.Response(200, json=payload([])))
    report = acquire([request()], tmp_path, http)
    assert report["requests"][0]["status"] == "EMPTY"
    assert report["requests"][0]["coverage_start"] is None
    assert report["requests"][0]["row_count"] == 0
    resumed = acquire([request()], tmp_path, client(lambda _: pytest.fail("Should resume")))
    assert resumed["requests"][0]["resumed"]


@pytest.mark.parametrize(("retry_after", "minimum"), [("75", 75), ("2", 60), (None, 60)])
def test_rate_limiting_and_429_retry_after_apply_to_all_requests(retry_after, minimum):
    clock = Clock()
    times = []

    def handler(_):
        times.append(clock.now)
        if len(times) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": retry_after} if retry_after else {},
                json={"status": "error"},
            )
        return httpx.Response(200, json=payload())

    http = client(handler, retries=1, clock=clock)
    http.get(request())
    http.get(request())
    assert times[1] - times[0] >= minimum
    assert times[2] - times[1] == pytest.approx(1.1)


def test_auth_failure_is_sanitized_and_stops_remaining_requests(tmp_path):
    http = client(
        lambda _: httpx.Response(
            401,
            json={
                "status": "error",
                "message": "private-test-credential",
                "errors": [{"errorCode": "UDAPI100050"}],
            },
        )
    )
    report = acquire([request(), request()], tmp_path, http)
    assert http.request_count == 1 and report["processed"] == 1
    assert report["requests"][0]["fatal"]
    assert "private-test-credential" not in (tmp_path / "report.json").read_text()


def test_redirect_never_transmits_credentials_to_another_host():
    calls = []

    def handler(incoming):
        calls.append(incoming.url.host)
        return httpx.Response(302, headers={"Location": "https://other.example/"}, json={})

    http = client(handler)
    with pytest.raises(DownloadError, match="302"):
        http.get(request())
    assert calls == ["api.upstox.com"]


def test_dry_run_uses_neither_credentials_lock_nor_network(tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "requests": [
                    {"contract": contract(), "from_date": "2026-01-05", "to_date": "2026-01-06"}
                ]
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download",
            "--plan",
            str(plan),
            "--output",
            str(tmp_path / "output"),
            "--token-file",
            str(tmp_path / "missing-token"),
            "--lock-file",
            str(tmp_path / "missing-lock"),
            "--dry-run",
        ],
    )
    assert main() == 0
    assert not (tmp_path / "output").exists()
    assert json.loads(capsys.readouterr().out)["network_used"] is False


def test_shared_lock_blocks_before_reading_credentials(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"requests": []}))
    lock = tmp_path / "lock"
    lock.touch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download",
            "--plan",
            str(plan),
            "--output",
            str(tmp_path / "output"),
            "--token-file",
            str(tmp_path / "missing-token"),
            "--lock-file",
            str(lock),
        ],
    )
    with lock.open("r") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DownloadError, match="shared lock"):
            main()
