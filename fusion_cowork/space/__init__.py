"""Fusion-Cowork Space — 协作空间模块。

V2.0 M6 里程碑: 离线协作空间基础。
V2.0 M7 里程碑: 共享对话 + 知识库绑定。
提供本地优先的多人协作环境，支持:
- 空间 CRUD (create/list/get/update/archive/delete)
- 成员管理 (invite/join/leave/role)
- 基于角色的权限控制 (Owner/Admin/Member/Viewer)
- 共享对话上下文 + SSE 流式
- 知识库绑定 + 文档管理 + RAG
- P2P 同步 (CRDT LWW-Register)
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
from .service import SpaceService
from .shared_context import SharedContext, extract_shared_context, inject_shared_context
from .store import SpaceStore

__all__ = [
    "AgentStudioClient",
    "ModuleRegistry",
    "NotificationService",
    "PeerInfo",
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
