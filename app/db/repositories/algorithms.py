from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db.models.algorithms import Algorithm, AlgorithmVersion
from app.db.models.backtests import BacktestRun


class AlgorithmRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_algorithms(self, include_inactive: bool = False) -> list[Algorithm]:
        statement = select(Algorithm).order_by(Algorithm.key)
        if not include_inactive:
            statement = statement.where(Algorithm.active.is_(True))
        return list(self.session.scalars(statement))

    def get_algorithm(self, key: str, *, lock: bool = False) -> Algorithm | None:
        statement = select(Algorithm).where(Algorithm.key == key)
        return self.session.scalar(statement.with_for_update() if lock else statement)

    def get_version(self, version_id: UUID) -> AlgorithmVersion | None:
        return self.session.scalar(
            select(AlgorithmVersion).where(AlgorithmVersion.id == version_id)
        )

    def list_versions(self, key: str, include_archived: bool = False) -> list[AlgorithmVersion]:
        statement = select(AlgorithmVersion).where(AlgorithmVersion.algorithm_key == key)
        if not include_archived:
            statement = statement.where(AlgorithmVersion.archived.is_(False))
        return list(self.session.scalars(statement.order_by(AlgorithmVersion.version_number)))

    def next_version_number(self, key: str) -> int:
        self.get_algorithm(key, lock=True)
        highest = self.session.scalar(
            select(func.max(AlgorithmVersion.version_number)).where(
                AlgorithmVersion.algorithm_key == key
            )
        )
        return (highest or 0) + 1

    def usage_summary(
        self, version_id: UUID, *, cursor: str | None, limit: int
    ) -> tuple[int, dict[str, int], datetime | None, list[BacktestRun], str | None]:
        version = self.get_version(version_id)
        if version is None:
            return 0, {}, None, [], None
        statement = select(BacktestRun).where(BacktestRun.algorithm_version_id == version_id)
        total = int(
            self.session.scalar(
                select(func.count())
                .select_from(BacktestRun)
                .where(BacktestRun.algorithm_version_id == version_id)
            )
            or 0
        )
        status_rows = self.session.execute(
            select(BacktestRun.status, func.count())
            .where(BacktestRun.algorithm_version_id == version_id)
            .group_by(BacktestRun.status)
        ).all()
        statuses: dict[str, int] = {str(row[0]): int(row[1]) for row in status_rows}
        latest = self.session.scalar(
            select(func.max(BacktestRun.created_at)).where(
                BacktestRun.algorithm_version_id == version_id
            )
        )
        if cursor:
            try:
                payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                created_at = datetime.fromisoformat(payload["created_at"])
                run_id = UUID(payload["id"])
            except Exception as exc:
                raise ValueError("Invalid usage cursor.") from exc
            statement = statement.where(
                (BacktestRun.created_at < created_at)
                | ((BacktestRun.created_at == created_at) & (BacktestRun.id < run_id))
            )
        rows = list(
            self.session.scalars(
                statement.order_by(desc(BacktestRun.created_at), desc(BacktestRun.id)).limit(
                    limit + 1
                )
            )
        )
        page, extra = rows[:limit], len(rows) > limit
        next_cursor = None
        if extra and page:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps({"created_at": last.created_at.isoformat(), "id": str(last.id)}).encode()
            ).decode()
        return total, statuses, latest, page, next_cursor
