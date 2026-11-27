"""Permission model — tool-call approval and leveled permissions.

Supports:
- 5 permission levels: CONFIRM (default, rule/high-risk driven) / MANUAL / AUTO / PLAN / BYPASS (allow all)
- rule matching: tool_name + scope (e.g. file:~/Desktop/**) — matching semantics delegated to the kernel
  fusion_harness.scoped_permissions (issue #493, K1 migration)
- high-risk node without explicit approval → deny (CR-16/17/18)
- permission rules persisted to a JSON config file

CR-16: check() order — approve-rule hit → allow (any level); deny → deny;
       high-risk → deny (needs explicit approve or Hook pass-through); otherwise → allow.
       approve() works for all levels (legacy MANUAL no-op removed).

#493 kernel migration boundary (consumer wiring explicitly named by the kernel scoped_permissions docstring
stays on this side, each site annotated with a parity comment):
- rule model/prefix scope matching (file:/command:/url:) → kernel ScopedPermission semantics;
  engine.check() does not delegate directly — it collapses bypass/allow-rule/default-allow into a single
  "allow" return value, while the check() flow needs three-state discrimination (trusted-sync and the high-risk gate sit between
  allow rules and default-allow), forcing delegation would change the CR-16 order
- hook audit/HITL gate exemption (issue #340)/trusted-sync (#301)/capability gate (#228)/
  guard IPC delegation (#73) = consumer wiring, stays on this side (kernel: guard clients and
  capability gates stay consumer-side)
- the pending-approval queue keeps the local rich-context implementation (approval-card diff blocks/execution_id wake/crash recovery);
  the kernel PendingApproval(id/action/resource/reason) has no card context and its resolve()
  auto-persists rules, conflicting with #340 gate no-persist semantics — a parity difference, deliberately not migrated

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

# issue #340: HITL gate tools — a gate decision is a workflow decision (feeds the on_reject=revise revision loop),
# not a tool-level permission grant/deny. deny rules must not pre-deny gates (otherwise one denial permanently kills the HITL
# cards of all subsequent gates), and confirm for gates only dequeues/wakes the run, persisting no durable rule.
HITL_GATE_TOOLS = {"approval_gate"}

_PERMISSIONS_DIR = os.path.expanduser("~/.fusion-cowork")

# issue #138: engine-side approval context (workflow/node) — WorkflowEngine writes it before check,
# so high-risk denials entering the pending queue carry the context the card display needs.
_approval_context: ContextVar = ContextVar("fc_approval_context")


def set_approval_context(ctx: Dict[str, Any]):
    return _approval_context.set({str(k): str(v) for k, v in dict(ctx).items()})


def reset_approval_context(token) -> None:
    _approval_context.reset(token)


# issue #326: upstream artifact evidence (producing node output snapshots) — WorkflowEngine writes it before check,
# pending cards carry an artifacts block so reviewers never leave the approval UI to find produced material.
_approval_evidence: ContextVar = ContextVar("fc_approval_evidence")


def set_approval_evidence(items: List[Dict[str, Any]]):
    return _approval_evidence.set(list(items))


def reset_approval_evidence(token) -> None:
    _approval_evidence.reset(token)


def _sanitize_approval_params(params: Dict[str, Any] = None) -> Dict[str, Any]:
    # Approval-card param snapshot: scalars only, 200-char string cap, secret key names redacted, 20-key cap
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


# issue #142: approval diff blocks for edit-style params (old_text/new_text) — raw-text truncation cap
_DIFF_MAX_CHARS = 20000


def _build_diff_blocks(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    # issue #142: apply_edit-style pending items → SequenceMatcher blocks, per-block accept/reject in the card
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
                logger.warning(f"Approval diff blocking failed: {e}")
                return []
    return []


def _build_raw_edit(params: Dict[str, Any] = None) -> Dict[str, Any]:
    # issue #142: per-block review rebuild needs the untruncated old/new raw text (unlike the redacted display snapshot)
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
        "worktree",  # issue #580: worktree exec runs arbitrary commands + destructive remove, same high-risk class as shell_exec
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
        "cdp_wait_for",  # E-10: wait_for_function injects arbitrary JS, same high-risk class as cdp_evaluate
        "managed_browser",  # issue #153: launches the local browser + imports login cookies, requires explicit allow
        "fusion_browser",  # issue #162: managed fusion-browser engine + reads local browser login state
        "fetch_url",  # E-11: SSRF surface, denied by default, requires explicit allow
        # External runtime trigger (issue #133): studio_run submits an agent-studio graph/agent
        # run, triggering remote multi-step agent behavior — requires explicit allow like fetch_url
        "studio_run",
        # issue #185: mcp_client calls arbitrary tools on external MCP servers (remote execution surface),
        # requires explicit allow; supports the url: prefix scope to authorize exactly one server
        "mcp_client",
        # issue #206: http_request arbitrary-target request surface (can carry auth headers/template-injected bodies),
        # requires explicit allow; supports the url: prefix scope like mcp_client
        "http_request",
        # issue #262: approval_gate human-decision gate — inherently requires approval (pauses for a human decision),
        # pairs with the http_webhook signed callback for "external system approval → callback resumes the run"
        "approval_gate",
        # issue #262: agent_exec external agent/CLI process execution surface (arbitrary commands), requires explicit allow
        "agent_exec",
        # issue #262: command_evidence allowlisted command execution + evidence archiving, requires explicit allow
        "command_evidence",
        # issue #308: claude_dispatch spawns short-lived coder sessions (local process + repo write access),
        # high-risk execution surface — budget cap + crash/timeout fallback managed by the node itself, allow requires explicit approval
        "claude_dispatch",
        # issue #467: coordinator_fanout fans out sub-agent execution (multi-step agent behavior surface),
        # session_fork derives parallel sub-session branches executing arbitrary registered nodes — both require explicit allow
        "coordinator_fanout",
        "session_fork",
        # issue #470: workflow_ladder staged orchestration — multi-stage sub-agents execute arbitrary worker_node,
        # same multi-step agent behavior surface as coordinator_fanout, requires explicit allow
        "workflow_ladder",
        # issue #488: team_call drives agent-studio team members to execute tasks — remote multi-step
        # agent behavior surface (same class as studio_run), requires explicit allow
        "team_call",
        # issue #489: deploy_service runs docker/launchd/installer-script deployments and starts services —
        # local command execution + service lifecycle operations (shell-exec class), requires explicit allow
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
        # parity coverage (intentionally-different items recorded in the kernel difference table):
        # 1. no params → matches any scope (upstream and kernel empty-resource semantics agree)
        # 2. params present but the key (command/url) missing → no match (kernel treats empty resource as a match — not copied)
        # 3. file: scope expanduser on both sides (E-3: kernel does not expand ~, upstream does — kept)
        # 4. unknown-prefix scope → True (kernel switched to resource prefix matching — upstream fallback kept)
        # kernel fix applied on the kernel side: file:/url: path boundary check (upstream raw startswith
        # let file:/a falsely match file:/ab — a quirk recorded in the kernel parity table, not copied)
        if self.tool_name != "*" and self.tool_name != tool_name:
            return False
        scope = self.scope
        if scope == "*":
            return True
        if not params:
            # parity: upstream no-arg calls match any scope; kernel empty resource also matches — semantics agree
            return True
        if not scope.startswith(("file:", "command:", "url:")):
            # parity: upstream unknown-prefix fallback = True (kernel switched to resource prefix matching — not copied)
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
            # parity: params present but no command key → no match (kernel treats empty resource as a match — not copied)
            cmd = params.get("command", "")
            return isinstance(cmd, str) and bool(cmd) and _scope_match(scope, cmd)
        # url: — issue #185 prefix scope (remote-call nodes like mcp_client)
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
        # R-3: concurrency protection for rules/_pending_approvals — check() traversal vs approve/deny/reset mutation race.
        self._lock = threading.Lock()
        # issue #73: guard delegation pending-confirm — action_id → tool_name, a successful confirm adds a local approve rule
        self._pending_guard_approvals: Dict[str, str] = {}
        # issue #124: local CONFIRM denial pending approval — with guard OFF, high-risk denials still enter the GUI approval queue
        self._pending_local_approvals: Dict[str, str] = {}
        # issue #138: pending card context — action_id → {tool_name/params/ts/workflow/node_id}
        self._pending_local_details: Dict[str, Dict[str, Any]] = {}
        # issue #154: pending-enqueue callback — hook point for outbound approval requests (webhook push)
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
        # A-6: deny rules must be evaluated before Hook approval — otherwise a Hook approve could override an explicit deny (bypass).
        # Scan deny first: a deny hit → immediate deny, a Hook approval cannot overturn it.
        # issue #340: gate tools are not pre-denied by deny rules — a gate decision is a workflow decision,
        # every evaluation must produce a fresh HITL pending card (poisoned historical rules no longer block)
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

        # Hook: PERMISSION_REQUEST — out-of-band confirmation entry (CR-2/3). A-6: BYPASS does not skip the audit hook either
        # (it still fires for the record), only the final verdict keeps allowing everything as before.
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
                logger.info(f"Permission denied by hook: {tool_name}")
                return False

        if self.level == PermissionLevel.BYPASS:
            return True

        # A-6: a deny-rule hit → immediate deny, a Hook approve cannot overturn it
        if denied:
            return False
        if ctx and ctx.modified_data.get("approved"):
            logger.info(f"Permission approved by hook: {tool_name}")
            return True

        # issue #554: SANDBOX tier without a runner = fail-closed, approval (rules/hooks) cannot rescue it —
        # isolation is a precondition for execution, placed before every allow path (zero behavior change when SANDBOX_TOOLS is unset)
        from .kernel_gates import build_sandbox_policy, sandbox_block_reason

        _sb_reason = sandbox_block_reason(build_sandbox_policy(), tool_name, params or {})
        if _sb_reason:
            logger.warning(f"kernel sandbox fail-closed: {tool_name} — {_sb_reason}")
            return False

        # CR-16: approve-rule hit → allow (any level); deny was already handled in the previous step
        # issue #340: gate tools also ignore approve rules — a persistent allow = permanently skipping the human gate
        if tool_name not in HITL_GATE_TOOLS:
            for rule in rules_snapshot:
                if rule.matches(tool_name, params) and rule.allowed:
                    return True

        # issue #301: trusted-sync targets (deterministic template sync) — allowed after rules, before the high-risk gate
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

        # high-risk node without explicit approval → delegate to guard (issue #73) or deny (needs an approve rule or Hook pass-through)
        if tool_name in HIGH_RISK_NODES:
            return await self._check_high_risk_guard(tool_name, action, params)

        # non-high-risk with no matching rule → allow after the kernel gate re-check (issue #554):
        # - SANDBOX fail-closed was already uniformly blocked before the allow path (not repeated here)
        # - a kernel destructive classification hit (classification table/name tokens) → the same high-risk approval path
        #   (pending enqueue → HITL park); after approval the engine issues a single-redeem approval token
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
            # issue #124: local denials enter the pending queue — the Web GUI can confirm, no longer a permanent denial
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
        # issue #124/#257: high-risk denials uniformly enter the local pending queue — the Web GUI can confirm,
        # the HITL confirm route wakes the paused run by execution_id
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
            # issue #181: HITL — the confirm route wakes the paused run by execution_id
            "execution_id": str(ctx.get("execution_id", "")),
            # issue #142: edit-style pending items carry diff blocks + raw text for rebuild
            "diff_blocks": _build_diff_blocks(params),
            "raw_edit": _build_raw_edit(params),
            # issue #326: upstream artifact evidence — the review card shows producing-node outputs directly
            "artifacts": list(_approval_evidence.get([])),
        }
        logger.info(f"High-risk node denial entered the pending queue: {tool_name} (action_id={action_id})")
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
            logger.debug(f"Guard rules cache refresh failed: {e}")

        action = getattr(verdict, "action", "block")
        risk = getattr(verdict, "risk_level", "l4")
        action_id = getattr(verdict, "action_id", None)

        if action == "block" or risk == "l4":
            logger.info(f"Guard denied (block/l4): {tool_name} risk={risk} reason={verdict.reason}")
            # issue #257: guard denials also enter the pending queue — visible in the approval inbox, recoverable by human approval
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
        # preview / redact → conservative deny (seatbelt is out of scope for this phase)
        logger.info(f"Guard conservative deny (preview/redact): {tool_name} action={action}")
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
                    logger.info(f"Guard unreachable, cached rules deny: {tool_name}")
                    return False
                if rule_action == "allow" or rule_allowed is True:
                    logger.info(f"Guard unreachable, cached rules allow: {tool_name}")
                    return True
        logger.warning(f"Guard unreachable with no matching cache, high-risk fail-closed deny: {tool_name}")
        self._register_local_pending(tool_name, None)
        return False

    def _notify_pending(self, tool_name: str, action_id: str, source: str, params: Any = None) -> None:
        """issue #154: notify on pending-approval enqueue (webhook push hook point) — best-effort, never raises or blocks.

        info: {action_id, tool_name, source, params, ts} — params pass through _sanitize_approval_params
        with the same redaction as the GUI card.
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
                # issue #207: approval events carry run attribution — push receivers route by source
                "execution_id": str(ctx.get("execution_id", "")),
                "ts": time.time(),
            }
            cb(info)
        except Exception as e:
            logger.warning(f"on_pending_created callback failed (ignored): {e}")

    async def confirm_guard(
        self, action_id: str, approved: bool, approved_by: str = "unknown", tenant_id: str = "default", scope: str = "*"
    ) -> bool:
        # issue #124: confirm for local CONFIRM denials — no guard needed online, persists a local rule directly
        with self._lock:
            local_tool = self._pending_local_approvals.pop(action_id, None)
        if local_tool:
            self._pending_local_details.pop(action_id, None)
            # issue #340: gate decisions persist no durable rule — both approve/deny only dequeue,
            # every gate evaluation produces a fresh HITL card (an approve rule would blindly allow all subsequent gates too)
            if local_tool in HITL_GATE_TOOLS:
                logger.info(
                    f"Gate one-shot decision (no rule persisted): {local_tool} approved={approved} (action_id={action_id})"
                )
                return True
            if approved:
                self.approve(local_tool, scope=scope)
                logger.info(
                    f"Local confirm approved, adding approve rule: {local_tool} (scope={scope}, by={approved_by})"
                )
            else:
                self.deny(local_tool)
                logger.info(f"Local confirm denied, adding deny rule: {local_tool} (by={approved_by})")
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
            logger.info(f"Guard confirm approved, adding local approve rule: {tool_name}")
        elif not approved and tool_name:
            self.deny(tool_name)
            logger.info(f"Guard confirm denied, adding local deny rule: {tool_name}")
        return True

    def list_pending_guard(self) -> List[Dict[str, str]]:
        # W19: guard L3 pending queue — action_id → tool_name snapshot (removed after confirm)
        with self._lock:
            return [
                {"action_id": action_id, "tool_name": tool_name}
                for action_id, tool_name in self._pending_guard_approvals.items()
            ]

    def list_pending(self) -> List[Dict[str, Any]]:
        # issue #124: guard + local CONFIRM denials merged into one pending queue (source field distinguishes origin)
        # issue #138: local entries carry approval-card context (params/ts/workflow/node_id)
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
        # issue #181: the confirm route reads pending details (including execution_id) to wake the HITL-paused run
        with self._lock:
            return dict(self._pending_local_details.get(action_id) or {})

    def find_pending_by_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        # issue #181: HITL — find the local pending item by execution_id (the engine decides whether to enter the paused wait)
        with self._lock:
            for action_id, details in self._pending_local_details.items():
                if details.get("execution_id") == execution_id:
                    return {"action_id": action_id, **details}
        return None

    def restore_pending_details(self, entries: List[Dict[str, Any]]) -> int:
        # issue #463: crash recovery — pending rows from the approval journal are rebuilt into the local pending queue
        # (details shaped like the #138 approval-card context, restored=True marks the origin)
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
            logger.info(f"Approval journal pending rebuild: {restored} entries restored into the pending queue")
        return restored

    def discard_pending_by_execution(self, execution_id: str) -> List[str]:
        # issue #187: HITL timeout — clear all stale local pending items for the run (no rules persisted), return the cleared action_id list
        removed: List[str] = []
        with self._lock:
            for action_id in list(self._pending_local_details.keys()):
                if self._pending_local_details[action_id].get("execution_id") == execution_id:
                    self._pending_local_details.pop(action_id, None)
                    self._pending_local_approvals.pop(action_id, None)
                    removed.append(action_id)
        if removed:
            logger.info(f"HITL timeout cleared pending items: exec={execution_id} removed={removed}")
        return removed

    def release_pending_once(self, action_id: str) -> bool:
        # issue #187: approve once — dequeues only this pending item, persists no durable rule (the approval-card "Approve once")
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
        # E-4: atomic write — write tmp then os.replace, prevents half-writes/concurrent reads of corrupt JSON from crashing
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
        # E-4: load tolerance — corrupt JSON does not crash, degrades to default empty rules + logging
        try:
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Permission rules file corrupt, discarding old rules: {target} ({e})")
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
        logger.info(f"Permission rules loaded: {len(new_rules)} entries (level={self.level.value})")

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
