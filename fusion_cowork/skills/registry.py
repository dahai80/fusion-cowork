from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    name: str
    description: str
    handler: Callable
    category: str = "tool"
    aliases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "aliases": self.aliases,
        }


class SkillRegistry:
    _skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill
        for alias in skill.aliases:
            self._skills[alias] = skill
        logger.info(f"注册技能: {skill.name} (aliases={skill.aliases})")

    def unregister(self, name: str) -> bool:
        skill = self._skills.pop(name, None)
        if skill is None:
            return False
        for alias in skill.aliases:
            self._skills.pop(alias, None)
        logger.info(f"注销技能: {name}")
        return True

    async def execute(self, name: str, args: str = "") -> Any:
        skill = self._skills.get(name)
        if skill is None:
            logger.error(f"未知技能: {name}")
            return {"error": f"未知技能: {name}"}
        try:
            result = await skill.handler(args)
            logger.info(f"技能执行成功: {name}")
            return result
        except Exception as e:
            logger.error(f"技能执行失败 {name}: {e}")
            return {"error": str(e)}

    def list_skills(self, category: str = "") -> List[Skill]:
        seen = set()
        skills = []
        for skill in self._skills.values():
            if skill.name in seen:
                continue
            seen.add(skill.name)
            if category and skill.category != category:
                continue
            skills.append(skill)
        return sorted(skills, key=lambda s: s.name)

    def search(self, query: str) -> List[Skill]:
        query = query.lower()
        results = []
        seen = set()
        for skill in self._skills.values():
            if skill.name in seen:
                continue
            if (query in skill.name.lower()
                    or query in skill.description.lower()
                    or any(query in a.lower() for a in skill.aliases)):
                results.append(skill)
                seen.add(skill.name)
        return results

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def clear(self) -> None:
        self._skills.clear()
