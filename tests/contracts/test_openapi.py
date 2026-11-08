from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from app.main import create_app


def test_openapi_document_is_deterministic() -> None:
    first = json.dumps(create_app().openapi(), indent=2, sort_keys=True)
    second = json.dumps(create_app().openapi(), indent=2, sort_keys=True)

    assert first == second
    assert "/api/v1/system" in first


def test_openapi_checksum_matches_the_published_contract() -> None:
    contract = (Path("contracts") / "openapi.json").read_bytes()
    expected = f"{sha256(contract).hexdigest()}  openapi.json\n"

    assert (Path("contracts") / "openapi.sha256").read_text(encoding="utf-8") == expected


def test_websocket_checksum_matches_the_published_contract() -> None:
    contract = (Path("contracts") / "websocket-v1.json").read_bytes()
    expected = f"{sha256(contract).hexdigest()}  websocket-v1.json\n"

    assert (Path("contracts") / "websocket-v1.sha256").read_text(encoding="utf-8") == expected
