"""issue #172 + #173/#174: /api/models cold-cache 200 envelope + shell_registry raw_key issued-key extraction.

#173/#174 Contract:
- The fusion-gateway K1 issuance response (writeShellKeyResponse) carries the plaintext under raw_key
  (top level or nested in data); _extract_issued_key must recognize it, otherwise the fm- key is shown
  once and then silently lost.
- Legacy field names (api_key/issued_key/key/module_key) stay back-compatible (see test_issue_168).

#172 Contract:
- /api/models with a missing models client (mlx=None) → 200 + unavailable=True empty-list envelope, no more 503.
- Frontend polling gets a 200 and stops retrying, eliminating the 503 spam in the shell tab.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fusion_cowork.server import shell_registry, web_api
from fusion_cowork.server.shell_registry import _extract_issued_key, load_module_key


@pytest.fixture(autouse=True)
def _key_path(tmp_path, monkeypatch):
    fake = tmp_path / "web_module_key"
    monkeypatch.setattr(shell_registry, "MODULE_KEY_PATH", fake)
    yield fake


def _response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    payload = payload if payload is not None else {}
    resp.text = json.dumps(payload)
    resp.json = lambda: payload
    return resp


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0) if self._responses else _response(200, {})


async def _drive(client, manifest, rounds=0.08):
    task = asyncio.create_task(shell_registry._registry_loop_body(client, manifest, interval=0.01))
    await asyncio.sleep(rounds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestRawKeyIssuance:
    def test_extract_raw_key_top_level(self):
        payload = {"code": "ok", "message": "issued", "module_id": "fusion-cowork", "key_id": "k1", "raw_key": "fm-x"}
        assert _extract_issued_key(payload) == "fm-x"

    def test_extract_raw_key_nested_data(self):
        assert _extract_issued_key({"data": {"raw_key": "fm-nested"}}) == "fm-nested"

    def test_raw_key_wins_over_legacy_fields(self):
        assert _extract_issued_key({"raw_key": "fm-new", "api_key": "old"}) == "fm-new"

    async def test_gateway_shaped_response_persists_key(self, monkeypatch, _key_path):
        # issue #173 reproduction path: the real gateway returns only raw_key, previously silently dropped
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        issued = {
            "code": "ok",
            "message": "key issued",
            "module_id": "fusion-cowork",
            "key_id": "k1",
            "raw_key": "fm-live",
        }
        client = _FakeClient([_response(200, issued), _response(200, {})])
        await _drive(client, {"id": "fusion-cowork"})
        assert load_module_key() == "fm-live"


class _NullMlx:
    pass


def _build_client(tmp_path, store_root, monkeypatch, nl_generator):
    from fastapi.testclient import TestClient

    from fusion_cowork.engine import WorkflowEngine

    web_api._WEB_DIR = tmp_path / "web"
    monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
    engine = WorkflowEngine()
    app = web_api.create_web_app(
        engine=engine, dist_dir=tmp_path / "no-dist", store_root=store_root, nl_generator=nl_generator
    )
    return TestClient(app)


class TestModelsUnavailableEnvelope:
    def test_no_mlx_client_returns_200_envelope(self, tmp_path, monkeypatch):
        client = _build_client(tmp_path, tmp_path / "workflows", monkeypatch, SimpleNamespace(mlx=None))
        resp = client.get("/api/models")
        assert resp.status_code == 200, "mlx 客户端缺失应返回 200 envelope (issue #172), 而非 503"  # i18n: user-facing
        body = resp.json()
        assert body["models"] == []
        assert body["count"] == 0
        assert body["unavailable"] is True
        assert body["stale"] is False

    def test_envelope_all_param_same_shape(self, tmp_path, monkeypatch):
        client = _build_client(tmp_path, tmp_path / "workflows", monkeypatch, SimpleNamespace(mlx=None))
        body = client.get("/api/models?all=1").json()
        assert body["unavailable"] is True
        assert body["models"] == []
