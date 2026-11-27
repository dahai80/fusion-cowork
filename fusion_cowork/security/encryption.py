"""Stage 6 — at-rest encryption (Fernet).

encrypt_at_rest(value, key) / decrypt_at_rest(value, key): Fernet symmetric encrypt/decrypt,
values carry the fernet: prefix marker.
derive_key(master, salt): HKDF derives a 32-byte Fernet key from master+salt.
get_encryption_key(): reads env FUSION_ENCRYPTION_KEY (urlsafe base64 32 bytes), returns None when missing.

opt-in: no key → encrypt_at_rest returns the original value + WARN (local plaintext compatibility), existing tests unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_PREFIX = "fernet:"
_ENC_ENV = "FUSION_ENCRYPTION_KEY"


def derive_key(master: str, salt: str = "fusion-cowork-config-v1") -> bytes:
    """HKDF-SHA256 derives 32 bytes from master+salt, returns urlsafe base64 (Fernet requirement)."""
    prk = hmac.new(b"fusion-cowork-salt", master.encode("utf-8"), hashlib.sha256).digest()
    okm = hashlib.pbkdf2_hmac("sha256", prk, salt.encode("utf-8"), 1000, dklen=32)
    return base64.urlsafe_b64encode(okm)


def get_encryption_key() -> Optional[bytes]:
    """Read env FUSION_ENCRYPTION_KEY, return the Fernet key (urlsafe base64 32 bytes). Missing/invalid → None.

    Accepts: urlsafe base64 32 bytes, or a raw 32-byte string. Uniformly encoded to the base64 Fernet requires.
    """
    raw = os.environ.get(_ENC_ENV, "").strip()
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw)
        if len(decoded) == 32:
            return base64.urlsafe_b64encode(decoded)
        logger.warning(
            f"FUSION_ENCRYPTION_KEY decodes to {len(decoded)} bytes (expected 32), at-rest encryption not enabled"
        )
        return None
    except Exception:
        logger.warning(
            "FUSION_ENCRYPTION_KEY malformed (expected urlsafe base64 32 bytes), at-rest encryption not enabled"
        )
        return None


def encrypt_at_rest(value: str, key: Optional[bytes] = None) -> str:
    """Encrypt a string, returns fernet:<ciphertext>. No key → returns the original value + WARN (local compatibility)."""
    if key is None:
        key = get_encryption_key()
    if key is None:
        logger.warning(
            "No FUSION_ENCRYPTION_KEY, secret value stored in plaintext (local/test compatibility only, never in production)"
        )
        return value
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning(
            "cryptography not installed, at-rest encryption falls back to plaintext (pip install fusion-cowork[cloud])"
        )
        return value
    f = Fernet(key)
    ct = f.encrypt(value.encode("utf-8"))
    return _PREFIX + ct.decode("utf-8")


def decrypt_at_rest(value: str, key: Optional[bytes] = None) -> str:
    """Decrypt fernet:<ciphertext>. Non-ciphertext (no prefix) → returns the original value (plaintext back-compat)."""
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return value
    if key is None:
        key = get_encryption_key()
    if key is None:
        logger.warning(
            f"Ciphertext value cannot be decrypted without FUSION_ENCRYPTION_KEY, returning ciphertext: {value[:24]}..."
        )
        return value
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.error("cryptography not installed, ciphertext cannot be decrypted")
        return value
    f = Fernet(key)
    pt = f.decrypt(value[len(_PREFIX) :].encode("utf-8"))
    return pt.decode("utf-8")


def is_encrypted(value: str) -> bool:
    """Whether the value is already encrypted (carries the fernet: prefix)."""
    return isinstance(value, str) and value.startswith(_PREFIX)
