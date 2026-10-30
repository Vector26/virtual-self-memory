from __future__ import annotations

from pathlib import Path

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.parsers import (
    activity,
    calendar,
    chrome,
    contacts,
    keep,
    location,
    mbox,
    meta_messages,
    photos,
    youtube,
)
from virtual_self.archive.parsers.base import ArchiveEvent

_PARSERS = {
    "mail": mbox.parse_file,
    "my_activity": activity.parse_file,
    "chrome": chrome.parse_file,
    "youtube": youtube.parse_file,
    "location": location.parse_file,
    "keep": keep.parse_file,
    "calendar": calendar.parse_file,
    "contacts": contacts.parse_file,
    "photos": photos.parse_file,
    "messages": meta_messages.parse_file,
}


def parse_detected(detected: DetectedFile) -> list[ArchiveEvent]:
    fn = _PARSERS.get(detected.product)
    if fn is None:
        return []
    return fn(detected.path, detected.rel_path, detected.provider, detected.product)
