"""Issue #523 Phase A acceptance: no mechanism-era :11442 references in fusion_cowork.

Roadmap K4 ruling (kernel-abstract-roadmap-2026-11-14.md §6): mechanism zero
HTTP — cowork consumes the kernel in-process; ":11442" may only appear at
product-level call sites that drive agent-studio-side entities (graph/agent
runs, teams board, approvals store, capability catalog). Those entities have
no in-process kernel counterpart, so replacing them would change what
executes — they stay HTTP, each annotated "# product-level call (roadmap §6)"
(classified in issue #494 Part C, PR #501).

This test turns that exit criterion into a permanent gate: any new ":11442"
reference must be added to _CLASSIFIED_SITES with a rationale, i.e. consciously
classified as product-level. An unclassified mechanism-level HTTP call to the
studio daemon fails CI.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "fusion_cowork"

# Every file in fusion_cowork/ allowed to reference ":11442", with the
# product-level rationale for each site (classified in issue #494 Part C,
# PR #501; re-verified against the 2026-11-18 kernel capability audit).
_CLASSIFIED_SITES = {
    "capabilities/catalog.py": (
        "capability discovery lists agent-studio-side entities (graphs/agents) — "
        "the catalog lives in the agent-studio daemon"
    ),
    "engine/executors.py": (
        "AgentExecutor HTTP fallback submits/polls agent runs — the agent runtime "
        "lives in the agent-studio daemon (UDS preferred when socket exists)"
    ),
    "engine/permission_manager.py": (
        "DEFAULT_TRUSTED_SYNC_URLS — approval task store lives in the agent-studio "
        "daemon (deterministic template-sync trust seed, never a blanket allow)"
    ),
    "nodes/ecosystem/studio_node.py": (
        "studio_run submits graph/agent runs — the executed entities live in the agent-studio daemon"
    ),
    "nodes/ecosystem/team_node.py": (
        "team_call drives agent-studio team members via teams.* JSON-RPC — members/"
        "tasks live in the agent-studio daemon (kernel TeamRuntime would change what "
        "executes; divergence documented in orchestrator/runner_pool.py)"
    ),
    "server/web_api.py": ("capability pitch definition fetch + spawn — graphs/agents live in the agent-studio daemon"),
    "templates/template_manager.py": ("template param default — approval task store lives in the agent-studio daemon"),
}

ANNOTATION_MARKER = "product-level call"


def _files_referencing_11442() -> list[str]:
    hits: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("issue #523: unreadable source skipped %s: %s", path, e)
            continue
        if "11442" in text:
            hits.append(path.relative_to(PACKAGE_ROOT).as_posix())
    return hits


def test_no_unclassified_11442_references():
    hits = _files_referencing_11442()
    unclassified = [rel for rel in hits if rel not in _CLASSIFIED_SITES]
    assert not unclassified, (
        "unclassified :11442 references found — mechanism-level studio HTTP calls are "
        "forbidden (roadmap K4: mechanism zero HTTP). Classify each site: product-level "
        "calls get a '# product-level call (roadmap §6)' annotation + an entry in "
        "_CLASSIFIED_SITES; replace genuine mechanism calls with in-process kernel calls. "
        f"Offenders: {unclassified}"
    )


def test_classified_sites_carry_annotation():
    missing = []
    for rel in _CLASSIFIED_SITES:
        path = PACKAGE_ROOT / rel
        if not path.exists():
            missing.append(f"{rel} (file gone — prune _CLASSIFIED_SITES)")
            continue
        text = path.read_text(encoding="utf-8")
        if ANNOTATION_MARKER not in text:
            missing.append(f"{rel} (annotation '# product-level call (roadmap §6)' absent)")
    assert not missing, f"classified sites must carry the in-code annotation: {missing}"


def test_classified_sites_still_exist():
    # Guard against silent drift: a site removed from the code must be pruned
    # from the allowlist so the list mirrors reality.
    hits = set(_files_referencing_11442())
    stale = sorted(set(_CLASSIFIED_SITES) - hits)
    assert not stale, f"_CLASSIFIED_SITES entries no longer reference :11442 — prune them: {stale}"
