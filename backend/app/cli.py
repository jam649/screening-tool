import json
import shutil
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, cast

import typer

from app.config import Config
from app.csl.parser import HASH_RULE_VERSION
from app.csl.service import version_payload
from app.database import create_database_engine, migrate, session_factory
from app.models import CSLVersion, ScreeningRun
from app.retention.service import purge_plan
from app.scheduler.service import register_default_connectors, run_operation
from app.screening.engine import (
    run_payload,
)
from app.screening.hash_upgrade import upgrade_hash_rules
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
        with session_factory(engine, config)() as db:
            validate_key_binding(db, config)
            result = upgrade_hash_rules(db)
            typer.echo(json.dumps({"hash_rule_version": HASH_RULE_VERSION, **asdict(result)}))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()


@contextmanager
def operator_database():
    config = Config()
    engine = create_database_engine(config)
    try:
        migrate(engine)
        factory = session_factory(engine, config)
        register_default_connectors()
        with factory() as db:
            validate_key_binding(db, config)
        yield config, factory
    finally:
        engine.dispose()


def require_success(result):
    if result["status"] != "success":
        typer.echo(json.dumps(result, default=str), err=True)
        raise typer.Exit(1)
    return result["outcome"]


@csl_cli.command("refresh")
def csl_refresh(
    file: Annotated[
        Path | None, typer.Option(help="Use a complete local CSL JSON fixture.")
    ] = None,
):
    """Download and apply the CSL, then process its durable comparison tasks."""
    if file is not None and not file.is_file():
        typer.echo("Unable to read the complete CSL file", err=True)
        raise typer.Exit(1)
    with operator_database() as (config, factory):
        outcome = require_success(run_operation(factory, config, "csl_refresh", csl_file=file))
        with factory() as db:
            version = db.get(CSLVersion, outcome["version_id"])
            typer.echo(json.dumps({**outcome, "version": version_payload(version)}))


@cli.command("screen")
def screen(
    all: Annotated[bool, typer.Option("--all", help="Screen every active party.")] = False,
    party: Annotated[str | None, typer.Option(help="Screen one party UUID.")] = None,
    changed: Annotated[bool, typer.Option(help="Process durable scoped screenings.")] = False,
):
    """Run the same protected comparison operation as HTTP and scheduling."""
    if sum((all, party is not None, changed)) != 1:
        raise typer.BadParameter("Choose exactly one of --all, --party, or --changed")
    operation = "screen_changed" if changed else "screen_party" if party else "screen_all"
    with operator_database() as (config, factory):
        outcome = require_success(run_operation(factory, config, operation, party_id=party))
        with factory() as db:
            typer.echo(
                json.dumps(
                    [run_payload(db.get(ScreeningRun, run_id)) for run_id in outcome["run_ids"]]
                )
            )


@cli.command("sync")
def sync(
    source: Annotated[str, typer.Argument(help="API source UUID.")],
    full: Annotated[
        bool, typer.Option("--full", help="Reconcile the complete company book.")
    ] = False,
):
    """Poll an API source; use Sources in the browser for a CSV full snapshot."""
    with operator_database() as (config, factory):
        result = run_operation(
            factory,
            config,
            "source_sync",
            source_id=source,
            parameters={"mode": "full"} if full else None,
        )
        require_success(result)
        typer.echo(json.dumps(result, default=str))


report_cli = typer.Typer(help="Send the frozen morning report.")
alert_cli = typer.Typer(help="Deliver committed immediate-alert and retry batches.")
retention_cli = typer.Typer(help="Preview or apply bounded old-evidence retention.")
monitor_cli = typer.Typer(help="Run a complete monitoring cycle.")
cli.add_typer(report_cli, name="report")
cli.add_typer(alert_cli, name="alert")
cli.add_typer(retention_cli, name="retention")
cli.add_typer(monitor_cli, name="monitor")


def print_operation(operation, **kwargs):
    with operator_database() as (config, factory):
        result = run_operation(factory, config, operation, **kwargs)
        require_success(result)
        typer.echo(json.dumps(result, default=str))
        delivery = result.get("outcome", {}).get("delivery", {})
        if (
            delivery.get("failed")
            or delivery.get("uncertain")
            or delivery.get("busy")
            or delivery.get("error_code")
        ):
            raise typer.Exit(1)


@report_cli.command("send")
def report_send():
    """Freeze the report and deliver one message per configured recipient."""
    print_operation("morning_report")


@alert_cli.command("send")
def alert_send():
    """Retry eligible deliveries without resending successful recipients."""
    print_operation("alert_dispatch")


@retention_cli.command("preview")
def retention_preview():
    """Show eligibility, holds, dependencies and the exact proposed evidence IDs."""
    with operator_database() as (_, factory):
        with factory() as db:
            typer.echo(json.dumps(purge_plan(db)))


@retention_cli.command("apply")
def retention_apply():
    """Apply the configured policy with a permanent certificate; disabled by default."""
    print_operation("retention_apply")


@monitor_cli.command("cycle")
def monitor_cycle(
    file: Annotated[
        Path | None, typer.Option(help="Complete CSL fixture, otherwise official bulk.")
    ] = None,
):
    """Refresh the list, poll API sources and consume pending changed comparisons."""
    print_operation("monitor_cycle", csl_file=file)


export_cli = typer.Typer(help="Download original recorded evidence; always choose an output path.")
cli.add_typer(export_cli, name="export")


@export_cli.callback(invoke_without_command=True)
def export_evidence(
    ctx: typer.Context,
    party: Annotated[str | None, typer.Option(help="Export one party UUID.")] = None,
    run: Annotated[str | None, typer.Option(help="Export one screening run UUID.")] = None,
    all: Annotated[bool, typer.Option("--all", help="Export the complete audit ZIP.")] = False,
    format: Annotated[str, typer.Option(help="pdf, csv, or zip for --all.")] = "pdf",
    output: Annotated[Path | None, typer.Option(help="Explicit destination file.")] = None,
):
    if ctx.invoked_subcommand is not None:
        return
    if sum((party is not None, run is not None, all)) != 1 or output is None:
        raise typer.BadParameter("Choose --party, --run, or --all and an explicit --output file")
    if format not in {"pdf", "csv", "zip"} or (all != (format == "zip")):
        raise typer.BadParameter("Party/run use pdf or csv; --all uses --format zip")
    from app.reports.export import Format, export_full_zip, export_party, export_run

    artifact = None
    try:
        with operator_database() as (_, factory):
            with factory() as db:
                if all:
                    artifact = export_full_zip(db)
                elif party is not None:
                    artifact = export_party(db, party, cast(Format, format))
                else:
                    assert run is not None
                    artifact = export_run(db, run, cast(Format, format))
                output.parent.mkdir(parents=True, exist_ok=True)
                if artifact.body is not None:
                    output.write_bytes(artifact.body)
                else:
                    assert artifact.path is not None
                    shutil.copyfile(artifact.path, output)
                typer.echo(
                    json.dumps(
                        {"output": str(output), "sha256": artifact.sha256, "size": artifact.size}
                    )
                )
    finally:
        if artifact is not None:
            artifact.close()


@export_cli.command("training-pairs")
def export_training_pairs_cli(
    output: Annotated[Path, typer.Option(help="Explicit destination for human training-pair CSV.")],
):
    """Export retained human labels and their original immutable comparison snapshots."""
    from app.reports.training import export_training_pairs

    artifact = None
    try:
        with operator_database() as (_, factory):
            with factory() as db:
                artifact = export_training_pairs(db)
                output.parent.mkdir(parents=True, exist_ok=True)
                assert artifact.body is not None
                output.write_bytes(artifact.body)
                typer.echo(
                    json.dumps(
                        {"output": str(output), "sha256": artifact.sha256, "size": artifact.size}
                    )
                )
    finally:
        if artifact is not None:
            artifact.close()


if __name__ == "__main__":
    cli()
