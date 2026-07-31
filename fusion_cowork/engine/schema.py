"""输出 Schema 校验 — 对标 --json-schema。

工作流/节点输出可按 JSON Schema 校验，
不符合 schema 时标记为 FAILED。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class OutputSchema:
    @staticmethod
    def validate(data: Any, schema: Dict[str, Any]) -> bool:
        try:
            import jsonschema
            jsonschema.validate(instance=data, schema=schema)
            return True
        except ImportError:
            logger.debug("jsonschema 未安装，使用内置校验")
            return OutputSchema._builtin_validate(data, schema)
        except Exception as e:
            logger.debug(f"Schema validation failed: {e}")
            return False

    @staticmethod
    def validate_detailed(data: Any, schema: Dict[str, Any]) -> List[str]:
        errors = []
        try:
            import jsonschema
            validator = jsonschema.Draft7Validator(schema)
            for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
                errors.append(f"{'.'.join(str(p) for p in err.absolute_path)}: {err.message}")
        except ImportError:
            if not OutputSchema._builtin_validate(data, schema):
                errors.append("Schema validation failed (jsonschema not installed, using basic check)")
        return errors

    @staticmethod
    def _builtin_validate(data: Any, schema: Dict[str, Any]) -> bool:
        schema_type = schema.get("type")
        if not schema_type:
            return True

        type_checks = {
            "object": lambda d: isinstance(d, dict),
            "array": lambda d: isinstance(d, list),
            "string": lambda d: isinstance(d, str),
            "integer": lambda d: isinstance(d, int) and not isinstance(d, bool),
            "number": lambda d: isinstance(d, (int, float)) and not isinstance(d, bool),
            "boolean": lambda d: isinstance(d, bool),
            "null": lambda d: d is None,
        }

        if schema_type in type_checks:
            if not type_checks[schema_type](data):
                return False

        if schema_type == "object" and isinstance(data, dict):
            required = schema.get("required", [])
            for field in required:
                if field not in data:
                    return False

            properties = schema.get("properties", {})
            for key, prop_schema in properties.items():
                if key in data:
                    if not OutputSchema._builtin_validate(data[key], prop_schema):
                        return False

        if schema_type == "array" and isinstance(data, list):
            items_schema = schema.get("items")
            if items_schema:
                for item in data:
                    if not OutputSchema._builtin_validate(item, items_schema):
                        return False

        return True
