from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import backtest_execution, backtests
from app.application.backtests import BacktestService
from app.db.models.backtests import EquityPoint, SignalEvaluation, Trade
from app.domain.algorithms import BULL_CALL_SPREAD, FUTURES_BULL_CALL_SPREAD
from app.domain.backtests import ENGINE_VERSION
from app.infrastructure.datasets.derivatives_bundle import (
    load_derivatives_bundle,
)
from tests.market_fixture import market_bundle
from tests.test_backtests import algorithm, dataset


@pytest.mark.parametrize(
    "engine_version,definition",
    [
        ("1.2.2-session-pipeline", BULL_CALL_SPREAD),
        (ENGINE_VERSION, BULL_CALL_SPREAD),
        (ENGINE_VERSION, FUTURES_BULL_CALL_SPREAD),
    ],
)
def test_estimated_run_keeps_signal_ids_through_completion_and_persists_safe_results(
    monkeypatch, tmp_path, engine_version, definition
):
    bundle = market_bundle(tmp_path, rising_futures=definition is FUTURES_BULL_CALL_SPREAD)
    manifest = load_derivatives_bundle(bundle)
    stored, configured = dataset(manifest["capabilities"]), algorithm()
    stored.profile = {"spot_checksum": manifest["components"]["spot"]["checksum"]}
    run = SimpleNamespace(
        id=uuid4(),
        dataset_version_id=stored.id,
        algorithm_version_id=configured.id,
        start_date=date(2025, 7, 21),
        end_date=date(2025, 7, 21),
        starting_capital="500000",
        mode="OPTION_SPREAD_SIMULATION",
        status="PENDING",
        cancellation_requested=False,
        progress=0,
        active_attempt_id=None,
        snapshot={
            "dataset": {
                "id": str(stored.id),
                "checksum": stored.checksum,
                "storage_key": stored.storage_key,
                "derivatives_storage_key": "bundle",
                "derivatives_checksum": manifest["bundle_checksum"],
            },
            "algorithm": {
                "key": configured.algorithm_key,
                "version_id": str(configured.id),
                "code_version": definition.code_version,
                "parameters": configured.parameters,
            },
            "range": {"start_date": "2025-07-21", "end_date": "2025-07-21"},
            "engine_version": engine_version,
            "coverage": {"included_sessions": ["2025-07-21"], "excluded_sessions": []},
        },
    )

    class Session:
        def __init__(self):
            self.added = []
            self.deletes = []
            self.merged = []
            self.first_signal_ids = None

        def add_all(self, values):
            for item in values:
                if item.id is None:
                    item.id = uuid4()
                self.added.append(item)

        def query(self, model):
            return SimpleNamespace(
                filter=lambda *_args: SimpleNamespace(delete=lambda: self.deletes.append(model))
            )

        def scalar(self, statement):
            params = statement.compile().params
            sequence = params.get("sequence_1")
            row = next(
                (
                    item
                    for item in self.added
                    if isinstance(item, SignalEvaluation) and item.sequence == sequence
                ),
                None,
            )
            return row.id if statement.column_descriptions[0]["name"] == "id" and row else row

        def flush(self):
            pass

        def scalars(self, statement):
            model = statement.column_descriptions[0]["entity"]
            return [item for item in self.added if isinstance(item, model)]

        def commit(self):
            signal_ids = [item.id for item in self.added if isinstance(item, SignalEvaluation)]
            if signal_ids and self.first_signal_ids is None:
                self.first_signal_ids = signal_ids

        def rollback(self):
            raise AssertionError("successful run must not roll back")

        def merge(self, item):
            self.merged.append(item)

    session = Session()

    class Runs:
        def __init__(self):
            self.journal = []
            self.days = []
            self.events = []

        def claim_lease(self, _id, owner):
            run.lease_owner = owner
            return run

        def start_attempt(self, _id, owner):
            run.active_attempt_id = uuid4()
            return SimpleNamespace(id=run.active_attempt_id, attempt_number=1)

        def attempt_is_active(self, _id, attempt, owner):
            return attempt == run.active_attempt_id and owner == run.lease_owner

        def renew_lease(self, _id, owner):
            return run if run.lease_owner == owner else None

        def append_event(self, _id, event_type, payload):
            self.events.append((event_type, payload))
            return None

        def get_run(self, _id, **_kwargs):
            return run

        def append_journal(self, _id, _attempt, **values):
            item = SimpleNamespace(sequence=len(self.journal) + 1, **values)
            self.journal.append(item)
            return item

        def replace_day_classifications(self, _id, _attempt, values):
            self.days = values

        def finish_attempt(self, _attempt, status, *_args):
            self.attempt_status = status

    repository = Runs()
    monkeypatch.setattr(
        backtests,
        "DatasetRepository",
        lambda _session: SimpleNamespace(get_version=lambda _id: stored),
    )
    monkeypatch.setattr(backtests, "AlgorithmRepository", lambda _session: SimpleNamespace())
    monkeypatch.setattr(backtests, "BacktestRepository", lambda _session: repository)
    monkeypatch.setattr(
        backtest_execution,
        "persist_completed_run_analytics",
        lambda *_args: SimpleNamespace(schema_version="tested-separately"),
    )
    storage = SimpleNamespace(
        resolve=lambda key: bundle if key == "bundle" else bundle / "spot/nifty_spot_5min.csv"
    )
    result = BacktestService(session, storage, object()).execute(run.id)
    assert result["status"] == run.status == "COMPLETED"
    assert repository.attempt_status == "COMPLETED"
    signals = [item for item in session.added if isinstance(item, SignalEvaluation)]
    assert len(signals) == 75
    assert session.first_signal_ids == [item.id for item in signals]
    assert session.deletes.count(SignalEvaluation) == 1
    trades = [item for item in session.added if isinstance(item, Trade)]
    equity = [item for item in session.added if isinstance(item, EquityPoint)]
    assert trades and equity and repository.journal and repository.days
    if definition is FUTURES_BULL_CALL_SPREAD:
        checks = [item for item in repository.journal if item.event_type == "POSITION_CHECK"]
        assert checks
        assert all(item.payload["vwap_comparison_source"] == "futures_close" for item in checks)
    assert (
        next(payload for name, payload in repository.events if name == "BACKTEST_STARTED")[
            "engine_version"
        ]
        == engine_version
    )
    first_option_event = next(
        index
        for index, item in enumerate(repository.journal)
        if item.event_type != "SIGNAL_EVALUATED"
    )
    if engine_version == ENGINE_VERSION:
        assert 0 < first_option_event < len(signals)
        assert [item.timestamp for item in repository.journal] == sorted(
            item.timestamp for item in repository.journal
        )
    else:
        assert first_option_event == len(signals)
    assert sum(item.event_type == "SIGNAL_EVALUATED" for item in repository.journal) == len(signals)
    if engine_version == ENGINE_VERSION:
        accepted = sum(item.decision == "ACCEPTED" for item in signals)
        assert accepted > len(trades)
        assert session.merged[-1].metrics["accepted_signals"] == accepted
        assert session.merged[-1].metrics["opened_spreads"] == len(trades)
        assert session.merged[-1].metrics["rejected_evaluations"] == len(signals) - accepted
        assert session.merged[-1].metrics["execution_rejections"] == sum(
            item.event_type == "ENTRY_REJECTED" for item in repository.journal
        )
    assert all(item.entry_signal_evaluation_id in session.first_signal_ids for item in trades)
    assert all(
        not item.synthetic and item.unit == "INR" and item.financial_pnl_available
        for item in trades + equity
    )
    assert all(not leg.synthetic and leg.unit == "INR" for item in trades for leg in item.legs)
    assert session.merged[-1].metrics["executable_quotes"] is False
    assert run.lease_owner is None and run.lease_expires_at is None
