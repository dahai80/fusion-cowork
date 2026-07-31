"""协作空间数据模型 — Space, SpaceMember, SpaceMessage 等。

核心实体:
- Space: 协作空间，包含配置、状态、KB 绑定
- SpaceConfig: 空间配置项（AI 功能开关、成员权限等）
- SpaceMember: 空间成员，含角色和活跃时间
- SpaceMessage: 空间消息，支持 user/assistant/system 角色
- SpaceSnapshot: 空间快照，用于克隆与回溯
- PeerInfo: P2P 节点信息，用于 Bonjour 发现
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class SpaceRole(Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class SpaceStatus(Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


@dataclass
class SpaceConfig:
    enable_web_search: bool = True
    enable_deep_research: bool = True
    enable_computer_use: bool = False
    allow_member_upload: bool = True
    allow_member_agent: bool = True
    allow_member_workflow: bool = True
    max_members: int = 20
    auto_archive_days: int = 0
    stream_response: bool = True
    default_model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enable_web_search": self.enable_web_search,
            "enable_deep_research": self.enable_deep_research,
            "enable_computer_use": self.enable_computer_use,
            "allow_member_upload": self.allow_member_upload,
            "allow_member_agent": self.allow_member_agent,
            "allow_member_workflow": self.allow_member_workflow,
            "max_members": self.max_members,
            "auto_archive_days": self.auto_archive_days,
            "stream_response": self.stream_response,
            "default_model": self.default_model,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SpaceConfig:
        return cls(
            enable_web_search=data.get("enable_web_search", True),
            enable_deep_research=data.get("enable_deep_research", True),
            enable_computer_use=data.get("enable_computer_use", False),
            allow_member_upload=data.get("allow_member_upload", True),
            allow_member_agent=data.get("allow_member_agent", True),
            allow_member_workflow=data.get("allow_member_workflow", True),
            max_members=data.get("max_members", 20),
            auto_archive_days=data.get("auto_archive_days", 0),
            stream_response=data.get("stream_response", True),
            default_model=data.get("default_model", ""),
        )


@dataclass
class Space:
    id: str = field(default_factory=lambda: f"sp_{uuid.uuid4().hex[:12]}")
    name: str = ""
    description: str = ""
    owner_id: str = ""
    status: str = SpaceStatus.ACTIVE.value
    kb_bind_mode: str = "new_private"
    kb_id: Optional[str] = None
    collab_mode: str = "local"
    config: SpaceConfig = field(default_factory=SpaceConfig)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "owner_id": self.owner_id,
            "status": self.status.value if isinstance(self.status, SpaceStatus) else self.status,
            "kb_bind_mode": self.kb_bind_mode,
            "kb_id": self.kb_id,
            "collab_mode": self.collab_mode,
            "config": self.config.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Space:
        config_data = data.get("config", {})
        config = SpaceConfig.from_dict(config_data) if config_data else SpaceConfig()
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            owner_id=data.get("owner_id", ""),
            status=data.get("status", SpaceStatus.ACTIVE.value),
            kb_bind_mode=data.get("kb_bind_mode", "new_private"),
            kb_id=data.get("kb_id"),
            collab_mode=data.get("collab_mode", "local"),
            config=config,
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
        )


@dataclass
class SpaceMember:
    space_id: str = ""
    user_id: str = ""
    role: str = SpaceRole.MEMBER.value
    display_name: str = ""
    joined_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_active: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "space_id": self.space_id,
            "user_id": self.user_id,
            "role": self.role.value if isinstance(self.role, SpaceRole) else self.role,
            "display_name": self.display_name,
            "joined_at": self.joined_at,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SpaceMember:
        role_val = data.get("role", SpaceRole.MEMBER.value)
        if isinstance(role_val, str):
            try:
                role_val = SpaceRole(role_val)
            except ValueError:
                pass
        return cls(
            space_id=data.get("space_id", ""),
            user_id=data.get("user_id", ""),
            role=role_val,
            display_name=data.get("display_name", ""),
            joined_at=data.get("joined_at", datetime.now().isoformat()),
            last_active=data.get("last_active", datetime.now().isoformat()),
        )


@dataclass
class SpaceMessage:
    id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    space_id: str = ""
    user_id: str = ""
    agent_id: Optional[str] = None
    role: str = "user"
    content: str = ""
    content_type: str = "text"
    attachments: List[Any] = field(default_factory=list)
    parent_msg_id: Optional[str] = None
    thread_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "content": self.content,
            "content_type": self.content_type,
            "attachments": self.attachments,
            "parent_msg_id": self.parent_msg_id,
            "thread_id": self.thread_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SpaceMessage:
        return cls(
            id=data.get("id", ""),
            space_id=data.get("space_id", ""),
            user_id=data.get("user_id", ""),
            agent_id=data.get("agent_id"),
            role=data.get("role", "user"),
            content=data.get("content", ""),
            content_type=data.get("content_type", "text"),
            attachments=data.get("attachments", []),
            parent_msg_id=data.get("parent_msg_id"),
            thread_id=data.get("thread_id"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )


@dataclass
class SpaceSnapshot:
    id: str = field(default_factory=lambda: f"snap_{uuid.uuid4().hex[:12]}")
    space_id: str = ""
    name: str = ""
    messages_count: int = 0
    agents_count: int = 0
    files_count: int = 0
    workflows_count: int = 0
    artifacts_count: int = 0
    snapshot_data: Dict[str, Any] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "name": self.name,
            "messages_count": self.messages_count,
            "agents_count": self.agents_count,
            "files_count": self.files_count,
            "workflows_count": self.workflows_count,
            "artifacts_count": self.artifacts_count,
            "snapshot_data": self.snapshot_data,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SpaceSnapshot:
        return cls(
            id=data.get("id", ""),
            space_id=data.get("space_id", ""),
            name=data.get("name", ""),
            messages_count=data.get("messages_count", 0),
            agents_count=data.get("agents_count", 0),
            files_count=data.get("files_count", 0),
            workflows_count=data.get("workflows_count", 0),
            artifacts_count=data.get("artifacts_count", 0),
            snapshot_data=data.get("snapshot_data", {}),
            created_by=data.get("created_by", ""),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )


@dataclass
class PeerInfo:
    user_id: str = ""
    display_name: str = ""
    host: str = ""
    port: int = 0
    space_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "host": self.host,
            "port": self.port,
            "space_ids": self.space_ids,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PeerInfo:
        return cls(
            user_id=data.get("user_id", ""),
            display_name=data.get("display_name", ""),
            host=data.get("host", ""),
            port=data.get("port", 0),
            space_ids=data.get("space_ids", []),
        )
