"""Skill persistence + user-authored skill packs — P2-11.

User skill packs live at ~/.fusion-cowork/skills/<name>/skill.json; three handler types:
- workflow: references a template id, executed via WorkflowEngine
- node:     references a node name + params, executed via NodeRegistry
- script:   references handler.py, calling async def handle(args) -> Any
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
    """Write the skill pack to disk. Returns the pack directory."""
    skills_dir = skills_dir or DEFAULT_SKILLS_DIR
    skills_dir.mkdir(parents=True, exist_ok=True)
    if pack.type not in VALID_TYPES:
        raise ValueError(f"非法 skill 类型: {pack.type}, 允许: {VALID_TYPES}")  # i18n: user-facing
    if not pack.name:
        raise ValueError("skill name 不能为空")  # i18n: user-facing
    pdir = _pack_dir(skills_dir, pack.name)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "skill.json").write_text(json.dumps(pack.to_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Skill pack saved: {pack.name} -> {pdir}")
    return pdir


def delete_skill_pack(name: str, skills_dir: Path = None) -> bool:
    skills_dir = skills_dir or DEFAULT_SKILLS_DIR
    pdir = _pack_dir(skills_dir, name)
    if not pdir.exists():
        logger.warning(f"Skill pack not found: {name}")
        return False
    import shutil

    shutil.rmtree(pdir)
    logger.info(f"Skill pack deleted: {name}")
    return True


def list_skill_packs(skills_dir: Path = None) -> List[SkillPack]:
    """Scan user skill packs on disk."""
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
            # issue #554: artifact_scan before skill load — a hit on injected-instruction or
            # credential-leak rules quarantines + skips the pack (not into SkillRegistry,
            # not into the MCP skill list)
            from ..engine.kernel_gates import get_quarantine, scan_or_quarantine

            clean, report = scan_or_quarantine(
                manifest.read_text(encoding="utf-8"), source=f"skill:{pdir.name}", kind="skill"
            )
            if not clean:
                get_quarantine().add(report, preview=json.dumps(data, ensure_ascii=False)[:200])
                logger.warning(
                    f"Skill pack failed scan, quarantined and skipped {pdir.name}: rules={report.rule_names()}"
                )
                continue
            packs.append(SkillPack.from_manifest(data))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Skill pack parse failed {pdir.name}: {e}")
    logger.info(f"Found {len(packs)} user skill packs")
    return packs


def make_pack_handler(pack: SkillPack, skills_dir: Path = None) -> Callable:
    """Build an async handler for a skill pack. skills_dir matches the install dir; script packs are located by directory."""

    async def _handler(args: str = "") -> Any:
        return await _execute_pack(pack, args, skills_dir)

    return _handler


async def _execute_pack(pack: SkillPack, args: str = "", skills_dir: Path = None) -> Any:
    if pack.type == "workflow":
        return await _exec_workflow_pack(pack, args)
    elif pack.type == "node":
        return await _exec_node_pack(pack, args)
    elif pack.type == "script":
        return await _exec_script_pack(pack, args, skills_dir)
    return {"error": f"未知 skill 类型: {pack.type}"}  # i18n: user-facing


async def _exec_workflow_pack(pack: SkillPack, args: str) -> Any:
    from ..engine import Workflow, WorkflowEngine
    from ..templates import TemplateManager

    mgr = TemplateManager()
    template = mgr.get_template(pack.template_id)
    if not template:
        return {"error": f"模板不存在: {pack.template_id}"}  # i18n: user-facing
    try:
        wf = Workflow.from_dict(template.get("workflow", template))
        engine = WorkflowEngine()
        result = await engine.execute(wf, dict(pack.inputs))
        return {
            "status": result.status.value if hasattr(result, "status") else "completed",
            "steps": len(result.steps) if hasattr(result, "steps") else 0,
        }
    except Exception as e:
        logger.error(f"Workflow skill execution failed {pack.name}: {e}")
        return {"error": str(e)}


async def _exec_node_pack(pack: SkillPack, args: str) -> Any:
    from ..engine.node import NodeConfig, NodeRegistry

    node = NodeRegistry.create(pack.node, config=NodeConfig(params=dict(pack.params)))
    if node is None:
        return {"error": f"节点不可用: {pack.node}"}  # i18n: user-facing
    try:
        result = await node.execute(dict(pack.inputs))
        return {"status": result.status.value, "summary": result.summary, "data": getattr(result, "data", None)}
    except Exception as e:
        logger.error(f"Node skill execution failed {pack.name}: {e}")
        return {"error": str(e)}


async def _exec_script_pack(pack: SkillPack, args: str, skills_dir: Path = None) -> Any:
    skills_dir = skills_dir or DEFAULT_SKILLS_DIR
    pdir = _pack_dir(skills_dir, pack.name)
    script_path = (pdir / pack.script) if pack.script else None
    if not script_path or not script_path.exists():
        return {"error": f"脚本不存在: {pack.script}"}  # i18n: user-facing
    try:
        mod_name = f"fusion_cowork_skill_{pack.name.lstrip('/')}"
        spec = importlib.util.spec_from_file_location(mod_name, str(script_path))
        if spec is None or spec.loader is None:
            return {"error": f"脚本模块加载失败: {script_path}"}  # i18n: user-facing
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handle = getattr(module, "handle", None)
        if handle is None:
            return {"error": "脚本缺少 async def handle(args) 函数"}  # i18n: user-facing
        return await handle(args)
    except Exception as e:
        logger.error(f"Script skill execution failed {pack.name}: {e}")
        return {"error": str(e)}


def register_user_packs(registry: SkillRegistry, skills_dir: Path = None) -> List[str]:
    """Load skill packs from disk and register them into the registry. Returns the list of registered names."""
    packs = list_skill_packs(skills_dir)
    registered = []
    for pack in packs:
        try:
            skill = Skill(
                name=pack.name,
                description=pack.description,
                handler=make_pack_handler(pack, skills_dir),
                category=pack.category,
                aliases=pack.aliases,
            )
            registry.register(skill)
            registered.append(pack.name)
        except Exception as e:
            logger.error(f"Failed to register user skill pack {pack.name}: {e}")
    logger.info(f"Registered {len(registered)} user skill packs")
    return registered
