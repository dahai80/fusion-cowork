"""协作空间知识库服务 — fusion-kb 绑定 + 文档管理。

业务逻辑:
- bind_kb: 绑定/创建空间专属知识库
- upload_document: 上传文档到空间知识库
- search: 语义搜索空间知识库
- query: RAG 问答
- list_documents: 列出知识库文档
- unbind_kb: 解绑知识库
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..ai.mlx_client import KBClient
from .permission import SpacePermission
from .store import SpaceStore

logger = logging.getLogger(__name__)


class SpaceKBService:
    """空间知识库服务 — 知识库绑定 + 文档管理 + RAG。"""

    def __init__(
        self,
        store: SpaceStore,
        kb_client: Optional[KBClient],
        permission: SpacePermission,
    ):
        self._store = store
        self._kb = kb_client
        self._perm = permission

    async def bind_kb(
        self,
        space_id: str,
        operator_id: str,
        kb_id: Optional[str] = None,
    ) -> str:
        if not await self._perm.check(space_id, operator_id, "manage_kb"):
            raise PermissionError(f"用户 {operator_id} 无权管理空间 {space_id} 知识库")
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"空间 {space_id} 不存在")

        if not self._kb:
            if not kb_id:
                kb_id = f"kb_{space_id}"
                logger.info(f"SpaceKB.bind_kb local fallback kb={kb_id} for space={space_id}")
            await self._store.update_space(space_id, kb_id=kb_id)
            logger.info(f"SpaceKB.bind_kb space={space_id} kb={kb_id} (no external KB)")
            return kb_id

        if kb_id:
            bases = await self._kb.list_bases()
            found = any(b.get("id") == kb_id or b.get("name") == kb_id for b in bases)
            if not found:
                raise ValueError(f"知识库 {kb_id} 不存在")
        else:
            kb_id = await self._kb.create_kb(name=f"space_{space_id}")
            logger.info(f"SpaceKB.bind_kb created new kb={kb_id} for space={space_id}")

        await self._store.update_space(space_id, kb_id=kb_id)
        logger.info(f"SpaceKB.bind_kb space={space_id} kb={kb_id}")
        return kb_id

    async def upload_document(
        self,
        space_id: str,
        operator_id: str,
        file_path: str,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not await self._perm.check(space_id, operator_id, "upload_document"):
            raise PermissionError(f"用户 {operator_id} 无权上传文档到空间 {space_id}")
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"空间 {space_id} 不存在")
        if not space.kb_id:
            raise ValueError(f"空间 {space_id} 尚未绑定知识库")
        if not self._kb:
            logger.warning(f"SpaceKB.upload_document no external KB, file={file_name or file_path} recorded only")
            return {"status": "recorded", "file": file_name or file_path, "kb_id": space.kb_id}

        result = await self._kb.upload_file(
            kb_id=space.kb_id,
            file_path=file_path,
            file_name=file_name,
        )
        logger.info(f"SpaceKB.upload_document space={space_id} kb={space.kb_id} file={file_name or file_path}")
        return result

    async def search(
        self,
        space_id: str,
        query: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"空间 {space_id} 不存在")
        if not space.kb_id:
            return []
        if not self._kb:
            return []

        results = await self._kb.search(
            kb_id=space.kb_id,
            query=query,
            top_k=top_k,
        )
        logger.debug(f"SpaceKB.search space={space_id} query={query[:30]} hits={len(results)}")
        return results

    async def query(
        self,
        space_id: str,
        question: str,
        top_k: int = 5,
    ) -> str:
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"空间 {space_id} 不存在")
        if not space.kb_id:
            return ""
        if not self._kb:
            return ""

        answer = await self._kb.query(
            kb_id=space.kb_id,
            question=question,
            top_k=top_k,
        )
        logger.debug(f"SpaceKB.query space={space_id} question={question[:30]}")
        return answer

    async def list_documents(
        self,
        space_id: str,
    ) -> List[Dict[str, Any]]:
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"空间 {space_id} 不存在")
        if not space.kb_id:
            return []
        if not self._kb:
            return []

        return await self._kb.list_documents(kb_id=space.kb_id)

    async def unbind_kb(
        self,
        space_id: str,
        operator_id: str,
    ) -> bool:
        if not await self._perm.check(space_id, operator_id, "manage_kb"):
            raise PermissionError(f"用户 {operator_id} 无权管理空间 {space_id} 知识库")
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"空间 {space_id} 不存在")
        if not space.kb_id:
            return False

        old_kb = space.kb_id
        await self._store.update_space(space_id, kb_id=None)
        logger.info(f"SpaceKB.unbind_kb space={space_id} old_kb={old_kb}")
        return True

    async def get_kb_status(
        self,
        space_id: str,
    ) -> Dict[str, Any]:
        space = await self._store.get_space(space_id)
        if not space:
            raise ValueError(f"空间 {space_id} 不存在")
        if not space.kb_id:
            return {"bound": False, "kb_id": None}
        if not self._kb:
            return {"bound": True, "kb_id": space.kb_id, "available": False}

        try:
            health = await self._kb.health()
            return {"bound": True, "kb_id": space.kb_id, "available": health}
        except Exception:
            return {"bound": True, "kb_id": space.kb_id, "available": False}
