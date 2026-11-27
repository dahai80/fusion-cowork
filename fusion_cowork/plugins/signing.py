"""Stage 7 — Plugin manifest Ed25519 signing.

sign_manifest(manifest_bytes, private_key) / verify_manifest(manifest_bytes, signature, public_key).
Private key from env FUSION_PLUGIN_SIGNING_KEY (CI signing), public keys from config plugins.signing_public_keys (PEM list).

cryptography is lazily installed (cloud extra); tests gated by importorskip. No key -> treated as unsigned (refused when require_signing).
"""

from __future__ import annotations

import base64
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# Signature coverage: sha256 over canonically sorted manifest fields, prevents field-reorder bypass
_SIG_FIELD = "signature"


def _canonical_bytes(manifest_data: dict) -> bytes:
    """Serialize the manifest without the signature field using sort_keys so the signed content is stable."""
    data = {k: v for k, v in manifest_data.items() if k != _SIG_FIELD}
    import json

    return json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign_manifest(manifest_data: dict, private_key_pem: str) -> Optional[str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        logger.error("cryptography not installed, cannot sign plugin (pip install fusion-cowork[cloud])")
        return None
    try:
        priv = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        if not isinstance(priv, Ed25519PrivateKey):
            logger.error("signing key is not Ed25519, refusing")
            return None
        sig = priv.sign(_canonical_bytes(manifest_data))
        return base64.urlsafe_b64encode(sig).decode("utf-8")
    except Exception as e:
        logger.error(f"plugin signing failed: {e}")
        return None


def verify_manifest(manifest_data: dict, public_key_pem: str) -> bool:
    sig = manifest_data.get(_SIG_FIELD, "")
    if not sig:
        return False
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        logger.warning("cryptography not installed, cannot verify plugin signature (treated as unsigned)")
        return False
    try:
        pub = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(pub, Ed25519PublicKey):
            logger.warning(f"verify key is not Ed25519: {type(pub).__name__}")
            return False
        sig_bytes = base64.urlsafe_b64decode(sig)
        pub.verify(sig_bytes, _canonical_bytes(manifest_data))
        return True
    except Exception as e:
        logger.debug(f"plugin signature verification failed: {e}")
        return False


def verify_any_key(manifest_data: dict, public_keys: List[str]) -> bool:
    """Verify against a list of public keys one by one; any passing key validates the signature."""
    if not manifest_data.get(_SIG_FIELD):
        return False
    return any(verify_manifest(manifest_data, pem) for pem in public_keys)


def get_configured_public_keys() -> List[str]:
    """Read config plugins.signing_public_keys (list of PEM strings)."""
    try:
        from ..config_center import ConfigCenter

        cc = ConfigCenter.get_instance()
        keys = cc.get("plugins.signing_public_keys", [])
        if isinstance(keys, list):
            return [str(k) for k in keys if k]
        if isinstance(keys, str):
            return [k.strip() for k in keys.split("-----END PUBLIC KEY-----") if "BEGIN" in k]
    except Exception as e:
        logger.debug(f"failed to read plugins.signing_public_keys: {e}")
    return []
