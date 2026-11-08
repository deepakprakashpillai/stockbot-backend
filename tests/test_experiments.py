from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import experiments
from app.application.experiments import ExperimentService, ExperimentServiceError
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import BacktestRun, ExperimentPlan
from app.domain.algorithms import BULL_CALL_SPREAD
from tests.conftest import make_test_settings


def experiment_service(monkeypatch):
    baseline = AlgorithmVersion(
        id=uuid4(),
        algorithm_key="bull_call_spread",
        code_version=BULL_CALL_SPREAD.code_version,
        parameters=BULL_CALL_SPREAD.templates[0].parameters,
        archived=False,
    )
    values = dict(
        baseline_algorithm_version_id=baseline.id,
        dataset_version_id=uuid4(),
        parameter_key="lots",
        parameter_values=[2],
        development_start_date=date(2025, 7, 21),
        development_end_date=date(2025, 7, 22),
        validation_start_date=date(2025, 8, 1),
        validation_end_date=date(2025, 8, 2),
        starting_capital=Decimal("500000"),
    )

    class Session:
        def __init__(self):
            self.added = []
            self.commits = 0
            self.plan = None
            self.records = []
            self.matching = None
            self.versions = {baseline.id: baseline}

        def get(self, model, identity):
            if model is AlgorithmVersion:
                return self.versions.get(identity)
            assert model is ExperimentPlan
            return self.plan

        def scalar(self, statement):
            return self.plan if "experiment_plans" in str(statement) else self.matching

        def scalars(self, _statement):
            return self.records

        def add(self, value):
            self.added.append(value)
            if isinstance(value, ExperimentPlan):
                value.id = uuid4()
                self.plan = value

        def commit(self):
            self.commits += 1

    session = Session()
    submitted = []
    fail_at = []

    class Backtests:
        def __init__(self, *_args):
            pass

        def preflight(self, *_args, **kwargs):
            params = kwargs.get("parameters_override", baseline.parameters)
            return {
                "allowed": False,
                "issues": [{"code": "PARTIAL_COVERAGE_ACKNOWLEDGEMENT_REQUIRED"}],
                "coverage_digest": f"coverage-lots-{params['lots']}",
                "included_session_count": 1,
                "excluded_sessions": [
                    {"session_date": "2025-07-22", "reason": "fixture missing candle"}
                ],
            }

        def create(self, **kwargs):
            if fail_at and len(submitted) == fail_at[0]:
                raise RuntimeError("variant creation failed")
            run = BacktestRun(
                id=uuid4(), algorithm_version_id=kwargs["algorithm_version_id"], status="PENDING"
            )
            submitted.append((run, kwargs))
            return run, False

    class Algorithms:
        def __init__(self, *_args):
            pass

        def validate(self, _key, _values):
            return True, [], []

        def create_version(self, key, _name, _notes, params, _parent, **kwargs):
            assert kwargs["commit"] is False
            version = AlgorithmVersion(
                id=uuid4(), algorithm_key=key, code_version=baseline.code_version, parameters=params
            )
            session.versions[version.id] = version
            return version

    monkeypatch.setattr(experiments, "BacktestService", Backtests)
    monkeypatch.setattr(experiments, "AlgorithmService", Algorithms)
    service = ExperimentService(session, object(), make_test_settings(), object())
    return service, session, baseline, values, submitted, fail_at


def create_plan(service, values):
    review = service.preview(**values)
    return service.create(
        **values,
        name="Test lots",
        hypothesis="Test quantity sensitivity",
        coverage_digest=review["coverage_digest"],
        notes=None,
    )


def test_review_covers_each_variant_and_binds_all_immutable_inputs(monkeypatch):
    service, _session, _baseline, values, _submitted, _fail = experiment_service(monkeypatch)
    review = service.preview(**values)
    assert review["allowed"] is True
    assert (
        service.preview(**{**values, "starting_capital": Decimal("500000.00")})["coverage_digest"]
        == review["coverage_digest"]
    )
    assert [row["parameters"]["lots"] for row in review["variants"]] == [1, 2]
    assert all(row["excluded_sessions"] for row in review["variants"])
    for changed in (
        {"parameter_values": [1, 2]},
        {"dataset_version_id": uuid4()},
        {"starting_capital": Decimal("600000")},
    ):
        assert (
            service.preview(**{**values, **changed})["coverage_digest"] != review["coverage_digest"]
        )
    with pytest.raises(ExperimentServiceError, match="Review and accept"):
        service.create(
            **values, name="Changed", hypothesis="stale review", coverage_digest="stale", notes=None
        )


@pytest.mark.parametrize(
    "change, code",
    [
        ({"parameter_key": "unknown"}, "EXPERIMENT_PARAMETER_INVALID"),
        ({"parameter_values": [0]}, "EXPERIMENT_PARAMETER_VALUE_INVALID"),
        ({"parameter_key": "pdh_weight", "parameter_values": [1]}, "EXPERIMENT_PARAMETER_INVALID"),
    ],
)
def test_preview_rejects_invalid_or_fixed_parameter_changes(monkeypatch, change, code):
    service, _session, _baseline, values, _submitted, _fail = experiment_service(monkeypatch)
    with pytest.raises(ExperimentServiceError) as error:
        service.preview(**{**values, **change})
    assert error.value.code == code


def test_development_batch_commits_once_with_sequential_durable_dependencies(monkeypatch):
    service, session, _baseline, values, submitted, _fail = experiment_service(monkeypatch)
    plan = create_plan(service, values)
    before = session.commits
    started, records = service.start(plan.id)
    assert started.status == "DEVELOPMENT_RUNNING"
    assert len(records) == len(submitted) == 2
    assert session.commits == before + 1
    assert all(kwargs["commit"] is False for _run, kwargs in submitted)
    assert [kwargs["depends_on_run_id"] for _run, kwargs in submitted] == [None, submitted[0][0].id]
    session.records = records
    assert service.start(plan.id)[1] == records
    assert len(submitted) == 2
    for record in records:
        record.backtest_run.status = "COMPLETED"
    assert service.get(plan.id).status == "DEVELOPMENT_COMPLETE"
    session.matching = submitted[1][0]
    selected = session.matching.algorithm_version_id
    _plan, validation = service.unlock_validation(plan.id, selected, "validation-coverage")
    assert validation.phase == "VALIDATION"
    assert plan.selected_algorithm_version_id == selected
    assert plan.status == "VALIDATION_RUNNING"
    session.records.append(validation)
    validation.backtest_run.status = "COMPLETED"
    assert service.get(plan.id).status == "VALIDATION_COMPLETE"
    assert service.unlock_validation(plan.id, selected, "validation-coverage")[1] is validation


def test_partial_batch_failure_has_no_commit_and_failed_runs_end_plan_lifecycle(monkeypatch):
    service, session, _baseline, values, _submitted, fail = experiment_service(monkeypatch)
    plan = create_plan(service, values)
    before = session.commits
    fail.append(1)
    with pytest.raises(RuntimeError, match="variant creation failed"):
        service.start(plan.id)
    assert session.commits == before
    plan.status = "DEVELOPMENT_RUNNING"
    session.records = [SimpleNamespace(phase="DEVELOPMENT", run_status="FAILED")]
    assert service.get(plan.id).status == "DEVELOPMENT_FAILED"
    with pytest.raises(ExperimentServiceError, match="every development variant"):
        service.unlock_validation(plan.id, uuid4(), "digest")


@pytest.mark.parametrize(
    "change, code",
    [
        ({"development_start_date": date(2025, 7, 23)}, "EXPERIMENT_DEVELOPMENT_RANGE_INVALID"),
        ({"validation_start_date": date(2025, 7, 22)}, "EXPERIMENT_VALIDATION_RANGE_INVALID"),
        ({"parameter_values": [2, 2]}, "EXPERIMENT_VALUES_DUPLICATED"),
    ],
)
def test_plan_rejects_overlapping_ranges_and_duplicate_variants(monkeypatch, change, code):
    service, _session, _baseline, values, _submitted, _fail = experiment_service(monkeypatch)
    with pytest.raises(ExperimentServiceError) as error:
        service.create(
            **{**values, **change},
            name="invalid",
            hypothesis="invalid",
            coverage_digest="irrelevant",
            notes=None,
        )
    assert error.value.code == code
