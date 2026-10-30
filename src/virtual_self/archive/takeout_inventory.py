"""Inventory Google Takeout zip archives without unpacking (central-directory scan)."""

from __future__ import annotations

import re
import struct
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from virtual_self.config import Settings, get_settings

_EOCD_SIG = b"PK\x05\x06"
_CDH_SIG = b"PK\x01\x02"
_LFH_SIG = b"PK\x03\x04"

_META_PATH = re.compile(
    r"(?i)(^|/)(messages/inbox/|your_facebook_activity/|your_instagram_activity/)"
)


@dataclass(frozen=True)
class ProductRollup:
    """Uncompressed byte total for one product folder inside a zip."""

    name: str
    uncompressed_bytes: int
    file_count: int


@dataclass
class ZipInventory:
    zip_name: str
    zip_bytes: int
    source: str
    products: list[ProductRollup] = field(default_factory=list)
    has_mail_mbox: bool = False
    has_keep: bool = False
    has_my_activity: bool = False
    has_meta: bool = False
    scan_error: str | None = None


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n_f = n / 1024.0
        if n_f < 1024.0:
            return f"{n_f:.1f} {unit}"
        n = int(n_f)
    return f"{n / 1024.0:.1f} PiB"


def _product_from_zip_path(name: str) -> str | None:
    norm = name.replace("\\", "/").lstrip("/")
    if _META_PATH.search(norm):
        return "Meta (export root)"
    if norm.startswith("Takeout/"):
        parts = norm.split("/")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def _parse_central_directory(data: bytes) -> list[tuple[str, int]]:
    """Return (path, uncompressed_size) from PK\x01\x02 records."""
    out: list[tuple[str, int]] = []
    i = 0
    while i + 46 <= len(data):
        if data[i : i + 4] != _CDH_SIG:
            i += 1
            continue
        (
            _ver_made,
            _ver_need,
            _flags,
            comp_method,
            _mtime,
            _mdate,
            _crc,
            comp_size,
            uncomp_size,
            name_len,
            extra_len,
            comment_len,
        ) = struct.unpack("<HHHHHHIIIHHH", data[i + 4 : i + 34])
        _ = comp_method, comp_size  # inventory uses uncompressed sizes only
        name_start = i + 46
        name_end = name_start + name_len
        if name_end > len(data):
            break
        name = data[name_start:name_end].decode("utf-8", errors="replace")
        if not name.endswith("/"):
            out.append((name, int(uncomp_size)))
        i = name_end + extra_len + comment_len
    return out


def _find_eocd(tail: bytes) -> tuple[int, int, int]:
    """Return (cd_offset, cd_size, eocd_index) within tail; tail covers file suffix."""
    idx = tail.rfind(_EOCD_SIG)
    if idx < 0:
        raise ValueError("EOCD signature not found in zip tail")
    if idx + 22 > len(tail):
        raise ValueError("truncated EOCD")
    cd_size, cd_offset = struct.unpack("<II", tail[idx + 12 : idx + 20])  # EOCD cd_size, cd_offset
    return int(cd_offset), int(cd_size), idx


def _rclone_bytes(settings: Settings, remote_rel: str, *, offset: int | None = None, tail: int | None = None) -> bytes:
    remote = f"{settings.archive_rclone_remote}:{settings.archive_takeout_path}/{remote_rel}"
    cmd = [
        "rclone",
        "cat",
        remote,
        "--config",
        str(settings.archive_rclone_config),
    ]
    if tail is not None:
        cmd.extend(["--tail", str(tail)])
    elif offset is not None:
        cmd.extend(["--offset", str(offset)])
    else:
        raise ValueError("offset or tail required")
    return subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=600)


def rclone_zip_sizes(settings: Settings) -> dict[str, int]:
    """One directory listing for all zip sizes (rclone lsjson is fast; lsl per-file is not)."""
    cmd = [
        "rclone",
        "lsjson",
        f"{settings.archive_rclone_remote}:{settings.archive_takeout_path}/",
        "--config",
        str(settings.archive_rclone_config),
        "--files-only",
    ]
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, timeout=300)
    import json as _json

    sizes: dict[str, int] = {}
    for item in _json.loads(out):
        name = item.get("Name", "")
        if name.lower().endswith(".zip"):
            sizes[name] = int(item.get("Size", 0))
    return sizes


def _entries_from_local_zip(path: Path) -> list[tuple[str, int]]:
    import zipfile

    with zipfile.ZipFile(path) as zf:
        return [(info.filename, int(info.file_size)) for info in zf.infolist() if not info.is_dir()]


def _entries_from_remote_zip(settings: Settings, zip_name: str, zip_bytes: int) -> list[tuple[str, int]]:
    # --offset seeks directly (measured ~4s for 128 KiB on a 1 GiB Drive object);
    # --tail makes Drive stream from object start and hits the 180s timeout.
    tail_n = min(131_072, zip_bytes)
    tail = _rclone_bytes(settings, zip_name, offset=zip_bytes - tail_n)
    cd_offset, cd_size, _ = _find_eocd(tail)
    tail_start = zip_bytes - len(tail)
    if cd_offset >= tail_start and cd_offset + cd_size <= zip_bytes:
        cd_blob = tail[cd_offset - tail_start : cd_offset - tail_start + cd_size]
    else:
        cd_blob = _rclone_bytes(settings, zip_name, offset=cd_offset)[:cd_size]
    return _parse_central_directory(cd_blob)


def summarize_zip_entries(entries: list[tuple[str, int]], *, zip_name: str, zip_bytes: int, source: str) -> ZipInventory:
    by_product: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    has_mail_mbox = False
    has_keep = False
    has_my_activity = False
    has_meta = False

    for path, uncomp in entries:
        norm = path.replace("\\", "/")
        if norm.lower().endswith(".mbox"):
            has_mail_mbox = True
        if _META_PATH.search(norm):
            has_meta = True
        product = _product_from_zip_path(path)
        if product is None:
            continue
        if product == "Mail":
            has_mail_mbox = has_mail_mbox or norm.lower().endswith(".mbox")
        if product == "Keep":
            has_keep = True
        if product == "My Activity":
            has_my_activity = True
        if product == "Meta (export root)":
            has_meta = True
        total, count = by_product[product]
        by_product[product] = (total + uncomp, count + 1)

    products = [
        ProductRollup(name=k, uncompressed_bytes=v[0], file_count=v[1])
        for k, v in sorted(by_product.items(), key=lambda kv: (-kv[1][0], kv[0].lower()))
    ]
    return ZipInventory(
        zip_name=zip_name,
        zip_bytes=zip_bytes,
        source=source,
        products=products,
        has_mail_mbox=has_mail_mbox,
        has_keep=has_keep,
        has_my_activity=has_my_activity,
        has_meta=has_meta,
    )


def inventory_local_zip(path: Path) -> ZipInventory:
    entries = _entries_from_local_zip(path)
    return summarize_zip_entries(
        entries,
        zip_name=path.name,
        zip_bytes=path.stat().st_size,
        source=str(path),
    )


def _rclone_copyto_local(settings: Settings, zip_name: str, dest: Path) -> None:
    remote = f"{settings.archive_rclone_remote}:{settings.archive_takeout_path}/{zip_name}"
    cmd = [
        "rclone",
        "copyto",
        remote,
        str(dest),
        "--config",
        str(settings.archive_rclone_config),
    ]
    subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=3600)


def inventory_remote_zip(
    settings: Settings, zip_name: str, zip_bytes: int | None = None
) -> ZipInventory:
    if zip_bytes is None:
        zip_bytes = rclone_zip_sizes(settings)[zip_name]
    remote = f"{settings.archive_rclone_remote}:{settings.archive_takeout_path}/{zip_name}"
    try:
        entries = _entries_from_remote_zip(settings, zip_name, zip_bytes)
        source = remote
    except (subprocess.TimeoutExpired, OSError, ValueError) as first_err:
        try:
            with tempfile.TemporaryDirectory(prefix="takeout-inv-") as tmp:
                local = Path(tmp) / zip_name
                _rclone_copyto_local(settings, zip_name, local)
                if local.stat().st_size != zip_bytes:
                    raise OSError(f"copy size mismatch: got {local.stat().st_size}, expected {zip_bytes}")
                entries = _entries_from_local_zip(local)
                source = f"{remote} (via local copy fallback)"
        except Exception as second_err:
            inv = ZipInventory(
                zip_name=zip_name,
                zip_bytes=zip_bytes,
                source=remote,
                scan_error=f"{first_err!s}; fallback: {second_err!s}",
            )
            return inv
    return summarize_zip_entries(entries, zip_name=zip_name, zip_bytes=zip_bytes, source=source)


def list_granted_takeout_zips(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    return sorted(rclone_zip_sizes(settings))


def build_full_inventory(
    settings: Settings | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> list[ZipInventory]:
    settings = settings or get_settings()
    rows: list[ZipInventory] = []
    inbox = settings.archive_data_root / "inbox" / "google"
    local_by_name = {p.name: p for p in inbox.glob("takeout*.zip")} if inbox.is_dir() else {}
    sizes = rclone_zip_sizes(settings)

    for zip_name in sorted(sizes):
        if progress:
            progress(zip_name)
        local_path = local_by_name.get(zip_name)
        use_local = (
            local_path is not None
            and local_path.is_file()
            and local_path.stat().st_size == sizes.get(zip_name, -1)
        )
        try:
            if use_local:
                try:
                    rows.append(inventory_local_zip(local_path))
                except OSError:
                    rows.append(inventory_remote_zip(settings, zip_name, sizes[zip_name]))
                except Exception as exc:
                    import zipfile

                    if isinstance(exc, zipfile.BadZipFile):
                        rows.append(inventory_remote_zip(settings, zip_name, sizes[zip_name]))
                    else:
                        raise
            else:
                rows.append(inventory_remote_zip(settings, zip_name, sizes[zip_name]))
        except Exception as exc:
            rows.append(
                ZipInventory(
                    zip_name=zip_name,
                    zip_bytes=sizes[zip_name],
                    source=f"{settings.archive_rclone_remote}:{settings.archive_takeout_path}/{zip_name}",
                    scan_error=str(exc),
                )
            )
    return rows


def voice_verdict(rows: list[ZipInventory]) -> dict[str, bool]:
    return {
        "Mail (.mbox in Takeout/Mail)": any(r.has_mail_mbox for r in rows),
        "Keep (Takeout/Keep)": any(r.has_keep for r in rows),
        "My Activity (Takeout/My Activity)": any(r.has_my_activity for r in rows),
        "Meta (messages / facebook / instagram export paths)": any(r.has_meta for r in rows),
    }


def render_markdown(rows: list[ZipInventory], *, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(tz=UTC)
    settings = get_settings()
    verdict = voice_verdict(rows)
    lines: list[str] = [
        "# Takeout zip inventory (granted scope only)",
        "",
        f"Generated: `{generated_at.isoformat()}`",
        "",
        "Sources:",
        f"- Remote: `{settings.archive_rclone_remote}:{settings.archive_takeout_path}/` "
        f"(rclone config `{settings.archive_rclone_config}`)",
        f"- Local inbox: `{settings.archive_data_root / 'inbox' / 'google'}`",
        "",
        "Method: read each zip central directory only (no unpack, no parsing). "
        "Product folder sizes sum **uncompressed** bytes of member files.",
        "",
        "## Voice-source verdict",
        "",
    ]
    for label, present in verdict.items():
        lines.append(f"- **{label}**: {'**yes**' if present else '**no**'}")
    lines.extend(["", "## Summary", "", f"- Zip count: **{len(rows)}**"])
    total_remote = sum(r.zip_bytes for r in rows)
    lines.append(f"- Total compressed size: **{_human_bytes(total_remote)}**")
    lines.append("")
    lines.append("## Per-zip product tree")
    lines.append("")
    for row in rows:
        lines.append(f"### `{row.zip_name}`")
        lines.append("")
        lines.append(f"- Compressed: {_human_bytes(row.zip_bytes)}")
        lines.append(f"- Source: `{row.source}`")
        flags = []
        if row.has_mail_mbox:
            flags.append("Mail/mbox")
        if row.has_keep:
            flags.append("Keep")
        if row.has_my_activity:
            flags.append("My Activity")
        if row.has_meta:
            flags.append("Meta")
        lines.append(f"- Voice flags: {', '.join(flags) if flags else '(none)'}")
        if row.scan_error:
            lines.append(f"- **Scan error:** `{row.scan_error}`")
        lines.append("")
        if not row.products:
            lines.append("_No Takeout product folders detected in this zip._")
        else:
            lines.append("| Product folder | Files | Uncompressed |")
            lines.append("|---|---:|---:|")
            for p in row.products:
                lines.append(f"| {p.name} | {p.file_count} | {_human_bytes(p.uncompressed_bytes)} |")
        lines.append("")
    return "\n".join(lines)


def write_inventory_markdown(out_path: Path, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    rows = build_full_inventory(settings)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(rows), encoding="utf-8")
    return out_path
