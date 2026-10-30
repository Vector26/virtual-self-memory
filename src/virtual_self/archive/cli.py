from __future__ import annotations

import json
from pathlib import Path

import click

from virtual_self.archive import ledger
from virtual_self.archive.dataset import export_datasets
from virtual_self.archive.olap import olap_refresh
from virtual_self.archive.promote import promote_backfill_from_parquet
from virtual_self.archive.run import archive_report, run_flow, run_pipeline
from virtual_self.archive.sync import sync_drive_and_inbox, sync_loop_forever
from virtual_self.archive.takeout_inventory import write_inventory_markdown
from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import init_pool, transaction


def _auth():
    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    return resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")


@click.group()
def archive_cli():
    """Personal data archive (Takeout / Meta inbox)."""


@archive_cli.command("sync")
def archive_sync():
    """Pull stable Takeout zips from Drive; scan inbox."""
    init_pool()
    auth = _auth()
    out = sync_drive_and_inbox(auth.subject_id)
    click.echo(out)


@archive_cli.command("run")
@click.option("--no-sync", is_flag=True, help="Skip Drive pull")
@click.option("--no-llm", is_flag=True, help="Parse only, skip LLM layers")
@click.option("--no-supervisor", is_flag=True, help="Skip supervisor/specialist classify")
def archive_run(no_sync: bool, no_llm: bool, no_supervisor: bool):
    import os

    init_pool()
    auth = _auth()
    if no_llm:
        os.environ["VSM_ARCHIVE_LLM_ENABLED"] = "false"
    if no_supervisor:
        os.environ["VSM_ARCHIVE_SUPERVISOR_ENABLED"] = "false"
    if no_llm or no_supervisor:
        get_settings.cache_clear()
    out = run_pipeline(auth, sync_first=not no_sync)
    click.echo(json.dumps(out, indent=2, default=str))


@archive_cli.command("flow")
@click.option("--no-llm", is_flag=True, help="Parse only, skip LLM layers")
@click.option("--no-supervisor", is_flag=True, help="Skip supervisor/specialist classify")
def archive_flow(no_llm: bool, no_supervisor: bool):
    """Stream Takeout: download one zip → process → delete → repeat."""
    import os

    from virtual_self.config import get_settings

    os.environ.setdefault("VSM_ARCHIVE_STREAMING", "true")
    os.environ.setdefault("VSM_ARCHIVE_DELETE_ZIP_AFTER", "true")
    if no_llm:
        os.environ["VSM_ARCHIVE_LLM_ENABLED"] = "false"
    if no_supervisor:
        os.environ["VSM_ARCHIVE_SUPERVISOR_ENABLED"] = "false"
    get_settings.cache_clear()
    init_pool()
    auth = _auth()
    out = run_flow(auth, analyze=not no_llm, promote=not no_llm)
    click.echo(json.dumps(out, indent=2, default=str))


@archive_cli.command("reconcile")
@click.option("--dry-run", is_flag=True, help="List phantoms only; no DB writes or deletes")
@click.option(
    "--delete",
    is_flag=True,
    help="Remove phantom rows (requires a live run; not allowed with --dry-run)",
)
@click.option("--no-phantoms", is_flag=True, help="Skip phantom/test row cleanup")
@click.option("--no-inflight", is_flag=True, help="Skip stuck unpacked/parsed reset")
def archive_reconcile(
    dry_run: bool,
    delete: bool,
    no_phantoms: bool,
    no_inflight: bool,
):
    """Mark phantom ledger rows failed; reset stuck in-flight rows when zip exists."""
    if delete and dry_run:
        raise click.ClickException("--delete cannot be used with --dry-run")
    init_pool()
    with transaction() as conn:
        stats = ledger.reconcile_ledger(
            conn,
            mark_phantoms=not no_phantoms,
            reset_inflight=not no_inflight,
            dry_run=dry_run,
            delete_phantoms=delete,
        )
    click.echo(json.dumps(stats, indent=2, default=str))


@archive_cli.command("status")
def archive_status():
    init_pool()
    with transaction() as conn:
        click.echo(json.dumps(ledger.status_summary(conn), indent=2))


@archive_cli.command("report")
@click.option("--limit", default=20, show_default=True)
def archive_report_cmd(limit: int):
    init_pool()
    auth = _auth()
    click.echo(json.dumps(archive_report(auth, limit=limit), indent=2, default=str))


@archive_cli.command("export-dataset")
def archive_export_dataset():
    init_pool()
    auth = _auth()
    click.echo(export_datasets(auth=auth))


@archive_cli.command("promote-backfill")
@click.option("--dry-run", is_flag=True, help="Count only; do not insert memories")
@click.option("--limit", type=int, default=None, help="Max parquet rows to scan")
def archive_promote_backfill(dry_run: bool, limit: int | None):
    """Promote OLAP parquet events into scope=archive memories (idempotent)."""
    init_pool()
    auth = _auth()
    out = promote_backfill_from_parquet(auth, dry_run=dry_run, limit=limit)
    click.echo(json.dumps(out, indent=2, default=str))


@archive_cli.command("sync-loop")
def archive_sync_loop():
    """Long-running sync poller (for archive-sync service)."""
    init_pool()
    sync_loop_forever()


@archive_cli.command("olap-refresh")
def archive_olap_refresh():
    """Rebuild DuckDB views over all parquet event batches."""
    click.echo(json.dumps(olap_refresh(), indent=2, default=str))


@archive_cli.command("takeout-inventory")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("docs/TAKEOUT_INVENTORY.md"),
    show_default=True,
    help="Write markdown inventory (central-directory scan only)",
)
def archive_takeout_inventory(out_path: Path):
    """List product folders inside granted Takeout zips (no unpack)."""
    from virtual_self.archive.takeout_inventory import build_full_inventory, render_markdown

    def _progress(name: str) -> None:
        click.echo(f"scanning {name}", err=True)

    rows = build_full_inventory(progress=_progress)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(rows), encoding="utf-8")
    click.echo(str(out_path.resolve()))
