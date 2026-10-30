from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

GOOGLE_PRODUCT_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("google", "mail", re.compile(r"Mail/.+\.mbox$", re.I)),
    ("google", "my_activity", re.compile(r"My Activity/.+/MyActivity\.json$", re.I)),
    ("google", "chrome", re.compile(r"Chrome/(?:.*/)?History\.json$", re.I)),
    ("google", "youtube", re.compile(r"YouTube.*/history/.+\.json$", re.I)),
    ("google", "location", re.compile(r"Location History", re.I)),
    ("google", "keep", re.compile(r"Keep/.+\.json$", re.I)),
    ("google", "calendar", re.compile(r"Calendar/.+\.ics$", re.I)),
    ("google", "contacts", re.compile(r"Contacts/.+\.vcf$", re.I)),
    ("google", "photos", re.compile(r"Google Photos/.+\.json$", re.I)),
    ("google", "maps_places", re.compile(r"Maps \(your places\)", re.I)),
    ("google", "fit", re.compile(r"\bFit/", re.I)),
]

META_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("meta", "facebook_activity", re.compile(r"your_facebook_activity/", re.I)),
    ("meta", "messages", re.compile(r"messages/inbox/.+/message_\d+\.json$", re.I)),
    ("meta", "instagram", re.compile(r"your_instagram_activity/", re.I)),
]

MAGIC = {
    b"\x1f\x8b": "gzip",
    b"PK\x03\x04": "zip",
    b"{": "json",
    b"[": "json",
}


@dataclass(frozen=True)
class DetectedFile:
    path: Path
    rel_path: str
    provider: str
    product: str
    mime_hint: str


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def sniff_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".json":
        return "application/json"
    if ext == ".mbox":
        return "application/mbox"
    if ext == ".ics":
        return "text/calendar"
    if ext == ".vcf":
        return "text/vcard"
    try:
        head = path.read_bytes()[:4]
    except OSError:
        return "application/octet-stream"
    for magic, hint in MAGIC.items():
        if head.startswith(magic):
            return hint
    return "application/octet-stream"


def classify_file(root: Path, path: Path, default_provider: str = "google") -> DetectedFile:
    rel = _rel(root, path)
    provider = default_provider
    product = "unknown"
    patterns = META_PATTERNS if default_provider == "meta" else GOOGLE_PRODUCT_PATTERNS
    for prov, prod, pat in patterns:
        if pat.search(rel):
            provider, product = prov, prod
            break
    if product == "unknown" and default_provider == "google":
        for prov, prod, pat in META_PATTERNS:
            if pat.search(rel):
                provider, product = prov, prod
                break
    return DetectedFile(
        path=path,
        rel_path=rel,
        provider=provider,
        product=product,
        mime_hint=sniff_mime(path),
    )


def iter_data_files(staging_root: Path, provider: str) -> list[DetectedFile]:
    return rough_classify(staging_root, provider)


def rough_classify(staging_root: Path, provider: str) -> list[DetectedFile]:
    """Stage-1 path/pattern classification (before supervisor planning)."""
    out: list[DetectedFile] = []
    if not staging_root.is_dir():
        return out
    for path in staging_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".webp", ".heic"}:
            continue
        out.append(classify_file(staging_root, path, default_provider=provider))
    return out


def repair_meta_text(raw: bytes) -> str:
    """Meta exports sometimes use latin-1 mislabeled as utf-8."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1").encode("utf-8").decode("utf-8")
