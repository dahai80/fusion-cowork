"""D1 trajectory flywheel - space_messages branch trajectory export.

Writes cowork space message branches (parent_msg_id / thread_id) into the unified
aggregation directory ~/.fusion/trajectories/cowork/space_<space_id>.jsonl, keeping
the thread branch structure so retries can be identified.

Integration:
- After SpaceStore.add_message, call export_message(msg) for incremental writes
- Or call SpaceTrajectoryExporter.export_space(store, space_id) for a full export
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_SPACE_TRAJECTORY_DIR = str(Path.home() / ".fusion" / "trajectories" / "cowork")


@dataclass
class SpaceMessageTrajectory:
    msg_id: str
    space_id: str
    role: str = "user"
    content: str = ""
    content_type: str = "text"
    parent_msg_id: Optional[str] = None
    thread_id: Optional[str] = None
    user_id: str = ""
    agent_id: Optional[str] = None
    created_at: str = ""
    is_retry: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class SpaceTrajectoryExporter:
    """space_messages branch trajectory exporter."""

    def __init__(self, trajectory_dir: str = DEFAULT_SPACE_TRAJECTORY_DIR):
        self._dir = Path(trajectory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"SpaceTrajectoryExporter initialized: {self._dir}")

    def _path(self, space_id: str) -> Path:
        safe = space_id or "unknown_space"
        return self._dir / f"space_{safe}.jsonl"

    def export_message(self, msg: Any) -> str:
        parent_msg_id = getattr(msg, "parent_msg_id", None)
        thread_id = getattr(msg, "thread_id", None)
        is_retry = bool(parent_msg_id) and bool(thread_id)
        traj = SpaceMessageTrajectory(
            msg_id=getattr(msg, "id", ""),
            space_id=getattr(msg, "space_id", ""),
            role=getattr(msg, "role", "user"),
            content=getattr(msg, "content", ""),
            content_type=getattr(msg, "content_type", "text"),
            parent_msg_id=parent_msg_id,
            thread_id=thread_id,
            user_id=getattr(msg, "user_id", ""),
            agent_id=getattr(msg, "agent_id", None),
            created_at=getattr(msg, "created_at", ""),
            is_retry=is_retry,
            metadata=getattr(msg, "metadata", {}) or {},
        )
        path = self._path(traj.space_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(traj.to_jsonl() + "\n")
        logger.debug(f"space trajectory write: {path.name} msg={traj.msg_id} retry={is_retry}")
        return str(path)

    async def export_space(self, store: Any, space_id: str, limit: int = 10000) -> int:
        messages = await store.get_messages(space_id, limit=limit)
        path = self._path(space_id)
        count = 0
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                traj = SpaceMessageTrajectory(
                    msg_id=getattr(msg, "id", ""),
                    space_id=getattr(msg, "space_id", space_id),
                    role=getattr(msg, "role", "user"),
                    content=getattr(msg, "content", ""),
                    content_type=getattr(msg, "content_type", "text"),
                    parent_msg_id=getattr(msg, "parent_msg_id", None),
                    thread_id=getattr(msg, "thread_id", None),
                    user_id=getattr(msg, "user_id", ""),
                    agent_id=getattr(msg, "agent_id", None),
                    created_at=getattr(msg, "created_at", ""),
                    is_retry=bool(getattr(msg, "parent_msg_id", None)) and bool(getattr(msg, "thread_id", None)),
                    metadata=getattr(msg, "metadata", {}) or {},
                )
                f.write(traj.to_jsonl() + "\n")
                count += 1
        logger.info(f"space full export: space={space_id} count={count} -> {path.name}")
        return count

    def list_space_trajectories(self) -> List[str]:
        if not self._dir.exists():
            return []
        return sorted(p.name for p in self._dir.glob("space_*.jsonl"))
