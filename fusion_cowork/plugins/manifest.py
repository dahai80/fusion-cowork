from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PluginManifest:
    name: str
    version: str
    description: str = ""
    author: str = ""
    nodes: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    entry_point: str = "plugin"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "nodes": self.nodes,
            "dependencies": self.dependencies,
            "entry_point": self.entry_point,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PluginManifest:
        return cls(
            name=data.get("name", ""),
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            author=data.get("author", ""),
            nodes=data.get("nodes", []),
            dependencies=data.get("dependencies", []),
            entry_point=data.get("entry_point", "plugin"),
        )

    @classmethod
    def from_json(cls, path: Path) -> Optional[PluginManifest]:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            manifest = cls.from_dict(data)
            logger.info(f"加载插件清单: {manifest.name} v{manifest.version}")
            return manifest
        except json.JSONDecodeError as e:
            logger.error(f"插件清单 JSON 解析失败 {path}: {e}")
            return None
        except Exception as e:
            logger.error(f"加载插件清单失败 {path}: {e}")
            return None
