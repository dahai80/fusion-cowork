"""协作空间持久化存储 — aiosqlite + SQLite WAL 模式。

9 张表: spaces, space_members, space_messages, space_comments,
        space_agents, space_snapshots, space_invite_links,
        space_workflows, space_artifacts, sync_events。

设计:
- aiosqlite 异步访问，WAL 模式支持并发读写
- 每个 CRUD 操作封装为 async 方法
- to_dict/from_dict 转换层隔离 SQL 与业务模型
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiosqlite

from .models import (
    Space,
    SpaceConfig,
    SpaceMember,
    SpaceMessage,
    SpaceRole,
    SpaceSnapshot,
    SpaceStatus,
)

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = os.path.expanduser("~/.fusion-cowork/spaces")
_DB_FILENAME = "spaces.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS spaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    kb_bind_mode TEXT NOT NULL DEFAULT 'new_private',
    kb_id TEXT,
    collab_mode TEXT NOT NULL DEFAULT 'local',
    config TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_members (
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    display_name TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL,
    last_active TEXT NOT NULL,
    PRIMARY KEY (space_id, user_id)
);

CREATE TABLE IF NOT EXISTS space_messages (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    agent_id TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    content TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'text',
    attachments TEXT NOT NULL DEFAULT '[]',
    parent_msg_id TEXT,
    thread_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_comments (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    author_id TEXT NOT NULL DEFAULT '',
    author_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_agents (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    agent_type TEXT NOT NULL DEFAULT 'assistant',
    system_prompt TEXT NOT NULL DEFAULT '',
    enable_rag INTEGER NOT NULL DEFAULT 0,
    config TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_snapshots (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    messages_count INTEGER NOT NULL DEFAULT 0,
    agents_count INTEGER NOT NULL DEFAULT 0,
    files_count INTEGER NOT NULL DEFAULT 0,
    workflows_count INTEGER NOT NULL DEFAULT 0,
    artifacts_count INTEGER NOT NULL DEFAULT 0,
    snapshot_data TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_invite_links (
    code TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    max_uses INTEGER NOT NULL DEFAULT 0,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_workflows (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    workflow_data TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_artifacts (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    artifact_type TEXT NOT NULL DEFAULT 'file',
    content TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    space_id TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT '',
    event_data TEXT NOT NULL DEFAULT '{}',
    lamport_ts INTEGER NOT NULL DEFAULT 0,
    node_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_space_members_space ON space_members(space_id);
CREATE INDEX IF NOT EXISTS idx_space_messages_space ON space_messages(space_id);
CREATE INDEX IF NOT EXISTS idx_space_messages_created ON space_messages(space_id, created_at);
CREATE INDEX IF NOT EXISTS idx_space_agents_space ON space_agents(space_id);
CREATE INDEX IF NOT EXISTS idx_space_snapshots_space ON space_snapshots(space_id);
CREATE INDEX IF NOT EXISTS idx_space_workflows_space ON space_workflows(space_id);
CREATE INDEX IF NOT EXISTS idx_space_artifacts_space ON space_artifacts(space_id);
CREATE INDEX IF NOT EXISTS idx_sync_events_space ON sync_events(space_id);
"""

_MIGRATION_SQL = [
    "ALTER TABLE space_artifacts ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE space_artifacts ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE space_artifacts ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE space_artifacts ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
    (
        "CREATE TABLE IF NOT EXISTS sidebar_modules ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '', "
        "route_path TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, "
        "metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS space_notifications ("
        "id TEXT PRIMARY KEY, space_id TEXT NOT NULL, user_id TEXT NOT NULL, "
        "notification_type TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', "
        "content TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}', "
        "read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
    ),
]


class SpaceStore:
    """协作空间存储 — aiosqlite 异步 CRUD。"""

    def __init__(self, data_dir: str = _DEFAULT_DATA_DIR, trajectory_exporter=None):
        self._data_dir = data_dir
        self._db_path = os.path.join(data_dir, _DB_FILENAME)
        self._db: Optional[aiosqlite.Connection] = None
        self._trajectory_exporter = trajectory_exporter
        # A-8: 写事务序列化锁 — 单共享连接下并发写必 OperationalError: database is locked。
        # asyncio.Lock 让写串行, 配 busy_timeout 重试, 不再裸 commit。
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # A-8: busy_timeout 5s — 写冲突时等待而非立即报 locked。
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(_SCHEMA_SQL)
        for sql in _MIGRATION_SQL:
            try:
                await self._db.execute(sql)
            except aiosqlite.OperationalError:
                pass
        await self._db.commit()
        logger.info(f"SpaceStore 初始化完成: {self._db_path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("SpaceStore 已关闭")

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.initialize()
        assert self._db is not None
        return self._db

    # A-8: 写事务上下文 — 串行锁 + BEGIN IMMEDIATE 取写锁 + 统一 commit/回滚。
    # 单共享连接下并发写必 database is locked; 锁内 BEGIN IMMEDIATE 立即拿写锁,
    # 异常自动 ROLLBACK 释放。所有写方法 (含 artifact service) 走此上下文, 杜绝裸 commit。
    class _WriteTx:
        def __init__(self, store: SpaceStore):
            self._store = store

        async def __aenter__(self) -> aiosqlite.Connection:
            await self._store._write_lock.acquire()
            db = await self._store._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            return db

        async def __aexit__(self, exc_type, exc, tb):
            db = self._store._db
            try:
                if exc_type is None:
                    await db.commit()
                else:
                    await db.execute("ROLLBACK")
            except Exception as ce:
                logger.error(f"_WriteTx 收尾异常: {ce}")
            finally:
                self._store._write_lock.release()
            return False

    def write_tx(self) -> _WriteTx:
        # 公共入口 — artifact/service 等跨模块写共享同一串行事务, 保证写隔离。
        return self._WriteTx(self)

    # ── Space CRUD ──

    async def create_space(self, space: Space) -> Space:
        async with self._WriteTx(self) as db:
            await db.execute(
                "INSERT INTO spaces (id, name, description, owner_id, status, "
                "kb_bind_mode, kb_id, collab_mode, config, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    space.id,
                    space.name,
                    space.description,
                    space.owner_id,
                    space.status.value if isinstance(space.status, SpaceStatus) else space.status,
                    space.kb_bind_mode,
                    space.kb_id,
                    space.collab_mode,
                    json.dumps(space.config.to_dict(), ensure_ascii=False),
                    space.created_at,
                    space.updated_at,
                ),
            )
        logger.info(f"SpaceStore.create_space id={space.id} name={space.name}")
        return space

    async def get_space(self, space_id: str) -> Optional[Space]:
        db = await self._ensure_db()
        cursor = await db.execute("SELECT * FROM spaces WHERE id = ?", (space_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_space(row)

    async def list_spaces(
        self,
        status: Optional[str] = None,
        owner_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Space]:
        db = await self._ensure_db()
        conditions = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if owner_id:
            conditions.append("owner_id = ?")
            params.append(owner_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        cursor = await db.execute(
            f"SELECT * FROM spaces{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_space(r) for r in rows]

    async def update_space(self, space_id: str, **kwargs) -> Optional[Space]:
        # CR-4: 列白名单, 拒非白名单列 (防 kwargs 插值注入未知列名)
        _ALLOWED = {"name", "description", "owner_id", "status", "kb_bind_mode", "kb_id", "collab_mode", "config"}
        sets = []
        params: list = []
        for key, val in kwargs.items():
            if key not in _ALLOWED:
                logger.warning(f"SpaceStore.update_space 拒非白名单列: {key}")
                continue
            if key == "config":
                sets.append("config = ?")
                params.append(json.dumps(val.to_dict() if hasattr(val, "to_dict") else val, ensure_ascii=False))
            else:
                sets.append(f"{key} = ?")
                params.append(val)
        if not sets:
            return await self.get_space(space_id)
        sets.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(space_id)
        async with self._WriteTx(self) as db:
            await db.execute(f"UPDATE spaces SET {', '.join(sets)} WHERE id = ?", params)
        logger.info(f"SpaceStore.update_space id={space_id} fields={list(kwargs.keys())}")
        return await self.get_space(space_id)

    async def delete_space(self, space_id: str) -> bool:
        async with self._WriteTx(self) as db:
            await db.execute("DELETE FROM space_members WHERE space_id = ?", (space_id,))
            await db.execute("DELETE FROM space_messages WHERE space_id = ?", (space_id,))
            await db.execute("DELETE FROM space_agents WHERE space_id = ?", (space_id,))
            await db.execute("DELETE FROM space_snapshots WHERE space_id = ?", (space_id,))
            await db.execute("DELETE FROM space_workflows WHERE space_id = ?", (space_id,))
            await db.execute("DELETE FROM space_artifacts WHERE space_id = ?", (space_id,))
            await db.execute("DELETE FROM space_invite_links WHERE space_id = ?", (space_id,))
            cursor = await db.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
            deleted = cursor.rowcount > 0
        logger.info(f"SpaceStore.delete_space id={space_id} deleted={deleted}")
        return deleted

    # ── Member CRUD ──

    async def add_member(self, member: SpaceMember) -> SpaceMember:
        async with self._WriteTx(self) as db:
            await db.execute(
                "INSERT INTO space_members (space_id, user_id, role, display_name, joined_at, last_active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    member.space_id,
                    member.user_id,
                    member.role.value if isinstance(member.role, SpaceRole) else member.role,
                    member.display_name,
                    member.joined_at,
                    member.last_active,
                ),
            )
        logger.info(f"SpaceStore.add_member space={member.space_id} user={member.user_id} role={member.role}")
        return member

    async def get_member(self, space_id: str, user_id: str) -> Optional[SpaceMember]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_members WHERE space_id = ? AND user_id = ?",
            (space_id, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_member(row)

    async def list_members(self, space_id: str) -> List[SpaceMember]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_members WHERE space_id = ? ORDER BY joined_at",
            (space_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_member(r) for r in rows]

    async def update_member(self, space_id: str, user_id: str, **kwargs) -> Optional[SpaceMember]:
        # CR-4: 列白名单
        _ALLOWED = {"role", "display_name", "last_active"}
        sets = []
        params: list = []
        for key, val in kwargs.items():
            if key not in _ALLOWED:
                logger.warning(f"SpaceStore.update_member 拒非白名单列: {key}")
                continue
            if isinstance(val, SpaceRole):
                val = val.value
            sets.append(f"{key} = ?")
            params.append(val)
        if not sets:
            return await self.get_member(space_id, user_id)
        sets.append("last_active = ?")
        params.append(datetime.now().isoformat())
        params.extend([space_id, user_id])
        async with self._WriteTx(self) as db:
            await db.execute(
                f"UPDATE space_members SET {', '.join(sets)} WHERE space_id = ? AND user_id = ?",
                params,
            )
        logger.info(f"SpaceStore.update_member space={space_id} user={user_id}")
        return await self.get_member(space_id, user_id)

    async def remove_member(self, space_id: str, user_id: str) -> bool:
        async with self._WriteTx(self) as db:
            cursor = await db.execute(
                "DELETE FROM space_members WHERE space_id = ? AND user_id = ?",
                (space_id, user_id),
            )
            deleted = cursor.rowcount > 0
        logger.info(f"SpaceStore.remove_member space={space_id} user={user_id} deleted={deleted}")
        return deleted

    async def count_members(self, space_id: str) -> int:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM space_members WHERE space_id = ?",
            (space_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Message CRUD ──

    async def add_message(self, msg: SpaceMessage) -> SpaceMessage:
        async with self._WriteTx(self) as db:
            await db.execute(
                "INSERT INTO space_messages (id, space_id, user_id, agent_id, role, content, "
                "content_type, attachments, parent_msg_id, thread_id, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg.id,
                    msg.space_id,
                    msg.user_id,
                    msg.agent_id,
                    msg.role,
                    msg.content,
                    msg.content_type,
                    json.dumps(msg.attachments, ensure_ascii=False),
                    msg.parent_msg_id,
                    msg.thread_id,
                    json.dumps(msg.metadata, ensure_ascii=False),
                    msg.created_at,
                ),
            )
        logger.debug(f"SpaceStore.add_message id={msg.id} space={msg.space_id}")
        if self._trajectory_exporter is not None:
            try:
                self._trajectory_exporter.export_message(msg)
            except Exception as e:
                logger.error(f"space 轨迹导出失败 msg={msg.id}: {e}")
        return msg

    async def get_messages(
        self,
        space_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> List[SpaceMessage]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_messages WHERE space_id = ? ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (space_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [self._row_to_message(r) for r in rows]

    async def delete_message(self, msg_id: str) -> bool:
        async with self._WriteTx(self) as db:
            cursor = await db.execute("DELETE FROM space_messages WHERE id = ?", (msg_id,))
            return cursor.rowcount > 0

    async def count_messages(self, space_id: str) -> int:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM space_messages WHERE space_id = ?",
            (space_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Agent CRUD ──

    async def add_agent(self, agent_data: Dict[str, Any]) -> str:
        import uuid

        agent_id = agent_data.get("id") or f"agent_{uuid.uuid4().hex[:8]}"
        async with self._WriteTx(self) as db:
            await db.execute(
                "INSERT INTO space_agents (id, space_id, name, agent_type, system_prompt, "
                "enable_rag, config, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    agent_data.get("space_id", ""),
                    agent_data.get("name", ""),
                    agent_data.get("agent_type", "assistant"),
                    agent_data.get("system_prompt", ""),
                    int(agent_data.get("enable_rag", False)),
                    json.dumps(agent_data.get("config", {}), ensure_ascii=False),
                    agent_data.get("created_by", ""),
                    datetime.now().isoformat(),
                ),
            )
        logger.info(f"SpaceStore.add_agent id={agent_id}")
        return agent_id

    async def get_agent_def(self, space_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_agents WHERE id = ? AND space_id = ?",
            (agent_id, space_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)

    async def list_agents(self, space_id: str) -> List[Dict[str, Any]]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_agents WHERE space_id = ?",
            (space_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_agent(self, space_id: str, agent_id: str, **kwargs) -> bool:
        if not kwargs:
            return False
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k in ("name", "agent_type", "system_prompt", "enable_rag", "config"):
                if k == "config":
                    v = json.dumps(v, ensure_ascii=False)
                elif k == "enable_rag":
                    v = int(v)
                sets.append(f"{k} = ?")
                vals.append(v)
        if not sets:
            return False
        vals.extend([agent_id, space_id])
        async with self._WriteTx(self) as db:
            await db.execute(
                f"UPDATE space_agents SET {', '.join(sets)} WHERE id = ? AND space_id = ?",
                vals,
            )
        logger.info(f"SpaceStore.update_agent id={agent_id} fields={list(kwargs.keys())}")
        return True

    async def remove_agent(self, space_id: str, agent_id: str) -> bool:
        async with self._WriteTx(self) as db:
            await db.execute(
                "DELETE FROM space_agents WHERE id = ? AND space_id = ?",
                (agent_id, space_id),
            )
        logger.info(f"SpaceStore.remove_agent id={agent_id} space={space_id}")
        return True

    # ── Snapshot CRUD ──

    async def create_snapshot(self, snapshot: SpaceSnapshot) -> SpaceSnapshot:
        async with self._WriteTx(self) as db:
            await db.execute(
                "INSERT INTO space_snapshots (id, space_id, name, messages_count, agents_count, "
                "files_count, workflows_count, artifacts_count, snapshot_data, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.id,
                    snapshot.space_id,
                    snapshot.name,
                    snapshot.messages_count,
                    snapshot.agents_count,
                    snapshot.files_count,
                    snapshot.workflows_count,
                    snapshot.artifacts_count,
                    json.dumps(snapshot.snapshot_data, ensure_ascii=False),
                    snapshot.created_by,
                    snapshot.created_at,
                ),
            )
        logger.info(f"SpaceStore.create_snapshot id={snapshot.id} space={snapshot.space_id}")
        return snapshot

    async def list_snapshots(self, space_id: str) -> List[SpaceSnapshot]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_snapshots WHERE space_id = ? ORDER BY created_at DESC",
            (space_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    async def get_snapshot(self, space_id: str, snapshot_id: str) -> Optional[SpaceSnapshot]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_snapshots WHERE id = ? AND space_id = ?",
            (snapshot_id, space_id),
        )
        row = await cursor.fetchone()
        return self._row_to_snapshot(row) if row else None

    async def delete_snapshot(self, space_id: str, snapshot_id: str) -> bool:
        async with self._WriteTx(self) as db:
            await db.execute(
                "DELETE FROM space_snapshots WHERE id = ? AND space_id = ?",
                (snapshot_id, space_id),
            )
        logger.info(f"SpaceStore.delete_snapshot id={snapshot_id}")
        return True

    # ── Comment CRUD ──

    async def add_comment(
        self,
        message_id: str,
        author_id: str,
        author_name: str,
        content: str,
        space_id: str = "",
        thread_id: str = "",
    ) -> str:
        import uuid

        comment_id = f"cmt_{uuid.uuid4().hex[:8]}"
        async with self._WriteTx(self) as db:
            await db.execute(
                "INSERT INTO space_comments (id, space_id, message_id, author_id, author_name, "
                "content, thread_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    comment_id,
                    space_id,
                    message_id,
                    author_id,
                    author_name,
                    content,
                    thread_id,
                    datetime.now().isoformat(),
                ),
            )
        logger.info(f"SpaceStore.add_comment id={comment_id} thread={thread_id or '-'}")
        return comment_id

    async def list_comments(self, message_id: str) -> List[Dict[str, Any]]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_comments WHERE message_id = ? ORDER BY created_at",
            (message_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Invite Link CRUD ──

    async def create_invite(
        self,
        code: str,
        space_id: str,
        role: str = "member",
        max_uses: int = 0,
        expires_at: Optional[str] = None,
        created_by: str = "",
    ) -> str:
        async with self._WriteTx(self) as db:
            role_str = role.value if isinstance(role, SpaceRole) else role
            await db.execute(
                "INSERT INTO space_invite_links (code, space_id, role, max_uses, uses, "
                "expires_at, created_by, created_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (code, space_id, role_str, max_uses, expires_at, created_by, datetime.now().isoformat()),
            )
        logger.info(f"SpaceStore.create_invite code={code} space={space_id}")
        return code

    async def get_invite(self, code: str) -> Optional[Dict[str, Any]]:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM space_invite_links WHERE code = ?",
            (code,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def use_invite(self, code: str) -> bool:
        async with self._WriteTx(self) as db:
            await db.execute(
                "UPDATE space_invite_links SET uses = uses + 1 WHERE code = ?",
                (code,),
            )
        return True

    # ── Sync Events ──

    async def add_sync_event(
        self, space_id: str, event_type: str, event_data: Dict[str, Any], lamport_ts: int, node_id: str
    ) -> int:
        async with self._WriteTx(self) as db:
            cursor = await db.execute(
                "INSERT INTO sync_events (space_id, event_type, event_data, lamport_ts, node_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    space_id,
                    event_type,
                    json.dumps(event_data, ensure_ascii=False),
                    lamport_ts,
                    node_id,
                    datetime.now().isoformat(),
                ),
            )
            rowid = cursor.lastrowid
        return rowid

    # ── Row Converters ──

    @staticmethod
    def _row_to_space(row: aiosqlite.Row) -> Space:
        config_data = {}
        raw_config = row["config"]
        if raw_config:
            try:
                config_data = json.loads(raw_config)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Space config parse error for id={row['id']}")
        status_val = row["status"]
        if isinstance(status_val, str):
            try:
                status_val = SpaceStatus(status_val)
            except ValueError:
                pass
        return Space(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            owner_id=row["owner_id"],
            status=status_val,
            kb_bind_mode=row["kb_bind_mode"],
            kb_id=row["kb_id"],
            collab_mode=row["collab_mode"],
            config=SpaceConfig.from_dict(config_data),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_member(row: aiosqlite.Row) -> SpaceMember:
        role_val = row["role"]
        if isinstance(role_val, str):
            try:
                role_val = SpaceRole(role_val)
            except ValueError:
                pass
        return SpaceMember(
            space_id=row["space_id"],
            user_id=row["user_id"],
            role=role_val,
            display_name=row["display_name"],
            joined_at=row["joined_at"],
            last_active=row["last_active"],
        )

    @staticmethod
    def _row_to_message(row: aiosqlite.Row) -> SpaceMessage:
        attachments = []
        raw_attachments = row["attachments"]
        if raw_attachments:
            try:
                attachments = json.loads(raw_attachments)
            except (json.JSONDecodeError, TypeError):
                pass
        metadata = {}
        raw_metadata = row["metadata"]
        if raw_metadata:
            try:
                metadata = json.loads(raw_metadata)
            except (json.JSONDecodeError, TypeError):
                pass
        return SpaceMessage(
            id=row["id"],
            space_id=row["space_id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            role=row["role"],
            content=row["content"],
            content_type=row["content_type"],
            attachments=attachments,
            parent_msg_id=row["parent_msg_id"],
            thread_id=row["thread_id"],
            metadata=metadata,
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_snapshot(row: aiosqlite.Row) -> SpaceSnapshot:
        snapshot_data = {}
        raw = row["snapshot_data"]
        if raw:
            try:
                snapshot_data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return SpaceSnapshot(
            id=row["id"],
            space_id=row["space_id"],
            name=row["name"],
            messages_count=row["messages_count"],
            agents_count=row["agents_count"],
            files_count=row["files_count"],
            workflows_count=row["workflows_count"],
            artifacts_count=row["artifacts_count"],
            snapshot_data=snapshot_data,
            created_by=row["created_by"],
            created_at=row["created_at"],
        )
