"""Staging classify → supervisor → specialists → parse queue.

Sequence (per zip staging dir):

.. mermaid::

   sequenceDiagram
       participant Run as process_archive_file
       participant Detect as detect.rough_classify
       participant Sup as supervisor.plan
       participant Spec as specialists.classify_file
       participant Parse as parsers.parse_detected
       participant DB as archive_llm_runs

       Run->>Detect: staging_root, provider
       Detect-->>Run: DetectedFile[]
       alt supervisor enabled and ready
           Run->>Sup: plan(files)
           Sup-->>DB: layer=supervisor
           loop each assignment
               Run->>Spec: classify_file(assignment, sample)
               Spec-->>DB: layer=specialist:*
               alt specialist ok
                   Spec-->>Run: should_parse flags
               else specialist failed
                   Run->>Run: quarantine (no parse, no promote)
               end
           end
       else supervisor skipped
           Run->>Run: default should_parse from detect
       end
       loop should_parse=true
           Run->>Parse: parse_detected
       end
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.archive import ledger
from virtual_self.archive.detect import DetectedFile, rough_classify
from virtual_self.archive.llm.specialists import SPECIALIST_REGISTRY, classify_file
from virtual_self.archive.llm.specialists._core import MAX_SAMPLE_CHARS
from virtual_self.archive.llm.specialists.specialists_prompts import PROMPT_VERSION as SPECIALIST_PROMPT_VERSION
from virtual_self.archive.llm.supervisor import (
    INTEGRATION_RETRY_NOTE,
    SupervisorAssignment,
    is_available as supervisor_available,
    plan as supervisor_plan,
)
from virtual_self.archive.parsers import parse_detected
from virtual_self.archive.parsers.base import ArchiveEvent
from virtual_self.config import get_settings
from virtual_self.persistence.db import transaction

_KNOWN_PARSER_PRODUCTS = frozenset(
    {
        "mail",
        "my_activity",
        "chrome",
        "youtube",
        "location",
        "keep",
        "calendar",
        "contacts",
        "photos",
        "messages",
    }
)


@dataclass
class ParseDecision:
    detected: DetectedFile
    should_parse: bool
    reason: str
    specialist_id: str | None = None
    quarantined: bool = False
    confidence: float = 0.0


@dataclass
class StagingParseResult:
    files_seen: int
    events: list[ArchiveEvent]
    unknown_samples: list[str]
    decisions: list[ParseDecision] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    supervisor_meta: dict[str, Any] = field(default_factory=dict)
    block_promote: bool = False


def _default_should_parse(d: DetectedFile) -> bool:
    return d.product in _KNOWN_PARSER_PRODUCTS


def _detect_fallback_decisions(files: list[DetectedFile]) -> list[ParseDecision]:
    return [
        ParseDecision(
            detected=f,
            should_parse=_default_should_parse(f),
            reason="detect_fallback",
            confidence=1.0 if _default_should_parse(f) else 0.0,
        )
        for f in files
    ]


def _files_by_rel(files: list[DetectedFile]) -> dict[str, DetectedFile]:
    return {f.rel_path: f for f in files}


def _log_llm(
    archive_run_id: UUID | None,
    layer: str,
    prompt_version: str,
    input_ref: dict[str, Any] | None,
    output_json: dict[str, Any] | None,
    verdict: str | None = None,
) -> None:
    if archive_run_id is None:
        return
    with transaction() as conn:
        ledger.record_llm_run(
            conn,
            archive_run_id=archive_run_id,
            layer=layer,
            prompt_version=prompt_version,
            input_ref=input_ref,
            output_json=output_json,
            verdict=verdict,
        )


def _run_specialist_assignment(
    assignment: SupervisorAssignment,
    batch: list[DetectedFile],
    archive_run_id: UUID | None,
) -> tuple[list[ParseDecision], bool, str | None]:
    """Classify one supervisor assignment; quarantine entire batch on failure."""
    if assignment.specialist_id not in SPECIALIST_REGISTRY:
        return [], False, f"unknown_specialist:{assignment.specialist_id}"

    use_llm = get_settings().archive_llm_enabled
    spec_assignment = {
        "specialist_id": assignment.specialist_id,
        "reason": assignment.hint or "",
    }
    decisions: list[ParseDecision] = []
    try:
        for det in batch:
            try:
                sample = det.path.read_text(encoding="utf-8", errors="replace")[:MAX_SAMPLE_CHARS]
            except OSError as exc:
                raise OSError(f"read_failed:{det.rel_path}:{exc}") from exc
            result = classify_file(spec_assignment, sample, det, use_llm=use_llm)
            _log_llm(
                archive_run_id,
                f"specialist:{assignment.specialist_id}",
                SPECIALIST_PROMPT_VERSION,
                {"rel_path": det.rel_path, "specialist_id": assignment.specialist_id},
                dict(result),
                verdict="ok" if result.get("should_parse") else "skip",
            )
            min_conf = get_settings().archive_min_confidence
            should = bool(result.get("should_parse")) and float(result.get("confidence", 0)) >= min_conf
            decisions.append(
                ParseDecision(
                    detected=det,
                    should_parse=should,
                    reason=str(result.get("notes") or "specialist"),
                    specialist_id=assignment.specialist_id,
                    confidence=float(result.get("confidence", 0)),
                )
            )
    except Exception as exc:
        return [], False, str(exc)
    return decisions, True, None


def _resolve_via_supervisor(
    files: list[DetectedFile],
    archive_run_id: UUID | None,
) -> tuple[list[ParseDecision], list[str], dict[str, Any], bool]:
    settings = get_settings()
    meta: dict[str, Any] = {
        "enabled": settings.archive_supervisor_enabled,
        "supervisor_ready": supervisor_available(),
    }
    quarantined: list[str] = []
    block_promote = False

    if not settings.archive_supervisor_enabled:
        meta["skipped"] = True
        meta["reason"] = "disabled"
        return _detect_fallback_decisions(files), quarantined, meta, block_promote

    if not supervisor_available():
        meta["skipped"] = True
        meta["reason"] = "not_ready"
        meta["integration_note"] = INTEGRATION_RETRY_NOTE
        return _detect_fallback_decisions(files), quarantined, meta, block_promote

    plan = supervisor_plan(files, archive_run_id=archive_run_id)
    if plan is None:
        meta["skipped"] = True
        meta["reason"] = "plan_failed"
        meta["integration_note"] = INTEGRATION_RETRY_NOTE
        return _detect_fallback_decisions(files), quarantined, meta, block_promote

    by_rel = _files_by_rel(files)
    decisions: list[ParseDecision] = []
    assigned: set[str] = set()

    for assignment in plan.assignments:
        batch = [by_rel[p] for p in assignment.file_rel_paths if p in by_rel]
        assigned.update(d.rel_path for d in batch)
        batch_decisions, ok, err = _run_specialist_assignment(assignment, batch, archive_run_id)
        if not ok:
            block_promote = True
            for d in batch:
                quarantined.append(d.rel_path)
                decisions.append(
                    ParseDecision(
                        detected=d,
                        should_parse=False,
                        reason=err or "specialist_failed",
                        specialist_id=assignment.specialist_id,
                        quarantined=True,
                    )
                )
            continue
        decisions.extend(batch_decisions)

    for f in files:
        if f.rel_path not in assigned:
            decisions.append(
                ParseDecision(
                    detected=f,
                    should_parse=_default_should_parse(f),
                    reason="supervisor_unassigned",
                    confidence=1.0 if _default_should_parse(f) else 0.0,
                )
            )

    meta["skipped"] = False
    meta["assignments"] = len(plan.assignments)
    meta["quarantined"] = len(quarantined)
    meta["notes"] = plan.notes
    return decisions, quarantined, meta, block_promote


def build_parse_decisions(
    files: list[DetectedFile],
    *,
    archive_run_id: UUID | None = None,
) -> tuple[list[ParseDecision], list[str], dict[str, Any], bool]:
    """Supervisor + specialists, or detect fallback."""
    return _resolve_via_supervisor(files, archive_run_id)


def classify_and_parse_staging(
    staging_root: Path,
    provider: str,
    *,
    subject_id: UUID | None = None,
    archive_file_id: UUID | None = None,
) -> StagingParseResult:
    files = rough_classify(staging_root, provider)
    classify_run_id: UUID | None = None
    if archive_file_id is not None:
        with transaction() as conn:
            classify_run_id = ledger.start_run(
                conn,
                "archive.classify",
                subject_id=subject_id,
                archive_file_id=archive_file_id,
            )

    decisions, quarantined, sup_meta, block_promote = build_parse_decisions(
        files, archive_run_id=classify_run_id
    )

    events: list[ArchiveEvent] = []
    unknown_samples: list[str] = []
    for dec in decisions:
        if dec.quarantined or not dec.should_parse:
            if dec.detected.product == "unknown" and not dec.quarantined:
                try:
                    unknown_samples.append(
                        dec.detected.path.read_text(encoding="utf-8", errors="replace")[:2000]
                    )
                except OSError:
                    pass
            continue
        evs = parse_detected(dec.detected)
        if not evs and dec.detected.product == "unknown":
            try:
                unknown_samples.append(
                    dec.detected.path.read_text(encoding="utf-8", errors="replace")[:2000]
                )
            except OSError:
                pass
        events.extend(evs)

    classify_stats = {
        "files": len(files),
        "parsed_files": sum(1 for d in decisions if d.should_parse and not d.quarantined),
        "quarantined": quarantined,
        "supervisor": sup_meta,
    }
    if classify_run_id is not None:
        with transaction() as conn:
            ledger.finish_run(conn, classify_run_id, True, classify_stats)

    return StagingParseResult(
        files_seen=len(files),
        events=events,
        unknown_samples=unknown_samples,
        decisions=decisions,
        quarantined=quarantined,
        supervisor_meta=sup_meta,
        block_promote=block_promote,
    )
