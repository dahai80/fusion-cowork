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

from .permission import SpacePermission
from .store import SpaceStore

logger = logging.getLogger(__name__)


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
    ) -> Dict[str, Any]:
        if not await self._perm.check(space_id, owner_user_id, "edit_artifact"):
            raise PermissionError(
                f"User {owner_user_id} cannot create artifact in space {space_id}"
            )
        db = await self._store._ensure_db()
        artifact_id = f"art_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO space_artifacts "
            "(id, space_id, name, artifact_type, content, owner_user_id, "
            "metadata, version, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (artifact_id, space_id, name, artifact_type, content,
             owner_user_id, json.dumps(metadata or {}, ensure_ascii=False),
             owner_user_id, now, now),
        )
        await db.commit()
        logger.info(f"SpaceArtifactService.create_artifact id={artifact_id} space={space_id}")
        return {
            "id": artifact_id, "space_id": space_id,
            "owner_user_id": owner_user_id, "artifact_type": artifact_type,
            "name": name, "version": 1, "created_at": now,
        }

    async def get_artifact(
        self, space_id: str, artifact_id: str, user_id: str,
    ) -> Optional[Dict[str, Any]]:
        if not await self._perm.check(space_id, user_id, "view_artifact"):
            raise PermissionError(
                f"User {user_id} cannot view artifact in space {space_id}"
            )
        db = await self._store._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_artifacts WHERE id = ? AND space_id = ?",
            (artifact_id, space_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_artifact(
        self,
        space_id: str,
        artifact_id: str,
        user_id: str,
        content: str = "",
        name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not await self._perm.check(space_id, user_id, "edit_artifact"):
            raise PermissionError(
                f"User {user_id} cannot edit artifact in space {space_id}"
            )
        db = await self._store._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_artifacts WHERE id = ? AND space_id = ?",
            (artifact_id, space_id),
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Artifact {artifact_id} not found")
        art = dict(row)
        if art["owner_user_id"] != user_id:
            if not await self._perm.is_owner_or_admin(space_id, user_id):
                raise PermissionError(
                    f"Only artifact owner or admin can edit: {artifact_id}"
                )
        now = datetime.now().isoformat()
        new_version = (art.get("version", 1) or 1) + 1
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
        vals.extend([artifact_id, space_id])
        await db.execute(
            f"UPDATE space_artifacts SET {', '.join(sets)} WHERE id = ? AND space_id = ?",
            vals,
        )
        await db.commit()
        logger.info(f"SpaceArtifactService.update_artifact id={artifact_id} v={new_version}")
        return {"id": artifact_id, "version": new_version, "updated_at": now}

    async def share_artifact(
        self, space_id: str, artifact_id: str, user_id: str,
    ) -> Dict[str, Any]:
        if not await self._perm.check(space_id, user_id, "share_artifact"):
            raise PermissionError(
                f"User {user_id} cannot share artifact in space {space_id}"
            )
        db = await self._store._ensure_db()
        cursor = await db.execute(
            "SELECT owner_user_id FROM space_artifacts WHERE id = ? AND space_id = ?",
            (artifact_id, space_id),
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Artifact {artifact_id} not found")
        if dict(row)["owner_user_id"] != user_id:
            if not await self._perm.is_owner_or_admin(space_id, user_id):
                raise PermissionError("Only owner/admin can share artifact")
        share_code = f"share_{uuid.uuid4().hex[:8]}"
        logger.info(f"SpaceArtifactService.share_artifact id={artifact_id} code={share_code}")
        return {"artifact_id": artifact_id, "share_code": share_code}

    async def transfer_ownership(
        self, space_id: str, artifact_id: str, from_user_id: str, to_user_id: str,
    ) -> Dict[str, Any]:
        if not await self._perm.check(space_id, from_user_id, "transfer_artifact"):
            raise PermissionError(
                f"User {from_user_id} cannot transfer artifact in space {space_id}"
            )
        db = await self._store._ensure_db()
        cursor = await db.execute(
            "SELECT owner_user_id FROM space_artifacts WHERE id = ? AND space_id = ?",
            (artifact_id, space_id),
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Artifact {artifact_id} not found")
        if dict(row)["owner_user_id"] != from_user_id:
            if not await self._perm.is_owner_or_admin(space_id, from_user_id):
                raise PermissionError("Only current owner or admin can transfer")
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE space_artifacts SET owner_user_id = ?, updated_at = ? WHERE id = ? AND space_id = ?",
            (to_user_id, now, artifact_id, space_id),
        )
        await db.commit()
        logger.info(f"SpaceArtifactService.transfer_ownership id={artifact_id} {from_user_id}->{to_user_id}")
        return {"artifact_id": artifact_id, "new_owner": to_user_id}

    async def list_artifacts(
        self, space_id: str, user_id: str, artifact_type: str = "",
    ) -> List[Dict[str, Any]]:
        if not await self._perm.check(space_id, user_id, "view_artifact"):
            raise PermissionError(
                f"User {user_id} cannot list artifacts in space {space_id}"
            )
        db = await self._store._ensure_db()
        if artifact_type:
            cursor = await db.execute(
                "SELECT * FROM space_artifacts WHERE space_id = ? AND artifact_type = ?",
                (space_id, artifact_type),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM space_artifacts WHERE space_id = ?",
                (space_id,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_artifact(
        self, space_id: str, artifact_id: str, user_id: str,
    ) -> bool:
        if not await self._perm.is_owner_or_admin(space_id, user_id):
            return False
        db = await self._store._ensure_db()
        cursor = await db.execute(
            "DELETE FROM space_artifacts WHERE id = ? AND space_id = ?",
            (artifact_id, space_id),
        )
        await db.commit()
        removed = cursor.rowcount > 0
        logger.info(f"SpaceArtifactService.delete_artifact id={artifact_id} removed={removed}")
        return removed
