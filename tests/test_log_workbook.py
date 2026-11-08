from datetime import date, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from app.api.schemas.backtests import ExportJobCreateRequest
from app.application import log_workbook

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_sheets(path):
    with ZipFile(path) as archive:
        names = [
            s.attrib["name"]
            for s in ET.fromstring(archive.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS)
        ]
        result = {}
        for index, name in enumerate(names, 1):
            root = ET.fromstring(archive.read(f"xl/worksheets/sheet{index}.xml"))
            assert root.find(".//m:f", NS) is None
            assert root.find(".//m:pane", NS) is not None
            assert root.find("m:autoFilter", NS) is not None
            result[name] = [
                ["".join(c.itertext()) for c in row]
                for row in root.findall("m:sheetData/m:row", NS)
            ]
        return result


def fixture():
    stamp = datetime.fromisoformat("2026-01-02T04:00:00+00:00")
    return dict(
        summary={"run_id": "run-1", "mode": "PLATFORM_DEMO", "snapshot": {"parameters": {"a": 10}}},
        attempt_id="attempt-1",
        signals=[
            {
                "id": "signal-1",
                "sequence": 1,
                "timestamp": stamp,
                "session_date": date(2026, 1, 2),
                "decision": "REJECTED",
                "score": 2,
                "rejection_reason": "SCORE_TOO_LOW",
                "inputs": {
                    "precise": Decimal("0.123456789123456789"),
                    "zero": 0,
                    "missing": None,
                    "label": '=HYPERLINK("bad")',
                },
                "diagnostic": {
                    "evaluation_steps": [
                        {
                            "order": 1,
                            "label": "A > B",
                            "observed_value": 10,
                            "comparator": ">",
                            "threshold_value": 20,
                            "status": "failed",
                            "score_contribution": 0,
                        }
                    ]
                },
            }
        ],
        trades=[],
        journal=[],
        days=[
            {
                "session_date": date(2026, 1, 2),
                "included": True,
                "classification": "NO_SIGNAL",
                "primary_reason": "SCORE_TOO_LOW",
            },
            {
                "session_date": date(2026, 1, 5),
                "included": False,
                "classification": "EXCLUDED",
                "primary_reason": "MISSING_OPTIONS",
            },
        ],
        events=[],
        equity=[],
        analytics=[],
    )


def test_export_keeps_comparisons_exclusions_exact_values_and_no_formulas(tmp_path):
    path = tmp_path / "logs.xlsx"
    counts = log_workbook.build_log_workbook(path, **fixture())
    sheets = read_sheets(path)
    assert counts["signals"] == 1
    assert counts["days"] == 2
    assert len(sheets["Days"]) == 3
    assert sheets["Days"][2][2:5] == ["EXCLUDED", "MISSING_OPTIONS", "0"]
    assert sheets["Checks"][1][4:9] == ["A > B", "failed", "10", ">", "20"]
    raw = dict(zip(sheets["Signal evidence"][0], sheets["Signal evidence"][1], strict=True))
    assert raw["inputs.precise"] == "0.123456789123456789"
    assert raw["inputs.zero"] == "0"
    assert raw["inputs.missing"] == "None"
    assert raw["inputs.label"].startswith("=HYPERLINK")
    assert log_workbook.local_time(fixture()["signals"][0]["timestamp"]).hour == 9
    assert ExportJobCreateRequest(format="XLSX").format == "XLSX"
    assert ExportJobCreateRequest().format == "ZIP"


def test_replayable_signal_and_journal_reads_match_list_workbook(tmp_path):
    data = fixture()
    data["journal"] = [
        {
            "sequence": 1,
            "timestamp": data["signals"][0]["timestamp"],
            "session_date": data["signals"][0]["session_date"],
            "event_type": "POSITION_CHECK",
            "signal_sequence": 1,
            "payload": {"action": "WAIT"},
        }
    ]
    data["events"] = [{"sequence": 1, "event_type": "COMPLETED", "payload": {"ok": True}}]
    data["equity"] = [
        {"sequence": 1, "timestamp": data["signals"][0]["timestamp"], "equity": Decimal("10.25")}
    ]
    data["analytics"] = [{"period_start": date(2026, 1, 2), "trade_count": 1}]
    data["trades"] = [
        {
            "sequence": 1,
            "entry_signal_evaluation_id": "signal-1",
            "entry_timestamp": data["signals"][0]["timestamp"],
            "exit_timestamp": data["signals"][0]["timestamp"],
            "exit_reason": "DEMO_EXIT",
            "quantity": 1,
            "entry_reference": Decimal("10"),
            "exit_reference": Decimal("13"),
            "gross_outcome": Decimal("3"),
            "costs": Decimal("0"),
            "net_outcome": Decimal("3"),
            "unit": "DEMO_UNITS",
            "assumptions": {},
        }
    ]
    expected = tmp_path / "listed.xlsx"
    actual = tmp_path / "streamed.xlsx"
    expected_counts = log_workbook.build_log_workbook(expected, **data)

    class Replayable:
        def __init__(self, rows):
            self.rows = rows
            self.reads = 0

        def __iter__(self):
            self.reads += 1
            return iter(self.rows)

    signals = Replayable(data["signals"])
    journal = Replayable(data["journal"])
    trades = Replayable(data["trades"])
    days = Replayable(data["days"])
    events = Replayable(data["events"])
    equity = Replayable(data["equity"])
    analytics = Replayable(data["analytics"])
    actual_counts = log_workbook.build_log_workbook(
        actual,
        **{
            **data,
            "signals": signals,
            "journal": journal,
            "trades": trades,
            "days": days,
            "events": events,
            "equity": equity,
            "analytics": analytics,
        },
        signal_count=1,
        trade_count=1,
        journal_count=1,
        day_count=2,
        event_count=1,
        equity_count=1,
        analytics_count=1,
    )
    assert actual_counts == expected_counts
    assert read_sheets(actual) == read_sheets(expected)
    assert signals.reads > 1 and journal.reads > 1
    assert trades.reads > 1
    assert days.reads > 1
    assert events.reads == equity.reads == analytics.reads == 1


def test_legacy_missing_checks_and_long_evidence_are_not_invented_or_truncated(tmp_path):
    data = fixture()
    data["signals"][0]["diagnostic"] = None
    data["signals"][0]["inputs"]["long"] = "x" * 60001
    path = tmp_path / "legacy.xlsx"
    log_workbook.build_log_workbook(path, **data)
    sheets = read_sheets(path)
    assert sheets["Checks"][1][5] == "unavailable"
    parts = [r[4] for r in sheets["Signal evidence"][1:] if r[2] == "inputs.long"]
    assert "".join(parts) == "x" * 60001
    assert "Legacy" in sheets["Decisions"][1][-1]


def test_large_evidence_is_split_into_numbered_sheets(tmp_path, monkeypatch):
    monkeypatch.setattr(log_workbook, "MAX_DATA_ROWS", 5)
    path = tmp_path / "split.xlsx"
    data = fixture()
    data["signals"] = [{**data["signals"][0], "sequence": i} for i in range(12)]
    log_workbook.build_log_workbook(path, **data)
    sheets = read_sheets(path)
    assert "Signal evidence 2" in sheets
    rows = [
        row
        for name, values in sheets.items()
        if name.startswith("Signal evidence")
        for row in values[1:]
    ]
    assert len(rows) == 12


def test_hard_gate_score_cell_is_blank_and_not_zero(tmp_path):
    data = fixture()
    data["signals"][0]["score"] = None
    path = tmp_path / "hard-gate.xlsx"
    log_workbook.build_log_workbook(path, **data)

    with ZipFile(path) as archive:
        decisions = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
    assert 'r="H2"' not in decisions


def test_trade_sheet_explains_contract_quantity_source(tmp_path):
    data = fixture()
    stamp = data["signals"][0]["timestamp"]
    data["trades"] = [
        {
            "sequence": 1,
            "entry_timestamp": stamp,
            "exit_timestamp": stamp,
            "exit_reason": "SESSION_SQUARE_OFF",
            "quantity": 65,
            "entry_reference": "80.00",
            "exit_reference": "75.00",
            "gross_outcome": "-325.00",
            "costs": "400.00",
            "net_outcome": "-725.00",
            "unit": "INR",
            "entry_signal_evaluation_id": "signal-1",
            "assumptions": {
                "expiry": "2026-01-06",
                "lot_size": 65,
                "lot_size_source": "selected_option_contract_metadata",
                "lots_traded": 1,
                "total_quantity": 65,
            },
        }
    ]
    path = tmp_path / "trade-quantity.xlsx"
    log_workbook.build_log_workbook(path, **data)
    trades = read_sheets(path)["Trades"]
    assert trades[0][4:9] == [
        "Expiry",
        "Lot size",
        "Lot-size source",
        "Lots",
        "Total quantity",
    ]
    assert trades[1][4:9] == [
        "2026-01-06",
        "65",
        "selected_option_contract_metadata",
        "1",
        "65",
    ]
