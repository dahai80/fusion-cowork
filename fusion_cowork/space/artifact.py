"""Artifact 权限服务 — 协同会话 Artifact 只读共享 + 创建者编辑。

前置实现（等 fusion-artifacts-engine P3/P4 上游就绪后对接）:
- Artifact ownership 追踪 (owner_user_id)
- view/edit/share/transfer 权限检查
- artifact.updated SSE 事件预留
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fusion_cowork.tenant import resolve_tenant_id

from .permission import SpacePermission
from .store import SpaceStore

logger = logging.getLogger(__name__)


class ConflictError(Exception):
    pass


class SpaceArtifactService:
    """Artifact 权限管理 — 创建者编辑 + 参与者只读 + 所有权转交。"""

    def __init__(self, store: SpaceStore, permission: SpacePermission):
        self._store = store
        self._perm = permission

    async def create_artifact(
        self,
        space_id: str,
        owner_user_id: str,
        name: str = "",
        artifact_type: str = "document",
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = resolve_tenant_id(tenant_id)
        if not await self._perm.check(space_id, owner_user_id, "edit_artifact"):
            raise PermissionError(f"User {owner_user_id} cannot create artifact in space {space_id}")
        artifact_id = f"art_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        # A-8: 经 store 串行写事务, 与 SpaceStore 写隔离 (单共享连接)。
        async with self._store.write_tx(tid) as h:
            await h.exec(
                "INSERT INTO space_artifacts "
                "(id, space_id, name, artifact_type, content, owner_user_id, "
                "metadata, version, created_by, created_at, updated_at, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (
                    artifact_id,
                    space_id,
                    name,
                    artifact_type,
                    content,
                    owner_user_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    owner_user_id,
                    now,
                    now,
                    tid,
                ),
            )
        logger.info(f"SpaceArtifactService.create_artifact id={artifact_id} space={space_id} tenant={tid}")
        return {
            "id": artifact_id,
            "space_id": space_id,
            "owner_user_id": owner_user_id,
            "artifact_type": artifact_type,
            "name": name,
            "version": 1,
            "created_at": now,
        }

    async def get_artifact(
        self,
        space_id: str,
        artifact_id: str,
        user_id: str,
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        if not await self._perm.check(space_id, user_id, "view_artifact"):
            raise PermissionError(f"User {user_id} cannot view artifact in space {space_id}")
        row = await self._store._fetchone(
            "SELECT * FROM space_artifacts WHERE id = ? AND space_id = ? AND tenant_id = ?",
            (artifact_id, space_id, tid),
        )
        return dict(row) if row else None

    async def update_artifact(
        self,
        space_id: str,
        artifact_id: str,
        user_id: str,
        content: str = "",
        name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = resolve_tenant_id(tenant_id)
        if not await self._perm.check(space_id, user_id, "edit_artifact"):
            raise PermissionError(f"User {user_id} cannot edit artifact in space {space_id}")
        now = datetime.now().isoformat()
        # A-8: 读版本 + 乐观写同一串行事务, 防 SELECT→UPDATE 间被并发写覆盖。
        async with self._store.write_tx(tid) as h:
            row = await h.fetchone(
                "SELECT * FROM space_artifacts WHERE id = ? AND space_id = ? AND tenant_id = ?",
                (artifact_id, space_id, tid),
            )
            if not row:
                raise ValueError(f"Artifact {artifact_id} not found")
            art = dict(row)
            if art["owner_user_id"] != user_id:
                if not await self._perm.is_owner_or_admin(space_id, user_id):
                    raise PermissionError(f"Only artifact owner or admin can edit: {artifact_id}")
            current_version = art.get("version", 1) or 1
            new_version = current_version + 1
            sets = ["version = ?", "updated_at = ?"]
            vals: list = [new_version, now]
            if content:
                sets.append("content = ?")
                vals.append(content)
            if name:
                sets.append("name = ?")
                vals.append(name)
            if metadata is not None:
                sets.append("metadata = ?")
                vals.append(json.dumps(metadata, ensure_ascii=False))
            # A-8: 乐观锁 — WHERE version=current 防并发编辑丢更新, rowcount=0 即冲突。
            vals.extend([artifact_id, space_id, tid, current_version])
            res = await h.exec(
                f"UPDATE space_artifacts SET {', '.join(sets)} "
                "WHERE id = ? AND space_id = ? AND tenant_id = ? AND version = ?",
                vals,
            )
            if res.rowcount == 0:
                logger.warning(f"SpaceArtifactService.update_artifact 乐观锁冲突 id={artifact_id} v={current_version}")
                raise ConflictError(f"Artifact {artifact_id} 已被他人修改, 请刷新重试")
        logger.info(f"SpaceArtifactService.update_artifact id={artifact_id} v={new_version} tenant={tid}")
        return {"id": artifact_id, "version": new_version, "updated_at": now}

    async def share_artifact(
        self,
        space_id: str,
        artifact_id: str,
        user_id: str,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = resolve_tenant_id(tenant_id)
        if not await self._perm.check(space_id, user_id, "share_artifact"):
            raise PermissionError(f"User {user_id} cannot share artifact in space {space_id}")
        row = await self._store._fetchone(
            "SELECT owner_user_id FROM space_artifacts WHERE id = ? AND space_id = ? AND tenant_id = ?",
            (artifact_id, space_id, tid),
        )
        if not row:
            raise ValueError(f"Artifact {artifact_id} not found")
        if dict(row)["owner_user_id"] != user_id:
            if not await self._perm.is_owner_or_admin(space_id, user_id):
                raise PermissionError("Only owner/admin can share artifact")
        share_code = f"share_{uuid.uuid4().hex[:8]}"
        logger.info(f"SpaceArtifactService.share_artifact id={artifact_id} code={share_code} tenant={tid}")
        return {"artifact_id": artifact_id, "share_code": share_code}

    async def transfer_ownership(
        self,
        space_id: str,
        artifact_id: str,
        from_user_id: str,
        to_user_id: str,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = resolve_tenant_id(tenant_id)
        if not await self._perm.check(space_id, from_user_id, "transfer_artifact"):
            raise PermissionError(f"User {from_user_id} cannot transfer artifact in space {space_id}")
        now = datetime.now().isoformat()
        async with self._store.write_tx(tid) as h:
            row = await h.fetchone(
                "SELECT owner_user_id FROM space_artifacts WHERE id = ? AND space_id = ? AND tenant_id = ?",
                (artifact_id, space_id, tid),
            )
            if not row:
                raise ValueError(f"Artifact {artifact_id} not found")
            if dict(row)["owner_user_id"] != from_user_id:
                if not await self._perm.is_owner_or_admin(space_id, from_user_id):
                    raise PermissionError("Only current owner or admin can transfer")
            await h.exec(
                "UPDATE space_artifacts SET owner_user_id = ?, updated_at = ? "
                "WHERE id = ? AND space_id = ? AND tenant_id = ?",
                (to_user_id, now, artifact_id, space_id, tid),
            )
        logger.info(
            f"SpaceArtifactService.transfer_ownership id={artifact_id} {from_user_id}->{to_user_id} tenant={tid}"
        )
        return {"artifact_id": artifact_id, "new_owner": to_user_id}

    async def list_artifacts(
        self,
        space_id: str,
        user_id: str,
        artifact_type: str = "",
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        if not await self._perm.check(space_id, user_id, "view_artifact"):
            raise PermissionError(f"User {user_id} cannot list artifacts in space {space_id}")
        if artifact_type:
            rows = await self._store._fetchall(
                "SELECT * FROM space_artifacts WHERE space_id = ? AND artifact_type = ? AND tenant_id = ?",
                (space_id, artifact_type, tid),
            )
        else:
            rows = await self._store._fetchall(
                "SELECT * FROM space_artifacts WHERE space_id = ? AND tenant_id = ?",
                (space_id, tid),
            )
        return [dict(r) for r in rows]

    async def delete_artifact(
        self,
        space_id: str,
        artifact_id: str,
        user_id: str,
        tenant_id: Optional[str] = None,
    ) -> bool:
        tid = resolve_tenant_id(tenant_id)
        if not await self._perm.is_owner_or_admin(space_id, user_id):
            return False
        async with self._store.write_tx(tid) as h:
            res = await h.exec(
                "DELETE FROM space_artifacts WHERE id = ? AND space_id = ? AND tenant_id = ?",
                (artifact_id, space_id, tid),
            )
            removed = res.rowcount > 0
        logger.info(f"SpaceArtifactService.delete_artifact id={artifact_id} removed={removed} tenant={tid}")
        return removed
