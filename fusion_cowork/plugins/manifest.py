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
    sandbox: bool = False
    # LO-7: plugin-declared node execution timeout (seconds); 0 = sandbox default (120s), >0 overrides
    timeout_seconds: float = 0.0
    # Stage 7: Ed25519 signature (base64 urlsafe), empty = unsigned; refused when require_signing
    signature: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "nodes": self.nodes,
            "dependencies": self.dependencies,
            "entry_point": self.entry_point,
            "sandbox": self.sandbox,
            "timeout_seconds": self.timeout_seconds,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PluginManifest:
        try:
            timeout_seconds = float(data.get("timeout_seconds", 0.0))
        except (TypeError, ValueError):
            timeout_seconds = 0.0
        if timeout_seconds < 0:
            timeout_seconds = 0.0
        name = data.get("name", "")
        # E-13: empty name or path separators -> target=plugins_dir or path traversal, rmtree deletes plugin root.
        if not name or not name.strip():
            logger.error("plugin manifest name is empty, refusing (prevents out-of-scope delete via target=plugins_dir)")
            raise ValueError("plugin manifest name must not be empty")
        if "/" in name or "\\" in name or name in (".", ".."):
            logger.error(f"plugin manifest name contains path separator/traversal chars: {name!r}, refusing")
            raise ValueError(f"invalid plugin manifest name: {name!r}")
        return cls(
            name=name,
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            author=data.get("author", ""),
            nodes=data.get("nodes", []),
            dependencies=data.get("dependencies", []),
            entry_point=data.get("entry_point", "plugin"),
            sandbox=bool(data.get("sandbox", False)),
            timeout_seconds=timeout_seconds,
            signature=str(data.get("signature", "")),
        )

    @classmethod
    def from_json(cls, path: Path) -> Optional[PluginManifest]:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            manifest = cls.from_dict(data)
            logger.info(f"loading plugin manifest: {manifest.name} v{manifest.version}")
            return manifest
        except json.JSONDecodeError as e:
            logger.error(f"plugin manifest JSON parse failed {path}: {e}")
            return None
        except Exception as e:
            logger.error(f"failed to load plugin manifest {path}: {e}")
            return None
