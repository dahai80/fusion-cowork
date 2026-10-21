"""fusion-browser UDS JSON-RPC Python client.

Issue #8: a maintained Python client so fusion-osagent (and other Python siblings)
can drive fusion-browser without reverse-engineering the UDS wire schema from
Swift sources. Pure-socket, no PyO3 — mirrors scripts/smoke_client.py codec.

Wire = length-prefixed JSON ([u32 BE len][JSON], snake_case keys). Auth handshake
REQUIRED first frame ({type:"auth",token}) -> auth_ack, else auth_denied.
"""

from .client import (FusionBrowserClient, FusionBrowserStream, FBError, BBox, AXTreeNode,
                     SessionInfo, CapacityInfo)

__all__ = [
    "FusionBrowserClient",
    "FusionBrowserStream",
    "FBError",
    "BBox",
    "AXTreeNode",
    "SessionInfo",
    "CapacityInfo",
]
