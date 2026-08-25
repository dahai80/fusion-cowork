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
import threading
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
        "cdp_wait_for",  # E-10: wait_for_function 任意 JS 注入, 同 cdp_evaluate 高危
        "fetch_url",  # E-11: SSRF 面, 默认拒, 须显式放行
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
            # E-3: scope 前缀含 ~/ 须 expanduser — 路径已展开为绝对 (/Users/x/...),
            # 否则 startswith("~/Desktop/") 恒 False, 规则静默失效。
            # params 值若仍带 ~/ 也一并展开比较 (两侧都对齐绝对路径)。
            prefix = os.path.expanduser(self.scope[5:].replace("**", ""))
            for key in ("path", "output_path", "save_path", "source_path"):
                val = params.get(key, "")
                if isinstance(val, str):
                    val_exp = os.path.expanduser(val)
                    if val_exp == prefix.rstrip("/") or val_exp.startswith(prefix):
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
        # R-3: rules/_pending_approvals 并发保护 — check() 遍历与 approve/deny/reset 改写竞态。
        self._lock = threading.Lock()

    async def check(self, tool_name: str, action: str = "", params: Dict[str, Any] = None) -> bool:
        # A-6: deny 规则须先于 Hook 批准判定 — 否则 Hook approve 可覆盖显式 deny (绕过)。
        # 先扫 deny: 命中 deny → 即拒, Hook 批准也不再翻盘。
        denied = False
        ctx = None
        with self._lock:
            rules_snapshot = list(self.rules)
        for rule in rules_snapshot:
            if rule.matches(tool_name, params) and not rule.allowed:
                logger.warning(f"权限拒绝 (deny 规则优先): {tool_name} (scope={rule.scope})")
                denied = True
                break

        # Hook: PERMISSION_REQUEST — 带外确认入口 (CR-2/3)。A-6: BYPASS 亦不跳过审计 Hook
        # (仍 fire 以记录), 仅最终判定照旧全放行。
        if self._hook_manager:
            from .hooks import HookEvent

            ctx = await self._hook_manager.fire(
                HookEvent.PERMISSION_REQUEST,
                {
                    "tool_name": tool_name,
                    "action": action,
                    "params": params or {},
                    "high_risk": tool_name in HIGH_RISK_NODES,
                    "denied_by_rule": denied,
                },
            )
            if ctx and ctx.cancelled:
                logger.info(f"权限被 Hook 拒绝: {tool_name}")
                return False

        if self.level == PermissionLevel.BYPASS:
            return True

        # A-6: deny 规则命中 → 即拒, Hook approve 不可翻盘
        if denied:
            return False
        if ctx and ctx.modified_data.get("approved"):
            logger.info(f"权限被 Hook 批准: {tool_name}")
            return True

        # CR-16: approve 规则命中→allow (任意 level); deny 已在上一步处理
        for rule in rules_snapshot:
            if rule.matches(tool_name, params) and rule.allowed:
                return True

        # 高风险节点无显式批准 → 拒绝 (需 approve 规则或 Hook 放行)
        if tool_name in HIGH_RISK_NODES:
            logger.info(f"高风险节点无显式批准, 拒绝: {tool_name}")
            return False

        # 非高风险且无匹配规则 → 放行 (CONFIRM/MANUAL 亦放行低风险)
        return True

    def approve(self, tool_name: str, scope: str = "*") -> None:
        rule = Permission(tool_name=tool_name, allowed=True, scope=scope)
        with self._lock:
            self.rules.append(rule)
        logger.info(f"权限批准: {tool_name} (scope={scope})")

    def deny(self, tool_name: str, scope: str = "*") -> None:
        rule = Permission(tool_name=tool_name, allowed=False, scope=scope)
        with self._lock:
            self.rules.append(rule)
        logger.info(f"权限拒绝: {tool_name} (scope={scope})")

    def request_approval(self, tool_name: str, params: Dict[str, Any] = None) -> str:
        import uuid

        req_id = f"perm_{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._pending_approvals[req_id] = Permission(tool_name=tool_name, allowed=False, scope="*")
        logger.info(f"权限请求: {req_id} → {tool_name}")
        return req_id

    def grant_approval(self, req_id: str, scope: str = "*") -> bool:
        with self._lock:
            perm = self._pending_approvals.pop(req_id, None)
        if not perm:
            return False
        self.approve(perm.tool_name, scope)
        return True

    def save(self, path: str = "") -> None:
        target = path or _PERMISSIONS_FILE
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with self._lock:
            rules_snapshot = list(self.rules)
        data = {
            "level": self.level.value,
            "rules": [{"tool_name": r.tool_name, "allowed": r.allowed, "scope": r.scope} for r in rules_snapshot],
        }
        # E-4: 原子写 — 写 tmp 再 os.replace, 杜绝半写/并发读到残缺 JSON 崩溃
        tmp_path = f"{target}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, target)
        logger.info(f"权限规则已保存: {target}")

    def load(self, path: str = "") -> None:
        target = path or _PERMISSIONS_FILE
        if not os.path.exists(target):
            return
        # E-4: load 容错 — 损坏 JSON 不崩, 退化默认空规则 + 日志
        try:
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"权限规则文件损坏, 丢弃旧规则: {target} ({e})")
            with self._lock:
                self.rules = []
            return
        try:
            self.level = PermissionLevel(data.get("level", "confirm"))
        except ValueError:
            logger.warning(f"权限 level 值非法, 退化 CONFIRM: {data.get('level')}")
            self.level = PermissionLevel.CONFIRM
        new_rules = [
            Permission(tool_name=r["tool_name"], allowed=r["allowed"], scope=r.get("scope", "*"))
            for r in data.get("rules", [])
        ]
        with self._lock:
            self.rules = new_rules
        logger.info(f"权限规则已加载: {len(new_rules)} 条 (level={self.level.value})")

    def reset(self, tool_name: str = "") -> int:
        with self._lock:
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
        with self._lock:
            rules_snapshot = list(self.rules)
        return {
            "level": self.level.value,
            "rules": [{"tool_name": r.tool_name, "allowed": r.allowed, "scope": r.scope} for r in rules_snapshot],
        }
