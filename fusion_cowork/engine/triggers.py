"""Inbound trigger registry (issue #459).

Trigger = persisted binding {name, workflow_id, params, token}. A signed or
token-authed inbound webhook (`POST /api/hooks/trigger/<token>`) creates a run
from the bound workflow definition — the event-driven path can now START runs,
not only resume existing ones. Token IS the auth for the hook route (the web
API middleware already exempts /api/hooks/ from static-key auth).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TriggerRegistry:
    def __init__(self, root: Optional[Path] = None):
        self._path = Path(root) / "triggers.json" if root else Path.home() / ".fusion-cowork" / "web" / "triggers.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._items: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._items = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("token")}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"trigger registry load failed, starting empty: {e}")
            self._items = {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def create(self, name: str, workflow_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        name = str(name or "").strip()
        workflow_id = str(workflow_id or "").strip()
        if not name:
            raise ValueError("trigger name 必填")
        if not workflow_id:
            raise ValueError("workflow_id 必填")
        trigger_id = f"trg_{secrets.token_hex(5)}"
        token = f"trgk_{secrets.token_hex(16)}"
        record = {
            "id": trigger_id,
            "name": name,
            "workflow_id": workflow_id,
            "params": dict(params) if isinstance(params, dict) else {},
            "token": token,
            "created_at": time.time(),
        }
        self._items[trigger_id] = record
        self._save()
        logger.info(f"trigger created: id={trigger_id} name={name} workflow={workflow_id}")
        return record

    def list(self) -> List[Dict[str, Any]]:
        # token masked — hook URL carries the secret, list responses never do
        # (full token + hook URL are returned once by create())
        out = []
        for r in sorted(self._items.values(), key=lambda x: x.get("created_at", 0)):
            out.append({**r, "token": f"{r['token'][:8]}…"})
        return out

    def delete(self, trigger_id: str) -> bool:
        if trigger_id not in self._items:
            return False
        del self._items[trigger_id]
        self._save()
        logger.info(f"trigger deleted: id={trigger_id}")
        return True

    def find_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        for r in self._items.values():
            if hmac.compare_digest(str(r.get("token", "")), token):
                return r
        return None
