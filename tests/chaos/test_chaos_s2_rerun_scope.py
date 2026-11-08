"""Chaos S2: re-run the same scope after failure (issue #386, spec §3.2 row 2).

Source defect: cow#381 — a failed run re-run created a DUPLICATE issue
and a second PR instead of reusing the existing open one. Assertion: the
receipt layer (kernel OutboxStore command receipts via the cowork
OutboxBridge facade) makes the replay a no-op returning the recorded
result; the underlying write fn executes exactly once.
"""

from __future__ import annotations

import pytest

from fusion_cowork.engine.outbox import OutboxBridge

pytestmark = [pytest.mark.chaos]


class TestRerunSameScope:
    async def test_github_write_replays_as_noop(self, tmp_path, require_harness):
        bridge = OutboxBridge(str(tmp_path / "chaos_s2.db"))
        calls = {"n": 0}

        def create_issue():
            # stands in for GitHubNode issue_create HTTP write
            calls["n"] += 1
            return {"issue_number": 42, "issue_url": "https://github.com/o/r/issues/42"}

        cmd_id = "run-chaos-s2:n1:issue_create"
        result, replayed = bridge.execute_idempotent(cmd_id, "github:o/r", create_issue)
        assert replayed is False and result["issue_number"] == 42 and calls["n"] == 1

        # the run failed downstream and the whole scope is re-run as-is
        result2, replayed2 = bridge.execute_idempotent(cmd_id, "github:o/r", create_issue)
        assert replayed2 is True
        assert result2["issue_number"] == 42
        assert calls["n"] == 1, "replayed command must NOT re-issue the GitHub write"

    async def test_async_write_receipt_skips_on_replay(self, tmp_path, require_harness):
        bridge = OutboxBridge(str(tmp_path / "chaos_s2b.db"))
        calls = {"n": 0}

        async def write():
            calls["n"] += 1
            return {"pr_number": 7}

        result, replayed = await bridge.execute_idempotent_async("run-chaos-s2b:n1:pr_write", "github:o/r", write)
        assert replayed is False and calls["n"] == 1

        result2, replayed2 = await bridge.execute_idempotent_async("run-chaos-s2b:n1:pr_write", "github:o/r", write)
        assert replayed2 is True and result2 == {"pr_number": 7}
        assert calls["n"] == 1
