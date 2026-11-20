"""Register a verified native bundle with explicit source-availability metadata."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.datasets import Dataset, DatasetVersion
from app.domain.model2.execution import market_at
from app.infrastructure.datasets.model2_bundle import decode_bar, price_rows, read_settlements
from app.infrastructure.market_data.model2_feed import Model2BundleFeed

PROFILE = "model2_derivatives_bundle_v3"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def register_bundle(session: Session, root: Path, storage_root: Path, name: str) -> DatasetVersion:
    root, storage_root = root.resolve(), storage_root.resolve()
    if root == storage_root or storage_root not in root.parents:
        raise ValueError("A Model 2 bundle must be inside the configured storage root.")
    feed = Model2BundleFeed(root)
    official = feed.manifest.component("settlements")
    times = (
        {item.expiry: item.available_at for item in read_settlements(root, official)}
        if official
        else {}
    )
    checks = [
        (expiry.isoformat(), times.get(expiry, market_at(expiry, 15, 30)).isoformat())
        for expiry in feed.calendar.weekly_expiries
    ]
    identity = digest(
        {
            "bundle_checksum": feed.manifest.bundle_checksum,
            "settlement_checks": checks,
            "profile": PROFILE,
        }
    )
    session.execute(select(func.pg_advisory_xact_lock(int(identity[:15], 16))))
    existing = session.scalar(
        select(DatasetVersion).where(
            DatasetVersion.checksum == identity, DatasetVersion.schema_profile == PROFILE
        )
    )
    if existing:
        return existing
    if not name.strip():
        raise ValueError("A dataset name is required.")
    session.execute(select(func.pg_advisory_xact_lock(int(digest(name)[:15], 16))))
    dataset = session.scalar(select(Dataset).where(Dataset.name == name))
    if dataset is None:
        dataset = Dataset(
            name=name,
            description="Immutable Model 2 historical bundle; estimated opening-price fills.",
            source="Preserved declared source components",
        )
        session.add(dataset)
        session.flush()
    next_version = (
        int(
            session.scalar(
                select(func.max(DatasetVersion.version_number)).where(
                    DatasetVersion.dataset_id == dataset.id
                )
            )
            or 0
        )
        + 1
    )
    spot = feed.manifest.component("spot")
    assert spot is not None
    first = last = None
    sessions = set()
    row_count = 0
    for raw in price_rows(root / spot.path):
        bar = decode_bar(raw, spot)
        first = bar.interval_start if first is None else min(first, bar.interval_start)
        last = bar.interval_end if last is None else max(last, bar.interval_end)
        sessions.add(bar.interval_start.date())
        row_count += 1
    if first is None or last is None:
        raise ValueError("A Model 2 dataset needs preserved spot observations.")
    normal = sorted(sessions)
    version = DatasetVersion(
        dataset_id=dataset.id,
        version_number=next_version,
        schema_profile=PROFILE,
        original_filename="model2_bundle_manifest.json",
        checksum=identity,
        storage_key=root.relative_to(storage_root).as_posix(),
        status="VALID",
        row_count=row_count,
        session_count=len(normal),
        session_dates=[day.isoformat() for day in normal],
        timezone="Asia/Kolkata",
        resolution_minutes=5,
        coverage_start=first,
        coverage_end=last,
        instruments=[
            {"key": "NIFTY", "kind": "INDEX"},
            {"key": "INDIA_VIX", "kind": "INDEX"},
            {"key": "NIFTY_OPTIONS", "kind": "OPTION"},
        ],
        capabilities={
            "model2_bundle_v3": True,
            "spot_ohlc": True,
            "india_vix": True,
            "option_ohlc": True,
            "option_contract_metadata": True,
        },
        profile={
            "bundle_checksum": feed.manifest.bundle_checksum,
            "bundle_schema_version": "stockbot.derivatives-bundle.v3",
            "calendar_sha256": feed.calendar.provenance.checksum,
            "calendar_version": feed.calendar.version,
            "model2_settlement_checks": checks,
            "settlement_checkpoint_policy": "SOURCED_AVAILABILITY_ELSE_EXPIRY_CLOSE_WITH_NO_OFFICIAL_RECORD",
            "pricing_quality": "estimated_historical_ohlc_open",
            "executable_quotes": False,
        },
        warnings=[
            {
                "code": "ESTIMATED_OPENING_FILLS",
                "message": "Historical candle opens with adverse slippage; not executable bid/ask quotes. Margin is assumed.",
                "details": {},
            }
        ],
        errors=[],
        validation_progress=100,
        completed_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version
