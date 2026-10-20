"""Issue #135 F-2 回归测试 — /api/runs 坏边形状 400 而非 500 KeyError。

Edge.from_dict / Workflow.from_dict 对缺 source_id/target_id 的边抛可读
ValueError (对齐既有含环 ValueError 先例), runs_create API 边界转 400。
"""

from __future__ import annotations

import pytest

from fusion_cowork.engine.workflow import Edge, Workflow

STATIC_KEY = "test-key-135"


def test_edge_from_dict_missing_source_id_raises_valueerror():
    with pytest.raises(ValueError) as exc_info:
        Edge.from_dict({"from": "n1", "to": "n2"})
    assert "source_id" in str(exc_info.value)


def test_edge_from_dict_missing_target_id_raises_valueerror():
    with pytest.raises(ValueError) as exc_info:
        Edge.from_dict({"source_id": "n1"})
    assert "target_id" in str(exc_info.value)


def test_edge_from_dict_blank_ids_raise_valueerror():
    with pytest.raises(ValueError):
        Edge.from_dict({"source_id": "  ", "target_id": "n2"})


def test_edge_from_dict_valid_roundtrip_unchanged():
    edge = Edge.from_dict({"source_id": "n1", "target_id": "n2", "label": "x"})
    assert edge.source_id == "n1"
    assert edge.target_id == "n2"
    assert edge.source_output == "output"
    assert edge.target_input == "input"
    assert edge.label == "x"


def test_workflow_from_dict_malformed_edges_valueerror_not_keyerror():
    wf_def = {
        "name": "bad-edges",
        "nodes": {"n1": {"name": "nonexistent_type"}, "n2": {"name": "nonexistent_type"}},
        "edges": [{"from": "n1", "to": "n2"}],
    }
    with pytest.raises(ValueError) as exc_info:
        Workflow.from_dict(wf_def)
    assert "source_id" in str(exc_info.value)


def test_workflow_from_dict_valid_edges_still_load():
    wf_def = {
        "name": "good-edges",
        "nodes": {"n1": {"name": "nonexistent_type"}, "n2": {"name": "nonexistent_type"}},
        "edges": [{"source_id": "n1", "target_id": "n2"}],
    }
    wf = Workflow.from_dict(wf_def)
    assert len(wf.edges) == 1
    assert wf.edges[0].source_id == "n1"
    assert wf.edges[0].target_id == "n2"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestRunsCreateEdgeShape:
    def test_malformed_edges_400_not_500(self, client):
        r = client.post(
            "/api/runs",
            json={
                "name": "bad",
                "workflow": {
                    "name": "bad",
                    "nodes": {"n1": {"name": "nonexistent_type"}, "n2": {"name": "nonexistent_type"}},
                    "edges": [{"from": "n1", "to": "n2"}],
                },
            },
            headers=_auth(),
        )
        assert r.status_code == 400
        assert "source_id" in r.json()["detail"]["message"]

    def test_cyclic_edges_400(self, client):
        r = client.post(
            "/api/runs",
            json={
                "name": "cycle",
                "workflow": {
                    "name": "cycle",
                    "nodes": {"n1": {"name": "nonexistent_type"}, "n2": {"name": "nonexistent_type"}},
                    "edges": [
                        {"source_id": "n1", "target_id": "n2"},
                        {"source_id": "n2", "target_id": "n1"},
                    ],
                },
            },
            headers=_auth(),
        )
        assert r.status_code == 400
        assert "环" in r.json()["detail"]["message"]

    def test_valid_workflow_still_202(self, client):
        r = client.post(
            "/api/runs",
            json={
                "name": "ok",
                "workflow": {
                    "name": "ok",
                    "nodes": {"n1": {"name": "nonexistent_type"}, "n2": {"name": "nonexistent_type"}},
                    "edges": [{"source_id": "n1", "target_id": "n2"}],
                },
            },
            headers=_auth(),
        )
        assert r.status_code == 202
        assert r.json()["run_id"]
