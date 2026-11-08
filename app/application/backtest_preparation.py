"""Verify immutable run inputs and construct the historical market feed."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, cast

from app.db.models.backtests import BacktestRun
from app.db.models.datasets import DatasetVersion
from app.domain.algorithms import AlgorithmDefinition, get_algorithm
from app.domain.backtests import ENGINE_VERSION, MarketCandle
from app.infrastructure.datasets.derivatives_bundle import (
    load_derivatives_bundle,
    verify_derivatives_bundle_files,
)
from app.infrastructure.datasets.storage import DatasetStorage
from app.infrastructure.market_data.csv_reader import CsvMarketDataProvider
from app.infrastructure.market_data.parquet_derivatives import ParquetDerivativesProvider


@dataclass(frozen=True)
class PreparedRunInputs:
    dataset: DatasetVersion
    algorithm: AlgorithmDefinition
    parameters: dict[str, Any]
    snapshot_algorithm: dict[str, object]
    included_sessions: set[date] | None
    signal_candles: Iterable[MarketCandle]
    derivatives_provider: ParquetDerivativesProvider | None


def prepare_run_inputs(
    run: BacktestRun, dataset: DatasetVersion | None, storage: DatasetStorage
) -> PreparedRunInputs:

    raw_snapshot_dataset = run.snapshot.get("dataset")
    raw_snapshot_algorithm = run.snapshot.get("algorithm")
    raw_snapshot_range = run.snapshot.get("range")
    snapshot_dataset = (
        cast(dict[str, object], raw_snapshot_dataset)
        if isinstance(raw_snapshot_dataset, dict)
        else {}
    )
    snapshot_algorithm = (
        cast(dict[str, object], raw_snapshot_algorithm)
        if isinstance(raw_snapshot_algorithm, dict)
        else {}
    )
    snapshot_range = (
        cast(dict[str, object], raw_snapshot_range) if isinstance(raw_snapshot_range, dict) else {}
    )
    raw_snapshot_coverage = run.snapshot.get("coverage")
    snapshot_coverage = (
        cast(dict[str, object], raw_snapshot_coverage)
        if isinstance(raw_snapshot_coverage, dict)
        else {}
    )
    included_session_values = snapshot_coverage.get("included_sessions")
    included_sessions = (
        {date.fromisoformat(str(value)) for value in included_session_values}
        if isinstance(included_session_values, list) and included_session_values
        else None
    )
    snapshot_engine_version = run.snapshot.get("engine_version")
    snapshot_code_version = snapshot_algorithm.get("code_version")
    engine_compatible = (
        snapshot_engine_version in {"1.2.2-session-pipeline", ENGINE_VERSION}
        if snapshot_code_version == "1.2.0-bull-call-spread-v1"
        else snapshot_engine_version
        in {
            "0.1.0-signal-only",
            "1.1.0-bull-call-spread-v1",
            "1.2.2-session-pipeline",
            ENGINE_VERSION,
        }
    )
    if (
        dataset is None
        or not snapshot_dataset
        or not snapshot_algorithm
        or not snapshot_range
        or snapshot_dataset.get("id") != str(run.dataset_version_id)
        or snapshot_dataset.get("checksum") != dataset.checksum
        or snapshot_dataset.get("storage_key") != dataset.storage_key
        or snapshot_algorithm.get("version_id") != str(run.algorithm_version_id)
        or snapshot_range.get("start_date") != run.start_date.isoformat()
        or snapshot_range.get("end_date") != run.end_date.isoformat()
        or not engine_compatible
    ):
        raise ValueError("The immutable backtest snapshot is invalid or inconsistent.")
    algorithm_key = snapshot_algorithm.get("key")
    code_version = snapshot_algorithm.get("code_version")
    parameters = snapshot_algorithm.get("parameters")
    if not isinstance(algorithm_key, str) or not isinstance(parameters, dict):
        raise ValueError("The immutable algorithm snapshot is invalid.")
    algorithm = get_algorithm(
        algorithm_key,
        code_version=str(code_version),
        include_inactive=True,
    )
    if algorithm is None:
        raise ValueError("The snapshotted algorithm code version is unavailable.")
    source_path = storage.resolve(dataset.storage_key)
    with source_path.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    expected_source_checksum = (
        dataset.profile.get("spot_checksum")
        if run.mode == "OPTION_SPREAD_SIMULATION"
        else dataset.checksum
    )
    if not isinstance(expected_source_checksum, str) or checksum != expected_source_checksum:
        raise ValueError("The dataset file checksum no longer matches the run snapshot.")
    provider = CsvMarketDataProvider(source_path)
    derivatives_provider: ParquetDerivativesProvider | None = None
    signal_candles: Iterator[MarketCandle]
    if run.mode == "OPTION_SPREAD_SIMULATION":
        derivatives_storage_key = snapshot_dataset.get("derivatives_storage_key")
        if not isinstance(derivatives_storage_key, str) or not derivatives_storage_key:
            raise ValueError("The immutable dataset does not reference a derivatives bundle.")
        derivatives_dir = storage.resolve(derivatives_storage_key)
        manifest = load_derivatives_bundle(derivatives_dir)
        if manifest.get("bundle_checksum") != snapshot_dataset.get("derivatives_checksum"):
            raise ValueError("The derivatives bundle checksum no longer matches the run snapshot.")
        verify_derivatives_bundle_files(derivatives_dir, manifest)
        derivatives_provider = ParquetDerivativesProvider(derivatives_dir)
        if not derivatives_provider.ready:
            raise ValueError("The referenced derivatives bundle is incomplete.")
        warmup_start = run.start_date - timedelta(days=7)
        signal_candles = derivatives_provider.iter_enriched_spot_candles(
            provider.stream(warmup_start, run.end_date)
        )
    else:
        signal_candles = provider.stream(run.start_date, run.end_date)
    return PreparedRunInputs(
        dataset=dataset,
        algorithm=algorithm,
        parameters=parameters,
        snapshot_algorithm=snapshot_algorithm,
        included_sessions=included_sessions,
        signal_candles=signal_candles,
        derivatives_provider=derivatives_provider,
    )
