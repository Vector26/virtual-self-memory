from __future__ import annotations

import re

_QUOTED = re.compile(r"""^["'](.+?)["']\s*$""")
_REMEMBER_PREFIX = re.compile(
    r"(?is)^(?:please\s+)?(?:i\s+need\s+you\s+to\s+)?"
    r"(?:remember|memorize|save|log|store)\s+(?:that\s+)?(?P<body>.+)$"
)
_SLASH = re.compile(r"^/remember\s+(?P<body>.+)$", re.I)


def parse_remember_request(text: str) -> str | None:
    """Extract explicit remember-this content from a user line, or None."""
    raw = text.strip()
    if not raw:
        return None
    m = _SLASH.match(raw)
    if m:
        return _unwrap_body(m.group("body"))
    m = _REMEMBER_PREFIX.match(raw)
    if m:
        return _unwrap_body(m.group("body"))
    return None


def _unwrap_body(body: str) -> str | None:
    s = body.strip()
    if not s:
        return None
    q = _QUOTED.match(s)
    if q:
        return q.group(1).strip() or None
    return s
