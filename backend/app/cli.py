import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from app.config import Config
from app.csl.fetch import CSLDownloadError
from app.csl.parser import HASH_RULE_VERSION, CSLValidationError
from app.csl.service import CSLBusyError, configured_api_key, refresh_csl, version_payload
from app.database import create_database_engine, migrate, session_factory
from app.screening.engine import (
    ScreeningBusyError,
    process_pending_tasks,
    run_payload,
    run_screening,
)
from app.screening.hash_upgrade import upgrade_hash_rules
from app.screening.settings import ScreeningValidationError
from app.security import validate_key_binding

cli = typer.Typer(help="Operate screening-tool from the command line.")


@cli.callback()
def main():
    """screening-tool operator commands."""


@cli.command()
def init():
    """Migrate the database; create the first Admin in the browser setup wizard."""
    engine = create_database_engine(Config())
    migrate(engine)
    engine.dispose()
    typer.echo("Database ready. Open the app to create the first Admin.")


csl_cli = typer.Typer(help="Manage the official Consolidated Screening List.")
cli.add_typer(csl_cli, name="csl")

hashes_cli = typer.Typer(help="Explicitly upgrade current matching cache hashes.")
cli.add_typer(hashes_cli, name="hashes")


@hashes_cli.command("upgrade")
def hashes_upgrade():
    """Rehash current caches under v2 and enqueue a full screening; preserve history."""
    config = Config()
    engine = create_database_engine(config)
    try:
        migrate(engine)
        with session_factory(engine)() as db:
            validate_key_binding(db, config)
            result = upgrade_hash_rules(db)
            typer.echo(json.dumps({"hash_rule_version": HASH_RULE_VERSION, **asdict(result)}))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()


@csl_cli.command("refresh")
def csl_refresh(
    file: Annotated[
        Path | None, typer.Option(help="Use a complete local CSL JSON fixture.")
    ] = None,
):
    """Download, validate and atomically apply the official bulk CSL."""
    config = Config()
    engine = create_database_engine(config)
    try:
        migrate(engine)
        with session_factory(engine)() as db:
            result = refresh_csl(db, file=file, api_key=configured_api_key(db, config))
            typer.echo(
                json.dumps(
                    {
                        "version": version_payload(result.version),
                        "fetch_id": result.fetch.id,
                        "changed": result.changed,
                        "pending_task_id": result.pending_task_id,
                        "closed_hits": result.closed_hits,
                    }
                )
            )
    except (CSLBusyError, CSLValidationError, CSLDownloadError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()


@cli.command("screen")
def screen(
    all: Annotated[bool, typer.Option("--all", help="Screen every active party.")] = False,
    party: Annotated[str | None, typer.Option(help="Screen one party UUID.")] = None,
    changed: Annotated[bool, typer.Option(help="Process durable scoped screenings.")] = False,
):
    """Screen deterministic candidates and retain immutable comparison evidence."""
    if sum((all, party is not None, changed)) != 1:
        raise typer.BadParameter("Choose exactly one of --all, --party, or --changed")
    engine = create_database_engine(Config())
    try:
        migrate(engine)
        with session_factory(engine)() as db:
            runs = (
                process_pending_tasks(db)
                if changed
                else [run_screening(db, party_ids=[party] if party else None)]
            )
            typer.echo(json.dumps([run_payload(run) for run in runs]))
    except (ScreeningValidationError, ScreeningBusyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()


if __name__ == "__main__":
    cli()
