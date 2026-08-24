"""权限模型 — 工具调用审批与分级权限。

支持:
- 5 级权限: CONFIRM(默认, 规则/高风险驱动) / MANUAL / AUTO / PLAN / BYPASS(全放行)
- 规则匹配: tool_name + scope (如 file:~/Desktop/**)
- 高风险节点无显式批准 → 拒绝 (CR-16/17/18)
- 权限规则持久化到 JSON 配置文件

CR-16: check() 顺序 — approve 规则命中→allow (任意 level); deny→deny;
       高风险→deny (需显式 approve 或 Hook 放行); 否则→allow。
       approve() 全 level 生效 (移除旧 MANUAL no-op)。
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
        # 代码执行
        "shell_exec",
        "python_repl",
        "apply_edit",
        # 文件破坏性操作
        "file_delete",
        "file_copy",
        "file_move",
        "disk_cleaner",
        "desktop_clean",
        "download_organizer",
        # 系统交互
        "app_lifecycle",
        "screen_capture",
        "clipboard",
        "notification",
        # 输入注入
        "mouse_click",
        "mouse_move",
        "keyboard_type",
        "keyboard_shortcut",
        "computer_use_loop",
        # 浏览器自动化 / 任意 JS
        "browser_automate",
        "cdp_evaluate",
        "cdp_navigate",
        "cdp_screenshot",
        "cdp_click",
        "cdp_fill",
        "cdp_fill_form",
        "cdp_emulate",
        "cdp_network",
    }
)


class PermissionLevel(Enum):
    CONFIRM = "confirm"
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

    def __init__(self, level: PermissionLevel = PermissionLevel.CONFIRM, hook_manager=None):
        self.level = level
        self.rules: List[Permission] = []
        self._pending_approvals: Dict[str, Permission] = {}
        self._hook_manager = hook_manager

    async def check(self, tool_name: str, action: str = "", params: Dict[str, Any] = None) -> bool:
        if self.level == PermissionLevel.BYPASS:
            return True

        # Hook: PERMISSION_REQUEST — 带外确认入口 (CR-2/3)
        if self._hook_manager:
            from .hooks import HookEvent

            ctx = await self._hook_manager.fire(
                HookEvent.PERMISSION_REQUEST,
                {
                    "tool_name": tool_name,
                    "action": action,
                    "params": params or {},
                    "high_risk": tool_name in HIGH_RISK_NODES,
                },
            )
            if ctx and ctx.cancelled:
                logger.info(f"权限被 Hook 拒绝: {tool_name}")
                return False
            if ctx and ctx.modified_data.get("approved"):
                logger.info(f"权限被 Hook 批准: {tool_name}")
                return True

        # CR-16: 规则优先 — approve 命中→allow (任意 level); deny 命中→deny
        for rule in self.rules:
            if rule.matches(tool_name, params):
                if rule.allowed:
                    return True
                logger.warning(f"权限拒绝: {tool_name} (scope={rule.scope})")
                return False

        # 高风险节点无显式批准 → 拒绝 (需 approve 规则或 Hook 放行)
        if tool_name in HIGH_RISK_NODES:
            logger.info(f"高风险节点无显式批准, 拒绝: {tool_name}")
            return False

        # 非高风险且无匹配规则 → 放行 (CONFIRM/MANUAL 亦放行低风险)
        return True

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
