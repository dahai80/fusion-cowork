"""#111: fusion-harness funnels/compactor adoption.

The kernel modules are consumed behind adapters (hard dependency, #190/#2 —
the in-tree fallback copies are deleted; an import failure is loud):
- Compactor drives context compaction for every space LLM call (LLMFunnel).
- LifecycleBus receives mirrored workflow events and space LLM lifecycle.
"""

import subprocess
import sys
from pathlib import Path

from fusion_cowork.engine.events import EventEmitter, WorkflowEvent

ENGINE = Path(__file__).resolve().parent.parent / "fusion_cowork" / "engine"


def test_hard_dependency_import_error_is_loud():
    # #190/#2: no fallback — a missing fusion-harness install must fail
    # loudly instead of silently swapping in a local copy.
    code = (
        "import sys\n"
        "for m in ('fusion_harness', 'fusion_harness.funnels', "
        "'fusion_harness.compactor', 'fusion_harness.permissions'):\n"
        "    sys.modules[m] = None\n"
        "try:\n"
        "    import fusion_cowork.engine.funnels\n"
        "except ImportError:\n"
        "    print('LOUD_IMPORT_ERROR')\n"
        "    sys.exit(0)\n"
        "print('UNEXPECTED_SUCCESS')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "LOUD_IMPORT_ERROR" in out.stdout, out.stdout + out.stderr


def test_public_surface_matches_kernel():
    import fusion_cowork.engine.compactor as compactor
    import fusion_cowork.engine.funnels as funnels

    for mod, names in [
        (compactor, ["Compactor", "CompactionConfig", "RetentionList"]),
        (funnels, ["LifecycleBus", "LifecycleEvent", "EventDomain", "LLMFunnel", "ToolFunnel", "FunnelOutcome"]),
    ]:
        for name in names:
            assert hasattr(mod, name), f"{mod.__name__} missing {name}"
            assert getattr(mod, name) is not None


def test_no_fallback_copies_remain():
    leftovers = sorted(p.name for p in ENGINE.glob("_*_local.py"))
    assert leftovers == [], f"kernel fallback copies still present: {leftovers}"


def test_event_emitter_mirrors_to_lifecycle_bus():
    from fusion_cowork.engine.funnels import LifecycleBus

    bus = LifecycleBus()
    emitter = EventEmitter()
    emitter.attach_lifecycle_bus(bus)
    emitter.emit(WorkflowEvent(event_type="node_start", execution_id="exec-1", node_id="n1"))

    events = bus.replay("exec-1")
    assert len(events) == 1
    assert events[0].domain == "session"
    assert events[0].event == "node_start"
    assert events[0].payload["node_id"] == "n1"


async def test_space_chat_llm_funnel_compacts_and_emits_lifecycle():
    from fusion_cowork.engine.lifecycle import get_lifecycle_bus, reset_lifecycle_bus
    from fusion_cowork.space.chat import SpaceChatService

    reset_lifecycle_bus()
    service = SpaceChatService(store=None, mlx_client=None, permission=None)
    messages = [{"role": "user", "content": "x" * 400} for _ in range(100)]

    prepared, ctx = await service._pre_llm("space-111", "space_stream", "model-a", messages)

    assert prepared is not None
    assert sum(len(m["content"]) for m in prepared) <= 24000
    assert ctx.session_id == "space-111"

    events = get_lifecycle_bus().replay("space-111")
    names = [e.event for e in events]
    assert "llm_before" in names
    assert "compact_before" in names
    assert "compact_after" in names
    reset_lifecycle_bus()


async def test_space_chat_llm_funnel_skips_compaction_for_small_context():
    from fusion_cowork.engine.lifecycle import reset_lifecycle_bus
    from fusion_cowork.space.chat import SpaceChatService

    reset_lifecycle_bus()
    service = SpaceChatService(store=None, mlx_client=None, permission=None)
    messages = [{"role": "user", "content": "hello"}]

    prepared, _ = await service._pre_llm("space-112", "space_stream", "model-a", messages)

    assert prepared == messages
    reset_lifecycle_bus()
