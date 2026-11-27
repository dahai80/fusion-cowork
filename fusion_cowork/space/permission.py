"""Collaborative space permission model — 4-level role matrix.

Roles: Owner > Admin > Member > Viewer
Permission actions:
- manage_space: modify space settings
- manage_members: invite/remove members
- send_message: send messages
- manage_agents: create/modify agents
- call_agent: call agents (including multi-agent relay)
- run_workflow: run workflows
- upload_file: upload files
- delete_data: delete data
- manage_snapshots: manage snapshots
- manage_kb: bind/unbind the knowledge base
- upload_document: upload documents to the knowledge base
- view_artifact: view artifact rendering
- edit_artifact: edit artifacts (creator/authorized users)
- share_artifact: share artifacts (creator/Owner/Admin)
- transfer_artifact: transfer artifact ownership (Owner/Admin)
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
        "view_artifact": True,
        "edit_artifact": True,
        "share_artifact": True,
        "transfer_artifact": True,
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
        "view_artifact": True,
        "edit_artifact": True,
        "share_artifact": True,
        "transfer_artifact": True,
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
        "view_artifact": True,
        "edit_artifact": False,
        "share_artifact": False,
        "transfer_artifact": False,
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
        "view_artifact": True,
        "edit_artifact": False,
        "share_artifact": False,
        "transfer_artifact": False,
    },
}


class SpacePermission:
    """Space permission checks — based on the role matrix."""

    def __init__(self, store: SpaceStore):
        self._store = store

    async def check(self, space_id: str, user_id: str, action: str) -> bool:
        member = await self._store.get_member(space_id, user_id)
        if not member:
            logger.warning(f"SpacePermission.check user {user_id} is not in space {space_id}")
            return False
        role_key = member.role.value if isinstance(member.role, SpaceRole) else member.role
        role_perms = _PERMISSION_MATRIX.get(role_key, {})
        allowed = role_perms.get(action, False)
        if not allowed:
            logger.info(
                f"SpacePermission.check denied: space={space_id} user={user_id} role={member.role} action={action}",
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
