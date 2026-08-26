"""Stage 7 — 插件清单 Ed25519 签名。

sign_manifest(manifest_bytes, private_key) / verify_manifest(manifest_bytes, signature, public_key)。
私钥 env FUSION_PLUGIN_SIGNING_KEY (CI 签名), 公钥 config plugins.signing_public_keys (PEM 列表)。

cryptography 懒装 (cloud extra); importorskip 门控测试。无 key → 视为未签名 (require_signing 时拒)。
"""

from __future__ import annotations

import base64
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# 签名覆盖: manifest 字段规范化排序后 sha256, 防字段重排绕签
_SIG_FIELD = "signature"


def _canonical_bytes(manifest_data: dict) -> bytes:
    """manifest 去 signature 字段后 sort_keys 序列化, 签名覆盖内容稳定。"""
    data = {k: v for k, v in manifest_data.items() if k != _SIG_FIELD}
    import json

    return json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign_manifest(manifest_data: dict, private_key_pem: str) -> Optional[str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        logger.error("cryptography 未装, 无法签名插件 (pip install fusion-cowork[cloud])")
        return None
    try:
        priv = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        if not isinstance(priv, Ed25519PrivateKey):
            logger.error("签名 key 非 Ed25519, 拒")
            return None
        sig = priv.sign(_canonical_bytes(manifest_data))
        return base64.urlsafe_b64encode(sig).decode("utf-8")
    except Exception as e:
        logger.error(f"插件签名失败: {e}")
        return None


def verify_manifest(manifest_data: dict, public_key_pem: str) -> bool:
    sig = manifest_data.get(_SIG_FIELD, "")
    if not sig:
        return False
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        logger.warning("cryptography 未装, 无法验签插件 (视为未签名)")
        return False
    try:
        pub = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(pub, Ed25519PublicKey):
            logger.warning(f"验签 key 非 Ed25519: {type(pub).__name__}")
            return False
        sig_bytes = base64.urlsafe_b64decode(sig)
        pub.verify(sig_bytes, _canonical_bytes(manifest_data))
        return True
    except Exception as e:
        logger.debug(f"插件验签失败: {e}")
        return False


def verify_any_key(manifest_data: dict, public_keys: List[str]) -> bool:
    """对一组公钥逐一验签, 任一通过即签名有效。"""
    if not manifest_data.get(_SIG_FIELD):
        return False
    return any(verify_manifest(manifest_data, pem) for pem in public_keys)


def get_configured_public_keys() -> List[str]:
    """读 config plugins.signing_public_keys (PEM 字符串列表)。"""
    try:
        from ..config_center import ConfigCenter

        cc = ConfigCenter.get_instance()
        keys = cc.get("plugins.signing_public_keys", [])
        if isinstance(keys, list):
            return [str(k) for k in keys if k]
        if isinstance(keys, str):
            return [k.strip() for k in keys.split("-----END PUBLIC KEY-----") if "BEGIN" in k]
    except Exception as e:
        logger.debug(f"读 plugins.signing_public_keys 失败: {e}")
    return []
