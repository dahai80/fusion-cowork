"""Fusion-Cowork node system.

7 node groups (macos/ai/io/logic/tools/browser/ecosystem), all registered into
NodeRegistry via @register_node. Node modules self-register at import time, so
server startup must explicitly import every node module, otherwise NodeRegistry
ends up empty or partial (historical bug: only the macos+browser modules
side-effect-imported by cli.py were registered, 33/47 nodes visible).
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
    """Explicitly import all node modules, triggering @register_node registration.

    Idempotent: repeated calls import only once (Python import cache).
    Fault-tolerant: a single module import failure is logged as a warning and
    skipped, never blocking service startup.

    Returns:
        Number of modules successfully imported
    """
    import importlib

    from fusion_cowork.engine.node import NodeRegistry

    # E-13: record names registered before the imports — those were not registered by
    # these modules (e.g. test fixtures) and must not enter the builtin snapshot.
    prior_names = set(NodeRegistry._registry.keys())
    ok = 0
    for mod_path in _NODE_MODULES:
        try:
            importlib.import_module(mod_path)
            ok += 1
        except Exception as e:
            logger.warning("node module import failed %s: %s", mod_path, e)
    # E-13: freeze only node names newly registered by these modules (prevents
    # loader.unload from deleting builtin nodes via a malicious node_map).
    # Accumulate into the existing builtin snapshot (import_all_nodes may run
    # multiple times; names registered on the first pass are the true builtins).
    new_names = set(NodeRegistry._registry.keys()) - prior_names
    NodeRegistry.freeze_builtins(extra_names=new_names)
    logger.info("node module import finished: %d/%d", ok, len(_NODE_MODULES))
    return ok
