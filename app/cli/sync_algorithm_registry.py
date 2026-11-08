from app.application.algorithms import sync_registry
from app.core.config import get_settings
from app.db.session import create_session_factory


def main() -> None:
    session = create_session_factory(get_settings())()
    try:
        changed = sync_registry(session)
        print(f"algorithm registry synchronized; {changed} record(s) changed")
    finally:
        session.close()


if __name__ == "__main__":
    main()
