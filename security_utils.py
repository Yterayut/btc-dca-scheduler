import base64
import json
import logging
import os
from functools import lru_cache
from typing import Any

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover - cryptography optional during install
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

logger = logging.getLogger(__name__)


def _derive_fernet_key(secret: str) -> bytes:
    raw = secret.strip().encode()
    if not raw:
        raise ValueError("APP_ENCRYPTION_KEY is empty")
    # Accept raw 32-byte urlsafe base64 keys; otherwise derive via SHA256
    if len(raw) == 44:
        try:
            base64.urlsafe_b64decode(raw)
            return raw
        except Exception:
            pass
    import hashlib
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache(maxsize=1)
def get_cipher() -> Any | None:
    secret = os.getenv("APP_ENCRYPTION_KEY")
    if not secret:
        return None
    if Fernet is None:
        logger.warning("cryptography not installed; cannot encrypt metadata")
        return None
    try:
        key = _derive_fernet_key(secret)
        return Fernet(key)
    except Exception as exc:
        logger.error("Failed to initialize encryption cipher: %s", exc)
        return None


def encrypt_metadata(payload: dict[str, Any]) -> tuple[str, bool]:
    """Serialize payload to JSON and encrypt when cipher available."""
    raw = json.dumps(payload, default=str, separators=(",", ":")).encode()
    cipher = get_cipher()
    if cipher is None:
        encoded = base64.urlsafe_b64encode(raw).decode()
        return encoded, False
    token = cipher.encrypt(raw).decode()
    return token, True


def decrypt_metadata(token: str, *, encrypted: bool) -> dict[str, Any]:
    """Decode payload previously produced by encrypt_metadata."""
    if not token:
        return {}
    data = token.encode()
    if encrypted:
        cipher = get_cipher()
        if cipher is None:
            raise RuntimeError("APP_ENCRYPTION_KEY required to decrypt metadata")
        try:
            raw = cipher.decrypt(data)
        except InvalidToken as exc:  # pragma: no cover - surface to caller
            raise RuntimeError("Invalid encryption token") from exc
    else:
        raw = base64.urlsafe_b64decode(data)
    try:
        return json.loads(raw.decode())
    except json.JSONDecodeError:
        return {"raw": raw.decode(errors="ignore")}
