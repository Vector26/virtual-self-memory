from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click
import uvicorn

from virtual_self.config import get_settings
from virtual_self.persistence.db import health_check, init_pool, transaction
from virtual_self.persistence.db import fetch_one


@click.group()
def cli():
    """Virtual Self Memory administration."""


@cli.command("migrate")
def migrate():
    """Apply SQL migrations."""
    from virtual_self.persistence.db import init_pool
    from virtual_self.persistence.migrate import apply_migrations

    init_pool()
    apply_migrations()
    click.echo("migrations applied")


@cli.command("health")
def health():
    init_pool()
    ok = health_check()
    if not ok:
        raise SystemExit(1)
    click.echo("ok")


@cli.command("serve")
def serve():
    settings = get_settings()
    uvicorn.run(
        "virtual_self.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


@cli.command("worker")
def worker():
    from virtual_self.worker.runner import run_forever

    run_forever()


@cli.command("import-my-life")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--no-memories", is_flag=True, help="Import evidence only, skip memory promotion")
@click.option("--root", envvar="VSM_MY_LIFE_ROOT", default=None, type=click.Path(exists=True, file_okay=False))
def import_my_life(dry_run: bool, no_memories: bool, root: str | None):
    """Read-only import from local My Life markdown export."""
    from pathlib import Path

    from virtual_self.auth.credentials import AuthContext, resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.services.my_life_import import run_import

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    target = Path(root or settings.my_life_root)
    result = run_import(
        auth,
        target,
        dry_run=dry_run,
        promote_memories=not no_memories,
    )
    click.echo(result)


@cli.command("bootstrap")
@click.option("--force", is_flag=True, help="Re-synthesize facets from cursor_agent enrichments")
def bootstrap(force: bool):
    """Agent-synthesized personality profile (requires enrich-my-life)."""
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.services.personality import bootstrap_personality, resynthesize_personality

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = resynthesize_personality(auth) if force else bootstrap_personality(auth)
    click.echo(out)


@cli.command("enrich-my-life")
@click.option("--full", is_flag=True, help="Re-enrich all FULL pages, not only missing")
@click.option(
    "--thaw",
    is_flag=True,
    help="Run ollama-cold thaw for VSM_CHAT_MODEL if it is on Expansion HDD",
)
@click.option("--root", envvar="VSM_MY_LIFE_ROOT", default=None, type=click.Path(exists=True, file_okay=False))
@click.option(
    "--only-prefix",
    default=None,
    help="Only enrich FULL pages whose rel_path starts with this prefix",
)
def enrich_my_life(full: bool, thaw: bool, root: str | None, only_prefix: str | None):
    """Cursor-agent summarization + labelling for every FULL My Life page."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.services.enrichment_batch import enrich_corpus
    from virtual_self.services.memory_refresh import refresh_memories_from_enrichments

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    target = Path(root or settings.my_life_root)
    out = enrich_corpus(
        auth.subject_id,
        target,
        only_missing=not full,
        only_prefix=only_prefix,
        thaw_chat_model=thaw,
    )
    click.echo(f"enrichment: {out}")
    refresh = refresh_memories_from_enrichments(auth, target)
    click.echo(f"memory_refresh: {refresh}")


@cli.command("apply-enrichment-jsonl")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def apply_enrichment_jsonl(path: str):
    """Apply offline Cursor-agent JSONL enrichments."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.services.page_enrichment import load_enrichment_jsonl

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    n = load_enrichment_jsonl(Path(path), auth.subject_id)
    click.echo(f"applied {n} enrichments")


@cli.command("reindex")
@click.option("--full", is_flag=True, help="Re-embed all active memories, not only missing")
def reindex(full: bool):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.services.indexing import index_stats, reindex_subject

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    before = index_stats(auth.subject_id)
    click.echo(f"before: {before}")
    out = reindex_subject(auth.subject_id, only_missing=not full)
    after = index_stats(auth.subject_id)
    click.echo(f"run: {out}")
    click.echo(f"after: {after}")


@cli.command("mcp")
def mcp_server():
    from virtual_self.mcp.app import main

    main()


@cli.command("bake")
def bake():
    """Full build: enrich, import, personality, index — notifies when done."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.domain.errors import VirtualSelfError
    from virtual_self.observability.notify import NotifyCategory, notify_user
    from virtual_self.services.bake import run_bake

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    root = Path(settings.my_life_root)
    try:
        out = run_bake(auth, root)
        click.echo(out)
    except VirtualSelfError as exc:
        notify_user(f"Virtual Self build failed: {exc}", category=NotifyCategory.BUILD)
        raise SystemExit(1) from exc
    except Exception as exc:
        notify_user(
            "Virtual Self build failed unexpectedly. Check .state/bake.log on the server.",
            category=NotifyCategory.BUILD,
        )
        raise SystemExit(1) from exc


@cli.command("status")
def status():
    """Show build readiness (enrichment coverage, index, bake process)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.services.readiness import is_start_ready

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    root = Path(settings.my_life_root)
    ready = is_start_ready(auth.subject_id, root)
    pidfile = Path(__file__).resolve().parents[3] / ".state" / "bake.pid"
    baking = False
    if pidfile.is_file():
        baking = _pid_alive(pidfile.read_text().strip())
    click.echo({"readiness": ready, "bake_running": baking, "log": str(pidfile.parent / "bake.log")})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _pid_alive(pid_text: str) -> bool:
    import os

    try:
        os.kill(int(pid_text), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _chat_may_open(ready: dict) -> bool:
    """Chat if notes are enriched. Never block on a bake PID from another namespace."""
    return float(ready.get("enrichment_coverage") or 0) >= 0.95


def _launch_background_bake(pidfile: Path, log_path: Path) -> None:
    """Start in-process bake. Do not run bake-and-notify.sh (host PIDs / write perms)."""
    import sys

    if pidfile.is_file():
        old = pidfile.read_text(encoding="utf-8").strip()
        if old and _pid_alive(old):
            return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        logf = log_path.open("a", encoding="utf-8")
    except OSError:
        logf = open("/tmp/vsm-bake.log", "a", encoding="utf-8")
    with logf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "virtual_self.cli.main", "bake"],
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        pidfile.write_text(f"{proc.pid}\n", encoding="utf-8")
    except OSError:
        pass


@cli.command("start")
def start():
    """One command: build in background if needed, then chat as your virtual self."""
    import time
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.cli.chat_repl import run_chat
    from virtual_self.services.readiness import is_start_ready

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    root = Path(settings.my_life_root)
    state = _repo_root() / ".state"
    pidfile = state / "bake.pid"
    log_path = state / "bake.log"
    ready = is_start_ready(auth.subject_id, root)
    if not ready.get("fully_indexed"):
        from virtual_self.services.indexing import reindex_subject

        click.echo("Indexing new memories…")
        reindex_subject(auth.subject_id, only_missing=True)
        ready = is_start_ready(auth.subject_id, root)
    if not _chat_may_open(ready):
        _launch_background_bake(pidfile, log_path)
        click.echo("Building personality and index; waiting until ready…")
        for _ in range(40):
            time.sleep(15)
            ready = is_start_ready(auth.subject_id, root)
            if _chat_may_open(ready):
                break
            baking = pidfile.is_file() and _pid_alive(pidfile.read_text().strip())
            if not baking:
                raise click.ClickException(f"Build stopped but system not ready: {ready}")
            click.echo(f"Still building… {ready}")
        else:
            raise click.ClickException(f"Build timed out: {ready}")
    run_chat(auth)


@cli.command("product-up")
def product_up():
    """Deprecated — use virtual-self start."""
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "bake-and-notify.sh"
    subprocess.run(["bash", str(script)], check=False)


@cli.command("ingest-notion-authored")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--root", envvar="VSM_MY_LIFE_ROOT", default=None, type=click.Path(exists=True, file_okay=False))
def ingest_notion_authored(dry_run: bool, root: str | None):
    """Read-only walk of My Life export → authored_texts (Notion splitter)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.notion_authored_split import run_notion_authored_split

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_notion_authored_split(
        auth,
        Path(root) if root else settings.my_life_root,
        dry_run=dry_run,
    )
    click.echo(out)


@cli.command("ingest-takeout-authored")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option(
    "--mbox",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Loose Takeout mbox (default: resolve local inbox or rclone copy)",
)
def ingest_takeout_authored(dry_run: bool, mbox: str | None):
    """Takeout voice scope: gmail_sent mbox only (Keep/Meta absent per A-1)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.takeout_gmail_authored_ingest import run_takeout_gmail_authored_ingest

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_takeout_gmail_authored_ingest(
        auth,
        mbox_path=Path(mbox) if mbox else None,
        dry_run=dry_run,
    )
    click.echo(out)


@cli.command("ingest-chat-authored")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
def ingest_chat_authored(dry_run: bool):
    """Ingest vself chat user turns → authored_texts (register=casual_chat)."""
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.chat_authored_ingest import run_chat_authored_ingest

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_chat_authored_ingest(auth, dry_run=dry_run)
    click.echo(out)


@cli.command("run-llm-likeness-pass")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
def run_llm_likeness_pass_cmd(dry_run: bool):
    """Char n-gram LLM-likeness on author=self rows → review queue + excluded_reason."""
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.llm_likeness_pass import run_llm_likeness_pass

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_llm_likeness_pass(auth, dry_run=dry_run)
    click.echo(json.dumps(out, indent=2, default=str))


@cli.command("run-authorship-chain-audit")
@click.option(
    "--report-out",
    default=None,
    type=click.Path(dir_okay=False),
    help="JSON audit report (default: .state/authorship_chain_audit.json)",
)
@click.option("--reconcile-splits/--no-reconcile-splits", default=False)
@click.option("--reconcile-apply/--reconcile-dry-run", default=False)
def run_authorship_chain_audit_cmd(report_out: str | None, reconcile_splits: bool, reconcile_apply: bool):
    """Held-out vs eval/heldout consistency, corpus counts, 50-row authorship sample."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.authorship_chain_audit import run_authorship_chain_audit

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_authorship_chain_audit(
        auth,
        report_path=Path(report_out) if report_out else None,
        reconcile_splits=reconcile_splits,
        reconcile_dry_run=not reconcile_apply,
    )
    click.echo(json.dumps(out, indent=2, default=str))


@cli.command("run-authored-register-pass")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
def run_authored_register_pass_cmd(dry_run: bool):
    """Second-pass register + sensitivity over all authored_texts."""
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.authored_register_sensitivity_pass import (
        run_authored_register_sensitivity_pass,
    )

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_authored_register_sensitivity_pass(auth, dry_run=dry_run)
    click.echo(json.dumps(out, indent=2, default=str))


@cli.command("build-style-profiles")
@click.option(
    "--doc",
    "doc_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write docs/STYLE_FINGERPRINT.md (default path under repo)",
)
def build_style_profiles(doc_path: str | None):
    """Compute per-register stylometry from train-split authored_texts → style_profiles."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.style_profiles import run_build_style_profiles

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_build_style_profiles(auth, doc_path=Path(doc_path) if doc_path else None)
    click.echo(out)


@cli.command("snapshot-heldout")
@click.option("--root", envvar="VSM_MY_LIFE_ROOT", default=None, type=click.Path(exists=True, file_okay=False))
@click.option(
    "--out",
    default=None,
    type=click.Path(file_okay=False),
    help="Output directory (default: eval/heldout under project root)",
)
def snapshot_heldout(root: str | None, out: str | None):
    """Build eval/heldout self-text reference set from Notion export + chat user turns."""
    from pathlib import Path

    from virtual_self.config import get_settings
    from virtual_self.eval.heldout_snapshot import build_heldout_snapshot, validate_heldout_dir

    settings = get_settings()
    target = Path(out) if out else None
    manifest = build_heldout_snapshot(
        my_life_root=Path(root) if root else None,
        out_dir=target,
    )
    errors = validate_heldout_dir(target or Path(__file__).resolve().parents[3] / "eval" / "heldout")
    if errors:
        raise click.ClickException("; ".join(errors))
    click.echo(
        {
            "entry_count": manifest["entry_count"],
            "registers": manifest["registers"],
            "out": str((target or Path(__file__).resolve().parents[3] / "eval" / "heldout")),
        }
    )


@cli.command("time-backfill-pilot")
@click.option("--limit", default=200, show_default=True, help="Max pilot memories to process")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option(
    "--audit-out",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write JSON audit report (default: .state/time_backfill_pilot.json)",
)
def time_backfill_pilot(limit: int, dry_run: bool, audit_out: str | None):
    """Backfill temporal columns on a pilot slice of memories from evidence."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.time_backfill import run_time_backfill_pilot

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    report = run_time_backfill_pilot(
        auth,
        limit=limit,
        dry_run=dry_run,
        audit_path=Path(audit_out) if audit_out else None,
    )
    click.echo(report.to_dict())


@cli.command("time-backfill-full")
@click.option("--batch-size", default=500, show_default=True, help="Memories per DB transaction")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option(
    "--audit-out",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write JSON audit report (default: .state/time_backfill_full.json)",
)
def time_backfill_full(batch_size: int, dry_run: bool, audit_out: str | None):
    """Backfill temporal columns on all remaining memories from evidence."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.time_backfill import run_time_backfill_full

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    report = run_time_backfill_full(
        auth,
        batch_size=batch_size,
        dry_run=dry_run,
        audit_path=Path(audit_out) if audit_out else None,
    )
    click.echo(report.to_dict())


@cli.command("chapter-digest-synth")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--use-llm/--no-llm", default=False, help="Synthesize with host LLM (ollama/llama-server)")
@click.option(
    "--digest-status",
    type=click.Choice(["draft", "proposed"], case_sensitive=False),
    default="draft",
    show_default=True,
    help="Persisted digest status (never active)",
)
@click.option("--replace/--no-replace", default=False, help="Replace existing digest for same chapter+status")
@click.option(
    "--audit-out",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write JSON audit report (default: .state/chapter_guided_digests.json)",
)
def chapter_digest_synth(
    dry_run: bool,
    use_llm: bool,
    digest_status: str,
    replace: bool,
    audit_out: str | None,
):
    """Guided per-chapter digests with memory-id evidence (TR-2, draft/proposed only)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.guided_chapter_digest import run_chapter_guided_digests

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    root = Path(__file__).resolve().parents[3]
    out = Path(audit_out) if audit_out else root / ".state" / "chapter_guided_digests.json"
    report = run_chapter_guided_digests(
        auth,
        dry_run=dry_run,
        use_llm=use_llm,
        digest_status=digest_status.lower(),  # type: ignore[arg-type]
        replace_existing=replace,
        audit_path=out,
    )
    click.echo(report.to_dict())


@cli.command("chapter-propose")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--use-llm/--no-llm", default=False, help="Name chapters with small LLM (GPU)")
@click.option("--min-proposals", default=5, show_default=True)
@click.option("--max-proposals", default=15, show_default=True)
@click.option(
    "--audit-out",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write JSON audit report (default: .state/chapter_proposals.json)",
)
def chapter_propose(
    dry_run: bool,
    use_llm: bool,
    min_proposals: int,
    max_proposals: int,
    audit_out: str | None,
):
    """Propose life chapters from dated memories (status=proposed, not active)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.chapter_proposals import run_chapter_proposals

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    root = Path(__file__).resolve().parents[3]
    out = Path(audit_out) if audit_out else root / ".state" / "chapter_proposals.json"
    report = run_chapter_proposals(
        auth,
        dry_run=dry_run,
        use_llm=use_llm,
        min_proposals=min_proposals,
        max_proposals=max_proposals,
        audit_path=out,
    )
    click.echo(report.to_dict())


@cli.command("build-voice-exemplars")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--embed/--no-embed", default=True, help="Fill embedding column via Ollama (skip in CI)")
@click.option(
    "--persona-eval",
    "persona_eval_path",
    default=None,
    type=click.Path(dir_okay=False, exists=True),
    help="Persona eval JSON for eval_finding anti-patterns (default: .state/persona_eval/20261015T113715Z-*.json)",
)
@click.option(
    "--audit-out",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write JSON audit report (default: .state/voice_exemplars_build.json)",
)
def build_voice_exemplars(dry_run: bool, embed: bool, persona_eval_path: str | None, audit_out: str | None):
    """Build voice_exemplars bank (train, sensitivity<3) + slop anti-pattern seeds (V-3)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.voice_exemplars import run_build_voice_exemplars

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_build_voice_exemplars(
        auth,
        dry_run=dry_run,
        embed=embed,
        persona_eval_path=Path(persona_eval_path) if persona_eval_path else None,
        audit_path=Path(audit_out) if audit_out else None,
    )
    click.echo(json.dumps(out, indent=2, default=str))


@cli.command("propose-trait-values-profile")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--notify/--no-notify", default=True, help="configured notifier [Self] on --apply")
@click.option("--use-llm/--no-llm", default=False, help="Refine profile with host LLM")
@click.option(
    "--digest-status",
    type=click.Choice(["draft", "proposed"], case_sensitive=False),
    default="draft",
    show_default=True,
)
@click.option(
    "--doc",
    "doc_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write docs/TRAITS_VALUES_PROFILE.md",
)
@click.option(
    "--audit-out",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write JSON audit (default: .state/trait_values_profile.json on --apply)",
)
def propose_trait_values_profile(
    dry_run: bool,
    notify: bool,
    use_llm: bool,
    digest_status: str,
    doc_path: str | None,
    audit_out: str | None,
):
    """Build trait/values profile from chapter digests → personality proposals (TR-3, pending)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.trait_values_profile import propose_trait_values_profile as run_propose

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    root = Path(__file__).resolve().parents[3]
    audit = Path(audit_out) if audit_out else (root / ".state" / "trait_values_profile.json")
    out = run_propose(
        auth,
        dry_run=dry_run,
        notify=notify,
        use_llm=use_llm,
        digest_status=digest_status.lower(),
        doc_path=Path(doc_path) if doc_path else None,
        audit_path=None if dry_run else audit,
    )
    click.echo(json.dumps(out, indent=2, default=str))


@cli.command("propose-voice-card")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--notify/--no-notify", default=True, help="configured notifier [Self] on --apply")
@click.option(
    "--doc",
    "doc_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write docs/VOICE_CARD.md (default path under repo)",
)
def propose_voice_card(dry_run: bool, notify: bool, doc_path: str | None):
    """Build evidence-linked voice card v2 → personality proposal (pending, not active)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.voice_cards import propose_voice_card_v2

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = propose_voice_card_v2(
        auth,
        dry_run=dry_run,
        notify=notify,
        doc_path=Path(doc_path) if doc_path else None,
    )
    click.echo(json.dumps(out, indent=2, default=str))


@cli.command("run-mode-reflection")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--notify/--no-notify", default=True, help="configured notifier digest on --apply")
@click.option(
    "--force/--respect-schedule",
    default=False,
    help="Run even when interval/turn threshold not met",
)
def run_mode_reflection_cmd(dry_run: bool, notify: bool, force: bool):
    """Per-mode chat reflection → pending personality proposals (E-1)."""
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.config import get_settings
    from virtual_self.services.mode_reflection import enqueue_persona_reflect, run_mode_reflection

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    if dry_run:
        out = run_mode_reflection(auth, dry_run=True, notify=False)
    elif force:
        out = run_mode_reflection(auth, dry_run=False, notify=notify)
    else:
        jid = enqueue_persona_reflect(auth, reason="manual", force=False)
        if jid is None:
            out = {"enqueued": False, "reason": "schedule_not_due"}
        else:
            out = {"enqueued": True, "job_id": str(jid)}
    click.echo(json.dumps(out, indent=2, default=str))


@cli.command("propose-mode-cards")
@click.option("--dry-run/--apply", default=True, help="Plan only unless --apply")
@click.option("--notify/--no-notify", default=True, help="configured notifier [Mode] labels on --apply")
@click.option(
    "--digest-status",
    type=click.Choice(["draft", "proposed"], case_sensitive=False),
    default="draft",
    show_default=True,
)
@click.option(
    "--doc",
    "doc_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write docs/MODE_CARDS.md",
)
def propose_mode_cards_cmd(
    dry_run: bool,
    notify: bool,
    digest_status: str,
    doc_path: str | None,
):
    """Build four evidence-linked mode cards → pending personality proposals (TR-4)."""
    from pathlib import Path

    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.mode_cards import propose_mode_cards as run_propose

    settings = get_settings()
    if settings.bootstrap_token is None:
        raise click.ClickException("set VSM_BOOTSTRAP_TOKEN")
    init_pool()
    auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
    out = run_propose(
        auth,
        dry_run=dry_run,
        notify=notify,
        digest_status=digest_status.lower(),
        doc_path=Path(doc_path) if doc_path else None,
    )
    click.echo(json.dumps(out, indent=2, default=str))


@cli.command("bootstrap-status")
def bootstrap_status():
    init_pool()
    with transaction() as conn:
        subj = fetch_one(conn, "SELECT COUNT(*) AS n FROM subjects")
        cred = fetch_one(conn, "SELECT COUNT(*) AS n FROM credentials WHERE revoked_at IS NULL")
    click.echo(f"subjects={subj['n']} active_credentials={cred['n']}")


from virtual_self.archive.cli import archive_cli  # noqa: E402

cli.add_command(archive_cli, name="archive")


if __name__ == "__main__":
    cli()
