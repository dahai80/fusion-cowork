"""#111: fusion-harness funnels/compactor adoption.

The two kernel modules are consumed behind adapters:
- Compactor drives context compaction for every space LLM call (LLMFunnel).
- LifecycleBus receives mirrored workflow events and space LLM lifecycle.
CI installs `.[test]` only, so the in-tree verbatim copies are exercised there.
"""

import ast
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from fusion_cowork.engine.events import EventEmitter, WorkflowEvent

ENGINE = Path(__file__).resolve().parent.parent / "fusion_cowork" / "engine"

try:
    import fusion_harness  # noqa: F401

    HAVE_KERNEL = True
except ImportError:
    HAVE_KERNEL = False


def _block_harness():
    return {
        "fusion_harness": None,
        "fusion_harness.compactor": None,
        "fusion_harness.funnels": None,
        "fusion_harness.permissions": None,
    }


def test_kernel_source_present():
    import fusion_cowork.engine.compactor as compactor
    import fusion_cowork.engine.funnels as funnels

    assert compactor.KERNEL_SOURCE in ("fusion-harness", "local")
    assert funnels.KERNEL_SOURCE in ("fusion-harness", "local")


def test_fallback_import_works():
    with patch.dict(sys.modules, _block_harness()):
        sys.modules.pop("fusion_cowork.engine.compactor", None)
        sys.modules.pop("fusion_cowork.engine.funnels", None)
        sys.modules.pop("fusion_cowork.engine._funnels_local", None)
        compactor = importlib.import_module("fusion_cowork.engine.compactor")
        funnels = importlib.import_module("fusion_cowork.engine.funnels")
        assert compactor.KERNEL_SOURCE == "local"
        assert funnels.KERNEL_SOURCE == "local"
        assert hasattr(compactor, "Compactor")
        assert hasattr(compactor, "CompactionConfig")
        assert hasattr(funnels, "LifecycleBus")
        assert hasattr(funnels, "LLMFunnel")
        assert hasattr(funnels, "ToolFunnel")
    sys.modules.pop("fusion_cowork.engine.compactor", None)
    sys.modules.pop("fusion_cowork.engine.funnels", None)
    sys.modules.pop("fusion_cowork.engine._funnels_local", None)


@pytest.mark.skipif(not HAVE_KERNEL, reason="fusion-harness not installed")
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


def test_local_copy_parseable():
    for name in ["_compactor_local.py", "_funnels_local.py", "_kernel_permissions_local.py"]:
        tree = ast.parse((ENGINE / name).read_text())
        assert tree.body


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
