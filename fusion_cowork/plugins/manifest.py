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
    # LO-7: 插件声明节点执行超时 (秒); 0 = 用沙箱默认 (120s), >0 覆盖默认
    timeout_seconds: float = 0.0
    # Stage 7: Ed25519 签名 (base64 urlsafe), 空 = 未签名; require_signing 时拒
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
        # E-13: name 空或含路径分隔符 → target=plugins_dir 或目录穿越, rmtree 删插件根。
        if not name or not name.strip():
            logger.error("插件清单 name 为空, 拒绝 (防 target=plugins_dir 越界删除)")
            raise ValueError("插件清单 name 不可为空")
        if "/" in name or "\\" in name or name in (".", ".."):
            logger.error(f"插件清单 name 含路径分隔符/遍历符: {name!r}, 拒绝")
            raise ValueError(f"插件清单 name 非法: {name!r}")
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
            logger.info(f"加载插件清单: {manifest.name} v{manifest.version}")
            return manifest
        except json.JSONDecodeError as e:
            logger.error(f"插件清单 JSON 解析失败 {path}: {e}")
            return None
        except Exception as e:
            logger.error(f"加载插件清单失败 {path}: {e}")
            return None
