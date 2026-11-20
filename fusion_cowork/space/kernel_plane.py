"""Space chat message plane on the kernel TeamMessage runtime (issue #496).

Chat-standardization roadmap §6-C4: the space chat message store re-points to
the fusion-harness TeamMessage plane. CHOICE: in-process kernel TeamRuntime
(NOT the agent-studio daemon teams.* RPC) — space chat members are the space
roster (users + agents) living in the cowork web-serve process; the studio
daemon teams.* surface (team_node.py) is a task-board dispatcher with no
send/inbox chat plane, and routing space chat through it would change what
executes and add a cross-repo runtime dependency for every message.

What the kernel provides (used verbatim, no reimplementation):
- member-validated send (sender/recipient/mentions must be team members)
- disk-write-confirmed delivery (inbox file write must succeed)
- reply threading (reply_to must be a msg_ id)
- per-member inbox records (kernel-format JSON files under state_dir)

Cowork adds only the mapping layer:
- roster (SpaceMember.user_id + space agent ids) -> kernel TeamDefinition;
  member names are deterministic hashes (kernel requires [A-Za-z0-9_-]+)
- rich SpaceMessage metadata travels in the kernel record's transcript field
  as a JSON envelope (kernel TeamMessage has no metadata field)
- attachments follow the ref-only rule: non-string entries are dropped

Storage retirement: SpaceStore.add_message is no longer written by chat —
existing rows stay readable via the dual-read path in SpaceChatService
(roadmap §7.3 allows the dual-read transition).
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fusion_harness.team import TeamDefError, TeamDefinition, TeamMember
from fusion_harness.team_runtime import TeamMessage, TeamMessageError, TeamRuntime

from .models import SpaceMessage

logger = logging.getLogger(__name__)

# @mention tokens: up to whitespace/punctuation, CJK-friendly
_MENTION_RE = re.compile(r"@([^\s@,，。;；!！?？:：()（）\[\]【】]+)")
_META_VERSION = 1


def _kernel_name(member_id: str) -> str:
    return "m_" + hashlib.sha1(member_id.encode("utf-8")).hexdigest()[:8]


def _duplicate_threshold() -> Optional[int]:
    # issue #558 (#134): FUSION_COWORK_TEAM_DUPLICATE_THRESHOLD — int >= 2
    # enables kernel duplicate-work detection; unset/invalid = off (None)
    raw = os.environ.get("FUSION_COWORK_TEAM_DUPLICATE_THRESHOLD", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"invalid FUSION_COWORK_TEAM_DUPLICATE_THRESHOLD={raw!r} — duplicate detection off")
        return None
    if value < 2:
        logger.warning("FUSION_COWORK_TEAM_DUPLICATE_THRESHOLD must be >= 2 — duplicate detection off")
        return None
    return value


class _PlaneEventBus:
    # Minimal LifecycleBus-shaped sink: captures kernel team events (e.g.
    # team_duplicate_work) per space; capture failure never breaks the emitter
    def __init__(self, space_id: str, sink: Dict[str, List[Dict[str, Any]]], max_events: int = 100):
        self._space_id = space_id
        self._sink = sink
        self._max = max_events

    def emit(self, source: str, event: str, payload: Dict[str, Any], session_id: str = "") -> None:
        try:
            bucket = self._sink.setdefault(self._space_id, [])
            bucket.append({"event": event, "payload": payload, "ts": datetime.now().isoformat()})
            del bucket[: max(0, len(bucket) - self._max)]
            if event == "team_duplicate_work":
                logger.warning(
                    f"SpaceKernelPlane duplicate work space={self._space_id} members={payload.get('members')} key={str(payload.get('key'))[:120]}"
                )
        except Exception as exc:
            logger.debug(f"SpaceKernelPlane event capture failed space={self._space_id}: {exc}")


def _parse_mentions(content: str) -> List[str]:
    seen: List[str] = []
    for tok in _MENTION_RE.findall(content or ""):
        if tok and tok not in seen:
            seen.append(tok)
    return seen


class SpaceKernelPlane:
    """Per-space kernel TeamRuntime bridge for the space chat plane."""

    def __init__(self, store: Any, state_root: str = ""):
        self._store = store
        self._state_root = state_root
        self._runtimes: Dict[str, TeamRuntime] = {}
        self._fingerprints: Dict[str, str] = {}
        self._roster_cache: Dict[str, List[str]] = {}
        self._fanout_warnings: Dict[str, List[str]] = {}
        self._duplicate_events: Dict[str, List[Dict[str, Any]]] = {}

    def _state_dir(self, space_id: str) -> str:
        if self._state_root:
            return os.path.join(self._state_root, space_id)
        data_dir = getattr(self._store, "_data_dir", "") or os.path.join(
            os.path.expanduser("~"), ".fusion-cowork", "space_teams"
        )
        return os.path.join(data_dir, "space_teams", space_id)

    def _roster_fingerprint(self, members: List[str]) -> str:
        return "|".join(sorted(members))

    async def _ensure_runtime(self, space_id: str) -> TeamRuntime:
        member_ids = await self._roster(space_id)
        fp = self._roster_fingerprint(member_ids)
        rt = self._runtimes.get(space_id)
        if rt is not None and self._fingerprints.get(space_id) == fp:
            return rt
        if not member_ids:
            raise ValueError(f"space {space_id} has no chat members (roster empty)")
        kmembers = [
            TeamMember(name=_kernel_name(mid), role="member", skills=[], budget_tokens=50_000) for mid in member_ids
        ]
        try:
            definition = TeamDefinition(
                name=f"space-{space_id}",
                namespace_key=f"space-chat:{space_id}",
                members=kmembers,
            )
        except TeamDefError as exc:
            raise ValueError(f"space {space_id} roster rejected by kernel: {exc}") from exc
        state_dir = self._state_dir(space_id)
        # issue #558 (#134): opt-in duplicate-work detection — env threshold
        # (int >= 2) wires TeamRuntime duplicate_threshold; detected repeats of
        # identical (tool, args) across members emit team_duplicate_work events
        # captured on the plane per space (None = off, zero behavior change)
        threshold = _duplicate_threshold()
        bus = _PlaneEventBus(space_id, self._duplicate_events) if threshold is not None else None
        runtime = TeamRuntime(definition, state_dir=state_dir, duplicate_threshold=threshold, bus=bus)
        # issue #558 (P-series #124): non-fatal fan-out guidance at spawn time —
        # member counts outside the FANOUT_* bands log a warning (token
        # multiplier cost) but never block; latest warnings kept per space
        try:
            from fusion_harness import fanout_warnings

            warnings = fanout_warnings(SimpleNamespace(concurrency=len(kmembers)))
            if warnings:
                self._fanout_warnings[space_id] = warnings
                for w in warnings:
                    logger.warning(f"SpaceKernelPlane fanout space={space_id}: {w}")
            else:
                self._fanout_warnings.pop(space_id, None)
        except ImportError:
            pass
        self._restore_inboxes(runtime, member_ids, state_dir)
        self._runtimes[space_id] = runtime
        self._fingerprints[space_id] = fp
        self._roster_cache[space_id] = list(member_ids)
        logger.info(f"SpaceKernelPlane runtime ready space={space_id} members={len(kmembers)} state_dir={state_dir}")
        return runtime

    def _restore_inboxes(self, runtime: TeamRuntime, member_ids: List[str], state_dir: str) -> None:
        # Kernel plane persistence: inbox files are written by TeamRuntime.send
        # on every delivery; on rebuild we read them back into the runtime so
        # history survives restarts (kernel format, not a cowork store).
        inboxes_dir = os.path.join(state_dir, "inboxes")
        for mid in member_ids:
            kname = _kernel_name(mid)
            path = os.path.join(inboxes_dir, f"{kname}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    records = json.load(fh)
                if isinstance(records, list):
                    runtime._inboxes[kname] = list(records)
            except (OSError, ValueError) as exc:
                logger.warning(f"SpaceKernelPlane inbox restore failed member={kname}: {exc}")

    async def _roster(self, space_id: str) -> List[str]:
        ids: List[str] = []
        try:
            for m in await self._store.list_members(space_id):
                if m.user_id and m.user_id not in ids:
                    ids.append(m.user_id)
        except Exception as exc:
            logger.warning(f"SpaceKernelPlane roster members read failed space={space_id}: {exc}")
        try:
            for a in await self._store.list_agents(space_id):
                aid = str(a.get("id", ""))
                if aid and aid not in ids:
                    ids.append(aid)
        except Exception as exc:
            logger.warning(f"SpaceKernelPlane roster agents read failed space={space_id}: {exc}")
        return ids

    async def send(
        self,
        space_id: str,
        sender_id: str,
        sender_is_agent: bool,
        content: str,
        content_type: str = "text",
        attachments: Optional[List[Any]] = None,
        parent_msg_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        tenant_id: str = "",
        mentions: Optional[List[str]] = None,
    ) -> SpaceMessage:
        runtime = await self._ensure_runtime(space_id)
        knames = {m.name for m in runtime.definition.members}
        raw_ids = self._roster_cache.get(space_id, [])
        sender_kname = _kernel_name(sender_id)
        if sender_kname not in knames:
            raise ValueError(f"sender {sender_id} is not a member of space {space_id}")

        mention_ids = mentions if mentions is not None else _parse_mentions(content)
        for mid in mention_ids:
            if _kernel_name(mid) not in knames:
                raise ValueError(f"unknown mention @{mid}: kernel team member validation failed (space {space_id})")

        # Ref-only rule: files travel as artifact refs, never as content.
        ref_attachments = [str(a) for a in (attachments or []) if isinstance(a, str) and a]
        dropped = len(attachments or []) - len(ref_attachments)
        if dropped:
            logger.warning(f"SpaceKernelPlane dropped {dropped} non-ref attachment(s) (ref-only rule)")

        msg_id = (
            "msg_"
            + hashlib.sha1(f"{space_id}:{sender_id}:{content}:{datetime.now().isoformat()}".encode()).hexdigest()[:12]
        )
        envelope = {
            "v": _META_VERSION,
            "space_id": space_id,
            "role": "assistant" if sender_is_agent else "user",
            "user_id": "" if sender_is_agent else sender_id,
            "agent_id": sender_id if sender_is_agent else "",
            "content_type": content_type,
            "thread_id": thread_id or "",
            "created_at": datetime.now().isoformat(),
            "tenant_id": tenant_id,
            "mentions": mention_ids,
        }
        recipients = mention_ids if mention_ids else [mid for mid in raw_ids if _kernel_name(mid) != sender_kname]
        if not recipients:
            # Single-member space (solo user, agents not added yet): deliver to
            # self so the message still lands on the kernel plane and history
            # stays complete — kernel send permits sender == recipient.
            recipients = [sender_id]

        delivered = 0
        last_error: Optional[str] = None
        for rid in recipients:
            msg = TeamMessage(
                type="text",
                sender=sender_kname,
                recipient=_kernel_name(rid),
                body=content,
                msg_id=msg_id,
                reply_to=parent_msg_id or "",
                mentions=tuple(_kernel_name(m) for m in mention_ids),
                attachments=tuple(ref_attachments),
                transcript=json.dumps(envelope, ensure_ascii=False),
            )
            try:
                result = runtime.send(msg)
            except TeamMessageError as exc:
                raise ValueError(f"kernel message rejected: {exc}") from exc
            if result.get("delivered"):
                delivered += 1
            else:
                last_error = f"recipient {rid} delivery failed"
        if delivered == 0 and last_error:
            raise RuntimeError(f"SpaceKernelPlane delivery failed: {last_error}")
        logger.info(
            f"SpaceKernelPlane.send space={space_id} sender={sender_id} msg={msg_id} recipients={len(recipients)} delivered={delivered}"
        )
        return self._record_to_space_message(
            {
                "msg_id": msg_id,
                "body": content,
                "reply_to": parent_msg_id or "",
                "attachments": ref_attachments,
                "transcript": json.dumps(envelope, ensure_ascii=False),
            }
        )

    async def history(self, space_id: str, limit: int = 100, offset: int = 0) -> List[SpaceMessage]:
        runtime = await self._ensure_runtime(space_id)
        by_id: Dict[str, SpaceMessage] = {}
        for records in runtime._inboxes.values():
            for rec in records:
                mid = str(rec.get("msg_id", ""))
                if not mid or mid in by_id:
                    continue
                by_id[mid] = self._record_to_space_message(rec)
        msgs = sorted(by_id.values(), key=lambda m: (m.created_at, m.id))
        return msgs[offset : offset + limit]

    async def inbox(self, space_id: str, member_id: str) -> List[dict]:
        runtime = await self._ensure_runtime(space_id)
        return runtime.inbox(_kernel_name(member_id))

    def _record_to_space_message(self, rec: dict) -> SpaceMessage:
        meta: Dict[str, Any] = {}
        raw = rec.get("transcript", "")
        if raw:
            try:
                meta = json.loads(raw)
                if not isinstance(meta, dict):
                    meta = {}
            except ValueError:
                logger.warning("SpaceKernelPlane transcript envelope unreadable, defaults applied")
        mentions = meta.get("mentions", [])
        kname_reverse = {v: k for k, v in {_kernel_name(m): m for m in mentions}.items()}
        return SpaceMessage(
            id=str(rec.get("msg_id", "")),
            space_id=str(meta.get("space_id", "")),
            user_id=str(meta.get("user_id", "")),
            agent_id=str(meta.get("agent_id", "")) or None,
            role=str(meta.get("role", "user")),
            content=str(rec.get("body", "")),
            content_type=str(meta.get("content_type", "text")),
            attachments=list(rec.get("attachments", [])),
            parent_msg_id=str(rec.get("reply_to", "")) or None,
            thread_id=str(meta.get("thread_id", "")) or None,
            metadata={"mentions": [kname_reverse.get(m, m) for m in mentions]},
            created_at=str(meta.get("created_at", "")),
            tenant_id=str(meta.get("tenant_id", "")) or "default",
        )
