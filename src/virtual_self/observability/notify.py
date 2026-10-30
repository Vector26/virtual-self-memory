from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path

SOURCE_KEY = "virtual_self"
_AUDIT = Path(__file__).resolve().parents[3] / ".state" / "notify_audit.jsonl"

_DEDUPE_CACHE: dict[str, float] = {}


class NotifySeverity(str, Enum):
    INFO = "info"
    USER_ACTIONABLE = "user_actionable"


class NotifyCategory(str, Enum):
    DEFAULT = "default"
    ENRICHMENT_MILESTONE = "enrichment_milestone"
    ARCHIVE_ZIP = "archive_zip"
    ARCHIVE_DOWNLOAD = "archive_download"
    ARCHIVE_SUMMARY = "archive_summary"
    BAKE = "bake"
    BUILD = "build"
    CHAT_MEMORY = "chat_memory"
    CHAPTER_PROPOSAL = "chapter_proposal"
    DISK_GUARD = "disk_guard"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _notifier_url() -> str:
    return os.environ.get("VSM_NOTIFIER_URL", "http://127.0.0.1:8001/notify")


def _notify_label() -> str:
    return os.environ.get("VSM_NOTIFY_LABEL", "Virtual Self")


def _dedupe_minutes() -> float:
    raw = os.environ.get("VSM_NOTIFY_DEDUPE_MINUTES", "15")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 15.0


def _in_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def _notify_enabled() -> bool:
    return _env_bool("VSM_NOTIFY_ENABLED", True)


def _setting_or_env(env_name: str, settings_val: bool) -> bool:
    """Prefer explicit process env; otherwise use Settings (includes .env file)."""
    if os.environ.get(env_name) is not None:
        return _env_bool(env_name, settings_val)
    return settings_val


def _category_allowed(category: NotifyCategory) -> bool:
    from virtual_self.config import get_settings

    s = get_settings()
    if category == NotifyCategory.ENRICHMENT_MILESTONE:
        return _setting_or_env("VSM_ENRICH_NOTIFY_MILESTONES", s.enrich_notify_milestones)
    if category == NotifyCategory.ARCHIVE_ZIP:
        return _env_bool("VSM_NOTIFY_ARCHIVE_ZIP", False)
    if category == NotifyCategory.ARCHIVE_DOWNLOAD:
        return _env_bool("VSM_NOTIFY_ARCHIVE_DOWNLOAD", False)
    if category == NotifyCategory.CHAT_MEMORY:
        return _setting_or_env("VSM_CHAT_MEMORY_NOTIFY_ON_SAVE", s.chat_memory_notify_on_save)
    return True


def _should_suppress_delivery(*, force: bool) -> str | None:
    if force:
        return None
    if not _notify_enabled():
        return "disabled"
    if _in_pytest() and not _env_bool("VSM_NOTIFY_ALLOW_IN_TESTS", False):
        return "pytest"
    return None


def _dedupe_key(body: str, category: NotifyCategory) -> str:
    digest = hashlib.sha256(f"{category.value}\0{body}".encode()).hexdigest()
    return digest[:32]


def _dedupe_would_block(key: str, severity: NotifySeverity) -> bool:
    window = _dedupe_minutes()
    if window <= 0 or severity == NotifySeverity.USER_ACTIONABLE:
        return False
    now = time.time()
    last = _DEDUPE_CACHE.get(key)
    return last is not None and (now - last) < window * 60


def _dedupe_record(key: str) -> None:
    _DEDUPE_CACHE[key] = time.time()


def _audit(record: dict) -> None:
    _AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def notify_user(
    message: str,
    *,
    retries: int = 3,
    category: NotifyCategory = NotifyCategory.DEFAULT,
    severity: NotifySeverity = NotifySeverity.USER_ACTIONABLE,
    force: bool = False,
    source_label: str | None = None,
) -> bool:
    """Send configured notifier via HTTP notifier. Returns True only if delivery confirmed."""
    label = source_label if source_label is not None else _notify_label()
    body = message.strip()
    if not body.endswith((".", "!", "?")):
        body += "."

    suppress_reason = _should_suppress_delivery(force=force)
    if suppress_reason:
        _audit(
            {
                "ts": time.time(),
                "delivered": False,
                "suppressed": suppress_reason,
                "category": category.value,
                "severity": severity.value,
                "message": body[:200],
            }
        )
        return False

    if not _category_allowed(category):
        _audit(
            {
                "ts": time.time(),
                "delivered": False,
                "suppressed": "category",
                "category": category.value,
                "severity": severity.value,
                "message": body[:200],
            }
        )
        return False

    dedupe_key = _dedupe_key(body, category)
    if severity == NotifySeverity.INFO and _dedupe_would_block(dedupe_key, severity):
        _audit(
            {
                "ts": time.time(),
                "delivered": False,
                "suppressed": "dedupe",
                "category": category.value,
                "message": body[:200],
            }
        )
        return False

    for attempt in range(1, retries + 1):
        channels: dict[str, bool] = {}
        payload = json.dumps({"message": body[:900], "source": label}).encode()
        req = urllib.request.Request(
            _notifier_url(),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        http_ok = False
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                http_ok = resp.status == 200
        except (urllib.error.URLError, OSError):
            http_ok = False
        channels["http"] = http_ok

        delivered = any(channels.values())
        _audit(
            {
                "ts": time.time(),
                "delivered": delivered,
                "attempt": attempt,
                "channels": channels,
                "category": category.value,
                "severity": severity.value,
                "message": body[:200],
            }
        )
        if delivered:
            if severity == NotifySeverity.INFO:
                _dedupe_record(dedupe_key)
            return True
        time.sleep(min(8, 2 ** attempt))

    return False
