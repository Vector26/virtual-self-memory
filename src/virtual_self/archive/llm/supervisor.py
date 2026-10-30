from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

from virtual_self.archive import ledger
from virtual_self.archive.detect import DetectedFile, classify_file, iter_data_files
from virtual_self.archive.llm.specialists import PRODUCT_TO_SPECIALIST_ID
from virtual_self.archive.llm.supervisor_prompts import (
    DETECT_PRODUCT_KEYS,
    SUPERVISOR_PROMPT_VERSION,
    SUPERVISOR_SYSTEM,
)
from virtual_self.config import get_settings
from virtual_self.persistence.db import fetch_one, transaction
from virtual_self.providers.agent_chat import chat_json

ChatJsonFn = Callable[..., dict[str, Any] | None]

SUPERVISED_PLAN_SCHEMA_VERSION = "supervised_plan.v1"
SUPERVISED_PLAN_FILENAME = "supervised_plan.json"
LAYER_SUPERVISOR = "supervisor"

INTEGRATION_RETRY_NOTE = (
    "Specialists agent: consume supervised_plan.json assignments where skip=false and "
    "abstain=false; specialist_id must match SPECIALIST_REGISTRY keys."
)

_DEFAULT_PRIORITY: dict[str, int] = {
    "mail": 9,
    "messages": 9,
    "contacts": 8,
    "location": 8,
    "fit": 7,
    "chrome": 5,
    "youtube": 5,
    "my_activity": 5,
    "calendar": 4,
    "keep": 4,
    "photos": 3,
    "maps_places": 3,
    "facebook_activity": 3,
    "instagram": 3,
    "unknown": 1,
}

_DEFAULT_SENSITIVITY: dict[str, int] = {
    "mail": 3,
    "messages": 3,
    "contacts": 2,
    "location": 2,
    "fit": 3,
    "unknown": 1,
}


class DetectCite(TypedDict):
    provider: str
    product: str


class SupervisedAssignment(TypedDict, total=False):
    kind: str
    rel_paths: list[str]
    detect_cite: DetectCite
    specialist_id: str | None
    priority: int
    sensitivity_hint: int
    skip: bool
    skip_reason: str | None
    abstain: bool


class SupervisedPlan(TypedDict, total=False):
    """supervised_plan.json — supervisor output for specialist agents.

    Persisted under the zip staging root and in ``archive_llm_runs.output_json``
    (layer ``supervisor``, prompt_version ``SUPERVISOR_V1``).

    Top-level keys:
        schema_version: ``supervised_plan.v1``
        prompt_version: ``SUPERVISOR_V1``
        staging_root, provider, input_fingerprint, assignments, clusters, generated_by

    Each assignment:
        kind: ``file`` | ``cluster``
        rel_paths: paths relative to staging_root
        detect_cite: ``provider`` + ``product`` from detect.py (required)
        specialist_id: registry id (e.g. ``chrome``, ``gmail``) or null when abstaining
        priority: 1–10; sensitivity_hint: 1–3
        skip / skip_reason / abstain: policy and unknown-product gates

    Specialists integration contract:
        - Load plan from staging ``supervised_plan.json`` or latest supervisor LLM run.
        - Only handle assignments with ``skip=false``, ``abstain=false``, non-null ``specialist_id``.
        - ``specialist_id`` must be a key in ``SPECIALIST_REGISTRY`` (see specialists package).
        - Process only ``rel_paths`` listed; do not re-guess ``product`` beyond ``detect_cite``.
        - Sort by descending ``priority``; apply ``sensitivity_hint`` in scrub/policy layers.
        - If ``detect_cite.product`` is ``unknown``, supervisor abstains — specialists must not invent parsers.
    """

    schema_version: str
    prompt_version: str
    staging_root: str
    provider: str
    input_fingerprint: str
    assignments: list[SupervisedAssignment]
    clusters: list[dict[str, Any]]
    generated_by: str


@dataclass(frozen=True)
class SupervisorAssignment:
    specialist_id: str
    file_rel_paths: list[str]
    hint: str | None = None


@dataclass(frozen=True)
class SupervisorPlan:
    assignments: list[SupervisorAssignment]
    notes: str | None = None


def specialist_id_for_product(product: str) -> str | None:
    return PRODUCT_TO_SPECIALIST_ID.get(product)


def is_available() -> bool:
    return True


def staging_root_from_files(files: Sequence[DetectedFile]) -> Path | None:
    if not files:
        return None
    f = files[0]
    depth = len(Path(f.rel_path).parts)
    root = f.path
    for _ in range(depth):
        root = root.parent
    return root


def supervised_plan_path(staging_root: Path) -> Path:
    return staging_root / SUPERVISED_PLAN_FILENAME


def _skip_plan_artifact(d: DetectedFile) -> bool:
    return Path(d.rel_path).name == SUPERVISED_PLAN_FILENAME


def collect_detected(
    staging_root: Path,
    provider: str,
    file_paths: Sequence[Path | str] | None = None,
) -> list[DetectedFile]:
    root = staging_root.resolve()
    if file_paths is not None:
        out: list[DetectedFile] = []
        for raw in file_paths:
            p = Path(raw)
            if not p.is_absolute():
                p = root / p
            if p.is_file() and p.name != SUPERVISED_PLAN_FILENAME:
                out.append(classify_file(root, p, default_provider=provider))
        return sorted(out, key=lambda d: d.rel_path)
    return [d for d in iter_data_files(root, provider) if not _skip_plan_artifact(d)]


def input_fingerprint(provider: str, detected: Sequence[DetectedFile]) -> str:
    parts = [provider] + sorted(d.rel_path for d in detected)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _cluster_by_product(detected: Sequence[DetectedFile]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for d in detected:
        buckets[(d.provider, d.product)].append(d.rel_path)
    return [
        {
            "detect_cite": {"provider": prov, "product": prod},
            "rel_paths": sorted(paths),
            "n_files": len(paths),
        }
        for (prov, prod), paths in sorted(buckets.items())
    ]


def _heuristic_assignment(d: DetectedFile) -> SupervisedAssignment:
    cite: DetectCite = {"provider": d.provider, "product": d.product}
    if d.product not in DETECT_PRODUCT_KEYS or d.product == "unknown":
        return {
            "kind": "file",
            "rel_paths": [d.rel_path],
            "detect_cite": cite,
            "specialist_id": None,
            "priority": 1,
            "sensitivity_hint": 1,
            "skip": True,
            "skip_reason": "unknown_product",
            "abstain": True,
        }
    sid = specialist_id_for_product(d.product)
    abstain = sid is None
    return {
        "kind": "file",
        "rel_paths": [d.rel_path],
        "detect_cite": cite,
        "specialist_id": sid,
        "priority": _DEFAULT_PRIORITY.get(d.product, 3),
        "sensitivity_hint": _DEFAULT_SENSITIVITY.get(d.product, 1),
        "skip": abstain,
        "skip_reason": "no_specialist" if abstain else None,
        "abstain": abstain,
    }


def build_heuristic_plan(
    staging_root: Path,
    provider: str,
    detected: Sequence[DetectedFile],
) -> SupervisedPlan:
    fp = input_fingerprint(provider, detected)
    return {
        "schema_version": SUPERVISED_PLAN_SCHEMA_VERSION,
        "prompt_version": SUPERVISOR_PROMPT_VERSION,
        "staging_root": str(staging_root.resolve()),
        "provider": provider,
        "input_fingerprint": fp,
        "assignments": [_heuristic_assignment(d) for d in detected],
        "clusters": _cluster_by_product(detected),
        "generated_by": "heuristic",
    }


def _payload_for_llm(provider: str, detected: Sequence[DetectedFile]) -> dict[str, Any]:
    return {
        "provider": provider,
        "files": [
            {
                "rel_path": d.rel_path,
                "detect_cite": {"provider": d.provider, "product": d.product},
                "mime_hint": d.mime_hint,
            }
            for d in detected
        ],
        "clusters": _cluster_by_product(detected),
        "allowed_specialist_ids": sorted(set(PRODUCT_TO_SPECIALIST_ID.values())),
    }


def _valid_cite(cite: Any) -> DetectCite | None:
    if not isinstance(cite, dict):
        return None
    prov = str(cite.get("provider") or "")
    prod = str(cite.get("product") or "")
    if prod not in DETECT_PRODUCT_KEYS:
        return None
    return {"provider": prov, "product": prod}


def _normalize_assignments(
    raw: dict[str, Any] | None,
    detected: Sequence[DetectedFile],
) -> list[SupervisedAssignment]:
    hint_index = {d.rel_path: d for d in detected}
    out: list[SupervisedAssignment] = []
    items = (raw or {}).get("assignments") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [_heuristic_assignment(d) for d in detected]

    seen: set[str] = set()
    allowed_sids = set(PRODUCT_TO_SPECIALIST_ID.values())
    for item in items:
        if not isinstance(item, dict):
            continue
        rel_paths = item.get("rel_paths")
        if not isinstance(rel_paths, list) or not rel_paths:
            continue
        rels = [str(p).replace("\\", "/") for p in rel_paths]
        cite = _valid_cite(item.get("detect_cite"))
        if cite is None:
            continue
        matched = [r for r in rels if r in hint_index]
        if not matched:
            continue
        hint = hint_index[matched[0]]
        if hint.provider != cite["provider"] or hint.product != cite["product"]:
            cite = {"provider": hint.provider, "product": hint.product}

        product = cite["product"]
        sid_raw = item.get("specialist_id")
        sid = str(sid_raw) if sid_raw else None
        abstain = bool(item.get("abstain"))
        skip = bool(item.get("skip"))

        expected = specialist_id_for_product(product)
        if product == "unknown":
            abstain = True
            skip = True
            sid = None
        elif expected is None:
            abstain = True
            skip = True
            sid = None
        elif sid and sid not in allowed_sids:
            abstain = True
            skip = True
            sid = None
        elif sid is None and expected:
            sid = expected

        try:
            priority = int(item.get("priority", _DEFAULT_PRIORITY.get(product, 3)))
        except (TypeError, ValueError):
            priority = _DEFAULT_PRIORITY.get(product, 3)
        priority = max(1, min(10, priority))
        try:
            sens = int(item.get("sensitivity_hint", _DEFAULT_SENSITIVITY.get(product, 1)))
        except (TypeError, ValueError):
            sens = _DEFAULT_SENSITIVITY.get(product, 1)
        sens = max(1, min(3, sens))

        assignment: SupervisedAssignment = {
            "kind": str(item.get("kind") or ("cluster" if len(rels) > 1 else "file")),
            "rel_paths": rels,
            "detect_cite": cite,
            "specialist_id": sid,
            "priority": priority,
            "sensitivity_hint": sens,
            "skip": skip,
            "skip_reason": str(item.get("skip_reason")) if item.get("skip_reason") else None,
            "abstain": abstain,
        }
        out.append(assignment)
        seen.update(rels)

    for d in detected:
        if d.rel_path not in seen:
            out.append(_heuristic_assignment(d))
    return sorted(out, key=lambda a: (-int(a.get("priority", 0)), a["rel_paths"][0]))


def merge_llm_supervisor_output(
    llm_out: dict[str, Any] | None,
    staging_root: Path,
    provider: str,
    detected: Sequence[DetectedFile],
) -> SupervisedPlan:
    return {
        "schema_version": SUPERVISED_PLAN_SCHEMA_VERSION,
        "prompt_version": SUPERVISOR_PROMPT_VERSION,
        "staging_root": str(staging_root.resolve()),
        "provider": provider,
        "input_fingerprint": input_fingerprint(provider, detected),
        "assignments": _normalize_assignments(llm_out, detected),
        "clusters": _cluster_by_product(detected),
        "generated_by": "llm",
    }


def write_supervised_plan(staging_root: Path, plan: SupervisedPlan) -> Path:
    path = supervised_plan_path(staging_root)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_supervised_plan(staging_root: Path) -> SupervisedPlan | None:
    path = supervised_plan_path(staging_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SUPERVISED_PLAN_SCHEMA_VERSION:
        return None
    return data  # type: ignore[return-value]


def _ledger_has_plan(conn, archive_run_id: UUID, fingerprint: str) -> bool:
    row = fetch_one(
        conn,
        """
        SELECT id FROM archive_llm_runs
        WHERE archive_run_id = %s AND layer = %s
          AND input_ref->>'input_fingerprint' = %s
        LIMIT 1
        """,
        (archive_run_id, LAYER_SUPERVISOR, fingerprint),
    )
    return row is not None


def record_supervised_plan(
    conn,
    *,
    archive_run_id: UUID,
    plan: SupervisedPlan,
    input_ref: dict[str, Any] | None = None,
) -> UUID:
    ref = {
        "input_fingerprint": plan["input_fingerprint"],
        "provider": plan["provider"],
        "n_assignments": len(plan.get("assignments") or []),
        **(input_ref or {}),
    }
    return ledger.record_llm_run(
        conn,
        archive_run_id=archive_run_id,
        layer=LAYER_SUPERVISOR,
        prompt_version=SUPERVISOR_PROMPT_VERSION,
        input_ref=ref,
        output_json=dict(plan),
    )


def run_supervisor(
    staging_root: Path,
    provider: str,
    archive_run_id: UUID | None = None,
    *,
    file_paths: Sequence[Path | str] | None = None,
    chat_json_fn: ChatJsonFn | None = None,
    persist: bool = True,
) -> SupervisedPlan:
    """Build or load supervised_plan.json for one staging root (idempotent on fingerprint)."""
    detected = collect_detected(staging_root, provider, file_paths=file_paths)
    fp = input_fingerprint(provider, detected)

    cached = load_supervised_plan(staging_root)
    if (
        cached
        and cached.get("input_fingerprint") == fp
        and cached.get("prompt_version") == SUPERVISOR_PROMPT_VERSION
    ):
        if archive_run_id and persist:
            with transaction() as conn:
                if not _ledger_has_plan(conn, archive_run_id, fp):
                    record_supervised_plan(conn, archive_run_id=archive_run_id, plan=cached)
        return cached

    settings = get_settings()
    llm_fn = chat_json_fn
    if settings.archive_llm_enabled and llm_fn is None:
        llm_fn = chat_json

    if llm_fn is not None and settings.archive_llm_enabled:
        user = json.dumps(_payload_for_llm(provider, detected), ensure_ascii=False)[:14000]
        raw = llm_fn(SUPERVISOR_SYSTEM, user, temperature=0.0)
        plan = merge_llm_supervisor_output(raw, staging_root, provider, detected)
    else:
        plan = build_heuristic_plan(staging_root, provider, detected)

    if persist:
        write_supervised_plan(staging_root, plan)
        if archive_run_id:
            with transaction() as conn:
                if not _ledger_has_plan(conn, archive_run_id, fp):
                    record_supervised_plan(conn, archive_run_id=archive_run_id, plan=plan)

    return plan


def _supervised_to_flow_plan(supervised: SupervisedPlan) -> SupervisorPlan:
    by_spec: dict[str, list[str]] = defaultdict(list)
    hints: dict[str, str] = {}
    for a in supervised.get("assignments") or []:
        if a.get("skip") or a.get("abstain"):
            continue
        sid = a.get("specialist_id")
        if not sid:
            continue
        by_spec[sid].extend(a.get("rel_paths") or [])
        cite = a.get("detect_cite") or {}
        hints[sid] = f"product={cite.get('product')}"
    assignments = [
        SupervisorAssignment(
            specialist_id=sid,
            file_rel_paths=sorted(set(paths)),
            hint=hints.get(sid),
        )
        for sid, paths in sorted(by_spec.items())
    ]
    notes = None
    if supervised.get("generated_by") == "heuristic":
        notes = "heuristic_supervisor"
    return SupervisorPlan(assignments=assignments, notes=notes)


def plan(
    files: list[DetectedFile],
    *,
    archive_run_id: UUID | None = None,
) -> SupervisorPlan | None:
    """Partition staging files into specialist assignments (flow integration)."""
    if not get_settings().archive_supervisor_enabled:
        return None
    if not is_available():
        return None
    root = staging_root_from_files(files)
    if root is None:
        return None
    provider = files[0].provider if files else "google"
    rels = [f.rel_path for f in files]
    supervised = run_supervisor(
        root,
        provider,
        archive_run_id,
        file_paths=rels,
        persist=True,
    )
    return _supervised_to_flow_plan(supervised)
