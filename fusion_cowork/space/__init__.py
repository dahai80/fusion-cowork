"""Fusion-Cowork Space — collaborative space module.

V2.0 M6 milestone: offline collaborative space foundation.
V2.0 M7 milestone: shared conversation + knowledge base binding.
Provides a local-first multi-user collaboration environment, supporting:
- Space CRUD (create/list/get/update/archive/delete)
- Member management (invite/join/leave/role)
- Role-based permission control (Owner/Admin/Member/Viewer)
- Shared conversation context + SSE streaming
- Knowledge base binding + document management + RAG
- P2P sync (CRDT LWW-Register)
"""

from __future__ import annotations

from .agent_runtime import SpaceAgentRuntime
from .agent_studio_client import AgentStudioClient
from .api import create_space_api
from .artifact import SpaceArtifactService
from .chat import SpaceChatService
from .fsb import ModuleRegistry, NotificationService
from .knowledge import SpaceKBService
from .member import SpaceMemberService
from .models import (
    PeerInfo,
    Space,
    SpaceConfig,
    SpaceMember,
    SpaceMessage,
    SpaceRole,
    SpaceSnapshot,
    SpaceStatus,
)
from .permission import SpacePermission
from .presence import PresenceManager, PresenceState
from .service import SpaceService
from .shared_context import SharedContext, extract_shared_context, inject_shared_context
from .store import SpaceStore

__all__ = [
    "AgentStudioClient",
    "ModuleRegistry",
    "NotificationService",
    "PeerInfo",
    "PresenceManager",
    "PresenceState",
    "SharedContext",
    "Space",
    "SpaceAgentRuntime",
    "SpaceArtifactService",
    "SpaceChatService",
    "SpaceConfig",
    "SpaceKBService",
    "SpaceMember",
    "SpaceMemberService",
    "SpaceMessage",
    "SpacePermission",
    "SpaceRole",
    "SpaceService",
    "SpaceSnapshot",
    "SpaceStatus",
    "SpaceStore",
    "create_space_api",
    "extract_shared_context",
    "inject_shared_context",
]
