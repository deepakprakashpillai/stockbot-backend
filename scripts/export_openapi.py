from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import create_app  # noqa: E402


def main() -> None:
    destination = PROJECT_ROOT / "contracts/openapi.json"
    content = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    destination.write_text(content, encoding="utf-8")
    (PROJECT_ROOT / "contracts/openapi.sha256").write_text(
        f"{sha256(content.encode()).hexdigest()}  openapi.json\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
