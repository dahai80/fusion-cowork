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

from fusion_cowork.tenant import resolve_tenant_id

from .store import SpaceStore

logger = logging.getLogger(__name__)


class ModuleRegistry:
    """侧边栏模块注册 — 管理一级入口的注册/启用/禁用 (按租户隔离)。"""

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
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = resolve_tenant_id(tenant_id)
        now = datetime.now().isoformat()
        # INSERT OR REPLACE 是 sqlite 专用; ON CONFLICT DO UPDATE 双后端兼容 (sqlite 3.24+/pg)。
        async with self._store.write_tx(tid) as h:
            await h.exec(
                "INSERT INTO sidebar_modules "
                "(id, name, icon, route_path, enabled, metadata, created_at, updated_at, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, icon=excluded.icon, "
                "route_path=excluded.route_path, enabled=excluded.enabled, "
                "metadata=excluded.metadata, updated_at=excluded.updated_at, tenant_id=excluded.tenant_id",
                (
                    module_id,
                    name,
                    icon,
                    route_path,
                    int(enabled),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    now,
                    tid,
                ),
            )
        logger.info(f"ModuleRegistry.register id={module_id} name={name} tenant={tid}")
        return {
            "id": module_id,
            "name": name,
            "icon": icon,
            "route_path": route_path,
            "enabled": enabled,
        }

    async def list_modules(self, enabled_only: bool = False, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        if enabled_only:
            rows = await self._store._fetchall(
                "SELECT * FROM sidebar_modules WHERE enabled = 1 AND tenant_id = ? ORDER BY id",
                (tid,),
            )
        else:
            rows = await self._store._fetchall(
                "SELECT * FROM sidebar_modules WHERE tenant_id = ? ORDER BY id",
                (tid,),
            )
        return [dict(r) for r in rows]

    async def enable_module(self, module_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self._store.write_tx(tid) as h:
            await h.exec(
                "UPDATE sidebar_modules SET enabled = 1, updated_at = ? WHERE id = ? AND tenant_id = ?",
                (datetime.now().isoformat(), module_id, tid),
            )
        logger.info(f"ModuleRegistry.enable id={module_id} tenant={tid}")
        return True

    async def disable_module(self, module_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self._store.write_tx(tid) as h:
            await h.exec(
                "UPDATE sidebar_modules SET enabled = 0, updated_at = ? WHERE id = ? AND tenant_id = ?",
                (datetime.now().isoformat(), module_id, tid),
            )
        logger.info(f"ModuleRegistry.disable id={module_id} tenant={tid}")
        return True

    async def get_module(self, module_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        row = await self._store._fetchone(
            "SELECT * FROM sidebar_modules WHERE id = ? AND tenant_id = ?",
            (module_id, tid),
        )
        return dict(row) if row else None


class NotificationService:
    """审批任务通知推送 — SSE 事件 + desk.notification.push (按租户隔离)。"""

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
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = resolve_tenant_id(tenant_id)
        now = datetime.now().isoformat()
        notif_id = f"notif_{uuid.uuid4().hex[:8]}"
        async with self._store.write_tx(tid) as h:
            await h.exec(
                "INSERT INTO space_notifications "
                "(id, space_id, user_id, notification_type, title, content, "
                "metadata, read, created_at, tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    notif_id,
                    space_id,
                    user_id,
                    notification_type,
                    title,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    tid,
                ),
            )
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
        logger.info(f"NotificationService.push id={notif_id} user={user_id} tenant={tid}")
        return event_data

    async def list_notifications(
        self,
        user_id: str,
        unread_only: bool = False,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        if unread_only:
            rows = await self._store._fetchall(
                "SELECT * FROM space_notifications WHERE user_id = ? AND read = 0 AND tenant_id = ? "
                "ORDER BY created_at DESC",
                (user_id, tid),
            )
        else:
            rows = await self._store._fetchall(
                "SELECT * FROM space_notifications WHERE user_id = ? AND tenant_id = ? ORDER BY created_at DESC",
                (user_id, tid),
            )
        return [dict(r) for r in rows]

    async def mark_read(self, notification_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self._store.write_tx(tid) as h:
            await h.exec(
                "UPDATE space_notifications SET read = 1 WHERE id = ? AND tenant_id = ?",
                (notification_id, tid),
            )
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
