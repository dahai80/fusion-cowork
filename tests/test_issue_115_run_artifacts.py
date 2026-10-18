"""#115 W10/B5: Runs 详情产物预览 — 步骤输出提取 + /api/runs/{id}/artifacts。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

STATIC_KEY = "fc-web-test-115"


def _step(node_id, output_data, node_name="n"):
    return SimpleNamespace(
        node_id=node_id,
        node_name=node_name,
        node_display_name=node_name,
        status=SimpleNamespace(value="success"),
        execution_time=0.1,
        summary="",
        error=None,
        output_data=output_data,
    )


class TestCollectArtifacts:
    def test_file_path_markdown(self, tmp_path):
        from fusion_cowork.server.web_api import _collect_artifacts

        p = tmp_path / "report.md"
        p.write_text("# Hello\n\nworld", encoding="utf-8")
        items = _collect_artifacts([_step("n1", {"file_path": str(p)})])
        assert len(items) == 1
        assert items[0]["type"] == "markdown"
        assert items[0]["name"] == "report.md"
        assert "# Hello" in items[0]["content"]
        assert items[0]["truncated"] is False

    def test_inline_text_fallback(self):
        from fusion_cowork.server.web_api import _collect_artifacts

        items = _collect_artifacts([_step("n2", {"markdown": "# inline"})])
        assert len(items) == 1
        assert items[0]["type"] == "markdown"
        assert items[0]["content"] == "# inline"

    def test_missing_file_is_listed_without_content(self, tmp_path):
        from fusion_cowork.server.web_api import _collect_artifacts

        items = _collect_artifacts([_step("n3", {"file_path": str(tmp_path / "gone.md")})])
        assert len(items) == 1
        assert items[0]["content"] == ""

    def test_truncation(self, tmp_path):
        from fusion_cowork.server.web_api import _MAX_ARTIFACT_BYTES, _collect_artifacts

        p = tmp_path / "big.txt"
        p.write_text("x" * (_MAX_ARTIFACT_BYTES + 100), encoding="utf-8")
        items = _collect_artifacts([_step("n4", {"file_path": str(p)})])
        assert items[0]["truncated"] is True
        assert len(items[0]["content"]) == _MAX_ARTIFACT_BYTES

    def test_max_artifacts(self):
        from fusion_cowork.server.web_api import _MAX_ARTIFACTS, _collect_artifacts

        steps = [_step(f"n{i}", {"content": "x"}) for i in range(_MAX_ARTIFACTS + 5)]
        assert len(_collect_artifacts(steps)) == _MAX_ARTIFACTS

    def test_non_dict_output_skipped(self):
        from fusion_cowork.server.web_api import _collect_artifacts

        assert _collect_artifacts([_step("n5", None), _step("n6", {})]) == []


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
    return TestClient(app), app


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestArtifactRoute:
    def _seed(self, registry, run_id, artifacts):
        registry._runs[run_id] = {
            "run_id": run_id,
            "workflow_id": "wf-1",
            "workflow_name": "wf",
            "status": "success",
            "created_at": 1.0,
            "finished_at": time.time(),
            "steps": [],
            "result": None,
            "error": None,
            "review": {"status": "pending_review", "note": "", "reviewed_at": None},
            "workflow_def": {},
            "artifacts": artifacts,
        }

    def test_list_artifacts(self, client):
        tc, app = client
        self._seed(
            app.state.run_registry,
            "run_a1",
            [{"artifact_id": "n1:path", "name": "r.md", "type": "markdown", "content": "# x"}],
        )
        body = tc.get("/api/runs/run_a1/artifacts", headers=_auth()).json()
        assert body["count"] == 1
        assert body["artifacts"][0]["name"] == "r.md"
        assert body["review"]["status"] == "pending_review"

    def test_missing_run(self, client):
        tc, _ = client
        assert tc.get("/api/runs/run_none/artifacts", headers=_auth()).status_code == 404
