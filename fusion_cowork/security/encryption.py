"""Stage 6 — 静态加密 (Fernet at-rest)。

encrypt_at_rest(value, key) / decrypt_at_rest(value, key): Fernet 对称加解密, 值带 fernet: 前缀标识。
derive_key(master, salt): HKDF 从 master+salt 派生 32 字节 Fernet key。
get_encryption_key(): 读 env FUSION_ENCRYPTION_KEY (urlsafe base64 32 bytes), 缺返 None。

opt-in: 无 key → encrypt_at_rest 返原值 + WARN (本地明文兼容), 现有测试行为不变。
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
    """HKDF-SHA256 从 master+salt 派生 32 字节, 返 urlsafe base64 (Fernet 要求)。"""
    prk = hmac.new(b"fusion-cowork-salt", master.encode("utf-8"), hashlib.sha256).digest()
    okm = hashlib.pbkdf2_hmac("sha256", prk, salt.encode("utf-8"), 1000, dklen=32)
    return base64.urlsafe_b64encode(okm)


def get_encryption_key() -> Optional[bytes]:
    """读 env FUSION_ENCRYPTION_KEY, 返 Fernet key (urlsafe base64 32 bytes)。缺/错返 None。

    接受: urlsafe base64 32 字节, 或 32 字节原始串。统一编码成 Fernet 要求的 base64。
    """
    raw = os.environ.get(_ENC_ENV, "").strip()
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw)
        if len(decoded) == 32:
            return base64.urlsafe_b64encode(decoded)
        logger.warning(f"FUSION_ENCRYPTION_KEY 解码后 {len(decoded)} 字节 (期望 32), 静态加密未启")
        return None
    except Exception:
        logger.warning("FUSION_ENCRYPTION_KEY 格式错 (期望 urlsafe base64 32 字节), 静态加密未启")
        return None


def encrypt_at_rest(value: str, key: Optional[bytes] = None) -> str:
    """加密字符串, 返 fernet:<ciphertext>。无 key → 返原值 + WARN (本地兼容)。"""
    if key is None:
        key = get_encryption_key()
    if key is None:
        logger.warning("无 FUSION_ENCRYPTION_KEY, secret 值明文落盘 (仅本地/测试兼容, 生产勿用)")
        return value
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning("cryptography 未装, 静态加密降级明文 (pip install fusion-cowork[cloud])")
        return value
    f = Fernet(key)
    ct = f.encrypt(value.encode("utf-8"))
    return _PREFIX + ct.decode("utf-8")


def decrypt_at_rest(value: str, key: Optional[bytes] = None) -> str:
    """解密 fernet:<ciphertext>。非密文 (无前缀) → 返原值 (向后兼容明文)。"""
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return value
    if key is None:
        key = get_encryption_key()
    if key is None:
        logger.warning(f"密文值无 FUSION_ENCRYPTION_KEY 无法解, 返原密文: {value[:24]}...")
        return value
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.error("cryptography 未装, 密文无法解密")
        return value
    f = Fernet(key)
    pt = f.decrypt(value[len(_PREFIX) :].encode("utf-8"))
    return pt.decode("utf-8")


def is_encrypted(value: str) -> bool:
    """判断值是否已加密 (带 fernet: 前缀)。"""
    return isinstance(value, str) and value.startswith(_PREFIX)
