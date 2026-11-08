"""Backtest comparison preflight and result routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.backtests import (
    ComparisonPreflightResponse,
    ComparisonRequest,
    ComparisonResponse,
    ComparisonSeriesRequest,
    ComparisonSeriesResponse,
)
from app.api.v1.backtest_helpers import _error
from app.application.backtests import BacktestServiceError
from app.application.results import ResultService

router = APIRouter()


@router.post("/comparisons/preflight", response_model=ComparisonPreflightResponse)
def comparison_preflight(
    body: ComparisonRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ComparisonPreflightResponse | JSONResponse:
    try:
        return ComparisonPreflightResponse.model_validate(
            ResultService(session).comparison_preflight(body.run_ids)
        )
    except BacktestServiceError as exc:
        return _error(request, exc)


@router.post("/comparisons", response_model=ComparisonResponse)
def compare_backtests(
    body: ComparisonRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ComparisonResponse | JSONResponse:
    try:
        return ComparisonResponse.model_validate(ResultService(session).compare(body.run_ids))
    except BacktestServiceError as exc:
        return _error(request, exc)


@router.post("/comparisons/series", response_model=ComparisonSeriesResponse)
def comparison_series(
    body: ComparisonSeriesRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ComparisonSeriesResponse | JSONResponse:
    try:
        return ComparisonSeriesResponse.model_validate(
            ResultService(session).comparison_series(
                body.run_ids,
                requested_series=body.series,
                start=body.start,
                end=body.end,
                resolution=body.resolution,
                max_points_per_run=body.max_points_per_run,
            )
        )
    except BacktestServiceError as exc:
        return _error(request, exc)
