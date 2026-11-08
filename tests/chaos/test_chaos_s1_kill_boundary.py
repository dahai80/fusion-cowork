"""Chaos S1: kill -9 at a node boundary (issue #386, spec §3.2 row 1).

Source defect class: cow#372 (in-memory run state died with the process,
run rows lost). Assertion: the run state stays honest across a hard kill
simulated by abandoning an in-flight effect and reopening the store —
the event journal + enqueued effect survive (WAL + synchronous=FULL),
boot reconcile requeues the leased effect, and re-committing the same
step does NOT duplicate side effects.
"""

from __future__ import annotations

import pytest
from fusion_harness import OutboxStore

pytestmark = [pytest.mark.chaos]


def _store(tmp_path):
    # lease_seconds=0: the reopened "reboot" pass must be able to requeue the
    # abandoned in-flight effect immediately (real daemons wait out the lease)
    return OutboxStore(str(tmp_path / "chaos_s1.db"), lease_seconds=0.0)


class TestKillAtNodeBoundary:
    def test_kill_between_commit_and_execution_survives(self, tmp_path, require_harness):
        store = _store(tmp_path)
        run_id = "run-chaos-s1"

        effect_ids = store.commit_step(
            run_id,
            [{"type": "node_result", "payload": {"node_id": "n1", "status": "success"}}],
            [{"node_id": "n1", "action": "github_write", "payload": {"op": "issue_create"}}],
        )
        assert len(effect_ids) == 1

        # claim = the process is mid-execution here; kill -9 abandons the lease
        claimed = store.claim_next()
        assert claimed is not None and claimed.id == effect_ids[0]

        # hard kill: NO settle, NO close — reopen the db cold
        store.close()
        reopened = _store(tmp_path)

        # nothing lost, nothing duplicated: still exactly one effect
        assert len(reopened.effects_for_run(run_id)) == 1

        # boot reconcile requeues the expired lease
        report = reopened.reconcile()
        assert report.requeued_effects == 1

        # the effect is deliverable again with attempts bumped (at-least-once)
        redelivered = reopened.claim_next()
        assert redelivered is not None
        assert redelivered.receipt_key == "run-chaos-s1:n1:github_write"
        assert redelivered.attempts == 2

        reopened.settle(redelivered.id, ok=True)
        assert reopened.effect(redelivered.id).status == "done"
        reopened.close()

    def test_recommit_same_step_does_not_duplicate_effects(self, tmp_path, require_harness):
        store = _store(tmp_path)
        run_id = "run-chaos-s1-dup"
        effects = [{"node_id": "n1", "action": "github_write", "payload": {"op": "issue_create"}}]

        first = store.commit_step(run_id, [{"type": "start", "payload": {}}], effects)
        # replayed commit after a crash before the first was consumed
        second = store.commit_step(run_id, [{"type": "start", "payload": {}}], effects)

        assert len(first) == 1
        assert second == [], "same receipt_key must dedupe (no duplicate side effect)"
        assert len(store.effects_for_run(run_id)) == 1
        store.close()

    def test_orphaned_run_effects_retire_honestly(self, tmp_path, require_harness):
        store = _store(tmp_path)
        store.commit_step(
            "run-orphan",
            [{"type": "node_result", "payload": {}}],
            [{"node_id": "n1", "action": "github_write", "payload": {}}],
        )
        report = store.reconcile(orphaned_run_ids=["run-orphan"])
        assert report.retired_effects == 1 and report.orphaned_runs == 1
        effect = store.effects_for_run("run-orphan")[0]
        assert effect.status == "retired" and effect.last_error == "run orphaned"
        store.close()
