"""FSB 模块集成 — 侧边栏入口、工作台会话隔离与权限模型复用。

提供:
- 模块注册机制 (desk.module.register/list/enable/disable)
- FSB Workspace ≈ Space 级隔离确认
- 权限模型复用 (SpacePermission 4 级角色)
- 审批任务通知推送 (SSE + desk.notification.push)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .store import SpaceStore

logger = logging.getLogger(__name__)


class ModuleRegistry:
    """侧边栏模块注册 — 管理一级入口的注册/启用/禁用。"""

    def __init__(self, store: SpaceStore):
        self._store = store

    async def register_module(
        self,
        module_id: str,
        name: str,
        icon: str = "",
        route_path: str = "",
        enabled: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        db = await self._store._ensure_db()
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO sidebar_modules "
            "(id, name, icon, route_path, enabled, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (module_id, name, icon, route_path, int(enabled), json.dumps(metadata or {}, ensure_ascii=False), now, now),
        )
        await db.commit()
        logger.info(f"ModuleRegistry.register id={module_id} name={name}")
        return {
            "id": module_id,
            "name": name,
            "icon": icon,
            "route_path": route_path,
            "enabled": enabled,
        }

    async def list_modules(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        db = await self._store._ensure_db()
        if enabled_only:
            cursor = await db.execute("SELECT * FROM sidebar_modules WHERE enabled = 1 ORDER BY id")
        else:
            cursor = await db.execute("SELECT * FROM sidebar_modules ORDER BY id")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def enable_module(self, module_id: str) -> bool:
        db = await self._store._ensure_db()
        await db.execute(
            "UPDATE sidebar_modules SET enabled = 1, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), module_id),
        )
        await db.commit()
        logger.info(f"ModuleRegistry.enable id={module_id}")
        return True

    async def disable_module(self, module_id: str) -> bool:
        db = await self._store._ensure_db()
        await db.execute(
            "UPDATE sidebar_modules SET enabled = 0, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), module_id),
        )
        await db.commit()
        logger.info(f"ModuleRegistry.disable id={module_id}")
        return True

    async def get_module(self, module_id: str) -> Optional[Dict[str, Any]]:
        db = await self._store._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM sidebar_modules WHERE id = ?",
            (module_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


class NotificationService:
    """审批任务通知推送 — SSE 事件 + desk.notification.push。"""

    def __init__(self, store: SpaceStore):
        self._store = store
        self._subscribers: Dict[str, List] = {}

    async def push_notification(
        self,
        space_id: str,
        user_id: str,
        notification_type: str,
        title: str,
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        db = await self._store._ensure_db()
        now = datetime.now().isoformat()
        notif_id = f"notif_{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT INTO space_notifications "
            "(id, space_id, user_id, notification_type, title, content, "
            "metadata, read, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                notif_id,
                space_id,
                user_id,
                notification_type,
                title,
                content,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
            ),
        )
        await db.commit()
        event_data = {
            "id": notif_id,
            "space_id": space_id,
            "user_id": user_id,
            "type": notification_type,
            "title": title,
            "created_at": now,
        }
        for queue in self._subscribers.get(user_id, []):
            queue.append(event_data)
        logger.info(f"NotificationService.push id={notif_id} user={user_id}")
        return event_data

    async def list_notifications(
        self,
        user_id: str,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        db = await self._store._ensure_db()
        if unread_only:
            cursor = await db.execute(
                "SELECT * FROM space_notifications WHERE user_id = ? AND read = 0 ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM space_notifications WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def mark_read(self, notification_id: str) -> bool:
        db = await self._store._ensure_db()
        await db.execute(
            "UPDATE space_notifications SET read = 1 WHERE id = ?",
            (notification_id,),
        )
        await db.commit()
        return True

    def subscribe(self, user_id: str) -> List[Dict[str, Any]]:
        queue: List[Dict[str, Any]] = []
        self._subscribers.setdefault(user_id, []).append(queue)
        return queue

    def unsubscribe(self, user_id: str, queue: List[Dict[str, Any]]) -> None:
        if user_id in self._subscribers:
            try:
                self._subscribers[user_id].remove(queue)
            except ValueError:
                pass
            if not self._subscribers[user_id]:
                del self._subscribers[user_id]
