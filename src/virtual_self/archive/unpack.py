from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from virtual_self.archive import disk_guard
from virtual_self.observability.logging import get_logger

log = get_logger(__name__)

MAX_COMPRESSION_RATIO = 100
MAX_MEMBER_BYTES = 4 * 1024 ** 3
MAX_STAGING_BYTES_FACTOR = 1.5


def _safe_member_name(name: str) -> bool:
    if name.startswith("/") or name.startswith("\\"):
        return False
    parts = Path(name).parts
    return ".." not in parts


def safe_unpack(zip_path: Path, dest: Path) -> tuple[int, int]:
    """
    Extract zip with zip-slip and zip-bomb guards.
    Returns (files_extracted, bytes_written).
    """
    if not disk_guard.ensure_disk_or_notify(f"unpack {zip_path.name}"):
        raise RuntimeError("disk guard blocked unpack")

    zip_size = zip_path.stat().st_size
    max_staging = int(zip_size * MAX_STAGING_BYTES_FACTOR) + (64 * 1024 ** 2)
    dest.mkdir(parents=True, exist_ok=True)
    files_out = 0
    bytes_out = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x800:
                pass
            name = info.filename
            if not _safe_member_name(name):
                log.warning("archive_zip_slip_rejected", member=name)
                continue
            if info.compress_size > 0:
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > MAX_COMPRESSION_RATIO:
                    log.warning("archive_zip_bomb_ratio", member=name, ratio=ratio)
                    continue
            if info.file_size > MAX_MEMBER_BYTES:
                log.warning("archive_zip_member_too_large", member=name, size=info.file_size)
                continue
            if bytes_out + info.file_size > max_staging:
                log.warning("archive_staging_cap", zip=str(zip_path))
                break
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.is_symlink():
                continue
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            if target.is_symlink():
                target.unlink(missing_ok=True)
                continue
            files_out += 1
            bytes_out += info.file_size

    return files_out, bytes_out


def cleanup_staging(staging: Path) -> None:
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
