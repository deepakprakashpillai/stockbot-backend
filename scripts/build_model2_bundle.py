"""Build a v3 manifest from explicitly prepared files, without downloading data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.infrastructure.datasets.derivatives_bundle import sha256_file
from app.infrastructure.datasets.model2_bundle import (
    BundleComponent,
    ComponentKind,
    TimestampSemantics,
    build_model2_bundle,
    component_path,
)


class ComponentDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ComponentKind
    path: str
    source_reference: str = Field(min_length=1)
    timestamp_semantics: TimestampSemantics | None = None
    contract_id: str | None = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument(
        "--components",
        type=Path,
        required=True,
        help="JSON list of component declarations; paths are relative to bundle-dir.",
    )
    args = parser.parse_args()
    declared = TypeAdapter(list[ComponentDeclaration]).validate_json(args.components.read_text())
    components = []
    for source in declared:
        component = BundleComponent(**source.model_dump(), checksum="0" * 64)
        path = component_path(args.bundle_dir, component)
        components.append(component.model_copy(update={"checksum": sha256_file(path)}))
    manifest = build_model2_bundle(args.bundle_dir, components)
    print(
        json.dumps(
            {
                "schema_version": manifest.schema_version,
                "bundle_checksum": manifest.bundle_checksum,
                "capabilities": sorted(manifest.capabilities),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
