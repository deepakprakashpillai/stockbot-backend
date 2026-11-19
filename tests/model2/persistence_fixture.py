from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.application.model2.execution import MODE
from app.application.model2.serialization import request_json
from app.db.models.algorithms import Algorithm, AlgorithmVersion
from app.db.models.backtests import BacktestRun
from app.db.models.datasets import Dataset, DatasetVersion
from app.db.models.model2 import ResearchScenarioGroup, ResearchScenarioMember
from app.domain.model2.approved_specification import approved_specification_digest
from app.domain.model2.execution import (
    ALGORITHM_CODE_VERSION,
    ALGORITHM_KEY,
    ENGINE_VERSION,
    POLICIES,
    ReplayRequest,
    market_at,
)
from app.domain.model2.market import BAR_INTERVAL
from app.domain.model2.parameters import Model2Parameters
from app.infrastructure.datasets.storage import LocalDatasetStorage
from app.infrastructure.market_data.model2_feed import Model2BundleFeed
from tests.conftest import make_test_settings


def prepared_case(
    factory,
    bundle_factory,
    *,
    pending_target=False,
    slip="0",
    missing_exit=False,
    source_reference=None,
):
    sample = bundle_factory(build=False)
    strikes = {"call_short": 25250, "call_wing": 25450, "put_short": 24750, "put_wing": 24550}
    for contract in sample.contracts:
        if contract["contract_id"] in strikes:
            contract["strike"] = strikes[contract["contract_id"]]
    sample.write_json("contracts", sample.contracts)
    for component in sample.components[:]:
        if (
            component.kind not in {"spot", "india_vix", "options"}
            or component.kind == "options"
            and component.contract_id not in strikes
        ):
            continue
        rows = []
        for session in sample.calendar["sessions"]:
            day = date.fromisoformat(session["session_date"])
            if component.kind == "options" and day > date(2026, 1, 6):
                continue
            for index in range(75):
                at = market_at(day, 9, 15) + index * BAR_INTERVAL
                value = (
                    "25000"
                    if component.kind == "spot"
                    else "16"
                    if component.kind == "india_vix"
                    else "30"
                    if component.contract_id.endswith("short")
                    else "10"
                )
                closing = value
                if (
                    pending_target
                    and component.kind == "options"
                    and at == market_at(date(2026, 1, 5), 15, 25)
                ):
                    closing = "15" if component.contract_id.endswith("short") else "5"
                if (
                    missing_exit
                    and component.kind == "options"
                    and (
                        day == date(2026, 1, 6)
                        or day == date(2026, 1, 5)
                        and at.time().isoformat() > "15:20:00"
                    )
                ):
                    continue
                rows.append(
                    {
                        "timestamp": (at + BAR_INTERVAL).isoformat(),
                        "open": value,
                        "high": str(max(Decimal(value), Decimal(closing))),
                        "low": str(min(Decimal(value), Decimal(closing))),
                        "close": closing,
                        "volume": 100,
                    }
                )
        sample.write_rows(component, rows)
    if source_reference:
        sample.components = [
            component.model_copy(update={"source_reference": source_reference})
            for component in sample.components
        ]
    manifest = sample.build()
    feed = Model2BundleFeed(sample.root)
    storage = LocalDatasetStorage(sample.root.parent)
    settings = make_test_settings().model_copy(update={"storage_root": sample.root.parent})
    request = ReplayRequest(
        manifest.bundle_checksum,
        date(2026, 1, 5),
        date(2026, 1, 6),
        Decimal("500000"),
        Model2Parameters(slippage_percent=Decimal(slip)),
        ((date(2026, 1, 6), market_at(date(2026, 1, 6), 15, 35)),),
    )
    with factory() as session:
        algorithm = Algorithm(
            key=ALGORITHM_KEY,
            display_name="Model 2",
            description="Research fixture",
            code_version=ALGORITHM_CODE_VERSION,
            schema_version=1,
            schema_hash="a" * 64,
            active=True,
            supported_modes=[MODE],
            required_capabilities=[],
        )
        session.add(algorithm)
        session.flush()
        version = AlgorithmVersion(
            id=uuid4(),
            algorithm_key=ALGORITHM_KEY,
            version_number=1,
            name="Research fixture",
            code_version=ALGORITHM_CODE_VERSION,
            schema_version=1,
            parameters=request.parameters.model_dump(mode="json"),
            creator_context={"type": "fixture"},
        )
        dataset = Dataset(id=uuid4(), name="Complete synthetic Model 2 fixture")
        session.add_all([version, dataset])
        session.flush()
        stored = DatasetVersion(
            id=uuid4(),
            dataset_id=dataset.id,
            version_number=1,
            schema_profile="stockbot.derivatives-bundle.v3",
            original_filename="manifest.model2.json",
            checksum=manifest.bundle_checksum,
            storage_key=sample.root.name,
            status="VALID",
            session_dates=["2026-01-05", "2026-01-06"],
            capabilities={
                "spot_ohlc": True,
                "india_vix": True,
                "option_ohlc": True,
                "option_contract_metadata": True,
                "model2_bundle_v3": True,
            },
            profile={},
            warnings=[],
            errors=[],
        )
        session.add(stored)
        session.flush()
        group = ResearchScenarioGroup(
            id=uuid4(),
            idempotency_key=str(uuid4()),
            request_digest="b" * 64,
            snapshot=request_json(request),
            status="PENDING",
        )
        session.add(group)
        session.flush()
        run_ids = []
        for ordinal, policy in enumerate(POLICIES, 1):
            run = BacktestRun(
                id=uuid4(),
                dataset_version_id=stored.id,
                algorithm_version_id=version.id,
                start_date=request.start,
                end_date=request.end,
                mode=MODE,
                starting_capital=request.starting_capital,
                snapshot={
                    "engine_version": ENGINE_VERSION,
                    "policy": policy,
                    "scenario_group_id": str(group.id),
                    "specification_sha256": approved_specification_digest(),
                    "model2_request": request_json(request),
                    "calendar_sha256": feed.calendar.provenance.checksum,
                    "bundle_storage_key": sample.root.name,
                },
                status="PENDING",
            )
            session.add(run)
            session.flush()
            session.add(
                ResearchScenarioMember(
                    run_id=run.id, group_id=group.id, policy=policy, ordinal=ordinal
                )
            )
            run_ids.append(run.id)
        session.commit()
        group_id = group.id
    return storage, settings, group_id, run_ids
