"""Acquire declared expired-option candles; no orders, inferred contracts or backups."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

IST = ZoneInfo("Asia/Kolkata")
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
CONTRACT_FIELDS = {
    "instrument_key",
    "expired_instrument_key",
    "expiry",
    "instrument_type",
    "option_type",
    "strike_price",
    "strike",
    "lot_size",
    "tick_size",
    "trading_symbol",
    "underlying_key",
    "underlying_symbol",
    "underlying_type",
    "exchange",
    "segment",
    "weekly",
    "name",
    "exchange_token",
    "minimum_lot",
    "freeze_quantity",
}
COMPLETED = {"COMPLETE", "EMPTY", "PARTIAL_INVALID"}


def encoded(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class CandleRequest:
    contract: dict[str, Any]
    from_date: date
    to_date: date

    @property
    def instrument_key(self) -> str:
        return str(self.contract.get("instrument_key") or self.contract["expired_instrument_key"])

    @property
    def declaration(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "from_date": str(self.from_date),
            "to_date": str(self.to_date),
            "interval": "5minute",
            "timestamp_semantics": "interval_start",
            "normalizer": "1",
        }

    @property
    def identity(self) -> str:
        return checksum(encoded(self.declaration))

    @property
    def route(self) -> str:
        return (
            "https://api.upstox.com/v2/expired-instruments/historical-candle/"
            f"{quote(self.instrument_key, safe='')}/5minute/{self.to_date}/{self.from_date}"
        )


def load_plan(path: Path) -> list[CandleRequest]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get("requests"), list):
        raise ValueError("Plan must contain a requests list.")
    requests: list[CandleRequest] = []
    for item in value["requests"]:
        if not isinstance(item, dict) or not isinstance(item.get("contract"), dict):
            raise ValueError("Every request needs provider contract metadata.")
        contract = {key: value for key, value in item["contract"].items() if key in CONTRACT_FIELDS}
        if any(
            not isinstance(value, (str, int, float, bool, type(None)))
            for value in contract.values()
        ):
            raise ValueError("Provider contract fields must be scalar values.")
        key = contract.get("instrument_key") or contract.get("expired_instrument_key")
        kind = contract.get("instrument_type") or contract.get("option_type")
        if not isinstance(key, str) or not key.startswith("NSE_FO|") or kind not in {"CE", "PE"}:
            raise ValueError("Use an actual NSE expired CE/PE contract key and type.")
        expiry = date.fromisoformat(str(contract["expiry"])[:10])
        if not key.endswith("|" + expiry.strftime("%d-%m-%Y")):
            raise ValueError("Expired contract key suffix must match its declared expiry.")
        start = date.fromisoformat(item["from_date"])
        end = min(date.fromisoformat(item["to_date"]), expiry)
        if start > end:
            raise ValueError(
                "The request range must overlap the contract's lifetime before expiry."
            )
        for field in ["lot_size", "tick_size", "strike_price"]:
            try:
                number = Decimal(str(contract[field]))
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError(f"Provider {field} must be numeric.") from None
            if not number.is_finite() or number <= 0:
                raise ValueError(f"Provider {field} must be positive.")
            if field == "lot_size" and number != number.to_integral_value():
                raise ValueError("Provider lot_size must be an integer.")
        while start <= end:
            following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            last = min(end, following - timedelta(days=1))
            requests.append(CandleRequest(contract, start, last))
            start = following
    return list({request.identity: request for request in requests}.values())


class DownloadError(RuntimeError):
    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class DownloadClient:
    def __init__(
        self,
        token: str,
        *,
        interval: float = 1.1,
        retries: int = 4,
        transport: httpx.BaseTransport | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        if not math.isfinite(interval) or interval < 1.1:
            raise ValueError("Request spacing must be at least 1.1 seconds.")
        if not 0 <= retries <= 10:
            raise ValueError("Retries must be between zero and ten.")
        self.http = httpx.Client(
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            timeout=httpx.Timeout(45, connect=10),
            follow_redirects=False,
            transport=transport,
        )
        self.interval, self.retries, self.clock, self.sleep = interval, retries, clock, sleep
        self.last_request: float | None = None
        self.request_count = 0

    def close(self) -> None:
        self.http.close()

    def get(self, request: CandleRequest) -> dict[str, Any]:
        for attempt in range(self.retries + 1):
            if self.last_request is not None:
                self.sleep(max(0, self.interval - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            self.request_count += 1
            try:
                response = self.http.get(request.route)
            except httpx.RequestError:
                if attempt < self.retries:
                    self.sleep(2**attempt)
                    continue
                raise DownloadError(
                    "Network request failed; rerun to resume.", fatal=True
                ) from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.retries:
                    pause = max(60 if response.status_code == 429 else 1, 2**attempt)
                    raw = response.headers.get("Retry-After")
                    if raw:
                        try:
                            requested = float(raw)
                        except ValueError:
                            try:
                                requested = (
                                    parsedate_to_datetime(raw) - datetime.now(UTC)
                                ).total_seconds()
                            except (TypeError, ValueError, OverflowError):
                                requested = 0
                        if math.isfinite(requested):
                            pause = max(pause, requested)
                    self.sleep(pause)
                    continue
            try:
                payload = response.json()
            except ValueError:
                raise DownloadError(
                    f"Provider returned non-JSON data (HTTP {response.status_code})."
                ) from None
            if not isinstance(payload, dict):
                raise DownloadError("Provider returned an unexpected payload.")
            if response.status_code != 200 or payload.get("status") != "success":
                errors = payload.get("errors")
                codes = [
                    str(item.get("errorCode", ""))
                    for item in (errors if isinstance(errors, list) else [])
                    if isinstance(item, dict)
                ]
                codes = [code for code in codes if re.fullmatch(r"[A-Z0-9_]{1,40}", code)]
                fatal = response.status_code in {401, 403, 429} or "UDAPI1149" in codes
                raise DownloadError(
                    f"Upstox HTTP {response.status_code} ({','.join(codes) or 'unspecified'}).",
                    fatal=fatal,
                )
            return payload
        raise DownloadError("Request attempts exhausted.", fatal=True)


def normalize(
    payload: dict[str, Any], request: CandleRequest
) -> tuple[list[list[str]], list[dict[str, Any]], int]:
    data = payload.get("data")
    candles = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(candles, list):
        raise DownloadError("Provider success payload has no candle list.")
    rows: dict[datetime, list[str]] = {}
    invalid: list[dict[str, Any]] = []
    duplicates = 0
    for index, candle in enumerate(candles):
        try:
            if not isinstance(candle, list) or len(candle) < 6:
                raise ValueError("malformed_row")
            stamp = datetime.fromisoformat(str(candle[0]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timezone_missing")
            stamp = stamp.astimezone(IST)
            if stamp.minute % 5 or stamp.second or stamp.microsecond:
                raise ValueError("off_grid_timestamp")
            if not request.from_date <= stamp.date() <= request.to_date:
                raise ValueError("outside_requested_range")
            numbers = [Decimal(str(value)) for value in candle[1:7]]
            if len(numbers) == 5:
                numbers.append(Decimal(0))
                interest = ""
            else:
                interest = str(candle[6])
            if any(not number.is_finite() for number in numbers):
                raise ValueError("nonfinite_number")
            opening, high, low, close, volume, oi = numbers
            if min(opening, high, low, close) <= 0 or min(volume, oi) < 0:
                raise ValueError("invalid_price_or_activity")
            if not low <= min(opening, close) <= max(opening, close) <= high:
                raise ValueError("invalid_ohlc")
            row = [stamp.isoformat(), *(str(value) for value in candle[1:6]), interest]
            if stamp in rows:
                previous = rows[stamp]
                same = all(
                    (a == b if not a or not b else Decimal(a) == Decimal(b))
                    for a, b in zip(previous[1:], row[1:], strict=True)
                )
                if not same:
                    raise DownloadError(
                        "Conflicting duplicate candle timestamps; raw response retained."
                    )
                duplicates += 1
            else:
                rows[stamp] = row
        except (ValueError, TypeError, InvalidOperation, OverflowError) as error:
            # Raw values remain in raw.json; reports expose only stable reason codes.
            reason = str(error) if re.fullmatch(r"[a-z_]+", str(error)) else "invalid_row"
            invalid.append({"row_index": index, "reason": reason})
    return [rows[stamp] for stamp in sorted(rows)], invalid, duplicates


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(encoded(value))
    temporary.replace(path)


def acquire(requests: list[CandleRequest], output: Path, client: DownloadClient) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    (output / "requests").mkdir(exist_ok=True)
    (output / "failed").mkdir(exist_ok=True)
    results = []
    for request in requests:
        final = output / "requests" / request.identity
        report_path = final / "report.json"
        if report_path.exists():
            saved = json.loads(report_path.read_text())
            if saved.get("request") != request.declaration or saved.get("status") not in COMPLETED:
                raise DownloadError("Existing request identity/report mismatch.")
            for name, digest in saved["checksums"].items():
                if checksum((final / name).read_bytes()) != digest:
                    raise DownloadError("A completed request artifact checksum changed.")
            results.append({**saved, "resumed": True, "path": str(final)})
            continue
        folder = output / (".incomplete-" + request.identity + "-" + uuid4().hex)
        folder.mkdir()
        payload = None
        try:
            payload = client.get(request)
            raw = encoded(payload)
            (folder / "raw.json").write_bytes(raw)
            (folder / "contract.json").write_bytes(encoded(request.contract))
            rows, invalid, duplicates = normalize(payload, request)
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerows(rows)
            (folder / "candles.csv").write_text(stream.getvalue(), newline="")
            status = "PARTIAL_INVALID" if invalid else "COMPLETE" if rows else "EMPTY"
            report = {
                "request_id": request.identity,
                "request": request.declaration,
                "status": status,
                "row_count": len(rows),
                "invalid_bars": invalid,
                "identical_duplicates": duplicates,
                "raw_candle_count": len(payload["data"]["candles"]),
                "coverage_start": rows[0][0] if rows else None,
                "coverage_end": rows[-1][0] if rows else None,
                "checksums": {
                    name: checksum((folder / name).read_bytes())
                    for name in ["raw.json", "candles.csv", "contract.json"]
                },
            }
            (folder / "report.json").write_bytes(encoded(report))
            folder.rename(final)
            results.append({**report, "resumed": False, "path": str(final)})
        except DownloadError as error:
            report = {
                "request_id": request.identity,
                "request": request.declaration,
                "status": "FAILED",
                "error": str(error),
                "fatal": error.fatal,
            }
            (folder / "report.json").write_bytes(encoded(report))
            failed = output / "failed" / (request.identity + "-" + uuid4().hex)
            folder.rename(failed)
            results.append({**report, "resumed": False, "path": str(failed)})
            if error.fatal:
                break
        summary = {
            "requested": len(requests),
            "processed": len(results),
            "requests": results,
            "api_calls_this_run": client.request_count,
        }
        atomic_json(output / "report.json", summary)
    summary = {
        "requested": len(requests),
        "processed": len(results),
        "requests": results,
        "api_calls_this_run": client.request_count,
    }
    atomic_json(output / "report.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="JSON with a requests list.")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("/Users/deepakprakash/Documents/stock-analyzer/token.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path(
            "/Users/deepakprakash/Documents/stock-analyzer/exports/.market_history_download.lock"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--request-interval-seconds", type=float, default=1.1)
    parser.add_argument("--max-retries", type=int, default=4)
    args = parser.parse_args()
    requests = load_plan(args.plan)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "requests": len(requests),
                    "contract_count": len({r.instrument_key for r in requests}),
                    "network_used": False,
                }
            )
        )
        return 0
    # The existing project lock is opened read-only: no source-project files are changed.
    with args.lock_file.open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DownloadError("Another market-data downloader holds the shared lock.") from None
        token = json.loads(args.token_file.read_text()).get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise DownloadError("No saved access token; refresh the existing Upstox sign-in.")
        client = DownloadClient(
            token.strip(), interval=args.request_interval_seconds, retries=args.max_retries
        )
        try:
            report = acquire(requests, args.output, client)
        finally:
            client.close()
    statuses = [request["status"] for request in report["requests"]]
    print(
        json.dumps(
            {
                "requested": len(requests),
                "processed": report["processed"],
                "api_calls_this_run": report["api_calls_this_run"],
                "complete": statuses.count("COMPLETE"),
                "empty": statuses.count("EMPTY"),
                "invalid": statuses.count("PARTIAL_INVALID"),
                "failed": statuses.count("FAILED"),
            }
        )
    )
    return (
        0
        if len(statuses) == len(requests) and all(status == "COMPLETE" for status in statuses)
        else 2
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DownloadError, ValueError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
