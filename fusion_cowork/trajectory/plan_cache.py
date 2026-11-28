import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# exp/plan-cache: AWM-style plan library (arXiv 2409.07429 online mode analog).
# fusion-horizon v15 evidence (factory-research/experiments/long-horizon,
# results_v15.json): a deterministic tool-inventory signature retrieves a
# previously compiled plan with zero LLM tokens on the warm path — the v15
# warm epoch served all 20 structures at 0 align tokens with strict success
# unchanged (the v9 baseline pays ~1.5k align tokens per structure every
# time). Retrieval is mechanical (rule 5): no LLM decides the cache hit, a
# multiset of node names does. Reuse is gated by a coverage integrity check —
# a cached plan whose nodes no longer cover the incoming evidence falls
# through to full compilation instead of corrupting the run (v11 lesson:
# a plan keyed on tools that are not in the target vocabulary fails silently
# unless validated as a separate check).

_CACHE_ENV = "FUSION_COWORK_PLAN_CACHE"


def plan_cache_enabled() -> bool:
    # opt-in, default OFF: absent env keeps every compile on the LLM path
    return str(os.environ.get(_CACHE_ENV, "")).strip().lower() in ("1", "true", "on", "yes")


def plan_signature(steps: List[Any]) -> Tuple[str, ...]:
    # deterministic signature: sorted multiset of node names in the evidence.
    # Node names are the task's actual vocabulary (v15: tool-inventory
    # signature); order and fault placement stay out of the key so held-out
    # family members hit while foreign structures miss.
    names = []
    for s in steps or []:
        if isinstance(s, dict):
            name = str(s.get("node_name", "") or "")
        else:
            name = str(getattr(s, "node_name", "") or "")
        if name:
            names.append(name)
    return tuple(sorted(names))


def plan_covers(plan: Dict[str, Any], signature: Tuple[str, ...]) -> bool:
    # integrity gate: every node the plan executes must exist in the incoming
    # evidence vocabulary. A plan referencing unknown nodes would fail at
    # create_node time (or worse, run a stale node); reject instead.
    evidence = set(signature)
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not steps:
        return False
    for step in steps:
        if not isinstance(step, dict):
            return False
        if str(step.get("node", "") or "") not in evidence:
            return False
    return True


class PlanLibrary:
    # in-memory plan library; keyed by plan_signature. One instance per
    # serving process; callers own the lifetime (module-level default below).
    def __init__(self) -> None:
        self._plans: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0

    def lookup(self, signature: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
        return self._plans.get(signature)

    def store(self, signature: Tuple[str, ...], plan: Dict[str, Any]) -> None:
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if not isinstance(steps, list) or not steps:
            logger.warning("plan_cache: refusing to store empty plan for signature of %s nodes", len(signature))
            return
        self._plans[signature] = plan
        logger.info("plan_cache: stored plan (%s steps) for signature of %s nodes", len(steps), len(signature))

    def size(self) -> int:
        return len(self._plans)


_default_library = PlanLibrary()


def get_plan_library() -> PlanLibrary:
    return _default_library


async def cached_compile(
    steps: List[Any],
    signature: Tuple[str, ...],
    extract: Any,
) -> Tuple[Dict[str, Any], bool]:
    # retrieval wrapper around full compilation. extract() is the async
    # zero-arg callable that pays the LLM call; on a valid hit it never runs.
    # Returns (plan, from_cache).
    lib = get_plan_library()
    if plan_cache_enabled():
        hit = lib.lookup(signature)
        if hit is not None and plan_covers(hit, signature):
            lib.hits += 1
            logger.info(
                "plan_cache: HIT for signature of %s nodes (hits=%s misses=%s)", len(signature), lib.hits, lib.misses
            )
            return hit, True
        lib.misses += 1
        logger.info(
            "plan_cache: MISS for signature of %s nodes (hits=%s misses=%s)", len(signature), lib.hits, lib.misses
        )
    plan = await extract()
    if plan_cache_enabled() and plan.get("steps"):
        lib.store(signature, plan)
    return plan, False
