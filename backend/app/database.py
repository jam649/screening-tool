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
    def sqlite_settings(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

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


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
