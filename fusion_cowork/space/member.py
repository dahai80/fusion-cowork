"""Collaborative space member management — invite/join/leave/role/remove.

Business logic:
- invite: generate an invite link (code can be used to join)
- join: join a space via invite code
- leave: leave a space (owner cannot leave)
- update_role: change a member's role (owner/admin only)
- remove: remove a member (owner/admin only)
- list: list space members
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from .models import SpaceMember, SpaceRole, SpaceStatus
from .permission import SpacePermission
from .store import SpaceStore

logger = logging.getLogger(__name__)


def _role_val(role):
    if isinstance(role, SpaceRole):
        return role.value
    return role


class SpaceMemberService:
    """Space member service — invite, join, role management."""

    def __init__(self, store: SpaceStore, permission: SpacePermission):
        self._store = store
        self._perm = permission

    async def invite(
        self,
        space_id: str,
        inviter_id: str,
        role: str = SpaceRole.MEMBER.value,
        max_uses: int = 0,
        expires_hours: int = 0,
    ) -> str:
        is_admin = await self._perm.is_owner_or_admin(space_id, inviter_id)
        if not is_admin:
            raise PermissionError(f"User {inviter_id} cannot invite members to space {space_id}")
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"space {space_id} not found")
        space_status = space.status.value if isinstance(space.status, SpaceStatus) else space.status
        if space_status != SpaceStatus.ACTIVE.value:
            raise ValueError(f"space {space_id} not found or archived")
        code = f"inv_{uuid.uuid4().hex[:8]}"
        expires_at = None
        if expires_hours != 0:
            from datetime import timedelta

            expires_at = (datetime.now() + timedelta(hours=expires_hours)).isoformat()
        await self._store.create_invite(
            code=code,
            space_id=space_id,
            role=role,
            max_uses=max_uses,
            expires_at=expires_at,
            created_by=inviter_id,
        )
        logger.info(f"SpaceMemberService.invite code={code} space={space_id} role={role}")
        return code

    async def join(self, code: str, user_id: str, display_name: str = "") -> SpaceMember:
        invite = await self._store.get_invite(code)
        if not invite:
            raise ValueError(f"invite code {code} does not exist")
        if invite["max_uses"] > 0 and invite["uses"] >= invite["max_uses"]:
            raise ValueError("invite code fully used")
        if invite.get("expires_at"):
            expires = datetime.fromisoformat(invite["expires_at"])
            if datetime.now() > expires:
                raise ValueError("invite code expired")
        space_id = invite["space_id"]
        role = invite["role"]
        existing = await self._store.get_member(space_id, user_id)
        if existing:
            logger.info(f"user {user_id} already in space {space_id}, skipping join")
            return existing
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"space {space_id} not found")
        # P2-7 (audit 0912): capacity guard + insert are now one serial write
        # transaction — concurrent joins could previously exceed max_members.
        role_enum = SpaceRole(role) if isinstance(role, str) else role
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=role_enum,
            display_name=display_name or user_id,
        )
        member = await self._store.add_member_checked(member, max_members=space.config.max_members)
        await self._store.use_invite(code)
        logger.info(f"SpaceMemberService.join user={user_id} space={space_id} role={role}")
        return member

    async def add_direct(
        self,
        space_id: str,
        user_id: str,
        role: str = SpaceRole.MEMBER.value,
        display_name: str = "",
        operator_id: str = "",
    ) -> SpaceMember:
        is_admin = await self._perm.is_owner_or_admin(space_id, operator_id)
        if not is_admin:
            raise PermissionError(f"User {operator_id} cannot add members directly to space {space_id}")
        existing = await self._store.get_member(space_id, user_id)
        if existing:
            logger.info(f"user {user_id} already in space {space_id}, skipping")
            return existing
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"space {space_id} not found")
        member_count = await self._store.count_members(space_id)
        if member_count >= space.config.max_members:
            raise ValueError(f"space {space_id} is full")
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=role,
            display_name=display_name or user_id,
        )
        member = await self._store.add_member(member)
        logger.info(f"SpaceMemberService.add_direct user={user_id} space={space_id}")
        return member

    async def leave(self, space_id: str, user_id: str) -> bool:
        member = await self._store.get_member(space_id, user_id)
        if not member:
            return False
        if _role_val(member.role) == SpaceRole.OWNER.value:
            raise ValueError("Owner cannot leave the space; transfer ownership first")
        removed = await self._store.remove_member(space_id, user_id)
        logger.info(f"SpaceMemberService.leave user={user_id} space={space_id} removed={removed}")
        return removed

    async def update_role(
        self,
        space_id: str,
        user_id: str,
        new_role: str,
        operator_id: str,
    ) -> Optional[SpaceMember]:
        new_role_str = new_role.value if isinstance(new_role, SpaceRole) else new_role
        is_admin = await self._perm.is_owner_or_admin(space_id, operator_id)
        if not is_admin:
            raise PermissionError(f"User {operator_id} cannot change roles")
        target = await self._store.get_member(space_id, user_id)
        if not target:
            raise ValueError(f"user {user_id} is not in space {space_id}")
        if _role_val(target.role) == SpaceRole.OWNER.value and new_role_str != SpaceRole.OWNER.value:
            operator = await self._store.get_member(space_id, operator_id)
            if _role_val(operator.role) != SpaceRole.OWNER.value:
                raise PermissionError("only the Owner can transfer ownership")
        if new_role_str == SpaceRole.OWNER.value:
            operator = await self._store.get_member(space_id, operator_id)
            if _role_val(operator.role) != SpaceRole.OWNER.value:
                raise PermissionError("only the Owner can transfer ownership")
            await self._store.update_member(space_id, operator_id, role=SpaceRole.ADMIN.value)
        result = await self._store.update_member(space_id, user_id, role=new_role_str)
        logger.info(f"SpaceMemberService.update_role user={user_id} role={new_role} operator={operator_id}")
        return result

    async def remove(self, space_id: str, user_id: str, operator_id: str) -> bool:
        is_admin = await self._perm.is_owner_or_admin(space_id, operator_id)
        if not is_admin:
            raise PermissionError(f"User {operator_id} cannot remove members")
        target = await self._store.get_member(space_id, user_id)
        if not target:
            return False
        if _role_val(target.role) == SpaceRole.OWNER.value:
            raise ValueError("cannot remove the Owner")
        removed = await self._store.remove_member(space_id, user_id)
        logger.info(f"SpaceMemberService.remove user={user_id} operator={operator_id}")
        return removed

    async def list_members(self, space_id: str) -> List[SpaceMember]:
        return await self._store.list_members(space_id)

    async def get_member(self, space_id: str, user_id: str) -> Optional[SpaceMember]:
        return await self._store.get_member(space_id, user_id)
