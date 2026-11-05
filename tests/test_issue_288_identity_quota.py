"""issue #288: identity JWT propagation for gateway quota metering + friendly quota notice.

- Caller identity JWT captured per request (X-Fusion-Identity-Token or JWT-shaped bearer)
- FusionMLXClient attaches Authorization: Bearer <identity-jwt> on outbound LLM calls
- Gateway 429 quota shape → bilingual friendly message, no retry
- Background runs carry the caller identity into engine LLM calls
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fusion_cowork.ai.mlx_client import FusionMLXClient
from fusion_cowork.auth.identity_propagation import (
    extract_identity_token,
    friendly_quota_error,
    get_current_identity_token,
    identity_auth_headers,
    identity_token_context,
    looks_like_jwt,
)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.sig"


# ── extraction ──


def test_looks_like_jwt():
    assert looks_like_jwt(JWT)
    assert not looks_like_jwt("fm-abc123")
    assert not looks_like_jwt("a.b")
    assert not looks_like_jwt("")


def test_extract_from_identity_header():
    assert extract_identity_token({"x-fusion-identity-token": JWT}) == JWT


def test_extract_from_jwt_bearer():
    assert extract_identity_token({"authorization": f"Bearer {JWT}"}) == JWT


def test_extract_ignores_module_key_bearer():
    # inbound module auth uses the fm- key on the same header — not an identity JWT
    assert extract_identity_token({"authorization": "Bearer fm-abc123"}) == ""
    assert extract_identity_token({}) == ""


def test_context_headers():
    with identity_token_context(JWT):
        assert get_current_identity_token() == JWT
        assert identity_auth_headers() == {"Authorization": f"Bearer {JWT}"}
    assert get_current_identity_token() == ""
    assert identity_auth_headers() == {}


# ── friendly quota error ──


def test_quota_shape_mapped():
    body = {"detail": "daily token quota exceeded", "quota": {"used": 100000, "budget": 100000}}
    msg = friendly_quota_error(429, body)
    assert msg is not None
    assert "daily token quota exceeded" in msg
    assert "quota" in msg


def test_non_quota_and_non_429_not_mapped():
    assert friendly_quota_error(429, {"detail": "rate limited"}) is None
    assert friendly_quota_error(429, "plain text no keyword") is None
    assert friendly_quota_error(500, {"detail": "daily token quota exceeded"}) is None


# ── FusionMLXClient wiring �──


@pytest.fixture(autouse=True)
def _force_fallback_client(monkeypatch):
    # fusion-core pool ignores the injected mock httpx client — pin the
    # handwritten-retry path for the chat() tests.
    from fusion_cowork.ai import mlx_client

    monkeypatch.setattr(mlx_client, "_HAS_FUSION_CORE", False)


def _transport_app(captured: list, responder):
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responder(request)

    return httpx.MockTransport(handler)


def test_chat_carries_identity_bearer():
    captured: list = []

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
        )

    client = FusionMLXClient(api_key="module-key")
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        timeout=5,
        headers={"Authorization": "Bearer module-key"},
        transport=_transport_app(captured, responder),
    )
    with identity_token_context(JWT):
        resp = asyncio.run(client.chat("m", [{"role": "user", "content": "hi"}]))
    assert resp.content == "ok"
    assert captured[0].headers["authorization"] == f"Bearer {JWT}"


def test_chat_without_identity_uses_module_key():
    captured: list = []

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
        )

    client = FusionMLXClient(api_key="module-key")
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        timeout=5,
        headers={"Authorization": "Bearer module-key"},
        transport=_transport_app(captured, responder),
    )
    asyncio.run(client.chat("m", [{"role": "user", "content": "hi"}]))
    assert captured[0].headers["authorization"] == "Bearer module-key"


def test_quota_429_raises_friendly_no_retry():
    captured: list = []
    quota_body = {"detail": "daily token quota exceeded", "quota": {"used": 100000, "budget": 100000}}

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=quota_body)

    client = FusionMLXClient(api_key="module-key", max_retries=3)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, timeout=5, transport=_transport_app(captured, responder)
    )
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(client.chat("m", [{"role": "user", "content": "hi"}]))
    assert "daily token quota exceeded" in str(exc.value)
    assert len(captured) == 1, "quota 429 must not retry"


def test_plain_429_still_retries():
    captured: list = []
    state = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, json={"detail": "busy"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
        )

    client = FusionMLXClient(api_key="module-key", retry_delay=0)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, timeout=5, transport=_transport_app(captured, responder)
    )
    resp = asyncio.run(client.chat("m", [{"role": "user", "content": "hi"}]))
    assert resp.content == "ok"
    assert len(captured) == 2


# ── background run identity ──


class _FakeExec:
    def __init__(self):
        from fusion_cowork.engine.workflow import WorkflowStatus

        self.status = WorkflowStatus.SUCCESS
        self.steps = []
        self.result_summary = "ok"
        self.error = None


class _FakeEngine:
    def __init__(self):
        self.seen_tokens = []

    async def execute(self, wf, **kwargs):
        self.seen_tokens.append(get_current_identity_token())
        return _FakeExec()


@pytest.mark.asyncio
async def test_run_registry_carries_identity_token(tmp_path):
    from fusion_cowork.server.web_api import RunRegistry

    eng = _FakeEngine()
    registry = RunRegistry(engine=eng)
    registry.start(
        {"name": "wf-288", "nodes": [], "edges": []},
        workflow_id="wf-288",
        identity_token=JWT,
    )
    for _ in range(100):
        if eng.seen_tokens:
            break
        await asyncio.sleep(0.02)
    assert eng.seen_tokens == [JWT]
