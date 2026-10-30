"""TR-7: trait fidelity + mode adherence eval suite."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.eval.harness import run_trait_mode_eval
from virtual_self.eval.mode_adherence_metrics import (
    check_council_format,
    check_no_mode_bleed,
    check_requested_mode_adherence,
    summarize_mode_adherence,
)
from virtual_self.eval.mode_adherence_scenarios import load_mode_adherence_scenarios
from virtual_self.eval.trait_fidelity_metrics import (
    assert_report_has_no_raw_scores,
    infer_trait_band_from_reply,
    profile_fidelity_vs_ipip,
    summarize_trait_fidelity,
)
from virtual_self.eval.trait_interview_scenarios import (
    SUITE_VERSION,
    load_trait_interview_scenarios,
)
from virtual_self.persistence.db import connection, execute, fetch_one, transaction
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.trait_values_profile import (
    TraitClaim,
    TraitValuesProfile,
    bands_within_tolerance,
    build_trait_values_profile,
)

ROOT = Path(__file__).resolve().parents[1]


def test_suite_version_and_counts():
    assert SUITE_VERSION == "trait_mode_eval_v0"
    assert len(load_trait_interview_scenarios()) == 8
    assert len(load_mode_adherence_scenarios()) == 10


def test_bands_within_tolerance_adjacent():
    assert bands_within_tolerance("moderate", "low")
    assert bands_within_tolerance("moderate", "elevated")
    assert not bands_within_tolerance("low", "elevated")


def test_infer_trait_band_never_numeric():
    band = infer_trait_band_from_reply(
        "openness",
        "I'd try a weird experiment and hunt for a new angle before defaulting.",
    )
    assert band in ("low", "moderate", "elevated")


def test_mode_adherence_fixture_replies():
    vec = check_requested_mode_adherence(
        "Concise evidence: compare the two synthetic plans before choosing the next action.",
        "Analyst",
    )
    assert vec.passed
    prom = check_requested_mode_adherence(
        "Supportive context: explain the fictional migration in patient, plain language.",
        "Guide",
    )
    assert prom.passed
    council = check_council_format(
        "**[Analyst]** grind.\n**[Guide]** breathe.\n**[Present self]** both can be true."
    )
    assert council.passed
    bleed = check_no_mode_bleed("Tonight I'm laying low — no big mode energy.")
    assert bleed.passed


def test_profile_fidelity_qualitative_only(tmp_path: Path):
    ipip = tmp_path / "ipip.json"
    ipip.write_text(json.dumps({"facet_order_30": [2.0] * 30}), encoding="utf-8")
    profile = TraitValuesProfile(
        profile_version=1,
        generated_at="2026-01-01T00:00:00+00:00",
        digest_count=1,
        traits=[
            TraitClaim("openness", "x", 0.35, 0.2, 0.5, ["m1"]),
            TraitClaim("conscientiousness", "x", 0.35, 0.2, 0.5, ["m2"]),
            TraitClaim("extraversion", "x", 0.35, 0.2, 0.5, ["m3"]),
            TraitClaim("agreeableness", "x", 0.35, 0.2, 0.5, ["m4"]),
            TraitClaim("neuroticism", "x", 0.55, 0.4, 0.7, ["m5"]),
        ],
        values=[],
        conflict_rules=[],
        interaction_policy=[],
        ipip_calibration_note="bands only",
        journaling_bias_notes=[],
    )
    checks = profile_fidelity_vs_ipip(profile, ipip_path=ipip)
    summary = summarize_trait_fidelity(checks)
    blob = json.dumps(summary)
    assert_report_has_no_raw_scores(blob)
    assert summary["traits_compared"] == 5


def test_run_trait_mode_eval_dry_run(tmp_path: Path):
    report = run_trait_mode_eval(dry_run=True, out_dir=tmp_path)
    assert report["dry_run"] is True
    assert report["scenario_count"] == 18
    assert report["reply_count"] == 0
    body = Path(report["report_path"]).read_text(encoding="utf-8")
    assert_report_has_no_raw_scores(body)


def test_run_eval_cli_trait_mode_dry_run(tmp_path: Path):
    script = ROOT / "eval" / "run_eval.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "trait_mode",
            "--dry-run",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1


def _seed_all_approved_modes(conn, subject_id: UUID) -> None:
    for name, register in (
        ("Analyst", "blunt"),
        ("Guide", "gentle"),
        ("Mediator", "measured"),
        ("Explorer", "playful"),
    ):
        mode_id = uuid7()
        ver_id = uuid7()
        execute(
            conn,
            """
            INSERT INTO persona_modes (
              id, subject_id, name, role_summary, triggers, voice_delta, values_delta, status
            ) VALUES (
              %s, %s, %s, %s, '[]'::jsonb,
              %s::jsonb, '{"emphasis":"test"}'::jsonb, 'active'
            )
            """,
            (
                mode_id,
                subject_id,
                name,
                f"{name} test card.",
                json.dumps({"register": register, "notes": register}),
            ),
        )
        execute(
            conn,
            """
            INSERT INTO persona_mode_versions (
              id, mode_id, version, role_summary, triggers, voice_delta, values_delta, status
            ) VALUES (
              %s, %s, 1, %s, '[]'::jsonb, %s::jsonb,
              '{"emphasis":"test"}'::jsonb, 'active'
            )
            """,
            (
                ver_id,
                mode_id,
                f"{name} test card.",
                json.dumps({"register": register, "notes": register}),
            ),
        )
        execute(
            conn,
            "UPDATE persona_modes SET current_version_id = %s WHERE id = %s",
            (ver_id, mode_id),
        )


def _insert_dated_memory(conn, auth, *, content: str, when: datetime) -> UUID:
    mem_id = uuid7()
    ver_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
        VALUES (%s, 'memory', %s, 1)
        """,
        (mem_id, auth.subject_id),
    )
    execute(
        conn,
        """
        INSERT INTO memories (
          id, subject_id, kind, scope, time_precision, valid_from, authored_at
        ) VALUES (%s, %s, 'fact', 'chat', 'day', %s, %s)
        """,
        (mem_id, auth.subject_id, when, when),
    )
    execute(
        conn,
        """
        INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
        VALUES (%s, 'memory_version', %s, 1)
        """,
        (ver_id, auth.subject_id),
    )
    execute(
        conn,
        """
        INSERT INTO memory_versions (id, memory_id, version, content, status)
        VALUES (%s, %s, 1, %s, 'active')
        """,
        (ver_id, mem_id, content),
    )
    execute(conn, "UPDATE memories SET current_version_id = %s WHERE id = %s", (ver_id, mem_id))
    return mem_id


def _insert_draft_digest(conn, auth, *, memory_id: UUID) -> None:
    strand = fetch_one(conn, "SELECT id FROM life_strands WHERE slug = 'place'")
    chapter_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapters (
          id, subject_id, strand_id, title, summary, status,
          valid_from, valid_to, time_precision,
          valid_from_precision, valid_to_precision, edge_fuzz_days
        ) VALUES (
          %s, %s, %s, %s, %s, 'proposed',
          %s, NULL, 'month', 'month', 'unknown', 0
        )
        """,
        (
            chapter_id,
            auth.subject_id,
            strand["id"],
            "TR-7 era",
            "chapter",
            datetime(2025, 6, 1, tzinfo=timezone.utc),
        ),
    )
    digest_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapter_guided_digests (
          id, subject_id, chapter_id, status, prompt_version, journaling_bias_note, metadata
        ) VALUES (%s, %s, %s, 'draft', 'test', NULL, '{}'::jsonb)
        """,
        (digest_id, auth.subject_id, chapter_id),
    )
    item_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapter_guided_digest_items (
          id, digest_id, step, layer, text, stance, sort_order
        ) VALUES (%s, %s, 'profile_sentence', 'values', %s, 'actual', 0)
        """,
        (item_id, digest_id, "Honesty beats hand-wavy authority."),
    )
    execute(
        conn,
        """
        INSERT INTO life_chapter_guided_digest_evidence (
          id, item_id, memory_id, relationship
        ) VALUES (%s, %s, %s, 'supports')
        """,
        (uuid7(), item_id, memory_id),
    )


def test_mode_metrics_with_seeded_approved_modes(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    with connection() as conn:
        conn.execute("UPDATE persona_modes SET current_version_id = NULL WHERE subject_id = %s", (auth.subject_id,))
        conn.execute(
            """
            DELETE FROM persona_mode_evidence
            WHERE mode_version_id IN (
              SELECT v.id FROM persona_mode_versions v
              JOIN persona_modes m ON m.id = v.mode_id
              WHERE m.subject_id = %s
            )
            """,
            (auth.subject_id,),
        )
        conn.execute(
            "DELETE FROM persona_mode_versions WHERE mode_id IN (SELECT id FROM persona_modes WHERE subject_id = %s)",
            (auth.subject_id,),
        )
        conn.execute("DELETE FROM persona_modes WHERE subject_id = %s", (auth.subject_id,))
        _seed_all_approved_modes(conn, auth.subject_id)
        conn.commit()

    checks = []
    for sc in load_mode_adherence_scenarios():
        if sc.kind == "requested_mode" and sc.expected_mode:
            reply = f"Stay {sc.expected_mode.lower()} register: gentle blunt playful measured".replace(
                "analyst", "blunt"
            )
            if sc.expected_mode == "Analyst":
                reply = "Concise evidence supports one practical action."
            elif sc.expected_mode == "Guide":
                reply = "Supportive context in patient, plain language."
            elif sc.expected_mode == "Mediator":
                reply = "Measured trade-offs balance competing constraints."
            elif sc.expected_mode == "Explorer":
                reply = "Curious exploration through a reversible experiment."
            checks.append(check_requested_mode_adherence(reply, sc.expected_mode))
        elif sc.kind == "council":
            checks.append(
                check_council_format(
                    "**[Analyst]** blunt.\n**[Guide]** gentle.\n**[Present self]** integrated."
                )
            )
        elif sc.kind == "no_mode_bleed":
            checks.append(check_no_mode_bleed("Mostly neutral reply tonight.", allow_mode_names=sc.allow_mode_names))
    summary = summarize_mode_adherence(checks)
    assert summary["mode_bleed_failures"] == 0
    assert summary["requested_mode_pass_rate"] == 1.0


def test_build_profile_ipip_bands_integration(ensure_db, monkeypatch, tmp_path: Path):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    ipip = tmp_path / "ipip.json"
    ipip.write_text(json.dumps({"facet_order_30": [3.0] * 30}), encoding="utf-8")
    monkeypatch.setattr(
        "virtual_self.services.trait_values_profile.IPIP_PRIVATE_PATH",
        ipip,
    )
    with connection() as conn:
        mem_id = _insert_dated_memory(
            conn,
            auth,
            content="I praise craftsmanship when a build finally works.",
            when=datetime(2024, 7, 1, tzinfo=timezone.utc),
        )
        _insert_draft_digest(conn, auth, memory_id=mem_id)
        conn.commit()

    profile = build_trait_values_profile(auth, ipip_path=ipip)
    checks = profile_fidelity_vs_ipip(profile, ipip_path=ipip)
    summary = summarize_trait_fidelity(checks)
    assert summary["traits_compared"] == 5
    assert "no scores exported" in profile.ipip_calibration_note.lower()
