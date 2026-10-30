from __future__ import annotations

import shutil

from virtual_self.config import get_settings
from virtual_self.observability.notify import NotifyCategory, notify_user


def free_gb(path: str = "/") -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 ** 3)


def free_bytes(path: str = "/") -> int:
    return shutil.disk_usage(path).free


def ensure_disk_or_notify(context: str) -> bool:
    """Return True if enough free space; notify and return False otherwise."""
    min_gb = get_settings().archive_min_free_gb
    free = free_gb()
    if free >= min_gb:
        return True
    notify_user(
        f"Archive {context}: stopped — only {free:.1f} GB free on / "
        f"(need {min_gb:.0f} GB). Free space and retry.",
        category=NotifyCategory.DISK_GUARD,
    )
    return False


def ensure_disk_for_zip(context: str, size_bytes: int, path: str = "/") -> bool:
    """Refuse download if below min free GB or zip unpack headroom (2.5× size)."""
    settings = get_settings()
    free_b = free_bytes(path)
    min_b = int(settings.archive_min_free_gb * (1024**3))
    if free_b < min_b:
        notify_user(
            f"Archive {context}: stopped — {free_b / (1024**3):.1f} GB free on / "
            f"(need {settings.archive_min_free_gb:.0f} GB).",
            category=NotifyCategory.DISK_GUARD,
        )
        return False
    if size_bytes > 0 and int(size_bytes * 2.5) > free_b:
        notify_user(
            f"Archive {context}: stopped — zip needs ~{size_bytes * 2.5 / (1024**3):.1f} GB "
            f"headroom but only {free_b / (1024**3):.1f} GB free.",
            category=NotifyCategory.DISK_GUARD,
        )
        return False
    return True
