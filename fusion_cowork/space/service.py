"""协作空间服务层 — Space CRUD + archive + delete。

业务逻辑:
- create: 创建空间 + 自动将 owner 加入成员表
- get/list: 查询空间
- update: 更新空间属性
- archive: 标记空间为 archived
- delete: 物理删除空间及关联数据
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .models import Space, SpaceConfig, SpaceMember, SpaceRole, SpaceStatus
from .store import SpaceStore

logger = logging.getLogger(__name__)


class SpaceService:
    """空间服务 — 封装空间 CRUD 业务逻辑。"""

    def __init__(self, store: SpaceStore):
        self._store = store

    async def create(
        self,
        name: str,
        owner_id: str,
        description: str = "",
        kb_bind_mode: str = "new_private",
        kb_id: Optional[str] = None,
        collab_mode: str = "local",
        config: Optional[SpaceConfig] = None,
    ) -> Space:
        space = Space(
            name=name,
            description=description,
            owner_id=owner_id,
            status=SpaceStatus.ACTIVE.value,
            kb_bind_mode=kb_bind_mode,
            kb_id=kb_id,
            collab_mode=collab_mode,
            config=config or SpaceConfig(),
        )
        space = await self._store.create_space(space)
        owner_member = SpaceMember(
            space_id=space.id,
            user_id=owner_id,
            role=SpaceRole.OWNER.value,
            display_name=owner_id,
        )
        await self._store.add_member(owner_member)
        logger.info(f"SpaceService.create id={space.id} name={name} owner={owner_id}")
        return space

    async def get(self, space_id: str) -> Optional[Space]:
        return await self._store.get_space(space_id)

    async def list(
        self,
        status: Optional[str] = None,
        owner_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Space]:
        return await self._store.list_spaces(
            status=status, owner_id=owner_id, limit=limit, offset=offset,
        )

    async def update(self, space_id: str, **kwargs) -> Optional[Space]:
        return await self._store.update_space(space_id, **kwargs)

    async def archive(self, space_id: str) -> Optional[Space]:
        result = await self._store.update_space(
            space_id, status=SpaceStatus.ARCHIVED.value,
        )
        if result:
            logger.info(f"SpaceService.archive id={space_id}")
        return result

    async def unarchive(self, space_id: str) -> Optional[Space]:
        result = await self._store.update_space(
            space_id, status=SpaceStatus.ACTIVE.value,
        )
        if result:
            logger.info(f"SpaceService.unarchive id={space_id}")
        return result

    async def delete(self, space_id: str) -> bool:
        result = await self._store.delete_space(space_id)
        if result:
            logger.info(f"SpaceService.delete id={space_id}")
        return result

    async def get_or_create(self, name: str, owner_id: str, **kwargs) -> Space:
        spaces = await self._store.list_spaces(owner_id=owner_id)
        for sp in spaces:
            if sp.name == name:
                return sp
        return await self.create(name=name, owner_id=owner_id, **kwargs)
