"""Strip reply chains, signatures, forwards, and boilerplate from email bodies."""

from __future__ import annotations

import re

_ON_WROTE_LINE = re.compile(
    r"^On .+ wrote:\s*$",
    re.I | re.M,
)
_ON_WROTE_INLINE = re.compile(
    r"\nOn .{8,400}? wrote:\s*",
    re.I | re.S,
)
_ORIGINAL_MESSAGE = re.compile(
    r"^-{2,}\s*Original Message\s*-{2,}\s*$",
    re.I | re.M,
)
_FORWARDED = re.compile(
    r"^(-+\s*)?Forwarded message\s*(-+)?\s*$",
    re.I | re.M,
)
_FROM_BLOCK = re.compile(
    r"^From:\s*.+\n(?:^Sent:\s*.+\n)?(?:^To:\s*.+\n)?(?:^Subject:\s*.+\n)?",
    re.I | re.M,
)
_SIGNATURE_CUTOFF = re.compile(
    r"^-- \s*$|^Sent from my (iPhone|iPad|Android)|^Get Outlook for",
    re.I | re.M,
)

_AUTO_FROM = re.compile(
    r"(^|\b)(no[-_.]?reply|mailer-daemon|postmaster|notifications?|notify|donotreply|do-not-reply)(@|\b)",
    re.I,
)


def is_auto_generated_mail(*, from_header: str, subject: str, headers: dict[str, str]) -> bool:
    frm = (from_header or "").strip()
    if _AUTO_FROM.search(frm):
        return True
    subj = (subject or "").lower()
    if subj.startswith("automatic reply") or subj.startswith("out of office"):
        return True
    prec = (headers.get("Precedence") or headers.get("precedence") or "").lower()
    if prec in ("bulk", "junk", "list"):
        return True
    auto = (headers.get("Auto-Submitted") or headers.get("auto-submitted") or "").lower()
    if auto and auto != "no":
        return True
    return False


def strip_email_body(raw: str) -> str:
    """Return user-authored prose only; drop quoted replies and trailing signatures."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""

    cut = _SIGNATURE_CUTOFF.search(text)
    if cut:
        text = text[: cut.start()]

    for pat in (_ORIGINAL_MESSAGE, _FORWARDED):
        m = pat.search(text)
        if m:
            text = text[: m.start()]

    m = _ON_WROTE_INLINE.search(text)
    if m:
        text = text[: m.start()]

    lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith(">"):
            continue
        if _ON_WROTE_LINE.match(line.strip()):
            break
        lines.append(line)

    text = "\n".join(lines)
    text = _FROM_BLOCK.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def gmail_labels_include_sent(labels_header: str | None) -> bool:
    if not labels_header:
        return False
    labels = [p.strip().lower() for p in labels_header.split(",")]
    return "sent" in labels
