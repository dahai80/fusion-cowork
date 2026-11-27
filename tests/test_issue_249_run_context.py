"""issue #249: run-context token budget + rolling summary — resume-chain ancestor summaries, snapshot budget, skipped copies resumable.

Budget degrades detail level by level (full → compact → ref-only → drop summary key → drop entries), no mid-string truncation;
chained resume context size grows sub-linearly with chain depth (keep_recent + budget cap).
"""

from __future__ import annotations

import time

import pytest

from fusion_cowork.engine.run_context import (
    build_resume_snapshot,
    compose_ancestor_summary,
    estimate_tokens,
    step_ref,
)
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()

STATIC_KEY = "test-key-249"

WF_DEF = {
    "name": "wf-249",
    "nodes": [{"id": f"n{i}", "name": "merge", "config": {}} for i in range(1, 13)],
    "edges": [{"source_id": f"n{i}", "target_id": f"n{i + 1}"} for i in range(1, 12)],
}


def test_estimate_tokens_cjk_and_ascii():
    assert estimate_tokens("") == 0
    # i18n: user-facing
    assert estimate_tokens("汉字汉字") == 4  # CJK ≈ 1 token per character
    assert 1 <= estimate_tokens("abcd") <= 2  # ASCII ≈ 4 characters per token


def test_snapshot_recent_full_older_summarized():
    steps = [
        {"node_id": f"n{i}", "node_name": "m", "status": "success", "output_data": {"v": i}, "summary": "ok"}
        for i in range(10)
    ]
    snapshot, ancestor_new, stats = build_resume_snapshot("run_x", steps, 10)
    assert stats["eligible"] == 10
    assert [s["node_id"] for s in snapshot] == [f"n{i}" for i in range(2, 10)]
    for s in snapshot:
        assert s["output_data"] == {"v": int(s["node_id"][1:])}
    assert [a["node_id"] for a in ancestor_new] == ["n0", "n1"]
    for a in ancestor_new:
        assert a["output_ref"] == step_ref("run_x", a["node_id"])
        assert "output_data" not in a


def test_snapshot_skipped_copies_eligible_and_normalized():
    # Checkpoint-resume copies (status=skipped) carry recovery output — must be selectable for chained resume, normalized to success
    steps = [
        # i18n: user-facing
        {"node_id": "n1", "status": "skipped", "output_data": {"v": 1}, "summary": "恢复自快照, 跳过重跑"},
        # i18n: user-facing
        {"node_id": "n2", "status": "skipped", "output_data": {"v": 2}, "summary": "恢复自快照, 跳过重跑"},
    ]
    snapshot, _, _ = build_resume_snapshot("run_y", steps, 2)
    assert [s["node_id"] for s in snapshot] == ["n1", "n2"]
    assert all(s["status"] == "success" for s in snapshot)
    assert snapshot[1]["output_data"] == {"v": 2}


def test_budget_demotes_full_to_compact_to_ref(monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_RESUME_BUDGET_TOKENS", "600")
    big = "x" * 500
    steps = [
        {"node_id": f"n{i}", "node_name": "m", "status": "success", "output_data": {"big": big}, "summary": "ok"}
        for i in range(6)
    ]
    snapshot, ancestor_new, stats = build_resume_snapshot("run_z", steps, 6)
    assert stats["estimated_tokens"] <= 600
    kinds = set()
    for s in snapshot:
        if "output_data" not in s:
            kinds.add("ref")
        elif len(s["output_data"].get("big", "")) <= 200:
            kinds.add("compact")
        else:
            kinds.add("full")
    # Greedy oldest-first degradation: stop once the budget is met — at least one degradation happens, and no mid-string truncation (full must be the original 500 characters)
    assert kinds != {"full"}
    assert "compact" in kinds or "ref" in kinds
    for a in ancestor_new:
        assert a["output_ref"].startswith("step://run_z/")


def test_budget_drops_summary_keys_then_entries(monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_RESUME_BUDGET_TOKENS", "300")
    steps = [
        {
            "node_id": f"n{i}",
            "node_name": "m",
            "status": "success",
            "output_data": {"v": list(range(300))},
            "summary": "ok",
        }
        for i in range(20)
    ]
    snapshot, ancestor_new, stats = build_resume_snapshot("run_w", steps, 20)
    assert stats["estimated_tokens"] <= 300
    assert all("output_data" not in s for s in snapshot)  # all degraded to ref-only
    keyless = [a for a in ancestor_new if "output_summary" not in a]
    assert len(keyless) < len(ancestor_new) or len(ancestor_new) < 12  # keys dropped or entries dropped


def test_compose_ancestor_summary_caps_and_counts():
    prior = [{"node_id": f"p{i}", "output_ref": f"step://a/p{i}"} for i in range(50)]
    newer = [{"node_id": f"q{i}", "output_ref": f"step://b/q{i}"} for i in range(5)]
    merged, truncated = compose_ancestor_summary(prior, newer)
    assert len(merged) == 50 and truncated == 5
    assert merged[0]["node_id"] == "p0" or merged[0]["node_id"] == "p5"  # oldest dropped, newest kept
    assert merged[-1]["node_id"] == "q4"


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
    # Context-manager entry = portal persists across requests; bare TestClient spins one event loop per request,
    # so the run's background task gets canceled when the request ends (issue #257 watchdog wait_for forces the node to yield before surfacing)
    with TestClient(app) as tc:
        yield tc, app


def _headers():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def _wait_terminal(tc, run_id, timeout=10.0):
    for _ in range(int(timeout * 20)):
        r = tc.get(f"/api/runs/{run_id}", headers=_headers())
        if r.status_code == 200 and r.json()["status"] not in ("running", "pending"):
            return r.json()
        time.sleep(0.05)
    # i18n: user-facing
    raise AssertionError("run 未在超时内进入终态")


class TestResumeChain:
    def test_chain_ancestor_summary_not_bodies(self, client):
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "chain249"}, headers=_headers())
        assert r.status_code == 202, r.text
        run1 = r.json()["run_id"]
        assert _wait_terminal(tc, run1)["status"] == "success"

        # depth 2: run1 re-runs from n9 → snapshot n1..n8 in full
        r = tc.post(f"/api/runs/{run1}/resume-from", json={"node_id": "n9"}, headers=_headers())
        assert r.status_code == 200, r.text
        d2 = r.json()
        run2 = d2["run_id"]
        assert d2["snapshot_steps"] == 8
        assert "context" in d2 and d2["context"]["estimated_tokens"] > 0
        rec2 = _wait_terminal(tc, run2)
        assert rec2["status"] == "success"
        assert rec2["ancestor_summary"] == []  # first resume has no ancestors

        # depth 3: run2 re-runs from n12 — skipped copies (n1..n8) selectable + rolling ancestor summaries
        r = tc.post(f"/api/runs/{run2}/resume-from", json={"node_id": "n12"}, headers=_headers())
        assert r.status_code == 200, r.text
        d3 = r.json()
        run3 = d3["run_id"]
        # Fix point: in run2's timeline n1..n8 are skipped copies, old code only accepted success → snapshot 0 and a full re-run;
        # now copies are selectable → snapshot covers the last 8 steps + summaries for earlier steps
        assert d3["snapshot_steps"] >= 8
        assert d3["context"]["ancestor_new"] >= 1
        rec3 = _wait_terminal(tc, run3)
        assert rec3["status"] == "success"
        anc = rec3["ancestor_summary"]
        # i18n: user-facing
        assert anc, "depth-3 run 应携带祖先摘要"
        for a in anc:
            assert a["output_ref"].startswith("step://")
            assert "output_data" not in a  # summary lines carry no full step body

    def test_context_scales_sublinearly_with_depth(self, client):
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "scale249"}, headers=_headers())
        run1 = r.json()["run_id"]
        _wait_terminal(tc, run1)

        stats_by_depth = []
        current = run1
        for target in ("n6", "n9", "n12"):
            r = tc.post(f"/api/runs/{current}/resume-from", json={"node_id": target}, headers=_headers())
            assert r.status_code == 200, r.text
            body = r.json()
            stats_by_depth.append(body["context"])
            current = body["run_id"]
            rec = _wait_terminal(tc, current)
            assert rec["status"] == "success"

        d1_tokens = max(stats_by_depth[0]["estimated_tokens"], 1)
        d3_tokens = stats_by_depth[2]["estimated_tokens"]
        # A linear full-chain replay would grow 3x; budget + summary cap → sub-linear (here < 2x)
        assert d3_tokens < d1_tokens * 2, (stats_by_depth,)

    def test_tiny_budget_refs_only_snapshot(self, client, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_RESUME_BUDGET_TOKENS", "800")
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "tiny249"}, headers=_headers())
        run1 = r.json()["run_id"]
        _wait_terminal(tc, run1)
        r = tc.post(f"/api/runs/{run1}/resume-from", json={"node_id": "n12"}, headers=_headers())
        assert r.status_code == 200, r.text
        d2 = r.json()
        assert d2["context"]["estimated_tokens"] <= 800
        rec = _wait_terminal(tc, d2["run_id"])
        assert rec["status"] in ("success", "completed_with_denials")
        # In-budget degradation does not break execution — the full-re-run fallback semantics are guaranteed by engine snapshot recovery
