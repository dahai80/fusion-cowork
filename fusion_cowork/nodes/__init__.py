"""Fusion-Cowork node system.

7 node categories (macos/ai/io/logic/tools/browser/ecosystem), all registered
to NodeRegistry via @register_node. Node modules self-register at import time,
so the server must explicitly import all node modules at startup; otherwise
NodeRegistry is empty or incomplete (historical bug: only the macos+browser
modules imported as side effects of cli.py were registered, 33/47 nodes visible).
"""

import logging

logger = logging.getLogger(__name__)

_NODE_MODULES = (
    "fusion_cowork.nodes.macos.system_nodes",
    "fusion_cowork.nodes.macos.input_nodes",
    "fusion_cowork.nodes.ai.classify",
    "fusion_cowork.nodes.io.file_io",
    "fusion_cowork.nodes.io.deliverables",
    "fusion_cowork.nodes.logic.logic_nodes",
    "fusion_cowork.nodes.tools.tool_nodes",
    "fusion_cowork.nodes.tools.http_request_node",
    "fusion_cowork.nodes.browser.browser_nodes",
    "fusion_cowork.nodes.browser.cdp_nodes",
    "fusion_cowork.nodes.ecosystem.trainer_node",
    "fusion_cowork.nodes.ecosystem.memory_node",
    "fusion_cowork.nodes.ecosystem.studio_node",
    "fusion_cowork.nodes.ecosystem.subworkflow_node",
    "fusion_cowork.nodes.ecosystem.mcp_client_node",
    "fusion_cowork.nodes.ecosystem.desigma_node",
    "fusion_cowork.nodes.ecosystem.fanout_node",
    "fusion_cowork.nodes.ecosystem.ladder_node",
    "fusion_cowork.nodes.ecosystem.team_node",
    "fusion_cowork.nodes.ecosystem.decision_gate_node",
    "fusion_cowork.nodes.ops.ops_nodes",
    "fusion_cowork.nodes.ops.match_node",
    "fusion_cowork.nodes.ops.demo_node",
    "fusion_cowork.nodes.ops.verify_node",
    "fusion_cowork.nodes.ops.deploy_node",
    "fusion_cowork.nodes.demand.demand_nodes",
    "fusion_cowork.nodes.office.office_nodes",
    "fusion_cowork.delivery.nodes",
)


def import_all_nodes() -> int:
    """Explicitly import all node modules to trigger @register_node registration.

    Idempotent: repeated calls import each module only once (Python import cache).
    Fault-tolerant: a single module import failure is logged as a warning and
    execution continues, never blocking service startup.

    Returns:
        Number of modules successfully imported
    """
    import importlib

    from fusion_cowork.engine.node import NodeRegistry

    # E-13: record names registered before importing — these were not registered by this
    # module batch (e.g. test fixtures) and must not enter the builtin snapshot.
    prior_names = set(NodeRegistry._registry.keys())
    ok = 0
    for mod_path in _NODE_MODULES:
        try:
            importlib.import_module(mod_path)
            ok += 1
        except Exception as e:
            logger.warning("Node module import failed %s: %s", mod_path, e)
    # E-13: freeze only the node names newly registered by this batch (prevents
    # loader.unload deleting builtin nodes via a malicious node_map).
    # Accumulate into the existing builtin snapshot (import_all_nodes may be called
    # multiple times; the first batch registers the true builtins).
    new_names = set(NodeRegistry._registry.keys()) - prior_names
    NodeRegistry.freeze_builtins(extra_names=new_names)
    logger.info("Node module import done: %d/%d", ok, len(_NODE_MODULES))
    return ok
