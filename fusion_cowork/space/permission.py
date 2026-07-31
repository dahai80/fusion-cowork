"""协作空间权限模型 — 4 级角色矩阵。

角色: Owner > Admin > Member > Viewer
权限动作:
- manage_space: 修改空间设置
- manage_members: 邀请/移除成员
- send_message: 发送消息
- manage_agents: 创建/修改 Agent
- call_agent: 调用 Agent（含多 Agent 接力）
- run_workflow: 运行工作流
- upload_file: 上传文件
- delete_data: 删除数据
- manage_snapshots: 管理快照
- manage_kb: 绑定/解绑知识库
- upload_document: 上传文档到知识库
"""

from __future__ import annotations

import logging
from typing import Dict

from .models import SpaceRole
from .store import SpaceStore

logger = logging.getLogger(__name__)

_PERMISSION_MATRIX: Dict[str, Dict[str, bool]] = {
    SpaceRole.OWNER.value: {
        "manage_space": True,
        "manage_members": True,
        "send_message": True,
        "manage_agents": True,
        "call_agent": True,
        "run_workflow": True,
        "upload_file": True,
        "delete_data": True,
        "manage_snapshots": True,
        "manage_kb": True,
        "upload_document": True,
    },
    SpaceRole.ADMIN.value: {
        "manage_space": True,
        "manage_members": True,
        "send_message": True,
        "manage_agents": True,
        "call_agent": True,
        "run_workflow": True,
        "upload_file": True,
        "delete_data": True,
        "manage_snapshots": True,
        "manage_kb": True,
        "upload_document": True,
    },
    SpaceRole.MEMBER.value: {
        "manage_space": False,
        "manage_members": False,
        "send_message": True,
        "manage_agents": False,
        "call_agent": True,
        "run_workflow": True,
        "upload_file": True,
        "delete_data": False,
        "manage_snapshots": False,
        "manage_kb": False,
        "upload_document": True,
    },
    SpaceRole.VIEWER.value: {
        "manage_space": False,
        "manage_members": False,
        "send_message": False,
        "manage_agents": False,
        "call_agent": False,
        "run_workflow": False,
        "upload_file": False,
        "delete_data": False,
        "manage_snapshots": False,
        "manage_kb": False,
        "upload_document": False,
    },
}


class SpacePermission:
    """空间权限检查 — 基于角色矩阵。"""

    def __init__(self, store: SpaceStore):
        self._store = store

    async def check(self, space_id: str, user_id: str, action: str) -> bool:
        member = await self._store.get_member(space_id, user_id)
        if not member:
            logger.warning(f"SpacePermission.check 用户 {user_id} 不在空间 {space_id}")
            return False
        role_key = member.role.value if isinstance(member.role, SpaceRole) else member.role
        role_perms = _PERMISSION_MATRIX.get(role_key, {})
        allowed = role_perms.get(action, False)
        if not allowed:
            logger.info(
                f"SpacePermission.check 拒绝: space={space_id} "
                f"user={user_id} role={member.role} action={action}",
            )
        return allowed

    async def get_role(self, space_id: str, user_id: str):
        member = await self._store.get_member(space_id, user_id)
        if not member:
            return None
        return member.role

    async def is_owner_or_admin(self, space_id: str, user_id: str) -> bool:
        role = await self.get_role(space_id, user_id)
        if role is None:
            return False
        role_val = role.value if isinstance(role, SpaceRole) else role
        return role_val in (SpaceRole.OWNER.value, SpaceRole.ADMIN.value)

    @staticmethod
    def get_permissions_for_role(role: str) -> Dict[str, bool]:
        return dict(_PERMISSION_MATRIX.get(role, {}))

    @staticmethod
    def list_roles() -> list:
        return [r.value for r in SpaceRole]
