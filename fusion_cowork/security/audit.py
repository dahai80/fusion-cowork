"""Stage 6 — tamper-evident audit log (hash chain).

AuditLog: each record's prev_hash = sha256(previous record's hash); deleting/reordering/tampering
with any record breaks the chain.
verify_chain(tenant_id): recomputes the whole chain, returns the first broken position.

Stored in the audit_log table (added in Stage 6 store._SCHEMA_SQL): tenant_id, actor, action, resource,
detail_hash, prev_hash, created_at. detail is not stored directly (hash only, leak prevention);
callers retain the detail themselves.
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
    """Compute sha256 for a single audit record — fixed field order prevents reordering."""
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
    """Hash the detail content (never stored directly, leak prevention)."""
    payload = json.dumps(detail, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    """Tamper-evident audit log — per-tenant hash chain.

    Relies on a store (SpaceStore or compatible object) for persistence; tests may pass an
    in-memory store (mocked _exec/_fetchall).
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
        """Write one audit log entry, returns this entry's hash (None on failure).

        prev_hash = hash of the previous entry (latest for the same tenant); first chain entry prev_hash="".
        """
        detail_hash = _hash_detail(detail)
        prev_hash = await self._last_hash(tenant_id)
        created_at = await self._now()
        entry_hash = _hash_entry(tenant_id, actor, action, resource, detail_hash, prev_hash, created_at)
        ok = await self._append(tenant_id, actor, action, resource, detail_hash, prev_hash, created_at, entry_hash)
        if ok:
            logger.info(f"audit log tenant={tenant_id} actor={actor} action={action} hash={entry_hash[:12]}")
            return entry_hash
        logger.error(f"audit log write failed tenant={tenant_id} action={action}")
        return None

    async def verify_chain(self, tenant_id: str) -> Dict[str, Any]:
        """Verify full chain integrity. Returns {ok: bool, broken_at: Optional[int], count: int}."""
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
                logger.error(f"audit chain broken tenant={tenant_id} at index={i} (prev_hash mismatch)")
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
            logger.warning(f"audit _last_hash failed (table may not exist): {e}")
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
            logger.error(f"audit _append failed: {e}", exc_info=True)
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
            logger.warning(f"audit _fetch_chain failed (table may not exist): {e}")
            return []

    async def _now(self) -> str:
        import datetime

        return datetime.datetime.utcnow().isoformat() + "Z"
