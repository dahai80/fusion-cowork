from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

BUILTIN_PACKAGES_DIR = Path(__file__).parent / "packages"
USER_PACKAGES_DIR = Path.home() / ".fusion-cowork" / "skills"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


@dataclass
class ParamSpec:
    name: str
    label: str = ""
    label_en: str = ""
    type: str = "string"
    required: bool = False
    default: Any = None
    options: List[str] = field(default_factory=list)
    placeholder: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "label_en": self.label_en or self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "options": self.options,
            "placeholder": self.placeholder,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ParamSpec:
        return cls(
            name=str(data.get("name", "")),
            label=str(data.get("label", "")),
            label_en=str(data.get("label_en", "")),
            type=str(data.get("type", "string")),
            required=bool(data.get("required", False)),
            default=data.get("default"),
            options=[str(o) for o in (data.get("options") or [])],
            placeholder=str(data.get("placeholder", "")),
        )


@dataclass
class SkillPackage:
    name: str
    title: str = ""
    title_en: str = ""
    description: str = ""
    description_en: str = ""
    version: str = ""
    category: str = "custom"
    aliases: List[str] = field(default_factory=list)
    command: str = ""
    allowed_tools: List[str] = field(default_factory=list)
    params: List[ParamSpec] = field(default_factory=list)
    instructions: str = ""
    output_template: str = ""
    handler: str = ""
    argument_template: str = ""
    workflow: Dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "title_en": self.title_en or self.title,
            "description": self.description,
            "description_en": self.description_en,
            "version": self.version,
            "category": self.category,
            "aliases": self.aliases,
            "command": self.command or self.name,
            "allowed_tools": self.allowed_tools,
            "params": [p.to_dict() for p in self.params],
            "instructions": self.instructions,
            "output_template": self.output_template,
            "handler": self.handler,
            "workflow": self.workflow,
            "source": self.source,
        }

    def coerce(self, values: Optional[Dict[str, Any]]) -> tuple[Dict[str, Any], List[str]]:
        values = dict(values or {})
        clean: Dict[str, Any] = {}
        errors: List[str] = []
        for spec in self.params:
            raw = values.get(spec.name, spec.default)
            if raw in (None, "") and spec.required:
                errors.append(f"Missing required parameter: {spec.name}")
                continue
            if raw in (None, ""):
                clean[spec.name] = "" if spec.type in ("string", "file", "dir", "text") else spec.default
                continue
            try:
                value = _coerce_value(spec.type, raw)
            except (TypeError, ValueError):
                errors.append(f"Parameter type error: {spec.name} should be {spec.type}")
                continue
            # issue #167: enum (options) validation - aligned with agent-studio validate_skill_params
            if spec.options and str(value) not in [str(o) for o in spec.options]:
                errors.append(f"Invalid parameter value: {spec.name} should be one of {', '.join(spec.options)}")
                continue
            clean[spec.name] = value
        return clean, errors


def _coerce_value(kind: str, raw: Any) -> Any:
    if kind in ("string", "file", "dir", "text", "select"):
        return str(raw)
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"Invalid boolean value: {raw}")
    if kind == "list":
        if isinstance(raw, list):
            return raw
        return [part.strip() for part in str(raw).split(",") if part.strip()]
    return raw


def _first(meta: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in meta and meta[key] is not None:
            return meta[key]
    return None


def _as_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


def _params_from_schema(schema: Any) -> List[ParamSpec]:
    # issue #167: shared-format parameters (JSON Schema: properties/required) -> ParamSpec.
    # Same schema as fusion-agent-studio skills_store; both sides render structured forms from the same source.
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = _as_list(schema.get("required"))
    type_map = {"string": "string", "integer": "int", "number": "float", "boolean": "bool", "array": "list"}
    out: List[ParamSpec] = []
    for key, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        kind = type_map.get(str(spec.get("type", "string")), "string")
        out.append(
            ParamSpec(
                name=str(key),
                label=str(spec.get("title", "") or key),
                label_en=str(spec.get("title_en", "") or spec.get("title", "") or key),
                type=kind,
                required=bool(spec.get("required", False)) or key in required,
                default=spec.get("default"),
                options=[str(e) for e in (spec.get("enum") or [])],
                placeholder=str(spec.get("description", "") or "")[:120],
            )
        )
    return out


def parse_markdown(text: str, source: str = "") -> SkillPackage:
    if not text.startswith("---"):
        raise ValueError("markdown skill package missing frontmatter (must start with ---)")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("markdown skill package frontmatter not closed")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise ValueError("markdown skill package frontmatter must be a mapping")
    name = str(_first(meta, "name", "command", "slash") or "").strip()
    if not name:
        raise ValueError("markdown skill package missing name")
    name = name if name.startswith("/") else f"/{name}"
    raw_params = _first(meta, "params")
    if isinstance(raw_params, list) and raw_params:
        params = [ParamSpec.from_dict(p) for p in raw_params]
    else:
        params = _params_from_schema(_first(meta, "parameters", "arguments"))
    command = str(_first(meta, "command", "slash") or "").strip()
    if command and not command.startswith("/"):
        command = f"/{command}"
    return SkillPackage(
        name=name,
        title=str(_first(meta, "title", "title-en") or ""),
        title_en=str(_first(meta, "title_en", "title-en") or ""),
        description=str(_first(meta, "description") or ""),
        description_en=str(_first(meta, "description_en", "description-en") or ""),
        version=str(_first(meta, "version") or ""),
        category=str(_first(meta, "category") or "custom"),
        aliases=_as_list(meta.get("aliases")),
        command=command or name,
        allowed_tools=_as_list(_first(meta, "allowed-tools", "allowed_tools", "tools")),
        params=params,
        instructions=parts[2].strip(),
        output_template=str(_first(meta, "output_template", "output-template") or ""),
        handler=str(_first(meta, "handler") or ""),
        argument_template=str(_first(meta, "argument_template", "argument-template") or ""),
        workflow=dict(meta.get("workflow") or {}),
        source=source,
    )


def load_package(path: Path) -> SkillPackage:
    return parse_markdown(path.read_text(encoding="utf-8"), source=str(path))


def _substitute(value: Any, params: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, params) for k, v in value.items()}
    return value


class SkillPackageRegistry:
    def __init__(self) -> None:
        self._packages: Dict[str, SkillPackage] = {}

    def register(self, pkg: SkillPackage) -> None:
        self._packages[pkg.name] = pkg
        for alias in pkg.aliases:
            self._packages[alias if alias.startswith("/") else f"/{alias}"] = pkg
        if pkg.command and pkg.command != pkg.name:
            self._packages[pkg.command] = pkg
        logger.info(f"Registered markdown skill package: {pkg.name} (source={pkg.source or '-'})")

    def get(self, name: str) -> Optional[SkillPackage]:
        key = name if name.startswith("/") else f"/{name}"
        return self._packages.get(key)

    def list_packages(self, category: str = "") -> List[SkillPackage]:
        seen = set()
        out = []
        for pkg in self._packages.values():
            if pkg.name in seen:
                continue
            seen.add(pkg.name)
            if category and pkg.category != category:
                continue
            out.append(pkg)
        return sorted(out, key=lambda p: p.name)

    def discover(self, root: Path) -> int:
        root = Path(root).expanduser()
        if not root.is_dir():
            return 0
        count = 0
        for path in sorted(root.rglob("*.md")):
            try:
                self.register(load_package(path))
                count += 1
            except (OSError, ValueError, yaml.YAMLError) as e:
                logger.warning(f"Skipping invalid markdown skill package {path}: {e}")
        return count

    def clear(self) -> None:
        self._packages.clear()

    async def run(self, name: str, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        pkg = self.get(name)
        if pkg is None:
            return {"error": f"Unknown skill package: {name}"}
        clean, errors = pkg.coerce(values)
        if errors:
            return {"error": "Parameter validation failed", "errors": errors}
        try:
            return await _dispatch(pkg, clean)
        except Exception as e:
            logger.error(f"Skill package execution failed {pkg.name}: {e}")
            return {"error": str(e)}


async def _dispatch(pkg: SkillPackage, params: Dict[str, Any]) -> Dict[str, Any]:
    if pkg.handler.startswith("builtin:"):
        from .builtin import register_builtin_skills
        from .registry import SkillRegistry

        target = pkg.handler.split(":", 1)[1]
        registry = SkillRegistry()
        register_builtin_skills(registry)
        args = pkg.argument_template.format(**params) if pkg.argument_template else ""
        logger.info(f"Skill package {pkg.name} delegating to builtin skill {target} args={args!r}")
        return await registry.execute(target, args)
    if pkg.workflow:
        return await _run_workflow(pkg, params)
    from .handlers import get_handler

    handler = get_handler(pkg.handler)
    if handler is None:
        return {"error": f"Skill package has no executable handler declared: {pkg.name}"}
    logger.info(f"Skill package {pkg.name} executing handler {pkg.handler}")
    return await handler(params, pkg)


async def _run_workflow(pkg: SkillPackage, params: Dict[str, Any]) -> Dict[str, Any]:
    from ..engine.workflow import Workflow, WorkflowEngine

    definition = _substitute(pkg.workflow, params)
    workflow = Workflow.from_dict(definition)
    engine = WorkflowEngine()
    result = await engine.execute(workflow)
    return {
        "status": result.status.value if hasattr(result.status, "value") else str(result.status),
        "summary": f"Skill package {pkg.name} workflow completed",
        "steps": [s.node_id for s in getattr(result, "steps", [])],
    }


_registry: Optional[SkillPackageRegistry] = None


def get_package_registry(reload: bool = False) -> SkillPackageRegistry:
    global _registry
    if _registry is not None and not reload:
        return _registry
    registry = SkillPackageRegistry()
    registry.discover(BUILTIN_PACKAGES_DIR)
    registry.discover(USER_PACKAGES_DIR)
    # issue #167: shared install directories (colon-separated) - one package install, shared by cowork / agent-studio
    for extra in os.environ.get("FUSION_COWORK_SKILLS_DIRS", "").split(":"):
        if extra.strip():
            registry.discover(Path(extra.strip()))
    _registry = registry
    logger.info(f"Markdown skill packages ready: {len(registry.list_packages())} loaded")
    return registry


def reset_package_registry() -> None:
    global _registry
    _registry = None
