from alembic import context
from app.config import Config
from app.database import create_database_engine
from app.models import Base


def run_migrations():
    connection = context.config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_database_engine(Config())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
