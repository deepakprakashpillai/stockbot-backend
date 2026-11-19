from __future__ import annotations

from datetime import date
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.backtests import (
    BacktestCreateRequest,
    BacktestPreflightRequest,
    BacktestPreflightResponse,
)
from app.api.schemas.model2 import (
    Model2AssessmentResponse,
    Model2CycleListResponse,
    Model2CycleResponse,
    Model2DatasetManifestResponse,
    Model2JournalEventResponse,
    Model2JournalListResponse,
    Model2RiskEntryResponse,
    Model2RiskLedgerResponse,
    OptionPositionListResponse,
    OptionPositionResponse,
    ResearchScenarioGroupListResponse,
    ResearchScenarioGroupResponse,
    ScenarioCycleComparisonResponse,
    ScenarioMemberResponse,
)
from app.api.v1.backtest_helpers import _run
from app.application.model2.groups import Model2GroupService, Model2ServiceError
from app.application.model2.reads import cycle_page, get_run, position
from app.core.errors import error_response
from app.db.models.backtests import BacktestRun, JournalEvent
from app.db.models.datasets import DatasetVersion
from app.db.models.model2 import (
    BacktestCycle,
    BacktestRiskEntry,
    ResearchScenarioGroup,
    ResearchScenarioMember,
)
from app.db.repositories.backtests import BacktestRepository
from app.domain.model2.execution import ScenarioPolicy
from app.infrastructure.datasets.storage import LocalDatasetStorage
from app.infrastructure.market_data.model2_feed import Model2BundleFeed

router = APIRouter(tags=["model2"])


def service(request: Request, session: Session) -> Model2GroupService:
    return Model2GroupService(
        session,
        LocalDatasetStorage(request.app.state.settings.storage_root),
        request.app.state.settings,
    )


def error(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, Model2ServiceError):
        return error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            request=request,
        )
    return error_response(
        status_code=422, code="MODEL2_INPUT_INVALID", message=str(exc), request=request
    )


def group_response(session: Session, group: ResearchScenarioGroup) -> ResearchScenarioGroupResponse:
    members = []
    for member in session.scalars(
        select(ResearchScenarioMember)
        .where(ResearchScenarioMember.group_id == group.id)
        .order_by(ResearchScenarioMember.ordinal)
    ):
        run = session.get(BacktestRun, member.run_id)
        assert run is not None
        summary = run.metrics.metrics.get("model2_summary", {}) if run.metrics else {}
        members.append(
            ScenarioMemberResponse(
                ordinal=member.ordinal,
                policy=cast(ScenarioPolicy, member.policy),
                primary=member.ordinal == 1,
                scope=summary.get("scope"),
                reliability_status=summary.get("reliability", {}).get("status"),
                priced_cycle_count=summary.get("priced_cycles"),
                excluded_cycle_count=summary.get("excluded_cycles"),
                run=_run(run),
            )
        )
    return ResearchScenarioGroupResponse(
        id=group.id,
        status=group.status,
        cancellation_requested=group.cancellation_requested,
        created_at=group.created_at,
        completed_at=group.completed_at,
        request_digest=group.request_digest,
        snapshot=group.snapshot,
        members=members,
    )


@router.post("/research-scenario-groups/preflight", response_model=BacktestPreflightResponse)
def preflight_group(
    body: BacktestPreflightRequest, request: Request, session: Session = Depends(get_db_session)
) -> BacktestPreflightResponse | JSONResponse:
    try:
        result = service(request, session).preflight(**body.model_dump())
        return BacktestPreflightResponse.model_validate(result)
    except (Model2ServiceError, ValueError, KeyError) as exc:
        return error(request, exc)


@router.post(
    "/research-scenario-groups", response_model=ResearchScenarioGroupResponse, status_code=202
)
def create_group(
    body: BacktestCreateRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ResearchScenarioGroupResponse | JSONResponse:
    try:
        group, _ = service(request, session).create(
            **body.model_dump(), idempotency_key=idempotency_key
        )
        return group_response(session, group)
    except (Model2ServiceError, ValueError, KeyError) as exc:
        session.rollback()
        return error(request, exc)


@router.get("/research-scenario-groups", response_model=ResearchScenarioGroupListResponse)
def list_groups(
    request: Request,
    session: Session = Depends(get_db_session),
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> ResearchScenarioGroupListResponse | JSONResponse:
    statement = select(ResearchScenarioGroup)
    if cursor:
        previous = session.get(ResearchScenarioGroup, cursor)
        if previous is None:
            return error(
                request,
                Model2ServiceError(
                    "GROUP_CURSOR_INVALID", "The research group cursor does not exist."
                ),
            )
        statement = statement.where(
            or_(
                ResearchScenarioGroup.created_at < previous.created_at,
                and_(
                    ResearchScenarioGroup.created_at == previous.created_at,
                    ResearchScenarioGroup.id < previous.id,
                ),
            )
        )
    rows = list(
        session.scalars(
            statement.order_by(
                ResearchScenarioGroup.created_at.desc(), ResearchScenarioGroup.id.desc()
            ).limit(limit + 1)
        )
    )
    return ResearchScenarioGroupListResponse(
        items=[group_response(session, row) for row in rows[:limit]],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.get("/research-scenario-groups/{group_id}", response_model=ResearchScenarioGroupResponse)
def get_group(
    group_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> ResearchScenarioGroupResponse | JSONResponse:
    group = session.get(ResearchScenarioGroup, group_id)
    if group is None:
        return error(
            request,
            Model2ServiceError("SCENARIO_GROUP_NOT_FOUND", "Research group not found.", 404),
        )
    return group_response(session, group)


@router.post(
    "/research-scenario-groups/{group_id}/cancel",
    response_model=ResearchScenarioGroupResponse,
    status_code=202,
)
def cancel_group(
    group_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> ResearchScenarioGroupResponse | JSONResponse:
    try:
        return group_response(session, service(request, session).cancel(group_id))
    except Model2ServiceError as exc:
        return error(request, exc)


@router.get("/backtests/{run_id}/cycles", response_model=Model2CycleListResponse)
def list_cycles(
    run_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
    cursor: date | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> Model2CycleListResponse | JSONResponse:
    try:
        run = get_run(session, run_id)
        rows, next_cursor = cycle_page(session, run, cursor, limit)
        return Model2CycleListResponse(
            items=[Model2CycleResponse.model_validate(row, from_attributes=True) for row in rows],
            active_attempt_id=run.active_attempt_id,
            next_cursor=next_cursor,
        )
    except Model2ServiceError as exc:
        return error(request, exc)


@router.get("/backtests/{run_id}/cycles/{cycle_id}", response_model=Model2CycleResponse)
def get_cycle(
    run_id: UUID, cycle_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> Model2CycleResponse | JSONResponse:
    try:
        run = get_run(session, run_id)
        row = session.get(BacktestCycle, cycle_id)
        if row is None or row.run_id != run_id or row.attempt_id != run.active_attempt_id:
            raise Model2ServiceError(
                "CYCLE_NOT_FOUND", "Cycle is not part of this active attempt.", 404
            )
        return Model2CycleResponse.model_validate(row, from_attributes=True)
    except Model2ServiceError as exc:
        return error(request, exc)


@router.get("/backtests/{run_id}/portfolio-journal", response_model=Model2JournalListResponse)
def portfolio_journal(
    run_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
    attempt_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> Model2JournalListResponse | JSONResponse:
    try:
        run = get_run(session, run_id)
        selected = attempt_id or run.active_attempt_id
        repository = BacktestRepository(session)
        values: list[JournalEvent] = []
        next_cursor = None
        if selected is not None:
            if selected not in {item.id for item in repository.list_attempts(run_id)}:
                raise Model2ServiceError(
                    "ATTEMPT_NOT_FOUND", "Attempt is not part of this run.", 404
                )
            values, next_cursor = repository.list_journal_chronological(
                run_id, selected, cursor, limit
            )
        elif cursor is not None:
            raise ValueError("A journal cursor requires an execution attempt.")
        return Model2JournalListResponse(
            run_id=run_id,
            attempt_id=selected,
            items=[
                Model2JournalEventResponse.model_validate(row, from_attributes=True)
                for row in values
            ],
            next_cursor=next_cursor,
        )
    except Model2ServiceError as exc:
        return error(request, exc)
    except ValueError:
        return error(
            request,
            Model2ServiceError(
                "JOURNAL_CURSOR_INVALID",
                "The journal cursor is invalid or belongs to another attempt.",
                422,
            ),
        )


@router.get("/backtests/{run_id}/option-positions", response_model=OptionPositionListResponse)
def list_positions(
    run_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
    cursor: date | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> OptionPositionListResponse | JSONResponse:
    try:
        run = get_run(session, run_id)
        rows, next_cursor = cycle_page(session, run, cursor, limit, positions=True)
        return OptionPositionListResponse(
            items=[OptionPositionResponse.model_validate(position(row)) for row in rows],
            active_attempt_id=run.active_attempt_id,
            next_cursor=next_cursor,
        )
    except Model2ServiceError as exc:
        return error(request, exc)


@router.get(
    "/backtests/{run_id}/option-positions/{cycle_id}", response_model=OptionPositionResponse
)
def get_position(
    run_id: UUID, cycle_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> OptionPositionResponse | JSONResponse:
    try:
        run = get_run(session, run_id)
        row = session.get(BacktestCycle, cycle_id)
        result = (
            position(row)
            if row and row.run_id == run_id and row.attempt_id == run.active_attempt_id
            else None
        )
        if result is None:
            raise Model2ServiceError(
                "POSITION_NOT_FOUND", "Position is not part of this active attempt.", 404
            )
        return OptionPositionResponse.model_validate(result)
    except Model2ServiceError as exc:
        return error(request, exc)


@router.get("/backtests/{run_id}/risk-ledger", response_model=Model2RiskLedgerResponse)
def risk_ledger(
    run_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
    cursor: date | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> Model2RiskLedgerResponse | JSONResponse:
    try:
        run = get_run(session, run_id)
        query = (
            select(BacktestRiskEntry)
            .join(BacktestCycle, BacktestCycle.id == BacktestRiskEntry.cycle_id)
            .where(
                BacktestRiskEntry.run_id == run_id,
                BacktestRiskEntry.attempt_id == run.active_attempt_id,
            )
        )
        if cursor:
            query = query.where(BacktestCycle.expiry > cursor)
        rows = list(session.scalars(query.order_by(BacktestCycle.expiry).limit(limit + 1)))
        next_cursor = None
        if len(rows) > limit:
            last_cycle = session.get(BacktestCycle, rows[limit - 1].cycle_id)
            assert last_cycle is not None
            next_cursor = last_cycle.expiry
        return Model2RiskLedgerResponse(
            items=[
                Model2RiskEntryResponse.model_validate(row, from_attributes=True)
                for row in rows[:limit]
            ],
            active_attempt_id=run.active_attempt_id,
            next_cursor=next_cursor,
        )
    except Model2ServiceError as exc:
        return error(request, exc)


@router.get("/backtests/{run_id}/assessment", response_model=Model2AssessmentResponse)
def assessment(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> Model2AssessmentResponse | JSONResponse:
    try:
        from app.application.model2.reads import assessment_data

        return Model2AssessmentResponse.model_validate(assessment_data(session, run_id))
    except Model2ServiceError as exc:
        return error(request, exc)


@router.get(
    "/dataset-versions/{dataset_version_id}/model2-manifest",
    response_model=Model2DatasetManifestResponse,
)
def dataset_manifest(
    dataset_version_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> Model2DatasetManifestResponse | JSONResponse:
    version = session.get(DatasetVersion, dataset_version_id)
    if version is None or not version.capabilities.get("model2_bundle_v3"):
        return error(
            request,
            Model2ServiceError(
                "MODEL2_DATASET_NOT_FOUND", "A registered native Model 2 bundle is required.", 404
            ),
        )
    try:
        feed = Model2BundleFeed(
            LocalDatasetStorage(request.app.state.settings.storage_root).resolve(
                version.storage_key
            )
        )
        return Model2DatasetManifestResponse(
            dataset_version_id=version.id,
            input_checksum=version.checksum,
            bundle_checksum=feed.manifest.bundle_checksum,
            calendar_version=feed.calendar.version,
            calendar_sha256=feed.calendar.provenance.checksum,
            components=[
                component.model_dump(mode="json") for component in feed.manifest.components
            ],
            official_settlement_available=feed.manifest.component("settlements") is not None,
            halt_source_available=feed.manifest.component("halts") is not None,
            limitations=[
                "Futures are optional.",
                "A file checksum establishes identity; wider calendar/quote qualification is separate.",
                "Without official settlement, unpriceable opened cycles are excluded and retain separate risk assumptions.",
            ],
        )
    except ValueError as exc:
        return error(request, exc)


@router.get(
    "/research-scenario-groups/{group_id}/comparison",
    response_model=ScenarioCycleComparisonResponse,
)
def group_comparison(
    group_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
    cursor: date | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> ScenarioCycleComparisonResponse | JSONResponse:
    group = session.get(ResearchScenarioGroup, group_id)
    if group is None:
        return error(
            request,
            Model2ServiceError("SCENARIO_GROUP_NOT_FOUND", "Research group not found.", 404),
        )
    expected = group.snapshot.get("preflight", {}).get("cycles", [])
    dates = sorted(
        date.fromisoformat(row["expiry"])
        for row in expected
        if cursor is None or date.fromisoformat(row["expiry"]) > cursor
    )
    page = dates[:limit]
    members = list(
        session.scalars(
            select(ResearchScenarioMember)
            .where(ResearchScenarioMember.group_id == group_id)
            .order_by(ResearchScenarioMember.ordinal)
        )
    )
    lookup: dict[tuple[UUID, date], BacktestCycle] = {}
    for member in members:
        run = session.get(BacktestRun, member.run_id)
        assert run is not None
        for saved_cycle in session.scalars(
            select(BacktestCycle).where(
                BacktestCycle.run_id == run.id,
                BacktestCycle.attempt_id == run.active_attempt_id,
                BacktestCycle.expiry.in_(page),
            )
        ):
            lookup[(run.id, saved_cycle.expiry)] = saved_cycle
    items = []
    for expiry in page:
        outcomes = []
        for member in members:
            run = session.get(BacktestRun, member.run_id)
            assert run is not None
            row = lookup.get((run.id, expiry))
            summary = run.metrics.metrics.get("model2_summary", {}) if run.metrics else {}
            raw = row.payload.get("result", {}) if row else {}
            valuation = raw.get("valuation")
            absent = (
                "NOT_VISITED_AFTER_HALT"
                if summary.get("scope") == "HALTED_AT_EXCLUSION"
                else "NOT_VISITED_" + run.status
                if run.status in {"FAILED", "CANCELLED"}
                else "NOT_YET_VISITED"
                if run.status in {"PENDING", "RUNNING"}
                else "NO_CYCLE_RECORD"
            )
            outcomes.append(
                {
                    "policy": member.policy,
                    "run_id": str(run.id),
                    "cycle_id": str(row.id) if row else None,
                    "state": row.state if row else absent,
                    "reason": row.reason if row else None,
                    "priced_net_pnl_inr": valuation.get("net_pnl_inr") if valuation else None,
                    "risk_contribution": raw.get("risk_entry"),
                    "scope": summary.get("scope"),
                }
            )
        items.append({"expiry": expiry.isoformat(), "outcomes": outcomes})
    return ScenarioCycleComparisonResponse.model_validate(
        {
            "group_id": group_id,
            "items": items,
            "next_cursor": page[-1] if len(dates) > limit else None,
        }
    )
