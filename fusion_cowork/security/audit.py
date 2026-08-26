"""Stage 6 — 防篡改审计日志 (tamper-evident hash chain)。

AuditLog: 每条记录 prev_hash = sha256(上一条记录的 hash), 删除/重排/篡改任一条 → 链断裂。
verify_chain(tenant_id): 重算全链, 断裂返首个坏位置。

存 audit_log 表 (Stage 6 store._SCHEMA_SQL 加): tenant_id, actor, action, resource,
detail_hash, prev_hash, created_at。detail 不直存 (存 hash 防泄露), 调用方自行留 detail。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _hash_entry(
    tenant_id: str, actor: str, action: str, resource: str, detail_hash: str, prev_hash: str, created_at: str
) -> str:
    """对单条审计记录算 sha256 — 字段固定顺序防重排。"""
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "actor": actor,
            "action": action,
            "resource": resource,
            "detail_hash": detail_hash,
            "prev_hash": prev_hash,
            "created_at": created_at,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hash_detail(detail: Any) -> str:
    """detail 内容算 hash (不直存, 防泄露)。"""
    payload = json.dumps(detail, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    """防篡改审计日志 — per-tenant hash chain。

    依赖 store (SpaceStore 或兼容对象) 提供持久化; 测试可传内存 store (mock _exec/_fetchall)。
    """

    def __init__(self, store=None):
        self._store = store

    async def log(
        self,
        tenant_id: str,
        actor: str,
        action: str,
        resource: str,
        detail: Any = None,
    ) -> Optional[str]:
        """记一条审计日志, 返本条 hash (失败返 None)。

        prev_hash = 上一条 (同 tenant 最新一条) 的 hash; 链首 prev_hash=""。
        """
        detail_hash = _hash_detail(detail)
        prev_hash = await self._last_hash(tenant_id)
        created_at = await self._now()
        entry_hash = _hash_entry(tenant_id, actor, action, resource, detail_hash, prev_hash, created_at)
        ok = await self._append(tenant_id, actor, action, resource, detail_hash, prev_hash, created_at, entry_hash)
        if ok:
            logger.info(f"audit log tenant={tenant_id} actor={actor} action={action} hash={entry_hash[:12]}")
            return entry_hash
        logger.error(f"audit log 写入失败 tenant={tenant_id} action={action}")
        return None

    async def verify_chain(self, tenant_id: str) -> Dict[str, Any]:
        """校验全链完整性。返 {ok: bool, broken_at: Optional[int], count: int}。"""
        rows = await self._fetch_chain(tenant_id)
        prev = ""
        for i, row in enumerate(rows):
            expected = _hash_entry(
                row["tenant_id"],
                row["actor"],
                row["action"],
                row["resource"],
                row["detail_hash"],
                prev,
                row["created_at"],
            )
            if expected != row["entry_hash"]:
                logger.error(f"audit chain 断裂 tenant={tenant_id} at index={i} (prev_hash 不匹配)")
                return {"ok": False, "broken_at": i, "count": len(rows)}
            prev = row["entry_hash"]
        return {"ok": True, "broken_at": None, "count": len(rows)}

    async def _last_hash(self, tenant_id: str) -> str:
        if self._store is None:
            return ""
        try:
            row = await self._store._fetchone(
                "SELECT entry_hash FROM audit_log WHERE tenant_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (tenant_id,),
            )
            return row["entry_hash"] if row else ""
        except Exception as e:
            logger.warning(f"audit _last_hash 失败 (表可能未建): {e}")
            return ""

    async def _append(
        self,
        tenant_id: str,
        actor: str,
        action: str,
        resource: str,
        detail_hash: str,
        prev_hash: str,
        created_at: str,
        entry_hash: str,
    ) -> bool:
        if self._store is None:
            return False
        try:
            async with self._store.write_tx(tenant_id) as h:
                await h.exec(
                    "INSERT INTO audit_log (tenant_id, actor, action, resource, detail_hash, prev_hash, created_at, entry_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, actor, action, resource, detail_hash, prev_hash, created_at, entry_hash),
                )
            return True
        except Exception as e:
            logger.error(f"audit _append 失败: {e}", exc_info=True)
            return False

    async def _fetch_chain(self, tenant_id: str) -> List[Dict[str, Any]]:
        if self._store is None:
            return []
        try:
            return await self._store._fetchall(
                "SELECT tenant_id, actor, action, resource, detail_hash, prev_hash, created_at, entry_hash "
                "FROM audit_log WHERE tenant_id = ? ORDER BY created_at ASC, id ASC",
                (tenant_id,),
            )
        except Exception as e:
            logger.warning(f"audit _fetch_chain 失败 (表可能未建): {e}")
            return []

    async def _now(self) -> str:
        import datetime

        return datetime.datetime.utcnow().isoformat() + "Z"
