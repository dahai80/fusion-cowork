"""issue #249: run-context token budget + rolling summary — the resume chain no longer replays full ancestor step bodies.

Three-tier detail demotion (dropped in order when over budget, no mid-string truncation):
  full body (output_data as-is) → compact (scalars capped at 200 chars / 10 keys) → ref-only
  (only the step://<run>/<node> reference line remains) → drop output_summary from ancestor_summary → drop entries.

Environment variables:
  FUSION_COWORK_RESUME_BUDGET_TOKENS  model-side context budget (default 12000, floor 200)
  FUSION_COWORK_RESUME_KEEP_RECENT    keep full output_data for the most recent N steps (default 8, floor 1)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET_TOKENS = 12000
_MIN_BUDGET_TOKENS = 200
_DEFAULT_KEEP_RECENT = 8
_MAX_ANCESTOR_ENTRIES = 50
_SCALAR_CHARS = 200
_MAX_KEYS = 10

# Snapshot-eligible statuses: success/completed = normal success step; skipped = preloaded copy for resume
# (the SKIPPED step produced when the engine skips a rerun, carrying restored output_data).
SNAPSHOT_ELIGIBLE_STATUSES = ("success", "completed", "skipped")


def estimate_tokens(text: str) -> int:
    # Documented heuristic: CJK chars ≈ 1 token, everything else ≈ 4 chars/token (documented budget, not exact counting)
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, cjk + (len(text) - cjk + 3) // 4)


def budget_tokens() -> int:
    raw = (os.getenv("FUSION_COWORK_RESUME_BUDGET_TOKENS") or "").strip()
    if not raw:
        return _DEFAULT_BUDGET_TOKENS
    try:
        return max(_MIN_BUDGET_TOKENS, int(raw))
    except ValueError:
        return _DEFAULT_BUDGET_TOKENS


def keep_recent() -> int:
    raw = (os.getenv("FUSION_COWORK_RESUME_KEEP_RECENT") or "").strip()
    if not raw:
        return _DEFAULT_KEEP_RECENT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_KEEP_RECENT


def step_ref(run_id: str, node_id: str) -> str:
    return f"step://{run_id}/{node_id}"


def _cap_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_SCALAR_CHARS]
    if isinstance(value, dict):
        return {k: _cap_value(v) for k, v in list(value.items())[:_MAX_KEYS]}
    if isinstance(value, list):
        return [_cap_value(v) for v in value[:_MAX_KEYS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_SCALAR_CHARS]


def summarize_step(run_id: str, step: Dict[str, Any]) -> Dict[str, Any]:
    # ancestor_summary entry: per-node result + key outputs (truncated) + reference line; no full output_data
    return {
        "node_id": step.get("node_id", ""),
        "node_name": step.get("node_name", ""),
        "status": step.get("status", ""),
        "summary": str(step.get("summary") or "")[:_SCALAR_CHARS],
        "output_ref": step_ref(run_id, str(step.get("node_id", ""))),
        "output_summary": _cap_value(step.get("output_data") or {}),
    }


def _snapshot_entry(step: Dict[str, Any], status: str) -> Dict[str, Any]:
    # engine snapshot recovery only accepts success/completed → skipped copies normalize to success (carrying restored outputs)
    entry = dict(step)
    entry["status"] = status
    return entry


def _ctx_tokens(snapshot: List[Dict[str, Any]], summaries: List[Dict[str, Any]]) -> int:
    return estimate_tokens(repr(snapshot)) + estimate_tokens(repr(summaries))


def build_resume_snapshot(
    run_id: str,
    steps: List[Dict[str, Any]],
    cut_index: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """Eligible steps before the cut point → (snapshot, newly added ancestor summaries, stats).

    The most recent keep_recent steps keep full output_data (execution correctness); earlier steps are
    summarized (snapshot entries demoted to compact output), summary lines go into ancestor_summary.
    Over budget, detail demotes stepwise: full → compact → ref-only → drop summary keys → drop entries.
    """
    eligible = [
        s
        for s in steps[:cut_index]
        if isinstance(s, dict)
        and str(s.get("status", "")).lower() in SNAPSHOT_ELIGIBLE_STATUSES
        and not s.get("user_correction")
    ]
    keep = keep_recent()
    older: List[Dict[str, Any]] = []
    recent: List[Dict[str, Any]] = eligible
    if keep < len(eligible):
        older = eligible[: len(eligible) - keep]
        recent = eligible[len(eligible) - keep :]

    snapshot = [_snapshot_entry(s, "success") for s in recent]
    ancestor_new = [summarize_step(run_id, s) for s in older]

    limit = budget_tokens()

    def demote(entry: Dict[str, Any]) -> bool:
        # True = detail demoted another level; False = already at the ref-only floor
        out = entry.get("output_data")
        entry.setdefault("output_ref", step_ref(run_id, str(entry.get("node_id", ""))))
        if out is None:
            return False
        capped = _cap_value(out) if isinstance(out, dict) else {}
        if capped == out:
            entry.pop("output_data", None)
        else:
            entry["output_data"] = capped
        return True

    while _ctx_tokens(snapshot, ancestor_new) > limit:
        if snapshot and demote(snapshot[0]):
            continue
        if snapshot:
            # oldest entry is already ref-only → skip one by one looking for something to demote; all ref-only → drop the oldest entry
            dropped = snapshot.pop(0)
            ancestor_new.insert(0, summarize_step(run_id, dropped))
            continue
        if ancestor_new and "output_summary" in ancestor_new[0]:
            ancestor_new[0].pop("output_summary")
            continue
        if ancestor_new:
            ancestor_new.pop(0)
            continue
        break

    full = sum(1 for s in snapshot if "output_data" in s)
    stats = {
        "eligible": len(eligible),
        "full_steps": full,
        "snapshot_steps": len(snapshot),
        "ancestor_new": len(ancestor_new),
        "estimated_tokens": _ctx_tokens(snapshot, ancestor_new),
        "budget": limit,
    }
    logger.info(
        f"[run-context] resume snapshot: eligible={stats['eligible']} full={full} "
        f"snapshot={stats['snapshot_steps']} ancestor_new={stats['ancestor_new']} "
        f"~{stats['estimated_tokens']}tok (budget {limit})"
    )
    return snapshot, ancestor_new, stats


def compose_ancestor_summary(
    prior: List[Dict[str, Any]],
    newer: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    # Chained continuation: ancestor summaries + newly added this round; over the cap drop the oldest (linear growth capped → sub-linear in chain depth)
    merged = list(prior or []) + list(newer or [])
    truncated = 0
    if len(merged) > _MAX_ANCESTOR_ENTRIES:
        truncated = len(merged) - _MAX_ANCESTOR_ENTRIES
        merged = merged[-_MAX_ANCESTOR_ENTRIES:]
    return merged, truncated
