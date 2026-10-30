from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from virtual_self.config import get_settings
from virtual_self.domain.errors import ValidationError


def _fernet() -> Fernet | None:
    key = get_settings().field_encryption_key
    if key is None or not key.get_secret_value().strip():
        return None
    raw = key.get_secret_value().strip().encode()
    try:
        return Fernet(raw)
    except Exception as exc:
        raise ValidationError("invalid VSM_FIELD_ENCRYPTION_KEY") from exc


def encrypt_text(plain: str) -> tuple[bytes | None, str | None]:
    """Returns (ciphertext, plain) — plain stored only when encryption unavailable (dev)."""
    f = _fernet()
    if f is None:
        return None, plain
    return f.encrypt(plain.encode("utf-8")), None


def decrypt_text(ciphertext: bytes | None, plain: str | None) -> str:
    if plain is not None:
        return plain
    if ciphertext is None:
        return ""
    f = _fernet()
    if f is None:
        raise ValidationError("ciphertext present but VSM_FIELD_ENCRYPTION_KEY not configured")
    try:
        return f.decrypt(ciphertext).decode("utf-8")
    except InvalidToken:
        raise ValidationError("failed to decrypt field") from None
