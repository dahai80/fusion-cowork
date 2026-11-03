"""issue #263: GitHub 仓库 onboarding 向导 — preview/apply/teardown 幂等 + REST 路由 (MockTransport 离线)。"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict

import httpx
import pytest
from fastapi.testclient import TestClient

from fusion_cowork.server.github_wizard import (
    DEFAULT_LABELS,
    ISSUE_FORMS,
    WIZARD_AUDIT_PATH,
    GithubWizard,
    _load_audit,
)

ORG = "my-org"
REPO = f"{ORG}/repo-a"
BRANCH = "main"
TOKEN = "tok-263"


def _b64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def _file(path: str, content: str) -> Dict[str, Any]:
    return {"path": path, "sha": f"sha-{path}", "content": _b64(content)}


def make_wizard(state: Dict[str, Any]) -> GithubWizard:
    """state: labels dict / files dict / protection bool / applied log — 全离线 MockTransport。"""

    def handler(request: httpx.Request) -> httpx.Response:
        m, path = request.method, request.url.path
        state.setdefault("calls", []).append(f"{m} {path}")
        if path == f"/orgs/{ORG}" and m == "GET":
            return httpx.Response(200, json={"login": ORG, "type": "Organization"})
        if path == f"/orgs/{ORG}/repos" and m == "GET":
            return httpx.Response(
                200, json=[{"name": "repo-a", "full_name": REPO, "private": False, "default_branch": BRANCH}]
            )
        if path == f"/repos/{REPO}" and m == "GET":
            return httpx.Response(200, json={"full_name": REPO, "default_branch": BRANCH})
        if path == f"/repos/{REPO}/labels" and m == "GET":
            return httpx.Response(200, json=list(state["labels"].values()))
        if path == f"/repos/{REPO}/labels" and m == "POST":
            body = json.loads(request.content)
            state["labels"][body["name"]] = body
            state["applied"].append(("create_label", body["name"]))
            return httpx.Response(201, json=body)
        if path.startswith(f"/repos/{REPO}/labels/") and m == "PATCH":
            body = json.loads(request.content)
            state["labels"][body["name"]] = body
            state["applied"].append(("update_label", body["name"]))
            return httpx.Response(200, json=body)
        if path.startswith(f"/repos/{REPO}/labels/") and m == "DELETE":
            name = path.rsplit("/", 1)[-1]
            state["labels"].pop(name, None)
            state["applied"].append(("delete_label", name))
            return httpx.Response(204)
        if path.startswith(f"/repos/{REPO}/contents/") and m == "GET":
            fpath = path.split("/contents/", 1)[1].split("?")[0]
            f = state["files"].get(fpath)
            if f is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=_file(fpath, f))
        if path.startswith(f"/repos/{REPO}/contents/") and m == "PUT":
            body = json.loads(request.content)
            fpath = path.split("/contents/", 1)[1]
            state["files"][fpath] = base64.b64decode(body["content"]).decode("utf-8")
            state["applied"].append(("put_file", fpath))
            return httpx.Response(201, json={"content": {"path": fpath, "sha": f"sha-{fpath}-new"}})
        if path.startswith(f"/repos/{REPO}/contents/") and m == "DELETE":
            body = json.loads(request.content)
            fpath = path.split("/contents/", 1)[1]
            state["files"].pop(fpath, None)
            state["applied"].append(("delete_file", fpath))
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/protection") and m == "GET":
            return httpx.Response(200, json={}) if state["protection"] else httpx.Response(404)
        if path.endswith("/protection") and m == "PUT":
            state["protection"] = True
            state["applied"].append(("set_protection", BRANCH))
            return httpx.Response(200, json={})
        if path.endswith("/protection") and m == "DELETE":
            state["protection"] = False
            state["applied"].append(("unset_protection", BRANCH))
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"no mock: {m} {path}"})

    def factory(timeout: float):
        return httpx.AsyncClient(base_url="https://gh.test", transport=httpx.MockTransport(handler), timeout=timeout)

    return GithubWizard(token=TOKEN, client_factory=factory)


@pytest.fixture
def state():
    return {"labels": {}, "files": {}, "protection": False, "applied": []}


class TestWizardService:
    @pytest.mark.asyncio
    async def test_preview_is_readonly(self, state):
        w = make_wizard(state)
        plans = await w.preview(ORG, [REPO])
        assert plans[0]["repo"] == REPO
        ops = [a["op"] for a in plans[0]["actions"]]
        assert "create_label" in ops and "create_file" in ops and "set_branch_protection" in ops
        assert state["applied"] == [] and not state["protection"]

    @pytest.mark.asyncio
    async def test_apply_creates_then_idempotent(self, state):
        w = make_wizard(state)
        first = await w.apply(ORG, [REPO])
        n_first = len(first[0]["applied"])
        assert n_first >= len(DEFAULT_LABELS) + len(ISSUE_FORMS) + 2
        assert (WIZARD_AUDIT_PATH) in state["files"]
        second = await w.apply(ORG, [REPO])
        # 幂等 — 第二轮只剩审计清单 1 个文件写
        assert len(second[0]["applied"]) == 1

    @pytest.mark.asyncio
    async def test_teardown_removes_only_wizard_artifacts(self, state):
        w = make_wizard(state)
        await w.apply(ORG, [REPO])
        state["files"]["README.md"] = "# keep"
        state["labels"]["type:bug"]["name"] = "type:bug"
        results = await w.teardown(ORG, [REPO])
        removed = {r["target"] for r in results[0]["removed"]}
        assert "README.md" not in removed
        assert WIZARD_AUDIT_PATH in removed
        assert "type:bug" in removed and "phase:done" in removed
        assert not state["protection"]
        for name in DEFAULT_LABELS:
            assert name["name"] not in state["labels"]

    @pytest.mark.asyncio
    async def test_teardown_without_audit_noop(self, state):
        w = make_wizard(state)
        results = await w.teardown(ORG, [REPO])
        assert results[0]["removed"] == [] and results[0]["note"]

    @pytest.mark.asyncio
    async def test_list_repos_org_fallback_user(self, state):
        w = make_wizard(state)
        repos = await w.list_repos(ORG)
        assert repos[0]["full_name"] == REPO

    def test_load_audit_fail_closed(self):
        assert _load_audit("not json") == {}


class TestWizardRoutes:
    def _client(self, monkeypatch, state):
        from fusion_cowork.server import github_wizard as gw
        from fusion_cowork.server.web_api import create_web_app

        monkeypatch.setattr(gw, "WIZARD_FACTORY", lambda: make_wizard(state))
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k263")
        app = create_web_app(space_data_dir=None)
        return TestClient(app)

    def test_routes_need_token(self, monkeypatch):

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from fusion_cowork.server import github_wizard as gw
        from fusion_cowork.server.web_api import create_web_app

        monkeypatch.setattr(gw, "WIZARD_FACTORY", None)
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k263")
        client = TestClient(create_web_app(space_data_dir=None))
        resp = client.get("/api/wizard/repos", params={"org": ORG}, headers={"x-api-key": "k263"})
        assert resp.status_code == 503

    def test_preview_route(self, monkeypatch, state):
        client = self._client(monkeypatch, state)
        resp = client.post("/api/wizard/preview", json={"org": ORG, "repos": [REPO]}, headers={"x-api-key": "k263"})
        assert resp.status_code == 200
        assert resp.json()["plans"][0]["repo"] == REPO

    def test_bad_request_missing_repos(self, monkeypatch, state):
        client = self._client(monkeypatch, state)
        assert client.post("/api/wizard/preview", json={"org": ORG}, headers={"x-api-key": "k263"}).status_code == 400
