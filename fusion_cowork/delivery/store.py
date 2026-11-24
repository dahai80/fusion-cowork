# issue #597: delivery rail persistence — orders (S0-S7 lifecycle), invite
# tokens (hashed, 72h single-use), synced GitHub issues (SLA scan), and a
# tamper-evident audit log for every gate transition and repo action.
# Same shape as demand/store.py: SQLite + aiosqlite, restart-safe, idempotent
# writes so a killed run can re-execute without duplicates.

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

# S0 -> S7 state machine; every stage produces a draft and parks on a human
# gate — no skipping (advance only to the next stage, and only by a human call).
STAGES = ["created", "sow_draft", "sow_gate", "tad_draft", "tad_gate", "test_report", "acceptance", "closed"]


def default_db_path() -> str:
    root = os.environ.get("FUSION_COWORK_WORKSPACE_ROOT")
    base = Path(root) if root else Path.home() / ".fusion-cowork"
    return str(Path(base) / "delivery.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    lead_id INTEGER NOT NULL DEFAULT 0,
    capability TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    price_cny INTEGER NOT NULL DEFAULT 0,
    repo TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'created',
    artifacts TEXT NOT NULL DEFAULT '{}',
    draft_sow TEXT NOT NULL DEFAULT '',
    draft_tad TEXT NOT NULL DEFAULT '',
    test_report TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_stage ON orders(stage);
CREATE TABLE IF NOT EXISTS invite_tokens (
    token_hash TEXT PRIMARY KEY,
    order_no TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS order_issues (
    issue_id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'open',
    sla_hours INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    overdue_notified INTEGER NOT NULL DEFAULT 0,
    synced_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS delivery_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL DEFAULT 0,
    order_no TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT NOT NULL DEFAULT '',
    entry_hash TEXT NOT NULL DEFAULT ''
);
"""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class OrderStore:
    def __init__(self, db_path: str = ""):
        self._path = db_path or default_db_path()
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> OrderStore:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("OrderStore not connected")
        return self._db

    # ── audit (sha256 chain, tamper-evident) ──

    async def audit(self, order_no: str, action: str, detail: Dict[str, Any]) -> None:
        async with self.db().execute("SELECT entry_hash FROM delivery_audit ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
        prev = row["entry_hash"] if row else ""
        ts = time.time()
        payload = json.dumps({"ts": ts, "order_no": order_no, "action": action, "detail": detail}, sort_keys=True)
        entry = _hash_token(prev + payload)
        await self.db().execute(
            "INSERT INTO delivery_audit(ts, order_no, action, detail, prev_hash, entry_hash) VALUES(?,?,?,?,?,?)",
            (ts, order_no, action, json.dumps(detail, ensure_ascii=False), prev, entry),
        )
        await self.db().commit()

    async def audit_verify(self) -> bool:
        prev = ""
        async with self.db().execute(
            "SELECT ts, order_no, action, detail, prev_hash, entry_hash FROM delivery_audit ORDER BY id"
        ) as cur:
            async for r in cur:
                payload = json.dumps(
                    {
                        "ts": r["ts"],
                        "order_no": r["order_no"],
                        "action": r["action"],
                        "detail": json.loads(r["detail"]),
                    },
                    sort_keys=True,
                )
                if r["prev_hash"] != prev or _hash_token(prev + payload) != r["entry_hash"]:
                    return False
                prev = r["entry_hash"]
        return True

    # ── orders ──

    async def create_order(self, lead_id: int, capability: str, title: str, price_cny: int) -> Dict[str, Any]:
        # idempotent: one order per lead; ORD-YYYY-NNN sequence per year
        async with self.db().execute("SELECT * FROM orders WHERE lead_id=?", (lead_id,)) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)
        year = time.localtime().tm_year
        async with self.db().execute(
            "SELECT COUNT(*) AS n FROM orders WHERE order_no LIKE ?", (f"ORD-{year}-%",)
        ) as cur:
            seq = (await cur.fetchone())["n"] + 1
        order_no = f"ORD-{year}-{seq:03d}"
        now = time.time()
        await self.db().execute(
            "INSERT INTO orders(order_no, lead_id, capability, title, price_cny, stage, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (order_no, lead_id, capability, title, price_cny, "created", now, now),
        )
        await self.db().commit()
        await self.audit(order_no, "order_created", {"lead_id": lead_id, "capability": capability})
        async with self.db().execute("SELECT * FROM orders WHERE order_no=?", (order_no,)) as cur:
            return dict(await cur.fetchone())

    async def get_order(self, order_no: str) -> Optional[Dict[str, Any]]:
        async with self.db().execute("SELECT * FROM orders WHERE order_no=?", (order_no,)) as cur:
            row = await cur.fetchone()
        return self._order_dict(row) if row else None

    async def list_orders(self, stage: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        if stage:
            sql, args = "SELECT * FROM orders WHERE stage=? ORDER BY id DESC LIMIT ?", (stage, limit)
        else:
            sql, args = "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        async with self.db().execute(sql, args) as cur:
            return [self._order_dict(r) for r in await cur.fetchall()]

    async def set_repo(self, order_no: str, repo: str) -> None:
        await self.db().execute(
            "UPDATE orders SET repo=?, updated_at=? WHERE order_no=?", (repo, time.time(), order_no)
        )
        await self.db().commit()

    async def advance_stage(self, order_no: str, artifact_field: str = "", artifact_text: str = "") -> Dict[str, Any]:
        # move to the next stage in STAGES — machine writes drafts + enters the
        # gate; only the human approve path (web API) advances out of a gate.
        order = await self.get_order(order_no)
        if order is None:
            raise ValueError(f"order not found: {order_no}")
        idx = STAGES.index(order["stage"])
        if idx >= len(STAGES) - 1:
            return order
        nxt = STAGES[idx + 1]
        sets = "stage=?, updated_at=?"
        args: List[Any] = [nxt, time.time()]
        if artifact_field and artifact_text:
            sets += f", {artifact_field}=?"
            args.append(artifact_text)
        args.append(order_no)
        await self.db().execute(f"UPDATE orders SET {sets} WHERE order_no=?", args)
        await self.db().commit()
        await self.audit(order_no, "stage_advanced", {"from": order["stage"], "to": nxt, "artifact": artifact_field})
        return await self.get_order(order_no)  # type: ignore[return-value]

    def _order_dict(self, row: aiosqlite.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["artifacts"] = json.loads(d.get("artifacts") or "{}")
        except Exception:
            d["artifacts"] = {}
        return d

    # ── invite tokens (single-use, 72h, stored hashed) ──

    async def create_invite(self, order_no: str, ttl_hours: int = 72) -> str:
        token = secrets.token_urlsafe(24)
        await self.db().execute(
            "INSERT INTO invite_tokens(token_hash, order_no, expires_at, created_at) VALUES(?,?,?,?)",
            (_hash_token(token), order_no, time.time() + ttl_hours * 3600, time.time()),
        )
        await self.db().commit()
        await self.audit(order_no, "invite_created", {"ttl_hours": ttl_hours})
        return token  # plaintext returned exactly once — never stored

    async def consume_invite(self, token: str) -> Optional[str]:
        row_hash = _hash_token(token)
        async with self.db().execute("SELECT * FROM invite_tokens WHERE token_hash=?", (row_hash,)) as cur:
            row = await cur.fetchone()
        if row is None or row["used_at"] or row["expires_at"] < time.time():
            return None
        await self.db().execute("UPDATE invite_tokens SET used_at=? WHERE token_hash=?", (time.time(), row_hash))
        await self.db().commit()
        await self.audit(row["order_no"], "invite_consumed", {})
        return row["order_no"]

    # ── issue sync + SLA scan ──

    async def upsert_issue(self, repo: str, issue: Dict[str, Any]) -> bool:
        labels = issue.get("labels") or []
        sla = 24 if "sla-24h" in labels else (72 if "sla-72h" in labels else 0)
        await self.db().execute(
            "INSERT INTO order_issues(issue_id, repo, title, labels, state, sla_hours, created_at, synced_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(issue_id) DO UPDATE SET title=excluded.title, labels=excluded.labels, "
            "state=excluded.state, sla_hours=excluded.sla_hours, synced_at=excluded.synced_at",
            (
                int(issue["issue_id"]),
                repo,
                issue.get("title", ""),
                json.dumps(labels),
                issue.get("state", "open"),
                sla,
                float(issue.get("created_at", 0)),
                time.time(),
            ),
        )
        await self.db().commit()
        return True

    async def scan_sla_overdue(self) -> List[Dict[str, Any]]:
        # open issues whose sla-24h/72h window vs created_at has elapsed and
        # not yet notified — flags them (overdue_notified=1) and returns them
        now = time.time()
        async with self.db().execute(
            "SELECT * FROM order_issues WHERE state='open' AND sla_hours>0 AND overdue_notified=0 AND created_at>0"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        overdue = [r for r in rows if now - r["created_at"] > r["sla_hours"] * 3600]
        for r in overdue:
            await self.db().execute("UPDATE order_issues SET overdue_notified=1 WHERE issue_id=?", (r["issue_id"],))
        if overdue:
            await self.db().commit()
            for r in overdue:
                await self.audit(
                    "", "sla_overdue", {"repo": r["repo"], "issue_id": r["issue_id"], "sla_hours": r["sla_hours"]}
                )
        return overdue
