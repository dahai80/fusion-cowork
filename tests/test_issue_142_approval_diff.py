"""Issue #142 回归测试 — 审批 diff 逐块审阅 + 文件产物 unified diff。

- ast_diff 文本工具: 分块 / 逐块选择重建 / unified diff
- 本地待审批详情附 diff_blocks + raw_edit
- confirm accepted_blocks: 部分接受窄化 / 全拒不落规则
- _collect_artifacts: 编辑对/diff 字段/.patch 后缀 → diff 产物
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

STATIC_KEY = "test-key-142"


def test_text_diff_blocks_multi_hunk():
    from fusion_cowork.engine.ast_diff import text_diff_blocks

    old = "line1\nline2\nline3\nline4\nline5\n"
    new = "line1\nXX\nline3\nline4\nYY\n"
    blocks = text_diff_blocks(old, new)
    tags = [(b["index"], b["tag"]) for b in blocks]
    assert (0, "equal") in tags and (1, "replace") in tags and (3, "replace") in tags
    assert blocks[1]["old"] == "line2\n" and blocks[1]["new"] == "XX\n"


def test_apply_block_selection_partial():
    from fusion_cowork.engine.ast_diff import apply_block_selection

    old = "line1\nline2\nline3\nline4\nline5\n"
    new = "line1\nXX\nline3\nline4\nYY\n"
    assert apply_block_selection(old, new, [1]) == "line1\nXX\nline3\nline4\nline5\n"
    assert apply_block_selection(old, new, []) == old
    assert apply_block_selection(old, new, [1, 3]) == new
    # equal 块不在可选集内 — 只传 equal 索引等价于无有效选择, 回退原文
    assert apply_block_selection(old, new, [0]) == old


def test_unified_diff_text_headers():
    from fusion_cowork.engine.ast_diff import unified_diff_text

    d = unified_diff_text("a\nb\n", "a\nc\n", "old.py", "new.py")
    lines = d.splitlines()
    assert lines[0] == "--- old.py"
    assert lines[1] == "+++ new.py"
    assert any(x.startswith("@@") for x in lines)
    assert "+c" in lines and "-b" in lines


def test_local_pending_details_diff_blocks():
    from fusion_cowork.engine.permission_manager import PermissionManager

    pm = PermissionManager()
    ok = asyncio.run(
        pm.check(
            "apply_edit",
            "execute",
            {"file_path": "/tmp/demo.txt", "old_text": "a\nb\n", "new_text": "a\nc\n"},
        )
    )
    assert ok is False
    entries = pm.list_pending()
    assert entries and entries[0]["tool_name"] == "apply_edit"
    assert entries[0]["diff_blocks"], "编辑型待审批应附 diff 分块"
    assert any(b["tag"] != "equal" for b in entries[0]["diff_blocks"])
    raw = entries[0]["raw_edit"]
    assert raw["old"] == "a\nb\n" and raw["new"] == "a\nc\n"
    assert raw["old_key"] == "old_text" and raw["new_key"] == "new_text"


def test_apply_edit_pending_via_engine_run(client):
    import time as _t

    pm = client.app.state.permission_manager
    r = client.post(
        "/api/runs",
        json={
            "name": "wf-edit-142",
            "workflow": {
                "name": "wf-edit-142",
                "nodes": {
                    "n1": {
                        "name": "apply_edit",
                        "config": {"params": {"file_path": "/tmp/x.txt", "old_text": "a\n", "new_text": "b\n"}},
                    }
                },
                "edges": [],
            },
        },
        headers=_auth(),
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    deadline = _t.time() + 5
    while _t.time() < deadline:
        rec = client.get(f"/api/runs/{run_id}", headers=_auth()).json()
        if rec.get("status") in ("completed", "failed", "canceled"):
            break
        _t.sleep(0.1)
    pend = [e for e in pm.list_pending() if e.get("workflow") == "wf-edit-142"]
    assert pend, "apply_edit 高危拒绝应落待审批队列"
    assert pend[0]["diff_blocks"]


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    import fusion_cowork.engine.permission_manager as _perm

    monkeypatch.setattr(_perm, "_PERMISSIONS_FILE", str(tmp_path / "perms.json"))
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app)


def _seed_edit_pending(pm, action_id):
    pm._pending_local_approvals[action_id] = "apply_edit"
    pm._pending_local_details[action_id] = {
        "tool_name": "apply_edit",
        "params": {"file_path": "/tmp/demo.txt", "old_text": "a\nb\nc\n", "new_text": "a\nXX\nc\nYY\n"},
        "ts": time.time(),
        "workflow": "wf",
        "node_id": "n1",
        "diff_blocks": [
            {"index": 0, "tag": "equal", "old": "a\n", "new": "a\n"},
            {"index": 1, "tag": "replace", "old": "b\n", "new": "XX\n"},
            {"index": 2, "tag": "equal", "old": "c\n", "new": "c\n"},
            {"index": 3, "tag": "insert", "old": "", "new": "YY\n"},
        ],
        "raw_edit": {"old_key": "old_text", "new_key": "new_text", "old": "a\nb\nc\n", "new": "a\nXX\nc\nYY\n"},
    }


class TestConfirmAcceptedBlocks:
    def test_partial_accept_narrows_params(self, client):
        pm = client.app.state.permission_manager
        _seed_edit_pending(pm, "perm_blks1")
        try:
            r = client.post(
                "/api/permissions/pending/perm_blks1/confirm",
                json={"approved": True, "scope": "*", "accepted_blocks": [1]},
                headers=_auth(),
            )
            body = r.json()
            assert body["confirmed"] is True
            assert body["edited_params"]["new_text"] == "a\nXX\nc\n"
        finally:
            pm._pending_local_approvals.pop("perm_blks1", None)
            pm._pending_local_details.pop("perm_blks1", None)
        assert any(x.tool_name == "apply_edit" and x.allowed for x in pm.rules)

    def test_all_rejected_no_rule(self, client):
        pm = client.app.state.permission_manager
        _seed_edit_pending(pm, "perm_blks2")
        try:
            r = client.post(
                "/api/permissions/pending/perm_blks2/confirm",
                json={"approved": True, "scope": "*", "accepted_blocks": []},
                headers=_auth(),
            )
            body = r.json()
            assert body["confirmed"] is False
            assert "拒绝" in body["reason"]
        finally:
            pm._pending_local_approvals.pop("perm_blks2", None)
            pm._pending_local_details.pop("perm_blks2", None)
        assert not any(x.tool_name == "apply_edit" for x in pm.rules)

    def test_full_accept_no_narrowing(self, client):
        pm = client.app.state.permission_manager
        _seed_edit_pending(pm, "perm_blks3")
        try:
            r = client.post(
                "/api/permissions/pending/perm_blks3/confirm",
                json={"approved": True, "scope": "*", "accepted_blocks": [1, 3]},
                headers=_auth(),
            )
            body = r.json()
        finally:
            pm._pending_local_approvals.pop("perm_blks3", None)
            pm._pending_local_details.pop("perm_blks3", None)
        assert body["confirmed"] is True
        assert "edited_params" not in body


class TestArtifactDiff:
    def test_edit_input_pair_produces_diff_artifact(self):
        from fusion_cowork.server.web_api import _collect_artifacts

        step = SimpleNamespace(
            node_id="n1",
            node_name="apply_edit",
            node_display_name="文件编辑",
            output_data={"success": True, "changes": 1},
            input_data={"file_path": "/tmp/demo.txt", "old_text": "a\nb\n", "new_text": "a\nc\n"},
        )
        artifacts = _collect_artifacts([step])
        diff_items = [a for a in artifacts if a["type"] == "diff"]
        assert diff_items, "编辑对输入应产出 diff 产物"
        assert diff_items[0]["content"].startswith("---")
        assert diff_items[0]["artifact_id"] == "n1:diff"

    def test_output_diff_field_produces_diff_artifact(self):
        from fusion_cowork.server.web_api import _collect_artifacts

        step = SimpleNamespace(
            node_id="n2",
            node_name="shell_exec",
            node_display_name="Shell",
            output_data={"diff": "--- a.txt\n+++ b.txt\n@@ -1 +1 @@\n-x\n+y\n"},
            input_data={},
        )
        artifacts = _collect_artifacts([step])
        assert artifacts[0]["type"] == "diff"
        assert "+y" in artifacts[0]["content"]

    def test_patch_suffix_file_is_diff_type(self, tmp_path):
        from fusion_cowork.server.web_api import _collect_artifacts

        p = tmp_path / "change.patch"
        p.write_text("--- a\n+++ b\n", encoding="utf-8")
        step = SimpleNamespace(
            node_id="n3",
            node_name="file_output",
            node_display_name="输出",
            output_data={"path": str(p)},
            input_data={},
        )
        artifacts = _collect_artifacts([step])
        assert artifacts[0]["type"] == "diff"
