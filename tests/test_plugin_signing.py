import pytest

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fusion_cowork.plugins.signing import (
    _canonical_bytes,
    sign_manifest,
    verify_any_key,
    verify_manifest,
)


def _gen_keypair():
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return priv_pem, pub_pem


def _manifest(name="demo", version="1.0.0", sig=""):
    return {
        "name": name,
        "version": version,
        "description": "test plugin",
        "nodes": ["A"],
        "signature": sig,
    }


def test_canonical_bytes_drops_signature_and_sorts():
    m1 = {"b": 2, "a": 1, "signature": "xxx"}
    m2 = {"a": 1, "b": 2}
    assert _canonical_bytes(m1) == _canonical_bytes(m2)


def test_sign_verify_roundtrip():
    priv_pem, pub_pem = _gen_keypair()
    m = _manifest()
    sig = sign_manifest(m, priv_pem)
    assert sig is not None
    m["signature"] = sig
    assert verify_manifest(m, pub_pem) is True


def test_verify_fails_tampered_manifest():
    priv_pem, pub_pem = _gen_keypair()
    m = _manifest()
    sig = sign_manifest(m, priv_pem)
    m["signature"] = sig
    m["description"] = "tampered"
    assert verify_manifest(m, pub_pem) is False


def test_verify_fails_unsigned():
    priv_pem, pub_pem = _gen_keypair()
    assert verify_manifest(_manifest(), pub_pem) is False


def test_verify_fails_wrong_key():
    priv_pem, _ = _gen_keypair()
    _, other_pub_pem = _gen_keypair()
    m = _manifest()
    sig = sign_manifest(m, priv_pem)
    m["signature"] = sig
    assert verify_manifest(m, other_pub_pem) is False


def test_verify_any_key_matches_one():
    priv_pem, pub_pem = _gen_keypair()
    _, other_pub_pem = _gen_keypair()
    m = _manifest()
    sig = sign_manifest(m, priv_pem)
    m["signature"] = sig
    assert verify_any_key(m, [other_pub_pem, pub_pem]) is True
    assert verify_any_key(m, [other_pub_pem]) is False


def test_sign_bad_key_returns_none():
    assert sign_manifest(_manifest(), "not-a-pem") is None


def test_registry_downgrade_rejected(tmp_path):
    from fusion_cowork.plugins.registry import PluginRegistry

    reg = PluginRegistry(str(tmp_path / "registry.json"))
    assert reg.register("demo", "2.0.0", True, checksum="abc") is True
    assert reg.register("demo", "1.9.0", True) is False
    assert reg.get_version("demo") == "2.0.0"
    assert reg.register("demo", "2.1.0", True) is True
    assert reg.get_version("demo") == "2.1.0"


def test_registry_persist_and_list(tmp_path):
    from fusion_cowork.plugins.registry import PluginRegistry

    f = tmp_path / "registry.json"
    reg1 = PluginRegistry(str(f))
    reg1.register("a", "1.0.0", False, checksum="x")
    reg1.register("b", "0.5.0", True, checksum="y")
    assert f.exists()
    reg2 = PluginRegistry(str(f))
    assert reg2.get_version("a") == "1.0.0"
    assert reg2.get_version("b") == "0.5.0"
    installed = {e["name"]: e for e in reg2.list_installed()}
    assert installed["a"]["signature_valid"] is False
    assert installed["b"]["signature_valid"] is True
    assert reg2.unregister("a") is True
    assert reg2.is_registered("a") is False
    assert reg2.unregister("nope") is False


def test_registry_equal_version_allowed(tmp_path):
    from fusion_cowork.plugins.registry import PluginRegistry

    reg = PluginRegistry(str(tmp_path / "registry.json"))
    assert reg.register("demo", "1.0.0", True) is True
    assert reg.register("demo", "1.0.0", True) is True
