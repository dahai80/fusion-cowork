"""技能持久化 + 用户自写 skill 包 — P2-11。

用户 skill 包存于 ~/.fusion-cowork/skills/<name>/skill.json, 三种 handler 类型:
- workflow: 引用模板 id, 经 WorkflowEngine 执行
- node:     引用节点名 + params, 经 NodeRegistry 执行
- script:   引用 handler.py, 调用 async def handle(args) -> Any
"""

from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from .registry import Skill, SkillRegistry

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = Path.home() / ".fusion-cowork" / "skills"

VALID_TYPES = ("workflow", "node", "script")


@dataclass
class SkillPack:
    name: str
    description: str = ""
    category: str = "custom"
    aliases: List[str] = field(default_factory=list)
    type: str = "node"
    template_id: str = ""
    node: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    script: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "aliases": self.aliases,
            "type": self.type,
            "template_id": self.template_id,
            "node": self.node,
            "params": self.params,
            "script": self.script,
            "inputs": self.inputs,
        }

    @classmethod
    def from_manifest(cls, data: Dict[str, Any]) -> SkillPack:
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            category=data.get("category", "custom"),
            aliases=list(data.get("aliases", []) or []),
            type=data.get("type", "node"),
            template_id=data.get("template_id", ""),
            node=data.get("node", ""),
            params=dict(data.get("params", {}) or {}),
            script=data.get("script", ""),
            inputs=dict(data.get("inputs", {}) or {}),
        )


def _pack_dir(skills_dir: Path, name: str) -> Path:
    safe = name.lstrip("/")
    return skills_dir / safe


def save_skill_pack(pack: SkillPack, skills_dir: Path = None) -> Path:
    """写入 skill 包到磁盘。返回包目录。"""
    skills_dir = skills_dir or DEFAULT_SKILLS_DIR
    skills_dir.mkdir(parents=True, exist_ok=True)
    if pack.type not in VALID_TYPES:
        raise ValueError(f"非法 skill 类型: {pack.type}, 允许: {VALID_TYPES}")
    if not pack.name:
        raise ValueError("skill name 不能为空")
    pdir = _pack_dir(skills_dir, pack.name)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "skill.json").write_text(json.dumps(pack.to_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"skill 包已保存: {pack.name} -> {pdir}")
    return pdir


def delete_skill_pack(name: str, skills_dir: Path = None) -> bool:
    skills_dir = skills_dir or DEFAULT_SKILLS_DIR
    pdir = _pack_dir(skills_dir, name)
    if not pdir.exists():
        logger.warning(f"skill 包不存在: {name}")
        return False
    import shutil

    shutil.rmtree(pdir)
    logger.info(f"skill 包已删除: {name}")
    return True


def list_skill_packs(skills_dir: Path = None) -> List[SkillPack]:
    """扫描磁盘上的用户 skill 包。"""
    skills_dir = skills_dir or DEFAULT_SKILLS_DIR
    if not skills_dir.exists():
        return []
    packs = []
    for pdir in sorted(skills_dir.iterdir()):
        if not pdir.is_dir():
            continue
        manifest = pdir / "skill.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            packs.append(SkillPack.from_manifest(data))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"skill 包解析失败 {pdir.name}: {e}")
    logger.info(f"扫描到 {len(packs)} 个用户 skill 包")
    return packs


def make_pack_handler(pack: SkillPack) -> Callable:
    """为 skill 包构造异步 handler。"""

    async def _handler(args: str = "") -> Any:
        return await _execute_pack(pack, args)

    return _handler


async def _execute_pack(pack: SkillPack, args: str = "") -> Any:
    if pack.type == "workflow":
        return await _exec_workflow_pack(pack, args)
    elif pack.type == "node":
        return await _exec_node_pack(pack, args)
    elif pack.type == "script":
        return await _exec_script_pack(pack, args)
    return {"error": f"未知 skill 类型: {pack.type}"}


async def _exec_workflow_pack(pack: SkillPack, args: str) -> Any:
    from ..engine import Workflow, WorkflowEngine
    from ..templates import TemplateManager

    mgr = TemplateManager()
    template = mgr.get_template(pack.template_id)
    if not template:
        return {"error": f"模板不存在: {pack.template_id}"}
    try:
        wf = Workflow.from_dict(template.get("workflow", template))
        engine = WorkflowEngine()
        result = await engine.execute(wf, dict(pack.inputs))
        return {
            "status": result.status.value if hasattr(result, "status") else "completed",
            "steps": len(result.steps) if hasattr(result, "steps") else 0,
        }
    except Exception as e:
        logger.error(f"workflow skill 执行失败 {pack.name}: {e}")
        return {"error": str(e)}


async def _exec_node_pack(pack: SkillPack, args: str) -> Any:
    from ..engine.node import NodeConfig, NodeRegistry

    node = NodeRegistry.create(pack.node, config=NodeConfig(params=dict(pack.params)))
    if node is None:
        return {"error": f"节点不可用: {pack.node}"}
    try:
        result = await node.execute(dict(pack.inputs))
        return {"status": result.status.value, "summary": result.summary, "data": getattr(result, "data", None)}
    except Exception as e:
        logger.error(f"node skill 执行失败 {pack.name}: {e}")
        return {"error": str(e)}


async def _exec_script_pack(pack: SkillPack, args: str) -> Any:
    skills_dir = DEFAULT_SKILLS_DIR
    pdir = _pack_dir(skills_dir, pack.name)
    script_path = (pdir / pack.script) if pack.script else None
    if not script_path or not script_path.exists():
        return {"error": f"脚本不存在: {pack.script}"}
    try:
        mod_name = f"fusion_cowork_skill_{pack.name.lstrip('/')}"
        spec = importlib.util.spec_from_file_location(mod_name, str(script_path))
        if spec is None or spec.loader is None:
            return {"error": f"脚本模块加载失败: {script_path}"}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handle = getattr(module, "handle", None)
        if handle is None:
            return {"error": "脚本缺少 async def handle(args) 函数"}
        return await handle(args)
    except Exception as e:
        logger.error(f"script skill 执行失败 {pack.name}: {e}")
        return {"error": str(e)}


def register_user_packs(registry: SkillRegistry, skills_dir: Path = None) -> List[str]:
    """加载磁盘 skill 包并注册到 registry。返回注册名列表。"""
    packs = list_skill_packs(skills_dir)
    registered = []
    for pack in packs:
        try:
            skill = Skill(
                name=pack.name,
                description=pack.description,
                handler=make_pack_handler(pack),
                category=pack.category,
                aliases=pack.aliases,
            )
            registry.register(skill)
            registered.append(pack.name)
        except Exception as e:
            logger.error(f"注册用户 skill 包失败 {pack.name}: {e}")
    logger.info(f"注册 {len(registered)} 个用户 skill 包")
    return registered
