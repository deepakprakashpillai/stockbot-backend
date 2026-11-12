from __future__ import annotations

import json
from base64 import urlsafe_b64decode
from datetime import UTC, datetime, time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_db_session
from app.api.v1 import algorithms as routes
from app.application import algorithms as service_module
from app.application.algorithms import (
    AlgorithmService,
    parameter_schema,
    schema_hash,
    sync_registry,
)
from app.cli import sync_algorithm_registry as sync_command
from app.db.models.algorithms import Algorithm, AlgorithmVersion
from app.db.repositories.algorithms import AlgorithmRepository
from app.domain.algorithms import (
    BULL_CALL_SPREAD,
    FUTURES_BULL_CALL_SPREAD,
    LEGACY_BULL_CALL_SPREAD,
    PLATFORM_DEMO,
    WEIGHTED_BULL_CALL_SPREAD,
    BullCallSpreadHardGateParameters,
    BullCallSpreadSignalParameters,
    PlatformDemoParameters,
    get_algorithm,
    validate_documentation,
)
from app.main import create_app
from tests.conftest import make_test_settings


def test_registry_exposes_primary_document_v1_and_preserves_legacy_code() -> None:
    definition = get_algorithm("bull_call_spread")
    assert definition is FUTURES_BULL_CALL_SPREAD
    assert [template.key for template in definition.templates] == ["futures_vwap_correction"]
    assert definition.code_version == "1.3.0-bull-call-spread-v1"
    assert (
        get_algorithm("bull_call_spread", code_version=BULL_CALL_SPREAD.code_version)
        is BULL_CALL_SPREAD
    )
    assert (
        get_algorithm(
            "bull_call_spread", code_version="1.1.0-bull-call-spread-v1", include_inactive=True
        )
        is WEIGHTED_BULL_CALL_SPREAD
    )
    assert LEGACY_BULL_CALL_SPREAD.code_version == "0.1.0-signal-research"
    assert (
        get_algorithm(
            "bull_call_spread", code_version="0.1.0-signal-research", include_inactive=True
        )
        is LEGACY_BULL_CALL_SPREAD
    )
    assert definition.fixed_rules["live_trading_approved"] is False
    assert get_algorithm("unknown") is None


def test_documentation_exposes_primary_document_v1_rules() -> None:
    documentation = BULL_CALL_SPREAD.documentation
    assert documentation.lifecycle_status == "pending_partner_confirmation_research"
    assert (
        next(
            rule for rule in documentation.rules if rule.key == "weighted-score-versus-hard-gates"
        ).status
        == "PENDING_PARTNER"
    )
    assert {step.id for step in documentation.logic_steps} >= {
        "pdh_gate",
        "vwap_gate",
        "confirmation_5",
        "contract_selection",
        "spread_management",
    }
    validate_documentation(BULL_CALL_SPREAD)
    assert PLATFORM_DEMO.documentation.lifecycle_status == "synthetic_fixture"


def test_parameter_model_enforces_primary_document_rules() -> None:
    configured = BullCallSpreadSignalParameters()
    assert configured.rsi_threshold == 58
    assert configured.lots == 1

    with pytest.raises(ValidationError, match="sum of the four score weights"):
        BullCallSpreadSignalParameters(
            minimum_score=11,
        )
    with pytest.raises(ValidationError, match="less than or equal to 2"):
        BullCallSpreadSignalParameters(lots=3)
    with pytest.raises(ValidationError, match="ordered, non-overlapping"):
        BullCallSpreadSignalParameters(entry_window_1_end=time(14, 0))

    corrected = BullCallSpreadHardGateParameters()
    assert corrected.pdh_weight == corrected.vwap_weight == 0
    assert corrected.rsi_weight == 2
    assert corrected.vix_weight == corrected.minimum_score == 3


def test_generated_schema_is_identifiable_and_ui_annotated() -> None:
    schema = parameter_schema()
    assert schema["x-stockbot"]["no_approved_default"] is True
    assert schema["properties"]["rsi_threshold"]["group"] == "Signal scoring"
    assert len(schema_hash()) == 64


def test_platform_demo_is_synthetic_candle_only_and_has_its_own_schema() -> None:
    definition = get_algorithm("platform_demo")

    assert definition is PLATFORM_DEMO
    assert definition.supported_modes == ("PLATFORM_DEMO",)
    assert definition.required_capabilities == ("price_candles",)
    assert definition.fixed_rules["synthetic"] is True
    assert definition.fixed_rules["financial_pnl_available"] is False
    assert definition.fixed_rules["live_trading_approved"] is False
    assert definition.templates[0].parameters == {
        "signal_every_n_candles": 5,
        "hold_candles": 3,
    }

    schema = parameter_schema(definition)
    assert set(schema["properties"]) == {"signal_every_n_candles", "hold_candles"}
    assert schema["x-stockbot"]["synthetic"] is True
    assert schema["x-stockbot"]["no_approved_default"] is False
    assert schema_hash(definition) != schema_hash(BULL_CALL_SPREAD)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signal_every_n_candles", 0),
        ("signal_every_n_candles", 10_001),
        ("hold_candles", -1),
        ("hold_candles", 10_001),
    ],
)
def test_platform_demo_parameter_boundaries(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        PlatformDemoParameters.model_validate({field: value})

    assert PlatformDemoParameters(signal_every_n_candles=1, hold_candles=0).model_dump() == {
        "signal_every_n_candles": 1,
        "hold_candles": 0,
    }


def test_public_catalog_schema_and_validation_routes() -> None:
    app = create_app(settings=make_test_settings())
    app.dependency_overrides[get_db_session] = lambda: object()
    with TestClient(app) as client:
        catalog = client.get("/api/v1/algorithms")
        detail = client.get("/api/v1/algorithms/bull_call_spread")
        schema = client.get("/api/v1/algorithms/bull_call_spread/schema")
        valid = client.post(
            "/api/v1/algorithms/bull_call_spread/validate",
            json={"parameters": BULL_CALL_SPREAD.templates[0].parameters},
        )
        invalid = client.post(
            "/api/v1/algorithms/bull_call_spread/validate",
            json={"parameters": {"score_model": "unknown"}},
        )

    assert catalog.status_code == detail.status_code == schema.status_code == 200
    assert len(catalog.json()["items"][0]["templates"]) == 1
    assert valid.json()["valid"] is True
    assert invalid.json()["valid"] is False
    assert invalid.json()["errors"]


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, value: object) -> None:
        self.added.append(value)
        if isinstance(value, AlgorithmVersion) and value.id is None:
            value.id = uuid4()

    def commit(self) -> None:
        self.commits += 1


class FakeRepository:
    def __init__(self, _session: object) -> None:
        self.catalog = Algorithm(
            key="bull_call_spread",
            display_name="old",
            description="old",
            code_version="old",
            schema_version=0,
            schema_hash="old",
            active=True,
            supported_modes=[],
            required_capabilities=[],
        )
        self.version: AlgorithmVersion | None = None

    def get_algorithm(self, _key: str, *, lock: bool = False) -> Algorithm | None:
        return self.catalog

    def get_version(self, _id):
        return self.version

    def next_version_number(self, _key: str) -> int:
        return 1

    def list_versions(self, _key: str, include_archived: bool = False) -> list[AlgorithmVersion]:
        return []


def test_registry_sync_and_version_creation_are_normalized(monkeypatch) -> None:
    session = FakeSession()
    repository = FakeRepository(session)
    monkeypatch.setattr(service_module, "AlgorithmRepository", lambda _session: repository)

    assert sync_registry(session) == 4
    demo_version = next(
        item
        for item in session.added
        if isinstance(item, AlgorithmVersion) and item.algorithm_key == "platform_demo"
    )
    assert demo_version.parameters == service_module.PLATFORM_DEMO.templates[0].parameters
    assert demo_version.creator_context == {"type": "system_fixture", "synthetic": "true"}
    version = AlgorithmService(session).create_version(
        "bull_call_spread",
        " First research version ",
        None,
        BULL_CALL_SPREAD.templates[0].parameters,
        None,
    )

    assert version.name == "First research version"
    assert version.creator_context == {"type": "authenticated_local", "principal": "local-mvp-user"}
    assert version.code_version == "1.3.0-bull-call-spread-v1"


def test_compatibility_is_strict_and_archive_is_idempotent(monkeypatch) -> None:
    session = FakeSession()
    repository = FakeRepository(session)
    version = AlgorithmVersion(
        id=uuid4(),
        algorithm_key="bull_call_spread",
        version_number=1,
        name="version",
        code_version="0.1.0-signal-research",
        schema_version=1,
        parameters={},
        creator_context={"type": "authenticated_local", "principal": "local-mvp-user"},
        archived=False,
        created_at=SimpleNamespace(),
    )
    repository.version = version
    monkeypatch.setattr(service_module, "AlgorithmRepository", lambda _session: repository)
    monkeypatch.setattr(
        service_module,
        "DatasetRepository",
        lambda _session: SimpleNamespace(
            get_version=lambda _id: SimpleNamespace(capabilities={"price_candles": True})
        ),
    )
    service = AlgorithmService(session)
    compatibility = service.compatibility("bull_call_spread", uuid4())
    first = service.archive(version.id)
    second = service.archive(version.id)

    assert compatibility["compatible"] is False
    assert compatibility["missing_capabilities"] == [
        "futures_vwap",
        "india_vix",
        "option_contracts",
        "option_ohlc",
    ]
    assert first is second
    assert first.archived is True


def test_version_routes_cover_create_list_read_compatibility_and_archive(monkeypatch) -> None:
    value = AlgorithmVersion(
        id=uuid4(),
        algorithm_key="bull_call_spread",
        version_number=1,
        name="research",
        code_version="0.1.0-signal-research",
        schema_version=1,
        parameters=BULL_CALL_SPREAD.templates[0].parameters,
        creator_context={"type": "authenticated_local", "principal": "local-mvp-user"},
        archived=False,
        created_at=datetime.now(UTC),
    )

    class RouteRepository:
        def __init__(self, _session: object) -> None:
            pass

        def list_versions(self, _key: str, _include: bool):
            return [value]

        def get_version(self, _id):
            return value

    class RouteService:
        def __init__(self, _session: object) -> None:
            pass

        def create_version(self, *_args):
            return value

        def archive(self, _id):
            value.archived = True
            return value

        def compatibility(self, key: str, dataset_id):
            return {
                "algorithm_key": key,
                "dataset_version_id": dataset_id,
                "required_capabilities": ["price_candles", "futures_vwap", "india_vix"],
                "available_capabilities": ["price_candles"],
                "missing_capabilities": ["futures_vwap", "india_vix"],
                "compatible": False,
            }

    monkeypatch.setattr(routes, "AlgorithmRepository", RouteRepository)
    monkeypatch.setattr(routes, "AlgorithmService", RouteService)
    request = SimpleNamespace(state=SimpleNamespace(request_id="algorithm-route"))
    dataset_id = uuid4()
    listed = routes.list_versions("bull_call_spread", session=object())
    created = routes.create_version(
        "bull_call_spread",
        routes.AlgorithmVersionCreateRequest(
            name="research", parameters=BULL_CALL_SPREAD.templates[0].parameters
        ),
        request,
        object(),
    )
    compatible = routes.compatibility(
        "bull_call_spread",
        routes.CompatibilityRequest(dataset_version_id=dataset_id),
        request,
        object(),
    )
    fetched = routes.get_version(value.id, object())
    archived = routes.archive_version(value.id, request, object())

    assert listed.items[0].id == value.id
    assert created.id == value.id
    assert compatible.compatible is False
    assert fetched.id == value.id
    assert archived.archived is True


def test_algorithm_repository_queries_and_allocates_versions() -> None:
    class QuerySession:
        def __init__(self) -> None:
            self.scalar_values = [
                Algorithm(key="bull_call_spread"),
                None,
                Algorithm(key="bull_call_spread"),
                2,
            ]
            self.algorithm = Algorithm(key="bull_call_spread")
            self.version = AlgorithmVersion(id=uuid4())

        def scalars(self, _statement):
            return [self.algorithm] if self.scalar_values else [self.version]

        def scalar(self, _statement):
            return self.scalar_values.pop(0)

    session = QuerySession()
    repository = AlgorithmRepository(session)  # type: ignore[arg-type]
    assert repository.list_algorithms() == [session.algorithm]
    found = repository.get_algorithm("bull_call_spread", lock=True)
    assert isinstance(found, Algorithm)
    assert repository.get_version(uuid4()) is None
    assert repository.next_version_number("bull_call_spread") == 3


def test_algorithm_repository_lists_versions_and_summarizes_usage() -> None:
    version_id = uuid4()
    now = datetime(2026, 8, 28, 9, 30, tzinfo=UTC)
    first_run = SimpleNamespace(id=uuid4(), created_at=now)
    second_run = SimpleNamespace(id=uuid4(), created_at=now.replace(hour=9))
    third_run = SimpleNamespace(id=uuid4(), created_at=now.replace(hour=8))

    class Result:
        def all(self):
            return [("completed", 2), ("failed", 1)]

    class QuerySession:
        def __init__(self) -> None:
            self.scalar_values = [AlgorithmVersion(id=version_id), 3, now]

        def scalar(self, _statement):
            return self.scalar_values.pop(0)

        def scalars(self, _statement):
            return [first_run, second_run, third_run]

        def execute(self, _statement):
            return Result()

    repository = AlgorithmRepository(QuerySession())  # type: ignore[arg-type]
    total, statuses, latest, page, cursor = repository.usage_summary(
        version_id, cursor=None, limit=2
    )

    assert total == 3
    assert statuses == {"completed": 2, "failed": 1}
    assert latest == now
    assert page == [first_run, second_run]
    assert cursor is not None
    decoded = json.loads(urlsafe_b64decode(cursor.encode()).decode())
    assert decoded == {"created_at": second_run.created_at.isoformat(), "id": str(second_run.id)}


def test_algorithm_repository_handles_missing_and_invalid_usage_cursors() -> None:
    version_id = uuid4()

    class MissingSession:
        def scalar(self, _statement):
            return None

    missing = AlgorithmRepository(MissingSession())  # type: ignore[arg-type]
    assert missing.usage_summary(version_id, cursor=None, limit=10) == (0, {}, None, [], None)

    class PresentSession:
        def __init__(self) -> None:
            self.scalar_values = [AlgorithmVersion(id=version_id), 0, None]

        def scalar(self, _statement):
            return self.scalar_values.pop(0)

        def execute(self, _statement):
            return SimpleNamespace(all=lambda: [])

    repository = AlgorithmRepository(PresentSession())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid usage cursor"):
        repository.usage_summary(version_id, cursor="not-a-cursor", limit=10)


def test_algorithm_registry_sync_command_closes_its_session(monkeypatch, capsys) -> None:
    session = SimpleNamespace(close=lambda: None)
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    session.close = close
    monkeypatch.setattr(sync_command, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(sync_command, "create_session_factory", lambda _settings: lambda: session)
    monkeypatch.setattr(sync_command, "sync_registry", lambda _session: 2)

    sync_command.main()

    assert "2 record(s) changed" in capsys.readouterr().out
    assert closed is True
