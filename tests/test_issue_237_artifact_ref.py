"""issue #237: Artifacts GUI 深链 artifact_ref — 状态区分 + 失败提示 + run 上下文透传。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

STATIC_KEY = "test-key-237"

CARD = {
    "id": "art_1",
    "name": "Q3 Report",
    "type": "report",
    "kind": "document",
    "current_version": 3,
    "summary": "十月对账异常项汇总",
    "source_module": "fusion-doc",
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setattr(web_api, "_WEB_DIR", tmp_path / "web")
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def _transport(status: int, payload: dict | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/artifacts/art_1"
        return httpx.Response(status, json=payload or {})

    return httpx.MockTransport(handler)


class TestFetchStatus:
    def test_success_reports_200(self):
        from fusion_cowork.server import web_api

        card, status = asyncio.run(
            web_api._fetch_artifact_card_with_status("art_1", transport=_transport(200, {"data": CARD}))
        )
        assert status == 200
        assert card and card["artifact_id"] == "art_1"

    @pytest.mark.parametrize("upstream", [401, 403, 404])
    def test_gone_or_forbidden_reports_upstream_status(self, upstream):
        from fusion_cowork.server import web_api

        card, status = asyncio.run(web_api._fetch_artifact_card_with_status("art_1", transport=_transport(upstream)))
        assert card is None
        assert status == upstream

    def test_transport_failure_reports_none(self):
        from fusion_cowork.server import web_api

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        card, status = asyncio.run(
            web_api._fetch_artifact_card_with_status("art_1", transport=httpx.MockTransport(boom))
        )
        assert card is None
        assert status is None

    def test_invalid_id_short_circuits(self):
        from fusion_cowork.server import web_api

        card, status = asyncio.run(web_api._fetch_artifact_card_with_status("../etc/passwd"))
        assert (card, status) == (None, None)


class TestRefRoute:
    def _fake(self, monkeypatch, result):
        from fusion_cowork.server import web_api

        async def fake_fetch(artifact_id, transport=None):
            return result

        monkeypatch.setattr(web_api, "_fetch_artifact_card_with_status", fake_fetch)

    def test_valid_artifact_returns_card_and_ref_text(self, client, monkeypatch):
        self._fake(monkeypatch, (dict(CARD, artifact_id="art_1", deep_link="/shell/fusion-artifacts/#/a/art_1"), 200))
        r = client.get("/api/artifacts/refs/art_1", headers=_auth())
        assert r.status_code == 200, r.text
        assert r.json()["artifact"]["ref_text"].startswith("[Artifact: Q3 Report | ID: art_1")

    @pytest.mark.parametrize("upstream", [401, 403, 404])
    def test_missing_or_denied_is_404_not_found(self, client, monkeypatch, upstream):
        self._fake(monkeypatch, (None, upstream))
        r = client.get("/api/artifacts/refs/art_1", headers=_auth())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "artifact_not_found"

    @pytest.mark.parametrize("upstream", [None, 500, 502, 503])
    def test_engine_unreachable_is_502(self, client, monkeypatch, upstream):
        self._fake(monkeypatch, (None, upstream))
        r = client.get("/api/artifacts/refs/art_1", headers=_auth())
        assert r.status_code == 502
        assert r.json()["detail"]["code"] == "upstream_unavailable"

    def test_bad_id_is_404_bad_id(self, client):
        r = client.get("/api/artifacts/refs/bad%20id", headers=_auth())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "bad_id"


class TestRunContextCarriesArtifact:
    def test_artifact_labels_and_inputs_reach_run_registry(self, client, monkeypatch):
        started = {}

        def _record_start(
            workflow_def,
            workflow_id="",
            workflow_name="",
            memory=True,
            message_input=None,
            source="",
            labels=None,
            space_id="",
            **kwargs,
        ):
            started.update({"message_input": message_input, "labels": labels, "source": source})
            return {"run_id": "run_stub", "status": "running"}

        monkeypatch.setattr(client.app.state.run_registry, "start", _record_start)
        ref_text = "[Artifact: Q3 Report | ID: art_1 | Version: v3 | Type: report | Summary: 十月对账异常项汇总]"
        body = {
            "name": "from artifact",
            "workflow": {"name": "wf", "nodes": [{"id": "n1", "name": "echo_probe_237", "config": {}}], "edges": []},
            "source": "home",
            "labels": {"artifact_ref": "art_1", "artifact_name": "Q3 Report"},
            "inputs": {"artifact_ref": ref_text, "artifact_id": "art_1", "artifact_name": "Q3 Report"},
        }
        r = client.post("/api/runs", json=body, headers=_auth())
        assert r.status_code == 202, r.text
        assert started["labels"]["artifact_ref"] == "art_1"
        assert started["message_input"]["artifact_ref"] == ref_text
        assert started["source"] == "home"
