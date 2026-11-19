"""The published release stays additive to the exact pre-Model-2 contract."""

import hashlib
import json
from pathlib import Path


def test_model2_contract_keeps_old_routes_and_optional_response_additions():
    root = Path(__file__).resolve().parents[2] / "contracts"
    archive = root / "archive/openapi-0.1.1.json"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == (
        "2d9fb56ef40c7ec5212c1fd5fa11ccd143e7ce936d9d94d7fb4ec13a473c0a1d"
    )
    assert hashlib.sha256((root / "archive/websocket-1.2.0.json").read_bytes()).hexdigest() == (
        "01daf3cbbd38b39cbd24a8c7d0fbbe8245fb7b4996743b7aaa66854dbae3ed0a"
    )
    previous = json.loads(archive.read_text())
    current = json.loads((root / "openapi.json").read_text())
    assert current["info"]["version"] == "0.1.3"
    assert hashlib.sha256((root / "archive/openapi-0.1.2.json").read_bytes()).hexdigest() == (
        "a00b1ad61c51a3400c9b1a7b02d2d5a65f1bc65d6e4ee214f6c6a87536458ef8"
    )
    for path, operations in previous["paths"].items():
        assert path in current["paths"]
        assert set(operations) <= set(current["paths"][path])
    for name, schema in previous["components"]["schemas"].items():
        replacement = current["components"]["schemas"][name]
        assert set(replacement.get("required", [])) <= set(schema.get("required", [])), name
        assert set(schema.get("properties", {})) <= set(replacement.get("properties", {})), name
    for name in (
        "DemoTradeResponse",
        "DemoTradeLegResponse",
        "DemoTradeListResponse",
        "JournalEventResponse",
        "JournalEventListResponse",
    ):
        assert current["components"]["schemas"][name] == previous["components"]["schemas"][name]
