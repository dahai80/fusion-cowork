from __future__ import annotations

import logging
from typing import Any

from .registry import Skill, SkillRegistry

logger = logging.getLogger(__name__)


async def _cleanup_handler(args: str = "") -> Any:
    from ..engine.node import NodeConfig, NodeRegistry
    node = NodeRegistry.create("desktop_clean", config=NodeConfig(params={
        "organize_by_type": True,
        "skip_hidden": True,
        "dry_run": False,
    }))
    if node is None:
        return {"error": "desktop_clean 节点不可用"}
    result = await node.execute({})
    return {"status": result.status.value, "summary": result.summary}


async def _classify_handler(args: str = "") -> Any:
    from ..engine.node import NodeConfig, NodeRegistry
    path = args.strip() or "~/Desktop"
    node = NodeRegistry.create("ai_classify", config=NodeConfig(params={
        "classify_by_content": True,
    }))
    if node is None:
        return {"error": "ai_classify 节点不可用"}
    result = await node.execute({"path": path})
    return {"status": result.status.value, "summary": result.summary}


async def _screenshot_handler(args: str = "") -> Any:
    from ..engine.node import NodeConfig, NodeRegistry
    node = NodeRegistry.create("screen_capture", config=NodeConfig(params={
        "save_to_clipboard": True,
    }))
    if node is None:
        return {"error": "screen_capture 节点不可用"}
    result = await node.execute({})
    return {"status": result.status.value, "summary": result.summary, "data": result.data}


async def _search_handler(args: str = "") -> Any:
    from ..engine.node import NodeConfig, NodeRegistry
    query = args.strip()
    if not query:
        return {"error": "请提供搜索关键词"}
    node = NodeRegistry.create("web_search", config=NodeConfig(params={
        "query": query,
        "max_results": 5,
    }))
    if node is None:
        return {"error": "web_search 节点不可用"}
    result = await node.execute({"query": query})
    return {"status": result.status.value, "data": result.data}


async def _organize_handler(args: str = "") -> Any:
    from ..engine.node import NodeConfig, NodeRegistry
    node = NodeRegistry.create("download_organizer", config=NodeConfig(params={
        "organize_by_type": True,
        "deduplicate": True,
        "dry_run": False,
    }))
    if node is None:
        return {"error": "download_organizer 节点不可用"}
    result = await node.execute({})
    return {"status": result.status.value, "summary": result.summary}


async def _diskclean_handler(args: str = "") -> Any:
    from ..engine.node import NodeConfig, NodeRegistry
    node = NodeRegistry.create("disk_cleaner", config=NodeConfig(params={
        "clean_cache": True,
        "clean_temp": True,
        "clean_pycache": True,
        "clean_ds_store": True,
        "dry_run": False,
    }))
    if node is None:
        return {"error": "disk_cleaner 节点不可用"}
    result = await node.execute({})
    return {"status": result.status.value, "summary": result.summary}


BUILTIN_SKILLS = [
    Skill(
        name="/cleanup",
        description="一键桌面清理 — 按类型自动规整桌面文件",
        handler=_cleanup_handler,
        category="desktop",
        aliases=["清理桌面", "桌面规整"],
    ),
    Skill(
        name="/classify",
        description="AI 文件分类 — 根据文件内容语义智能分类",
        handler=_classify_handler,
        category="ai",
        aliases=["文件分类", "智能分类"],
    ),
    Skill(
        name="/screenshot",
        description="截屏 — 截取当前屏幕并保存到剪贴板",
        handler=_screenshot_handler,
        category="desktop",
        aliases=["截屏", "屏幕截图"],
    ),
    Skill(
        name="/search",
        description="网页搜索 — 搜索互联网信息",
        handler=_search_handler,
        category="tool",
        aliases=["搜索", "网页搜索"],
    ),
    Skill(
        name="/organize",
        description="下载整理 — 自动归档下载文件夹",
        handler=_organize_handler,
        category="desktop",
        aliases=["下载整理", "下载归档"],
    ),
    Skill(
        name="/diskclean",
        description="磁盘清理 — 清理缓存、临时文件等垃圾",
        handler=_diskclean_handler,
        category="desktop",
        aliases=["磁盘清理", "清理磁盘"],
    ),
]


def register_builtin_skills(registry: SkillRegistry) -> None:
    for skill in BUILTIN_SKILLS:
        registry.register(skill)
    logger.info(f"注册 {len(BUILTIN_SKILLS)} 个内置技能")
