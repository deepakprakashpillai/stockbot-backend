import hashlib
import json
import zipfile
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient
from persistence_fixture import prepared_case
from sqlalchemy import select

from app.application.backtests import BacktestService
from app.application.exports import BacktestExportService
from app.application.model2.datasets import register_bundle
from app.db.models.backtests import BacktestRun
from app.db.models.model2 import ResearchScenarioMember
from app.main import create_app


def api_case(postgres_factory, bundle_factory, **options):
    storage, settings, _, run_ids = prepared_case(
        postgres_factory, bundle_factory, slip="2", **options
    )
    settings = settings.model_copy(update={"model2_enabled": True})
    with postgres_factory() as session:
        run = session.get(BacktestRun, run_ids[0])
        stored = register_bundle(
            session,
            storage.resolve(run.snapshot["bundle_storage_key"]),
            storage.root,
            "Model 2 native test bundle",
        )
        session.commit()
        body = {
            "dataset_version_id": str(stored.id),
            "algorithm_version_id": str(run.algorithm_version_id),
            "start_date": "2026-01-05",
            "end_date": "2026-01-06",
            "starting_capital": "500000",
            "warning_acknowledged": True,
        }
    app = create_app(settings=settings)
    app.state.session_factory = postgres_factory
    return TestClient(app), storage, settings, body


def create(client, body, key="api-fixture"):
    response = client.post(
        "/api/v1/research-scenario-groups", json=body, headers={"Idempotency-Key": key}
    )
    assert response.status_code == 202, response.text
    return response.json()


def execute(postgres_factory, storage, settings, group):
    with postgres_factory() as session:
        for member in group["members"]:
            assert (
                BacktestService(session, storage, settings).execute(UUID(member["run"]["id"]))[
                    "status"
                ]
                == "COMPLETED"
            )


def test_group_http_journey_preserves_four_legs_risk_counts_and_primary_history(
    postgres_factory, bundle_factory
):
    client, storage, settings, body = api_case(postgres_factory, bundle_factory)
    preview = client.post("/api/v1/research-scenario-groups/preflight", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["allowed"]
    assert preview.json()["requested_cycle_count"] == preview.json()["entry_ready_cycle_count"] == 1
    assert preview.json()["cycles"][0]["entry_estimate"]["margin_source"] == "ASSUMED"
    group = create(client, body)
    assert len(group["members"]) == 3
    assert [member["primary"] for member in group["members"]] == [True, False, False]
    assert create(client, body)["id"] == group["id"]
    changed = client.post(
        "/api/v1/research-scenario-groups",
        json={**body, "starting_capital": "600000"},
        headers={"Idempotency-Key": "api-fixture"},
    )
    assert changed.status_code == 409
    execute(postgres_factory, storage, settings, group)
    saved = client.get(f"/api/v1/research-scenario-groups/{group['id']}").json()
    assert saved["status"] == "COMPLETED"
    comparison = client.get(f"/api/v1/research-scenario-groups/{group['id']}/comparison")
    assert comparison.status_code == 200, comparison.text
    assert [row["state"] for row in comparison.json()["items"][0]["outcomes"]] == ["CLOSED"] * 3
    manifest = client.get(f"/api/v1/dataset-versions/{body['dataset_version_id']}/model2-manifest")
    assert manifest.status_code == 200, manifest.text
    assert not manifest.json()["official_settlement_available"]
    assert not manifest.json()["halt_source_available"]

    for member in saved["members"]:
        run_id = member["run"]["id"]
        assert member["run"]["scenario_group_id"] == saved["id"]
        assert member["run"]["outcome_summary"]["unit"] == "INR"
        assert Decimal(member["run"]["outcome_summary"]["net_outcome"]) == Decimal("-608.00")
        positions = client.get(f"/api/v1/backtests/{run_id}/option-positions")
        assert positions.status_code == 200, positions.text
        (position,) = positions.json()["items"]
        assert len(position["legs"]) == 4
        assert position["net_pnl_inr"] == "-608.00"
        assert position["priced_performance_included"]
        assert (
            client.get(f"/api/v1/backtests/{run_id}/option-positions/{position['id']}").status_code
            == 200
        )
        assert client.get(f"/api/v1/backtests/{run_id}/trades").status_code == 409
        legacy_journal = client.get(f"/api/v1/backtests/{run_id}/journal")
        assert legacy_journal.status_code == 409
        assert legacy_journal.json()["error"]["details"]["path"].endswith("/portfolio-journal")
        events, cursor = [], None
        while True:
            page = client.get(
                f"/api/v1/backtests/{run_id}/portfolio-journal",
                params={"limit": 2, **({"cursor": cursor} if cursor else {})},
            )
            assert page.status_code == 200, page.text
            events.extend(page.json()["items"])
            cursor = page.json()["next_cursor"]
            if cursor is None:
                break
        assert "ENTRY_PENDING" in {event["position_state"] for event in events}
        assert "CLOSED" in {event["position_state"] for event in events}
        assert len({event["id"] for event in events}) == len(events)
        assert [event["sequence"] for event in events] == sorted(
            event["sequence"] for event in events
        )
        wrong_attempt = saved["members"][(member["ordinal"] % 3)]["run"]["active_attempt_id"]
        assert (
            client.get(
                f"/api/v1/backtests/{run_id}/portfolio-journal",
                params={"attempt_id": wrong_attempt},
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/v1/backtests/{run_id}/portfolio-journal", params={"cursor": "invalid"}
            ).status_code
            == 422
        )
        assert (
            client.get(f"/api/v1/backtests/{run_id}/trades/{position['trade_id']}").status_code
            == 409
        )
        risk = client.get(f"/api/v1/backtests/{run_id}/risk-ledger").json()["items"]
        assert len(risk) == 1 and risk[0]["source"] == "PRICED_RESULT"
        report = client.get(f"/api/v1/backtests/{run_id}/assessment")
        assert report.status_code == 200, report.text
        assert report.json()["overall_status"] == "INSUFFICIENT_SAMPLE"
        assert report.json()["reliability"]["data_gate_passed"]
        assert report.json()["priced_statistics"]["trade_count"] == 1
        assert any(
            item["status"] == "NOT_ASSESSED_POLICY_PENDING" for item in report.json()["criteria"]
        )
        snapshot = client.get(f"/api/v1/backtests/{run_id}/live-snapshot").json()
        assert snapshot["unit"] == "INR" and not snapshot["synthetic"]
    history = client.get(
        "/api/v1/backtests", params={"algorithm_key": "model2_overnight_iron_condor"}
    ).json()
    with postgres_factory() as session:
        expected = {
            str(value)
            for value in session.scalars(
                select(ResearchScenarioMember.run_id).where(ResearchScenarioMember.ordinal == 1)
            )
        }
    assert {row["id"] for row in history["items"]} == expected

    assert all(row["financial_pnl_available"] and not row["synthetic"] for row in history["items"])


def test_common_run_create_and_cancel_delegate_to_atomic_group(postgres_factory, bundle_factory):
    client, storage, settings, body = api_case(postgres_factory, bundle_factory)
    response = client.post(
        "/api/v1/backtests", json=body, headers={"Idempotency-Key": "common-create"}
    )
    assert response.status_code == 202, response.text
    primary = response.json()
    assert primary["scenario_group_id"]
    cancelled = client.post(f"/api/v1/backtests/{primary['id']}/cancel")
    assert cancelled.status_code == 202, cancelled.text
    group = client.get(f"/api/v1/research-scenario-groups/{primary['scenario_group_id']}").json()
    assert group["status"] == "CANCELLED"
    assert all(member["run"]["status"] == "CANCELLED" for member in group["members"])


def test_preflight_does_not_remove_future_missing_exits_and_exports_unknown_results(
    postgres_factory, bundle_factory
):
    client, storage, settings, body = api_case(postgres_factory, bundle_factory, missing_exit=True)
    preview = client.post("/api/v1/research-scenario-groups/preflight", json=body)
    assert preview.json()["allowed"]
    group = create(client, body)
    execute(postgres_factory, storage, settings, group)
    saved = client.get(f"/api/v1/research-scenario-groups/{group['id']}").json()
    for member in saved["members"]:
        run_id = member["run"]["id"]
        position = client.get(f"/api/v1/backtests/{run_id}/option-positions").json()["items"][0]
        assert position["state"] == "EXCLUDED"
        assert position["net_pnl_inr"] is None
        assert not position["priced_performance_included"]
        assert member["run"]["outcome_summary"] is None
    with postgres_factory() as session:
        job = BacktestExportService(session, settings).create(
            UUID(group["members"][0]["run"]["id"])
        )
        job = BacktestExportService(session, settings).build(job.id)
        assert job.status == "COMPLETED"
        path = storage.resolve(job.artifact_path)
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["group_status"] == "COMPLETED"
            for name, identity in manifest["files"].items():
                content = archive.read(name)
                assert hashlib.sha256(content).hexdigest() == identity["sha256"]
                assert len(content) == identity["bytes"]
                assert len(content.splitlines()) == identity["rows"]
            primary = json.loads(archive.read("MAX_LOSS_RISK/risk.jsonl").splitlines()[0])
            assert primary["source"] == "ASSUMED_MAX_LOSS"
            assert primary["priced_performance_amount"] is None
            assert archive.read("MAX_LOSS_RISK/trades.jsonl") == b""
            assert len(archive.read("MAX_LOSS_RISK/legs.jsonl").splitlines()) == 4
            assert manifest["scenario_results_are_not_combined"]


def test_disabled_submissions_and_warning_acknowledgement_are_enforced(
    postgres_factory, bundle_factory
):
    client, storage, settings, body = api_case(postgres_factory, bundle_factory)
    refused = client.post(
        "/api/v1/research-scenario-groups", json={**body, "warning_acknowledged": False}
    )
    assert refused.status_code == 409
    client.app.state.settings = settings.model_copy(update={"model2_enabled": False})
    assert client.post("/api/v1/research-scenario-groups", json=body).status_code == 409
    assert "model2_overnight_iron_condor" not in {
        item["key"] for item in client.get("/api/v1/algorithms").json()["items"]
    }


def test_xlsx_export_keeps_large_values_without_truncation(postgres_factory, bundle_factory):
    client, storage, settings, body = api_case(
        postgres_factory,
        bundle_factory,
        source_reference="Research source " + ("reference " * 5000),
    )
    group = create(client, body)
    execute(postgres_factory, storage, settings, group)
    with postgres_factory() as session:
        service = BacktestExportService(session, settings)
        job = service.create(UUID(group["members"][0]["run"]["id"]), format="XLSX")
        job = service.build(job.id)
        assert job.status == "COMPLETED"
        with zipfile.ZipFile(storage.resolve(job.artifact_path)) as workbook:
            names = workbook.read("xl/workbook.xml").decode()
            assert "Cell continuations" in names and "_legs_" in names
            all_sheets = "".join(
                workbook.read(name).decode()
                for name in workbook.namelist()
                if name.startswith("xl/worksheets/sheet")
            )
            assert "Full value: Cell continuations rows" in all_sheets
            assert job.row_counts["MAX_LOSS_RISK/legs"] == 4
