"""Stage 6 — security scale tests: rate limiting / encryption / audit chain / circuit breaker.

Opt-in design: no env/config -> behavior unchanged; activated only when set. cryptography is lazily installed, importorskip gates the encryption tests.
"""

from __future__ import annotations

import pytest

from fusion_cowork.security.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from fusion_cowork.security.rate_limit import RateLimiter, TokenBucket

# -- TokenBucket --


class TestTokenBucket:
    def test_burst_then_deny(self):
        # rate=1/s burst=3 -> first 3 pass, 4th denied (no refill in the burst)
        bucket = TokenBucket(rate=1.0, burst=3)
        results = [bucket.allow() for _ in range(5)]
        assert results[:3] == [True, True, True]
        # Instant burst, 4th/5th (tokens < 1) denied
        assert results[3] is False
        assert results[4] is False

    def test_refill_over_time(self):
        bucket = TokenBucket(rate=100.0, burst=1)
        assert bucket.allow() is True
        # No tokens instantly -> deny
        assert bucket.allow() is False
        # Manually rewind _last to simulate elapsed time (rate=100/s, 50ms refills 5 tokens, cap 1)
        bucket._last -= 0.1
        assert bucket.allow() is True

    def test_rate_zero_unlimited(self):
        bucket = TokenBucket(rate=0, burst=1)
        for _ in range(100):
            assert bucket.allow() is True


# -- RateLimiter --


class TestRateLimiter:
    def test_per_tenant_isolation(self):
        limiter = RateLimiter(rate=1.0, burst=2)
        # tenantA exhausted (2 burst)
        assert limiter.allow("tenantA") is True
        assert limiter.allow("tenantA") is True
        assert limiter.allow("tenantA") is False
        # tenantB unaffected (independent bucket)
        assert limiter.allow("tenantB") is True
        assert limiter.allow("tenantB") is True

    def test_unlimited_passthrough(self):
        limiter = RateLimiter(unlimited=True)
        for _ in range(1000):
            assert limiter.allow("any") is True

    def test_reset(self):
        limiter = RateLimiter(rate=1.0, burst=1)
        assert limiter.allow("t1") is True
        assert limiter.allow("t1") is False
        limiter.reset("t1")
        assert limiter.allow("t1") is True


# -- Encryption (cryptography lazily installed) --


@pytest.fixture
def enc_key(monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    monkeypatch.setenv("FUSION_ENCRYPTION_KEY", key.decode("utf-8"))
    return key


class TestEncryption:
    def test_roundtrip(self, enc_key):
        from fusion_cowork.security.encryption import decrypt_at_rest, encrypt_at_rest

        secret = "super-secret-token-12345"
        ct = encrypt_at_rest(secret)
        assert ct != secret
        assert ct.startswith("fernet:")
        pt = decrypt_at_rest(ct)
        assert pt == secret

    def test_decrypt_plaintext_passthrough(self, enc_key):
        from fusion_cowork.security.encryption import decrypt_at_rest

        # Non-ciphertext (no prefix) returned as-is (backward-compatible with plaintext)
        assert decrypt_at_rest("plain-value") == "plain-value"
        assert decrypt_at_rest("") == ""

    def test_no_key_returns_plaintext_with_warn(self, monkeypatch):
        monkeypatch.delenv("FUSION_ENCRYPTION_KEY", raising=False)
        from fusion_cowork.security.encryption import encrypt_at_rest

        # No key -> plaintext + WARN (local compatibility), no raise
        assert encrypt_at_rest("secret") == "secret"

    def test_is_encrypted(self, enc_key):
        from fusion_cowork.security.encryption import encrypt_at_rest, is_encrypted

        ct = encrypt_at_rest("x")
        assert is_encrypted(ct) is True
        assert is_encrypted("plain") is False


# -- ConfigCenter encrypted at rest --


class TestConfigCenterEncryption:
    def test_secret_encrypted_on_disk(self, enc_key, tmp_path, monkeypatch):
        from fusion_cowork.config_center import ConfigCenter

        monkeypatch.setattr("fusion_cowork.config_center._CONFIG_DIR", str(tmp_path), raising=False)
        ConfigCenter.reset_instance()
        cc = ConfigCenter(config_file=str(tmp_path / "config.json"))
        cc.set("ai.api_key", "sk-live-secret-999", source="user")
        cc.save()

        # Read the raw on-disk file — the secret value must be ciphertext (fernet:), not plaintext
        import json

        with open(str(tmp_path / "config.json"), encoding="utf-8") as f:
            raw = json.load(f)
        stored = raw["entries"]["ai.api_key"]["value"]
        assert stored.startswith("fernet:")
        assert "sk-live-secret-999" not in stored

        # reload -> decrypted back to plaintext
        cc2 = ConfigCenter(config_file=str(tmp_path / "config.json"))
        cc2.load()
        assert cc2.get("ai.api_key") == "sk-live-secret-999"
        ConfigCenter.reset_instance()

    def test_non_secret_plaintext_on_disk(self, enc_key, tmp_path, monkeypatch):
        from fusion_cowork.config_center import ConfigCenter

        monkeypatch.setattr("fusion_cowork.config_center._CONFIG_DIR", str(tmp_path), raising=False)
        ConfigCenter.reset_instance()
        cc = ConfigCenter(config_file=str(tmp_path / "config.json"))
        cc.set("log.level", "DEBUG", source="user")
        cc.save()

        import json

        with open(str(tmp_path / "config.json"), encoding="utf-8") as f:
            raw = json.load(f)
        # Non-secret stays plaintext
        assert raw["entries"]["log.level"]["value"] == "DEBUG"
        ConfigCenter.reset_instance()


# -- AuditLog tamper-evident chain --


class TestAuditLogChain:
    @pytest.mark.asyncio
    async def test_chain_verify_ok(self, tmp_path):
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        from fusion_cowork.security.audit import AuditLog

        audit = AuditLog(store=store)
        h1 = await audit.log("tA", "u1", "create", "space:s1", {"name": "alpha"})
        h2 = await audit.log("tA", "u1", "update", "space:s1", {"name": "beta"})
        assert h1 and h2
        result = await audit.verify_chain("tA")
        assert result["ok"] is True
        assert result["count"] == 2
        await store.close()

    @pytest.mark.asyncio
    async def test_tamper_breaks_chain(self, tmp_path):
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        from fusion_cowork.security.audit import AuditLog

        audit = AuditLog(store=store)
        await audit.log("tA", "u1", "create", "space:s1", {"name": "alpha"})
        await audit.log("tA", "u1", "update", "space:s1", {"name": "beta"})
        # Tamper with the first entry_hash -> chain broken
        async with store.write_tx("tA") as h:
            await h.exec(
                "UPDATE audit_log SET entry_hash = 'tampered' WHERE action = ?",
                ("create",),
            )
        result = await audit.verify_chain("tA")
        assert result["ok"] is False
        assert result["broken_at"] == 0
        await store.close()

    @pytest.mark.asyncio
    async def test_per_tenant_isolation(self, tmp_path):
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        from fusion_cowork.security.audit import AuditLog

        audit = AuditLog(store=store)
        await audit.log("tA", "u1", "create", "space:s1")
        await audit.log("tB", "u2", "create", "space:s2")
        ra = await audit.verify_chain("tA")
        rb = await audit.verify_chain("tB")
        assert ra["count"] == 1
        assert rb["count"] == 1
        assert ra["ok"] and rb["ok"]
        await store.close()


# ── CircuitBreaker ──


class TestCircuitBreaker:
    def test_closed_to_open_on_threshold(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=30.0)
        for _ in range(3):
            cb.on_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.is_call_allowed() is False

    def test_open_rejects_call(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=30.0)
        cb.on_failure()
        with pytest.raises(CircuitOpenError):
            cb.call_sync(lambda: 1)

    def test_open_to_half_open_after_timeout(self, monkeypatch):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=30.0)
        cb.on_failure()
        assert cb.state == CircuitState.OPEN
        # recovery_timeout floor=1.0s, too slow; monkeypatch time.monotonic to simulate expiry.
        fake = [cb._opened_at + cb.recovery_timeout + 1.0]
        monkeypatch.setattr("fusion_cowork.security.circuit_breaker.time.monotonic", lambda: fake[0])
        # state property auto-transitions OPEN→HALF_OPEN after recovery_timeout
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.is_call_allowed() is True

    def test_half_open_success_closes(self, monkeypatch):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=30.0)
        cb.on_failure()
        fake = [cb._opened_at + cb.recovery_timeout + 1.0]
        monkeypatch.setattr("fusion_cowork.security.circuit_breaker.time.monotonic", lambda: fake[0])
        assert cb.state == CircuitState.HALF_OPEN
        cb.on_success()
        assert cb.state == CircuitState.CLOSED
        assert cb._failures == 0

    def test_half_open_failure_reopens(self, monkeypatch):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=30.0)
        cb.on_failure()
        fake = [cb._opened_at + cb.recovery_timeout + 1.0]
        monkeypatch.setattr("fusion_cowork.security.circuit_breaker.time.monotonic", lambda: fake[0])
        assert cb.state == CircuitState.HALF_OPEN
        cb.on_failure()
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_call_wraps_coroutine_success(self):
        cb = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=30.0)

        async def ok():
            return 42

        assert await cb.call(ok) == 42
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_call_wraps_coroutine_failure(self):
        cb = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=30.0)

        async def boom():
            raise RuntimeError("upstream down")

        with pytest.raises(RuntimeError):
            await cb.call(boom)
        assert cb._failures == 1
        # One more failure -> OPEN
        with pytest.raises(RuntimeError):
            await cb.call(boom)
        assert cb.state == CircuitState.OPEN
