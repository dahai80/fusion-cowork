"""SharedContext - workflow nodes can access space context.

Provides a lightweight context container that workflow nodes
can use to read space messages, query KB, and access space metadata.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SharedContext:
    """Space-aware context for workflow node execution.

    Injected into node inputs as `_shared_context` key.
    Nodes can access space messages, KB search, and metadata
    through this object without direct coupling to space services.
    """

    def __init__(
        self,
        space_id: str,
        chat_service: Optional[Any] = None,
        kb_service: Optional[Any] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        self.space_id = space_id
        self._chat = chat_service
        self._kb = kb_service
        self._extra = extra or {}

    async def get_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self._chat:
            return []
        try:
            msgs = await self._chat.list_messages(self.space_id, limit=limit)
            return [m.to_dict() for m in msgs]
        except Exception as e:
            logger.warning(f"SharedContext.get_messages failed: {e}")
            return []

    async def search_kb(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        if not self._kb:
            return []
        try:
            return await self._kb.search(self.space_id, query, top_k=top_k)
        except Exception as e:
            logger.warning(f"SharedContext.search_kb failed: {e}")
            return []

    async def query_kb(self, question: str, top_k: int = 5) -> str:
        if not self._kb:
            return ""
        try:
            return await self._kb.query(self.space_id, question, top_k=top_k)
        except Exception as e:
            logger.warning(f"SharedContext.query_kb failed: {e}")
            return ""

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extra[key] = value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "space_id": self.space_id,
            "extra_keys": list(self._extra.keys()),
            "has_chat": self._chat is not None,
            "has_kb": self._kb is not None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SharedContext:
        return cls(
            space_id=data.get("space_id", ""),
            extra=data.get("extra"),
        )


def inject_shared_context(
    node_input: Dict[str, Any],
    context: SharedContext,
) -> Dict[str, Any]:
    """Inject SharedContext into node input dict."""
    node_input["_shared_context"] = context
    return node_input


def extract_shared_context(
    node_input: Dict[str, Any],
) -> Optional[SharedContext]:
    """Extract SharedContext from node input dict."""
    ctx = node_input.get("_shared_context")
    if isinstance(ctx, SharedContext):
        return ctx
    return None
