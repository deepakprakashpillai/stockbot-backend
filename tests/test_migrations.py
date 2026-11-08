from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_foundation_migration_has_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["20261108_15"]
