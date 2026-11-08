from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Metadata root for future PostgreSQL models."""


# Import model modules so Alembic sees their tables through Base.metadata.
from app.db import models as _models  # noqa: E402, F401
