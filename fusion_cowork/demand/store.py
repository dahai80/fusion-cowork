# issue #585-#588: SQLite persistence for the demand engine mail rail.
# raw_messages (dedupe by Message-ID) + leads (unified schema, match results,
# drafts, A/B telemetry) + poll_state (per-folder UID cursor, restart-safe).

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    message_id TEXT PRIMARY KEY,
    uid INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    subject TEXT NOT NULL DEFAULT '',
    from_addr TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    arrived_at REAL NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'unknown',
    external_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    budget_hint TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '[]',
    client_meta TEXT NOT NULL DEFAULT '{}',
    arrived_at REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'normalized',
    matched_capabilities TEXT NOT NULL DEFAULT '[]',
    match_score REAL NOT NULL DEFAULT 0,
    match_reason TEXT NOT NULL DEFAULT '',
    catalog_version TEXT NOT NULL DEFAULT '',
    draft_a TEXT NOT NULL DEFAULT '',
    draft_b TEXT NOT NULL DEFAULT '',
    draft_meta TEXT NOT NULL DEFAULT '{}',
    draft_pick TEXT NOT NULL DEFAULT '',
    draft_edit_distance REAL NOT NULL DEFAULT -1,
    win_loss TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_external ON leads(source, external_id) WHERE external_id != '';
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE TABLE IF NOT EXISTS poll_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def default_db_path() -> str:
    return os.environ.get(
        "FUSION_COWORK_DEMAND_DB",
        str(Path.home() / ".fusion-cowork" / "demand" / "leads.db"),
    )


class LeadStore:
    """Demand-engine persistence. All methods async (aiosqlite)."""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path or default_db_path()
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info(f"demand store connected: {self.db_path}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> LeadStore:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("LeadStore not connected — call connect() first")
        return self._db

    # ── poll cursor (issue #585: restart never re-emits) ──

    async def get_cursor(self, key: str) -> int:
        async with self.db.execute("SELECT value FROM poll_state WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return int(row["value"]) if row else 0

    async def set_cursor(self, key: str, uid: int) -> None:
        await self.db.execute(
            "INSERT INTO poll_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(uid)),
        )
        await self.db.commit()

    # ── raw messages (issue #585: dedupe by Message-ID) ──

    async def upsert_raw(self, message: Dict[str, Any]) -> bool:
        """Insert raw mail; returns False when Message-ID already seen (duplicate)."""
        try:
            await self.db.execute(
                "INSERT INTO raw_messages(message_id, uid, folder, subject, from_addr, body_text, arrived_at, fetched_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message["message_id"],
                    int(message.get("uid", 0)),
                    message.get("folder", "INBOX"),
                    message.get("subject", ""),
                    message.get("from_addr", ""),
                    message.get("body_text", ""),
                    float(message.get("arrived_at", 0)),
                    time.time(),
                ),
            )
            await self.db.commit()
            return True
        except Exception as e:
            if "UNIQUE" in str(e):
                logger.info(f"raw message duplicate skipped: {message.get('message_id', '')[:60]}")
                return False
            raise

    async def list_unnormalized(self, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT r.* FROM raw_messages r LEFT JOIN leads l ON l.message_id = r.message_id "
            "WHERE l.id IS NULL ORDER BY r.uid LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── leads ──

    async def insert_lead(self, lead: Dict[str, Any]) -> int:
        cur = await self.db.execute(
            "INSERT INTO leads(message_id, source, external_id, title, description, budget_hint, skills, "
            "client_meta, arrived_at, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lead.get("message_id", ""),
                lead.get("source", "unknown"),
                lead.get("external_id", ""),
                lead.get("title", ""),
                lead.get("description", ""),
                lead.get("budget_hint", ""),
                json.dumps(lead.get("skills", []), ensure_ascii=False),
                json.dumps(lead.get("client_meta", {}), ensure_ascii=False),
                float(lead.get("arrived_at", 0)),
                lead.get("status", "normalized"),
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)) as cur:
            row = await cur.fetchone()
        return self._lead_dict(row) if row else None

    async def list_leads(self, status: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        if status:
            sql, args = "SELECT * FROM leads WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
        else:
            sql, args = "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)
        async with self.db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [self._lead_dict(r) for r in rows]

    async def update_lead(self, lead_id: int, fields: Dict[str, Any]) -> None:
        allowed = {
            "source",
            "external_id",
            "title",
            "description",
            "budget_hint",
            "status",
            "matched_capabilities",
            "match_score",
            "match_reason",
            "catalog_version",
            "draft_a",
            "draft_b",
            "draft_meta",
            "draft_pick",
            "draft_edit_distance",
            "win_loss",
        }
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("skills", "client_meta", "matched_capabilities", "draft_meta"):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return
        args.append(lead_id)
        await self.db.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", args)
        await self.db.commit()

    def _lead_dict(self, row: aiosqlite.Row) -> Dict[str, Any]:
        d = dict(row)
        for k in ("skills", "client_meta", "matched_capabilities", "draft_meta"):
            try:
                d[k] = json.loads(d.get(k) or ("[]" if k in ("skills", "matched_capabilities") else "{}"))
            except Exception:
                pass
        return d

    # ── match input (issue #586: spam-marked excluded) ──

    async def list_for_match(self, limit: int = 500) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM leads WHERE status NOT IN ('spam', 'duplicate') ORDER BY id LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._lead_dict(r) for r in rows]

    # ── A/B telemetry metrics (issue #588 acceptance) ──

    async def metrics(self) -> Dict[str, Any]:
        async with self.db.execute("SELECT COUNT(*) AS n FROM leads WHERE draft_pick IN ('a', 'b')") as cur:
            total_picks = (await cur.fetchone())["n"]
        async with self.db.execute(
            "SELECT draft_pick, COUNT(*) AS n FROM leads WHERE draft_pick IN ('a', 'b') GROUP BY draft_pick"
        ) as cur:
            by_pick = {r["draft_pick"]: r["n"] for r in await cur.fetchall()}
        async with self.db.execute(
            "SELECT AVG(draft_edit_distance) AS avg FROM leads WHERE draft_edit_distance >= 0"
        ) as cur:
            avg_edit = (await cur.fetchone())["avg"] or 0.0
        async with self.db.execute(
            "SELECT matched_capabilities, win_loss FROM leads WHERE win_loss IN ('win', 'loss')"
        ) as cur:
            cap_wins: Dict[str, Dict[str, int]] = {}
            for r in await cur.fetchall():
                try:
                    caps = json.loads(r["matched_capabilities"] or "[]")
                except Exception:
                    caps = []
                for c in caps:
                    slot = cap_wins.setdefault(c, {"win": 0, "loss": 0})
                    slot[r["win_loss"]] = slot.get(r["win_loss"], 0) + 1
        return {
            "total_picks": total_picks,
            "pick_rate_local": round(by_pick.get("a", 0) / total_picks, 4) if total_picks else 0.0,
            "pick_rate_cloud": round(by_pick.get("b", 0) / total_picks, 4) if total_picks else 0.0,
            "avg_edit_distance": round(float(avg_edit), 2),
            "per_capability_win_rate": {
                c: round(v["win"] / (v["win"] + v["loss"]), 4) if (v["win"] + v["loss"]) else 0.0
                for c, v in sorted(cap_wins.items())
            },
        }
