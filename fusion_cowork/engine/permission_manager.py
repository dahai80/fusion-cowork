"""Permission model — tool call approval and tiered permissions.

Supports:
- 5 permission levels: CONFIRM (default, rule/high-risk driven) / MANUAL / AUTO / PLAN / BYPASS (allow all)
- Rule matching: tool_name + scope (e.g. file:~/Desktop/**) — matching semantics delegated to kernel
  fusion_harness.scoped_permissions (issue #493, K1 migration)
- High-risk node without explicit approval → deny (CR-16/17/18)
- Permission rules persisted to a JSON config file

CR-16: check() ordering — approve rule hit → allow (any level); deny → deny;
       high-risk → deny (needs explicit approve or Hook pass-through); otherwise → allow.
       approve() takes effect at all levels (removed the old MANUAL no-op).

#493 kernel migration boundary (consumer wiring explicitly documented in the kernel
scoped_permissions docstring stays on this side, each with a parity note):
- Rule model / prefix scope matching (file:/command:/url:) → kernel ScopedPermission semantics;
  engine.check() does not delegate directly — it folds bypass/allow-rule/default-allow into a
  single "allow" return value, while the check() flow needs three-state distinction
  (trusted-sync and the high-risk gate sit between allow rules and default allow);
  forced delegation would change the CR-16 ordering
- Hook audit / HITL gate exemption (issue #340) / trusted-sync (#301) / capability gate (#228) /
  guard IPC delegation (#73) = consumer wiring, stays on this side (kernel: guard clients and
  capability gates stay consumer-side)
- Pending approval queue keeps the local rich-context implementation (approval card diff
  blocks / execution_id wakeup / crash recovery); kernel PendingApproval(id/action/resource/reason)
  has no card context and resolve() auto-persists rules, conflicting with the #340 gate's
  no-rule semantics — parity difference, deliberately not migrated

#521 drop-fallback ruling (2026-11-18): this is the documented deliberate
divergence (K1 keep), NOT an in-tree fallback copy. fusion-harness is a
mandatory install (unified venv, [plugins] extra) — missing harness is a
startup error via the shims (permission.py), never a degraded mode. Kernel
permission types (PermissionEngine/PermissionRule/PermissionDecision/
PermissionMode) and rule scope-matching semantics come in through
permission.py; the CR-16 three-state check() orchestration and the
rich-context approval queue stay here by design.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# issue #340: HITL gate tools — the gate decision is a workflow decision (feeding the
# on_reject=revise revision loop), not a tool-level permission grant/deny. Deny rules must
# not pre-deny gates (otherwise one denial permanently kills the HITL cards of all later
# gates), and confirm on a gate only dequeues/wakes the run without persisting any rule.
HITL_GATE_TOOLS = {"approval_gate"}

_PERMISSIONS_DIR = os.path.expanduser("~/.fusion-cowork")

# issue #138: engine-side approval context (workflow/node) — written by WorkflowEngine
# before check(), attached when a high-risk denial lands in the pending queue for card display.
_approval_context: ContextVar = ContextVar("fc_approval_context")


def set_approval_context(ctx: Dict[str, Any]):
    return _approval_context.set({str(k): str(v) for k, v in dict(ctx).items()})


def reset_approval_context(token) -> None:
    _approval_context.reset(token)


# issue #326: upstream artifact evidence (snapshot of producing node outputs) — written by
# WorkflowEngine before check(), the pending approval card carries an artifacts block so
# reviewers don't have to leave the approval UI to find produced materials.
_approval_evidence: ContextVar = ContextVar("fc_approval_evidence")


def set_approval_evidence(items: List[Dict[str, Any]]):
    return _approval_evidence.set(list(items))


def reset_approval_evidence(token) -> None:
    _approval_evidence.reset(token)


def _sanitize_approval_params(params: Dict[str, Any] = None) -> Dict[str, Any]:
    # Approval card params snapshot: scalars only, 200-char string cap, secret-like key names masked, 20-key cap
    if not isinstance(params, dict):
        return {}
    snapshot: Dict[str, Any] = {}
    for key, val in list(params.items())[:20]:
        low = str(key).lower()
        if any(marker in low for marker in ("key", "token", "password", "secret")):
            snapshot[str(key)] = "***"
            continue
        if isinstance(val, (str, int, float, bool)):
            text = str(val)
            snapshot[str(key)] = text[:200]
        else:
            snapshot[str(key)] = f"<{type(val).__name__}>"
    return snapshot


# issue #142: approval diff blocks for edit-type params (old_text/new_text) — original text truncation cap
_DIFF_MAX_CHARS = 20000


def _build_diff_blocks(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    # issue #142: apply_edit-like pending approvals → SequenceMatcher blocks, per-block accept/reject in the card
    if not isinstance(params, dict):
        return []
    from .ast_diff import text_diff_blocks

    for old_key, new_key in (("old_text", "new_text"), ("old_string", "new_string")):
        old_val = params.get(old_key)
        new_val = params.get(new_key)
        if isinstance(old_val, str) and isinstance(new_val, str) and (old_val or new_val):
            try:
                return text_diff_blocks(old_val[:_DIFF_MAX_CHARS], new_val[:_DIFF_MAX_CHARS])
            except Exception as e:
                logger.warning(f"Approval diff block build failed: {e}")
                return []
    return []


def _build_raw_edit(params: Dict[str, Any] = None) -> Dict[str, Any]:
    # issue #142: block-by-block review rebuild needs the untruncated old/new originals (distinct from the masked display snapshot)
    if not isinstance(params, dict):
        return {}
    for old_key, new_key in (("old_text", "new_text"), ("old_string", "new_string")):
        old_val = params.get(old_key)
        new_val = params.get(new_key)
        if isinstance(old_val, str) and isinstance(new_val, str) and (old_val or new_val):
            return {
                "old_key": old_key,
                "new_key": new_key,
                "old": old_val[:_DIFF_MAX_CHARS],
                "new": new_val[:_DIFF_MAX_CHARS],
            }
    return {}


_PERMISSIONS_FILE = os.path.join(_PERMISSIONS_DIR, "permissions.json")


def _guard_cached_epoch() -> int:
    try:
        from ..security.guard import get_cached_epoch

        return get_cached_epoch()
    except ImportError:
        return 0


HIGH_RISK_NODES = frozenset(
    {
        # Code execution
        "shell_exec",
        "python_repl",
        "apply_edit",
        "worktree",  # issue #580: worktree exec runs arbitrary commands + remove is destructive, same high risk as shell_exec
        # Destructive file operations
        "file_delete",
        "file_copy",
        "file_move",
        "disk_cleaner",
        "desktop_clean",
        "download_organizer",
        # System interaction
        "app_lifecycle",
        "screen_capture",
        "clipboard",
        "notification",
        # Input injection
        "mouse_click",
        "mouse_move",
        "keyboard_type",
        "keyboard_shortcut",
        "computer_use_loop",
        # Browser automation / arbitrary JS
        "browser_automate",
        "cdp_evaluate",
        "cdp_navigate",
        "cdp_screenshot",
        "cdp_click",
        "cdp_fill",
        "cdp_fill_form",
        "cdp_emulate",
        "cdp_network",
        "cdp_wait_for",  # E-10: wait_for_function injects arbitrary JS, same high risk as cdp_evaluate
        "managed_browser",  # issue #153: launches the local browser + imports login cookies, needs explicit approval
        "fusion_browser",  # issue #162: managed fusion-browser engine + reads local browser login state
        "fetch_url",  # E-11: SSRF surface, deny by default, needs explicit approval
        # External runtime trigger (issue #133): studio_run submits an agent-studio graph/agent
        # execution, triggering remote multi-step agent behavior — needs explicit approval like fetch_url
        "studio_run",
        # issue #185: mcp_client calls arbitrary tools on external MCP servers (remote execution surface),
        # needs explicit approval; url: prefix scope supports precise authorization of a single server
        "mcp_client",
        # issue #206: http_request arbitrary target request surface (can carry auth headers / template-injected request body),
        # needs explicit approval; url: prefix scope supported like mcp_client
        "http_request",
        # issue #262: approval_gate human decision gate — inherently requires approval (pauses for a human decision),
        # pairs with the http_webhook signed callback for "external system approval → callback resume"
        "approval_gate",
        # issue #262: agent_exec external agent/CLI process execution surface (arbitrary commands), needs explicit approval
        "agent_exec",
        # issue #262: command_evidence whitelisted command execution + evidence archiving, needs explicit approval
        "command_evidence",
        # issue #308: claude_dispatch dispatches short-lived coder sessions (local process + repo write access),
        # high-risk execution surface — budget cap + crash/timeout fallback owned by the node; approval must be explicit
        "claude_dispatch",
        # issue #467: coordinator_fanout fans out sub-agent execution (multi-step agent behavior surface),
        # session_fork derives parallel sub-session branches executing arbitrary registered nodes — both need explicit approval
        "coordinator_fanout",
        "session_fork",
        # issue #470: workflow_ladder ladder orchestration — multi-stage sub-agents execute arbitrary worker_node,
        # same multi-step agent behavior surface as coordinator_fanout, needs explicit approval
        "workflow_ladder",
        # issue #488: team_call drives agent-studio team members to execute tasks — remote multi-step
        # agent behavior surface (same class as studio_run), needs explicit approval
        "team_call",
        # issue #489: deploy_service executes docker/launchd/install-script deployment and starts services —
        # local command execution + service lifecycle operations (shell-exec class), needs explicit approval
        "deploy_service",
    }
)


# issue #301: deterministic template sync targets that skip the per-node
# permission confirm on a fresh deployment. Only exact http_request URL
# prefixes are trusted — never a blanket allow for http_request itself.
# Operators override via FUSION_COWORK_TRUSTED_SYNC_URLS (comma-separated
# prefixes; "off" disables seeding entirely).
# product-level call (roadmap §6): approval sync targets the agent-studio
# approvals endpoint — the approval task store lives there (deterministic
# template-sync trust seed, never a blanket http_request allow).
DEFAULT_TRUSTED_SYNC_URLS = ("http://127.0.0.1:11442/v1/agent-platform/approvals",)


def trusted_sync_urls() -> tuple:
    raw = os.environ.get("FUSION_COWORK_TRUSTED_SYNC_URLS", "").strip()
    if not raw:
        return DEFAULT_TRUSTED_SYNC_URLS
    if raw.lower() in ("off", "none", "false", "0"):
        return ()
    return tuple(u.strip().rstrip("/") for u in raw.split(",") if u.strip())


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
        # issue #493: matching semantics delegated to kernel scoped_permissions._scope_match
        # (the matching layer of the K1 deny-first rule model).
        # Parity coverage (intentionally-different items recorded in the kernel diff table):
        # 1. No params → match any scope (upstream consistent with kernel empty-resource semantics)
        # 2. params present but missing the key (command/url) → no match (kernel treats empty resource as match, not copied)
        # 3. file: scope expanduser on both sides (E-3: kernel doesn't expand ~, upstream does — keep)
        # 4. Unknown prefix scope → True (kernel switched to resource prefix matching — upstream fallback kept)
        # Kernel fixes applied kernel-side: file:/url: path boundary checks (upstream raw startswith
        # makes file:/a wrongly match file:/ab — quirk recorded in the kernel parity table, not copied)
        if self.tool_name != "*" and self.tool_name != tool_name:
            return False
        scope = self.scope
        if scope == "*":
            return True
        if not params:
            # parity: upstream matches any scope when called without params; kernel also matches on empty resource — semantically consistent
            return True
        if not scope.startswith(("file:", "command:", "url:")):
            # parity: upstream unknown-prefix fallback = True (kernel switched to resource prefix matching, not copied)
            return True
        from fusion_harness.scoped_permissions import _scope_match

        if scope.startswith("file:"):
            # E-3: scope prefix containing ~/ needs expanduser — param values aligned to absolute paths on both sides
            expanded_scope = "file:" + os.path.expanduser(scope[5:])
            for key in ("path", "output_path", "save_path", "source_path"):
                val = params.get(key, "")
                if isinstance(val, str) and val:
                    if _scope_match(expanded_scope, os.path.expanduser(val)):
                        return True
            return False
        if scope.startswith("command:"):
            # parity: params present but no command key → no match (kernel treats empty resource as match, not copied)
            cmd = params.get("command", "")
            return isinstance(cmd, str) and bool(cmd) and _scope_match(scope, cmd)
        # url: — issue #185 prefix scope (mcp_client and other remote-call nodes)
        # parity: params present but no url key → no match (same as above)
        target = params.get("url", "")
        return isinstance(target, str) and bool(target) and _scope_match(scope, target)


class PermissionManager:
    """Permission manager — checks whether a tool call is allowed."""

    def __init__(self, level: PermissionLevel = PermissionLevel.CONFIRM, hook_manager=None):
        self.level = level
        self.rules: List[Permission] = []
        self._pending_approvals: Dict[str, Permission] = {}
        self._hook_manager = hook_manager
        # R-3: concurrency protection for rules/_pending_approvals — race between check() iteration and approve/deny/reset rewrites.
        self._lock = threading.Lock()
        # issue #73: guard delegation pending confirmation — action_id → tool_name; on confirm success a local approve rule is added
        self._pending_guard_approvals: Dict[str, str] = {}
        # issue #124: local CONFIRM denials pending approval — with guard OFF, high-risk node denials also enter the GUI approval queue
        self._pending_local_approvals: Dict[str, str] = {}
        # issue #138: pending approval card context — action_id → {tool_name/params/ts/workflow/node_id}
        self._pending_local_details: Dict[str, Dict[str, Any]] = {}
        # issue #154: pending-enqueue callback — hook point for pushing approval requests out (webhook push)
        self.on_pending_created = None
        # issue #228: workspace capability gate — async (tool, params, workspace_id) -> (verdict, reason);
        # not injected = zero behavior change. verdict: allow / deny / approval (approval goes through the existing HITL pending queue)
        self.capability_gate = None

    def _is_trusted_sync_target(self, tool_name: str, params: Dict[str, Any] = None) -> bool:
        # issue #301: deterministic template sync targets skip the per-node
        # confirm — checked directly (NOT seeded as rule objects) so persisted
        # rule listings/roundtrips stay pristine. Deny rules already won above;
        # approval_gate and every non-matching URL still gate.
        if tool_name != "http_request" or not params:
            return False
        target = str(params.get("url", "")).rstrip("/")
        if not target:
            return False
        return any(target.startswith(prefix) for prefix in trusted_sync_urls())

    async def check(self, tool_name: str, action: str = "", params: Dict[str, Any] = None) -> bool:
        # A-6: deny rules must precede the Hook approval decision — otherwise a Hook approve could override an explicit deny (bypass).
        # Scan deny first: a deny hit → immediate denial, Hook approval can no longer overturn it.
        # issue #340: gate tools are not pre-denied by deny rules — the gate decision is a workflow decision,
        # each evaluation must produce a fresh HITL pending approval card (poisoned historical rules no longer block)
        denied = False
        ctx = None
        with self._lock:
            rules_snapshot = list(self.rules)
        if tool_name not in HITL_GATE_TOOLS:
            for rule in rules_snapshot:
                if rule.matches(tool_name, params) and not rule.allowed:
                    logger.warning(f"Permission denied (deny rule takes precedence): {tool_name} (scope={rule.scope})")
                    denied = True
                    break

        # Hook: PERMISSION_REQUEST — out-of-band confirmation entry (CR-2/3). A-6: BYPASS does not skip the audit Hook
        # (still fired for the record), only the final verdict keeps allowing everything as before.
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
                logger.info(f"Permission denied by Hook: {tool_name}")
                return False

        if self.level == PermissionLevel.BYPASS:
            return True

        # A-6: deny rule hit → immediate denial, Hook approve cannot overturn
        if denied:
            return False
        if ctx and ctx.modified_data.get("approved"):
            logger.info(f"Permission approved by Hook: {tool_name}")
            return True

        # issue #554: SANDBOX layer without a runner = fail-closed, approval (rule/Hook) cannot save it —
        # isolation is an execution prerequisite, placed before every allow path (zero behavior change when SANDBOX_TOOLS is unset)
        from .kernel_gates import build_sandbox_policy, sandbox_block_reason

        _sb_reason = sandbox_block_reason(build_sandbox_policy(), tool_name, params or {})
        if _sb_reason:
            logger.warning(f"kernel sandbox fail-closed: {tool_name} — {_sb_reason}")
            return False

        # CR-16: approve rule hit → allow (any level); deny already handled in the previous step
        # issue #340: gate tools likewise don't consume approve rules — persistent approval = permanently skipping the human gate
        if tool_name not in HITL_GATE_TOOLS:
            for rule in rules_snapshot:
                if rule.matches(tool_name, params) and rule.allowed:
                    return True

        # issue #301: trusted sync targets (deterministic template sync) — allowed after rules, before the high-risk gate
        if self._is_trusted_sync_target(tool_name, params):
            logger.info(f"trusted sync target allowed: {tool_name} url={params.get('url', '')}")
            return True

        # issue #228: workspace capability pool gate — not installed/disabled → deny; sensitive scope unauthorized → approval path
        if self.capability_gate is not None:
            workspace_id = str(_approval_context.get({}).get("workspace_id", ""))
            verdict, reason = await self.capability_gate(tool_name, params or {}, workspace_id)
            if verdict == "deny":
                logger.warning(f"Capability gate denied: {tool_name} — {reason}")
                return False
            if verdict == "approval":
                logger.info(f"Capability gate pending approval: {tool_name} — {reason}")
                return await self._check_high_risk_guard(tool_name, action, params)

        # High-risk node without explicit approval → delegate to guard (issue #73) or deny (needs an approve rule or Hook pass-through)
        if tool_name in HIGH_RISK_NODES:
            return await self._check_high_risk_guard(tool_name, action, params)

        # Non-high-risk with no matching rule → kernel gate re-check (issue #554) then allow:
        # - SANDBOX fail-closed already blocks uniformly before the allow path (not repeated here)
        # - kernel destructive classification hit (classification table / name token) → same high-risk approval path
        #   (pending enqueue → HITL park); after approval the engine issues a single-use redemption approval token
        from .kernel_gates import classify_destructive

        destructive, why = classify_destructive(tool_name, params)
        if destructive:
            logger.info(f"kernel destructive_gate: {tool_name} classified destructive ({why}), routes to approval")
            return await self._check_high_risk_guard(tool_name, action, params)
        return True

    async def _check_high_risk_guard(self, tool_name: str, action: str, params: Dict[str, Any] = None) -> bool:
        # issue #73: guard not enabled → fall back to the original logic (high-risk without approval → deny), zero behavior change
        from ..security.guard import get_guard_client, node_to_guard_content

        client = get_guard_client()
        if client is None:
            # issue #124: local denial enters the pending approval queue — Web GUI can confirm, no longer permanently denied
            self._register_local_pending(tool_name, params)
            return False

        content, content_type = node_to_guard_content(tool_name, params)
        tenant_id = "default"
        requester = "unknown"
        try:
            from ..tenant import get_current_tenant, get_current_user

            tid = get_current_tenant()
            uid = get_current_user()
            if tid:
                tenant_id = tid
            if uid:
                requester = uid
        except ImportError:
            pass

        caller_epoch = _guard_cached_epoch()
        verdict = await client.evaluate(
            content=content,
            caller_epoch=caller_epoch,
            tenant_id=tenant_id,
            requester=requester,
            action=action or tool_name,
            content_type=content_type,
        )
        return await self._apply_verdict(tool_name, verdict)

    def _register_local_pending(self, tool_name: str, params: Dict[str, Any] = None) -> str:
        # issue #124/#257: high-risk denials uniformly enter the local pending approval queue — Web GUI can confirm,
        # the HITL confirm route uses execution_id to wake the paused run
        import uuid

        action_id = f"local_{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._pending_local_approvals[action_id] = tool_name
        ctx = _approval_context.get({})

        self._pending_local_details[action_id] = {
            "tool_name": tool_name,
            "params": _sanitize_approval_params(params),
            "ts": time.time(),
            "workflow": str(ctx.get("workflow", "")),
            "node_id": str(ctx.get("node_id", "")),
            # issue #181: HITL — the confirm route uses execution_id to wake the paused run
            "execution_id": str(ctx.get("execution_id", "")),
            # issue #142: edit-type pending approvals carry diff blocks + originals for rebuild
            "diff_blocks": _build_diff_blocks(params),
            "raw_edit": _build_raw_edit(params),
            # issue #326: upstream artifact evidence — the review card directly shows producing node outputs
            "artifacts": list(_approval_evidence.get([])),
        }
        logger.info(f"High-risk node denial entered pending approval queue: {tool_name} (action_id={action_id})")
        self._notify_pending(tool_name, action_id, source="local", params=params)
        return action_id

    async def _apply_verdict(self, tool_name: str, verdict) -> bool:
        if verdict is None:
            return self._guard_fail_closed(tool_name)
        from ..security.guard import get_guard_client, save_cached_rules

        try:
            dumped = await get_guard_client().rules_dump()
            if dumped is not None:
                rules, epoch = dumped
                save_cached_rules(rules, epoch)
        except Exception as e:
            logger.debug(f"Guard rule cache refresh failed: {e}")

        action = getattr(verdict, "action", "block")
        risk = getattr(verdict, "risk_level", "l4")
        action_id = getattr(verdict, "action_id", None)

        if action == "block" or risk == "l4":
            logger.info(f"Guard denied (block/l4): {tool_name} risk={risk} reason={verdict.reason}")
            # issue #257: guard denials also enter the pending approval queue — visible in the approval inbox, manual approval can recover
            self._register_local_pending(tool_name, None)
            return False
        if action == "allow" or risk in ("l1", "l2"):
            logger.info(f"Guard allowed: {tool_name} action={action} risk={risk}")
            return True
        if getattr(verdict, "requires_approval", False):
            if action_id:
                with self._lock:
                    self._pending_guard_approvals[action_id] = tool_name
            logger.info(f"Guard requires confirmation (l3): {tool_name} action_id={action_id}")
            self._notify_pending(tool_name, action_id, source="guard", params=None)
            return False
        # preview / redact → conservative denial (seatbelt out of scope for this phase)
        logger.info(f"Guard conservative denial (preview/redact): {tool_name} action={action}")
        self._register_local_pending(tool_name, None)
        return False

    def _guard_fail_closed(self, tool_name: str) -> bool:
        from ..security.guard import load_cached_rules

        cached = load_cached_rules()
        if cached is not None:
            rules, _epoch = cached
            for rule in rules:
                rule_tool = rule.get("tool") or rule.get("node") or rule.get("name")
                rule_action = rule.get("action", "")
                rule_allowed = rule.get("allowed")
                if rule_tool != tool_name:
                    continue
                if rule_action == "block" or rule_allowed is False:
                    logger.info(f"Guard unreachable, cached rule denies: {tool_name}")
                    return False
                if rule_action == "allow" or rule_allowed is True:
                    logger.info(f"Guard unreachable, cached rule allows: {tool_name}")
                    return True
        logger.warning(f"Guard unreachable with no matching cache, high-risk fail-closed denial: {tool_name}")
        self._register_local_pending(tool_name, None)
        return False

    def _notify_pending(self, tool_name: str, action_id: str, source: str, params: Any = None) -> None:
        """issue #154: notify on pending-enqueue (webhook push hook point) — best-effort, never raises or blocks.

        info: {action_id, tool_name, source, params, ts} — params go through _sanitize_approval_params,
        masked consistently with the GUI card.
        """
        cb = self.on_pending_created
        if not callable(cb):
            return
        try:
            ctx = _approval_context.get({})
            info = {
                "action_id": action_id,
                "tool_name": tool_name,
                "source": source,
                "params": _sanitize_approval_params(params),
                "workflow": str(ctx.get("workflow", "")),
                "node_id": str(ctx.get("node_id", "")),
                # issue #207: approval event carries run attribution — push receivers route by source
                "execution_id": str(ctx.get("execution_id", "")),
                "ts": time.time(),
            }
            cb(info)
        except Exception as e:
            logger.warning(f"on_pending_created callback failed (ignored): {e}")

    async def confirm_guard(
        self, action_id: str, approved: bool, approved_by: str = "unknown", tenant_id: str = "default", scope: str = "*"
    ) -> bool:
        # issue #124: confirm for local CONFIRM denials — no need for guard online, directly writes a local rule
        with self._lock:
            local_tool = self._pending_local_approvals.pop(action_id, None)
        if local_tool:
            self._pending_local_details.pop(action_id, None)
            # issue #340: gate decisions never persist rules — approve/deny both only dequeue,
            # each gate evaluation produces a fresh HITL card (an approve rule would blindly allow all later gates too)
            if local_tool in HITL_GATE_TOOLS:
                logger.info(f"Gate one-shot decision (no rule persisted): {local_tool} approved={approved} (action_id={action_id})")
                return True
            if approved:
                self.approve(local_tool, scope=scope)
                logger.info(f"Local confirm approved, approve rule added: {local_tool} (scope={scope}, by={approved_by})")
            else:
                self.deny(local_tool)
                logger.info(f"Local confirm denied, deny rule added: {local_tool} (by={approved_by})")
            return True

        from ..security.guard import get_guard_client

        client = get_guard_client()
        if client is None:
            logger.warning("Guard not enabled, confirm is a no-op")
            return False
        ok = await client.confirm(action_id=action_id, approved=approved, approved_by=approved_by, tenant_id=tenant_id)
        if not ok:
            return False
        with self._lock:
            tool_name = self._pending_guard_approvals.pop(action_id, None)
        if approved and tool_name:
            self.approve(tool_name)
            logger.info(f"Guard confirm approved, local approve rule added: {tool_name}")
        elif not approved and tool_name:
            self.deny(tool_name)
            logger.info(f"Guard confirm denied, local deny rule added: {tool_name}")
        return True

    def list_pending_guard(self) -> List[Dict[str, str]]:
        # W19: guard L3 pending approval queue — action_id → tool_name snapshot (removed after confirm)
        with self._lock:
            return [
                {"action_id": action_id, "tool_name": tool_name}
                for action_id, tool_name in self._pending_guard_approvals.items()
            ]

    def list_pending(self) -> List[Dict[str, Any]]:
        # issue #124: merged pending approval queue of guard + local CONFIRM denials (source field distinguishes origin)
        # issue #138: local entries carry approval card context (params/ts/workflow/node_id)
        with self._lock:
            entries: List[Dict[str, Any]] = [
                {"action_id": action_id, "tool_name": tool_name, "source": "guard"}
                for action_id, tool_name in self._pending_guard_approvals.items()
            ]
            entries.extend(
                {
                    "action_id": action_id,
                    "source": "local",
                    **(self._pending_local_details.get(action_id) or {"tool_name": tool_name}),
                }
                for action_id, tool_name in self._pending_local_approvals.items()
            )
        return entries

    def get_pending_details(self, action_id: str) -> Optional[Dict[str, Any]]:
        # issue #181: the confirm route reads pending approval details (incl. execution_id) to wake the HITL-paused run
        with self._lock:
            return dict(self._pending_local_details.get(action_id) or {})

    def find_pending_by_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        # issue #181: HITL — find a local pending approval item by execution_id (engine decides whether to enter paused wait)
        with self._lock:
            for action_id, details in self._pending_local_details.items():
                if details.get("execution_id") == execution_id:
                    return {"action_id": action_id, **details}
        return None

    def restore_pending_details(self, entries: List[Dict[str, Any]]) -> int:
        # issue #463: crash recovery — rebuild pending lines from the approval log into the local pending queue
        # (details shape matches the #138 approval card context, restored=True marks the origin)
        restored = 0
        with self._lock:
            for entry in entries or []:
                action_id = str((entry or {}).get("action_id") or "")
                if not action_id or action_id in self._pending_local_details:
                    continue
                details = {
                    "tool_name": str(entry.get("tool_name") or ""),
                    "params": entry.get("params") if isinstance(entry.get("params"), dict) else {},
                    "ts": float(entry.get("ts") or 0) or time.time(),
                    "workflow": str(entry.get("workflow") or ""),
                    "node_id": str(entry.get("node_id") or ""),
                    "execution_id": str(entry.get("run_id") or ""),
                    "restored": True,
                }
                self._pending_local_details[action_id] = details
                self._pending_local_approvals[action_id] = details["tool_name"]
                restored += 1
        if restored:
            logger.info(f"Approval log pending rebuild: {restored} entries restored into the pending queue")
        return restored

    def discard_pending_by_execution(self, execution_id: str) -> List[str]:
        # issue #187: HITL timeout — clear all stalled local pending items of this run (no rules persisted), returns the cleared action_id list
        removed: List[str] = []
        with self._lock:
            for action_id in list(self._pending_local_details.keys()):
                if self._pending_local_details[action_id].get("execution_id") == execution_id:
                    self._pending_local_details.pop(action_id, None)
                    self._pending_local_approvals.pop(action_id, None)
                    removed.append(action_id)
        if removed:
            logger.info(f"HITL timeout pending cleanup: exec={execution_id} removed={removed}")
        return removed

    def release_pending_once(self, action_id: str) -> bool:
        # issue #187: approve once — only dequeues this pending item, no persistent rule (matches the approval card "Approve once")
        with self._lock:
            existed = self._pending_local_approvals.pop(action_id, None) is not None
            self._pending_local_details.pop(action_id, None)
        if existed:
            logger.info(f"One-shot approval (no rule persisted): action_id={action_id}")
        return existed

    def approve(self, tool_name: str, scope: str = "*") -> None:
        rule = Permission(tool_name=tool_name, allowed=True, scope=scope)
        with self._lock:
            self.rules.append(rule)
        logger.info(f"Permission approved: {tool_name} (scope={scope})")

    def deny(self, tool_name: str, scope: str = "*") -> None:
        rule = Permission(tool_name=tool_name, allowed=False, scope=scope)
        with self._lock:
            self.rules.append(rule)
        logger.info(f"Permission denied: {tool_name} (scope={scope})")

    def request_approval(self, tool_name: str, params: Dict[str, Any] = None) -> str:
        import uuid

        req_id = f"perm_{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._pending_approvals[req_id] = Permission(tool_name=tool_name, allowed=False, scope="*")
        logger.info(f"Permission request: {req_id} → {tool_name}")
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
        # E-4: atomic write — write tmp then os.replace, eliminates partial writes / concurrent reads of truncated JSON crashes
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
        logger.info(f"Permission rules saved: {target}")

    def load(self, path: str = "") -> None:
        target = path or _PERMISSIONS_FILE
        if not os.path.exists(target):
            return
        # E-4: load tolerance — corrupted JSON doesn't crash, degrades to default empty rules + log
        try:
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Permission rules file corrupted, discarding old rules: {target} ({e})")
            with self._lock:
                self.rules = []
            return
        try:
            self.level = PermissionLevel(data.get("level", "confirm"))
        except ValueError:
            logger.warning(f"Invalid permission level value, falling back to CONFIRM: {data.get('level')}")
            self.level = PermissionLevel.CONFIRM
        new_rules = [
            Permission(tool_name=r["tool_name"], allowed=r["allowed"], scope=r.get("scope", "*"))
            for r in data.get("rules", [])
        ]
        with self._lock:
            self.rules = new_rules
        logger.info(f"Permission rules loaded: {len(new_rules)} rules (level={self.level.value})")

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
            self._pending_local_approvals.clear()
        logger.info(f"PermissionManager.reset removed={removed} tool_name={tool_name or '*'}")
        return removed

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            rules_snapshot = list(self.rules)
        return {
            "level": self.level.value,
            "rules": [{"tool_name": r.tool_name, "allowed": r.allowed, "scope": r.scope} for r in rules_snapshot],
        }
