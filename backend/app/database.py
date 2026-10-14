from pathlib import Path

from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app import audit  # noqa: F401 -- enforce audit guards for CLI and API database sessions
from app.config import Config


def create_database_engine(config: Config) -> Engine:
    database = make_url(config.database_url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        config.database_url, connect_args={"check_same_thread": False}, hide_parameters=True
    )

    @event.listens_for(engine, "connect")
    def sqlite_settings(connection, record):
        capability: set[tuple[str, str]] = set()
        record.info["retention_delete_capability"] = capability
        connection.create_function(
            "retention_delete_allowed",
            2,
            lambda table, row_id: int((str(table), str(row_id)) in capability),
        )
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    @event.listens_for(engine, "checkin")
    def clear_retention_capability(connection, record):
        record.info.get("retention_delete_capability", set()).clear()

    @event.listens_for(engine, "rollback")
    def clear_retention_on_rollback(connection):
        connection.info.get("retention_delete_capability", set()).clear()

    return engine


def migrate(engine: Engine) -> None:
    root = Path(__file__).resolve().parents[1]
    if (root / "alembic.ini").is_file():
        config_path, scripts = root / "alembic.ini", root / "alembic"
    else:
        config_path, scripts = root / "app" / "alembic.ini", root / "app" / "migrations"
    config = AlembicConfig(str(config_path))
    config.set_main_option("script_location", str(scripts))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def session_factory(engine: Engine, config: Config | None = None) -> sessionmaker[Session]:
    info = {"config": config} if config is not None else {}
    return sessionmaker(engine, expire_on_commit=False, info=info)
