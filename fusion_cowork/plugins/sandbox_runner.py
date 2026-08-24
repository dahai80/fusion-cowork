from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import traceback
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("sandbox_runner")

_RESULT_MARKER = "__SANDBOX_RESULT__"


def _serialize(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _load_module(entry_file: str):
    spec = importlib.util.spec_from_file_location("__sandbox_plugin__", entry_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载插件模块: {entry_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["__sandbox_plugin__"] = module
    spec.loader.exec_module(module)
    sys.path.insert(0, str(Path(entry_file).resolve().parent))
    return module


def _introspect(entry_file: str) -> Dict[str, Any]:
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeConfig

    module = _load_module(entry_file)
    nodes = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name, None)
        if attr is None:
            continue
        try:
            if not (isinstance(attr, type) and issubclass(attr, BaseNode) and attr is not BaseNode):
                continue
        except TypeError:
            continue
        try:
            instance = attr(node_id="_introspect", config=NodeConfig())
            schema = instance.get_params_schema()
        except Exception:
            schema = {"type": "object", "properties": {}, "required": []}
        cat = getattr(attr, "category", NodeCategory.TOOL)
        nodes.append(
            {
                "class_name": attr.__name__,
                "name": getattr(attr, "name", attr.__name__),
                "display_name": getattr(attr, "display_name", attr.__name__),
                "category": cat.value if hasattr(cat, "value") else str(cat),
                "description": getattr(attr, "description", ""),
                "icon": getattr(attr, "icon", "📦"),
                "default_label": getattr(attr, "default_label", "插件节点"),
                "params_schema": schema,
            }
        )
    return {"nodes": nodes}


async def _run_node(entry_file: str, class_name: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    from fusion_cowork.engine.node import BaseNode, NodeConfig, NodeResult

    module = _load_module(entry_file)
    node_cls = getattr(module, class_name, None)
    if node_cls is None or not (isinstance(node_cls, type) and issubclass(node_cls, BaseNode)):
        raise RuntimeError(f"节点类不存在或非 BaseNode 子类: {class_name}")

    node = node_cls(node_id=f"sandbox_{class_name}", config=NodeConfig())
    result = await node.execute(inputs or {})
    if not isinstance(result, NodeResult):
        raise RuntimeError(f"节点 execute 未返回 NodeResult: {type(result)}")
    return _serialize(result)


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(_RESULT_MARKER + json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.flush()


async def main() -> int:
    raw = sys.stdin.read()
    if not raw:
        _emit({"ok": False, "error": "空输入"})
        return 1
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as e:
        _emit({"ok": False, "error": f"输入 JSON 解析失败: {e}"})
        return 1

    action = req.get("action", "introspect")
    entry_file = req.get("entry_file", "")
    try:
        if action == "introspect":
            _emit({"ok": True, "data": _introspect(entry_file)})
            return 0
        elif action == "run":
            data = await _run_node(entry_file, req.get("class_name", ""), req.get("inputs", {}))
            _emit({"ok": True, "data": data})
            return 0
        else:
            _emit({"ok": False, "error": f"未知 action: {action}"})
            return 1
    except Exception as e:
        logger.error(f"sandbox_runner 执行失败: {e}\n{traceback.format_exc()}")
        _emit({"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()})
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
