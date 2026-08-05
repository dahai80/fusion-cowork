"""权限模型 — 工具调用审批与分级权限。

支持:
- 4 级权限: MANUAL(每次确认) / AUTO(自动放行已批准) / PLAN(规划放行) / BYPASS(全放行)
- 规则匹配: tool_name + scope (如 file:~/Desktop/**)
- 高风险节点默认需 MANUAL 确认
- 权限规则持久化到 JSON 配置文件
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_PERMISSIONS_DIR = os.path.expanduser("~/.fusion-cowork")
_PERMISSIONS_FILE = os.path.join(_PERMISSIONS_DIR, "permissions.json")

HIGH_RISK_NODES = frozenset(
    {
        "shell_exec",
        "python_repl",
        "file_delete",
        "apply_edit",
        "browser_automate",
    }
)


class PermissionLevel(Enum):
    MANUAL = "manual"
    AUTO = "auto"
    PLAN = "plan"
    BYPASS = "bypass"


@dataclass
class Permission:
    tool_name: str
    allowed: bool = True
    scope: str = "*"

    def matches(self, tool_name: str, params: Dict[str, Any] = None) -> bool:
        if self.tool_name != "*" and self.tool_name != tool_name:
            return False
        if self.scope == "*":
            return True
        if not params:
            return True
        if self.scope.startswith("file:"):
            prefix = self.scope[5:].replace("**", "")
            for key in ("path", "output_path", "save_path", "source_path"):
                val = params.get(key, "")
                if isinstance(val, str) and val.startswith(prefix):
                    return True
            return False
        if self.scope.startswith("command:"):
            prefix = self.scope[8:].replace("*", "")
            cmd = params.get("command", "")
            if isinstance(cmd, str) and cmd.startswith(prefix):
                return True
            return False
        return True


class PermissionManager:
    """权限管理器 — 检查工具调用是否被允许。"""

    def __init__(self, level: PermissionLevel = PermissionLevel.MANUAL, hook_manager=None):
        self.level = level
        self.rules: List[Permission] = []
        self._pending_approvals: Dict[str, Permission] = {}
        self._hook_manager = hook_manager

    async def check(self, tool_name: str, action: str = "", params: Dict[str, Any] = None) -> bool:
        if self.level == PermissionLevel.BYPASS:
            return True

        # Hook: PERMISSION_REQUEST
        if self._hook_manager:
            from .hooks import HookEvent

            ctx = await self._hook_manager.fire(
                HookEvent.PERMISSION_REQUEST,
                {
                    "tool_name": tool_name,
                    "action": action,
                    "params": params or {},
                },
            )
            if ctx and ctx.cancelled:
                logger.info(f"权限被 Hook 拒绝: {tool_name}")
                return False
            if ctx and ctx.modified_data.get("approved"):
                logger.info(f"权限被 Hook 批准: {tool_name}")
                return True

        for rule in self.rules:
            if rule.matches(tool_name, params):
                if self.level == PermissionLevel.AUTO and rule.allowed:
                    return True
                if not rule.allowed:
                    logger.warning(f"权限拒绝: {tool_name} (scope={rule.scope})")
                    return False

        is_high_risk = tool_name in HIGH_RISK_NODES
        if is_high_risk:
            if self.level == PermissionLevel.MANUAL:
                logger.info(f"高风险节点需确认: {tool_name}")
                return False
            if self.level == PermissionLevel.AUTO:
                return False
            if self.level == PermissionLevel.PLAN:
                return True

        if self.level == PermissionLevel.MANUAL:
            return False
        if self.level == PermissionLevel.AUTO:
            return True
        if self.level == PermissionLevel.PLAN:
            return True
        return False

    def approve(self, tool_name: str, scope: str = "*") -> None:
        rule = Permission(tool_name=tool_name, allowed=True, scope=scope)
        self.rules.append(rule)
        logger.info(f"权限批准: {tool_name} (scope={scope})")

    def deny(self, tool_name: str, scope: str = "*") -> None:
        rule = Permission(tool_name=tool_name, allowed=False, scope=scope)
        self.rules.append(rule)
        logger.info(f"权限拒绝: {tool_name} (scope={scope})")

    def request_approval(self, tool_name: str, params: Dict[str, Any] = None) -> str:
        import uuid

        req_id = f"perm_{uuid.uuid4().hex[:8]}"
        self._pending_approvals[req_id] = Permission(tool_name=tool_name, allowed=False, scope="*")
        logger.info(f"权限请求: {req_id} → {tool_name}")
        return req_id

    def grant_approval(self, req_id: str, scope: str = "*") -> bool:
        perm = self._pending_approvals.pop(req_id, None)
        if not perm:
            return False
        self.approve(perm.tool_name, scope)
        return True

    def save(self, path: str = "") -> None:
        target = path or _PERMISSIONS_FILE
        os.makedirs(os.path.dirname(target), exist_ok=True)
        data = {
            "level": self.level.value,
            "rules": [{"tool_name": r.tool_name, "allowed": r.allowed, "scope": r.scope} for r in self.rules],
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"权限规则已保存: {target}")

    def load(self, path: str = "") -> None:
        target = path or _PERMISSIONS_FILE
        if not os.path.exists(target):
            return
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        self.level = PermissionLevel(data.get("level", "manual"))
        self.rules = [
            Permission(tool_name=r["tool_name"], allowed=r["allowed"], scope=r.get("scope", "*"))
            for r in data.get("rules", [])
        ]
        logger.info(f"权限规则已加载: {len(self.rules)} 条 (level={self.level.value})")

    def reset(self, tool_name: str = "") -> int:
        if tool_name:
            before = len(self.rules)
            self.rules = [r for r in self.rules if r.tool_name != tool_name]
            removed = before - len(self.rules)
        else:
            removed = len(self.rules)
            self.rules.clear()
        self._pending_approvals.clear()
        logger.info(f"PermissionManager.reset removed={removed} tool_name={tool_name or '*'}")
        return removed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "rules": [{"tool_name": r.tool_name, "allowed": r.allowed, "scope": r.scope} for r in self.rules],
        }
