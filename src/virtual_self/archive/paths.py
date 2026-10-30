from __future__ import annotations

from pathlib import Path

from virtual_self.config import get_settings


def archive_root() -> Path:
    root = get_settings().archive_data_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def inbox_google() -> Path:
    p = archive_root() / "inbox" / "google"
    p.mkdir(parents=True, exist_ok=True)
    return p


def inbox_meta() -> Path:
    p = archive_root() / "inbox" / "meta"
    p.mkdir(parents=True, exist_ok=True)
    return p


def inbox_manual() -> Path:
    p = archive_root() / "inbox" / "manual"
    p.mkdir(parents=True, exist_ok=True)
    return p


def staging_dir(file_id: str) -> Path:
    p = archive_root() / "staging" / file_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def olap_dir() -> Path:
    p = archive_root() / "olap"
    p.mkdir(parents=True, exist_ok=True)
    return p


def duckdb_path() -> Path:
    return olap_dir() / "archive.duckdb"


def parquet_events_dir() -> Path:
    p = olap_dir() / "events"
    p.mkdir(parents=True, exist_ok=True)
    return p


def datasets_dir() -> Path:
    p = archive_root() / "datasets"
    p.mkdir(parents=True, exist_ok=True)
    return p
