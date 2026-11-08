from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Algorithm(Base):
    __tablename__ = "algorithms"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    code_version: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supported_modes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    required_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    versions: Mapped[list[AlgorithmVersion]] = relationship(back_populates="algorithm")


class AlgorithmVersion(Base):
    __tablename__ = "algorithm_versions"
    __table_args__ = (
        UniqueConstraint("algorithm_key", "version_number", name="uq_algorithm_version_number"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    algorithm_key: Mapped[str] = mapped_column(
        ForeignKey("algorithms.key", ondelete="RESTRICT"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    parent_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("algorithm_versions.id", ondelete="RESTRICT"), index=True
    )
    code_version: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    creator_context: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    algorithm: Mapped[Algorithm] = relationship(back_populates="versions")
    parent: Mapped[AlgorithmVersion | None] = relationship(remote_side="AlgorithmVersion.id")
