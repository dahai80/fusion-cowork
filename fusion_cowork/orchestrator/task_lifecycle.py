"""Task lifecycle guard — kernel-derived transition table (issue #533 item 4).

#533 K4 Phase B item-4 ruling (2026-11-19): AgentOrchestrator's task
lifecycle converges onto the kernel TeamTask state machine topology.
fusion_harness.team_runtime._TASK_TRANSITIONS is the source graph; the
cowork table below is that graph with kernel statuses renamed to the
product vocabulary (todo=pending, in_progress=running, done=completed)
plus documented product extensions the kernel models differently:
- failed: cowork terminal error state (kernel reports failure via member
  messages, no failed status — audit A-4/A-7 honest-failure rulings)
- skipped: upstream-failed dependency short-circuit (audit deadlock
  detection)
- pending -> cancelled / skipped, failed -> pending (acceptance-rejected
  rework reopen, v2 P2)
Kernel TeamRuntime remains the team entity (space plane #496); AgentTask
stays the product task record (acceptance/retrospective fields have no
kernel counterpart). Illegal transitions raise TaskTransitionError —
fail loudly, no silent status drift.
"""

from __future__ import annotations

import logging
from typing import Any, Set, Tuple

from ..engine._harness_guard import harness_import_error

logger = logging.getLogger(__name__)

try:  # kernel state machine is the single source topology
    import fusion_harness.team_runtime as _kernel_tr
except ImportError as exc:  # issue #521: mandatory dependency, fail fast
    raise harness_import_error(exc) from exc

# kernel graph core-lifecycle statuses renamed into the cowork product
# vocabulary. queued/blocked are kernel scheduling states cowork models via
# resource locks (P3-2), not statuses — their edges are dropped, not renamed.
_KERNEL_TO_COWORK = {
    "todo": "pending",
    "in_progress": "running",
    "done": "completed",
    "cancelled": "cancelled",
}


def _derive_kernel_transitions() -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for src, dst in _kernel_tr._TASK_TRANSITIONS:
        if src not in _KERNEL_TO_COWORK or dst not in _KERNEL_TO_COWORK:
            continue
        edge = (_KERNEL_TO_COWORK[src], _KERNEL_TO_COWORK[dst])
        if edge[0] != edge[1]:
            out.add(edge)
    return out


# kernel-derived edges + documented product extensions (running -> failed /
# cancelled carry the kernel blocked->cancelled cancellation semantics into
# the execution state; failed/skipped have no kernel status counterpart)
TASK_TRANSITIONS: Set[Tuple[str, str]] = _derive_kernel_transitions() | {
    ("pending", "cancelled"),
    ("pending", "failed"),
    ("pending", "skipped"),
    ("running", "cancelled"),
    ("running", "failed"),
    ("running", "skipped"),
    ("skipped", "cancelled"),
    ("failed", "pending"),  # acceptance-rejected rework reopen (v2 P2)
    ("completed", "pending"),  # acceptance-rejected rework on a completed task
}

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "skipped")


class TaskTransitionError(ValueError):
    """Illegal task status transition (kernel TeamStateError counterpart)."""


def transition_allowed(current: str, target: str) -> bool:
    if current == target:
        return True  # idempotent rewrite (P2-3 racing terminal writes)
    return (current, target) in TASK_TRANSITIONS


def transition_task(task: Any, target: str, reason: str = "") -> str:
    """Guarded status write: validates against the kernel-derived table.

    Returns the effective status. Idempotent same-status writes are no-ops
    (P2-3 first-terminal-wins); terminal -> other-status writes are refused
    via the transition table.
    """
    current = task.status
    if current == target:
        return current
    if not transition_allowed(current, target):
        raise TaskTransitionError(
            f"illegal task transition {current} -> {target} (task={getattr(task, 'task_id', '?')})"
        )
    logger.debug(f"task transition: {current} -> {target} {reason or ''}")
    task.status = target
    return target


def claim_task(task: Any, claimant: str) -> bool:
    """Atomic compare-and-set claim (kernel claim_task semantics): exactly
    one concurrent claimant wins; the winner records the claim on the task."""
    claimed = getattr(task, "_claimed_by", "")
    if claimed:
        if claimed == claimant:
            return True
        logger.info(f"task claim lost task={getattr(task, 'task_id', '?')} claimant={claimant}")
        return False
    task._claimed_by = claimant
    return True
