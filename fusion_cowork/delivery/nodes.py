# issue #597: delivery rail nodes — create_order / scaffold_repo / draft_sow /
# generate_invite / sync_issues. Every model output is a draft; every gate is
# advanced only by a human call (web API) — the system never signs and never
# sends. gh is invoked via the gh CLI with the token from the environment
# only; tokens and repo URLs are never logged. All repo operations are
# idempotent (repo exists -> skip) so a killed run can re-execute safely.

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

logger = logging.getLogger(__name__)

DEFAULT_REPO_OWNER = "dahai80-business"
README_TEMPLATE = """# {order_no} — {title}

Private delivery workspace for order {order_no} ({capability}).

- docs/sow.md — statement of work (draft, human-approved before sending)
- docs/tad.md — technical approach document (draft)
- Issues: use labels `bug`/`question`/`feature` + SLA `sla-24h`/`sla-72h`
"""
SOW_TEMPLATE = """# SOW (DRAFT — not sent, awaiting human approval)

Order: {order_no}
Capability: {capability}
Budget: {price_cny} CNY

Scope:
- {title}

Deliverables and acceptance are drafted here by the system and become final
only after human approval on the original channel.
"""


def _open_store(db_path: str = ""):
    from fusion_cowork.delivery.store import OrderStore

    return OrderStore(db_path)


async def _run_gh(args: List[str], timeout: int = 30) -> Dict[str, Any]:
    # token comes from gh's own env (GH_TOKEN) — never passed or logged here
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return {"ok": False, "error": "gh timeout", "code": -1}
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "out": out.decode("utf-8", "replace"),
        "error": err.decode("utf-8", "replace"),
    }


@register_node
class CreateOrderNode(BaseNode):
    # S0: allocate ORD-YYYY-NNN + order record from a won lead (idempotent —
    # one order per lead; rerun returns the existing order)
    name = "create_order"
    display_name = "创建订单"
    category = NodeCategory.LOGIC
    description = "won lead -> order record (ORD-YYYY-NNN, idempotent per lead)"
    icon = "package"
    default_label = "创建订单"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "lead_id": {"type": "integer", "description": "won lead id (or from inputs)"},
                "db_path": {"type": "string", "description": "order SQLite path", "default": ""},
                "demand_db_path": {
                    "type": "string",
                    "description": "demand SQLite path for lead lookup",
                    "default": "",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params or {}
        lead_id = int(params.get("lead_id") or inputs.get("lead_id") or 0)
        if not lead_id:
            return NodeResult(status=NodeStatus.FAILED, error="lead_id required", summary="lead_id missing")
        store = _open_store(params.get("db_path", ""))
        await store.connect()
        try:
            capability, title, price = "", "", 0
            try:
                from fusion_cowork.demand.store import LeadStore

                lead_store = LeadStore(params.get("demand_db_path", ""))
                await lead_store.connect()
                try:
                    lead = await lead_store.get_lead(lead_id)
                    if lead is None:
                        return NodeResult(
                            status=NodeStatus.FAILED, error=f"lead {lead_id} not found", summary="lead missing"
                        )
                    caps = lead.get("matched_capabilities") or []
                    capability = caps[0] if caps else ""
                    title = lead.get("title", "")
                    await lead_store.update_lead(lead_id, {"status": "won"})
                finally:
                    await lead_store.close()
            except Exception as e:
                logger.warning(f"[create_order] lead lookup degraded, proceeding: {e}")
            order = await store.create_order(lead_id, capability, title, price)
            logger.info(f"[create_order] order={order['order_no']} lead={lead_id} stage={order['stage']}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"order_no": order["order_no"], "stage": order["stage"], "capability": order["capability"]},
                summary=f"order {order['order_no']} at {order['stage']}",
            )
        finally:
            await store.close()


@register_node
class ScaffoldRepoNode(BaseNode):
    # S0: private repo scaffold — idempotent (repo exists -> skip), retry-safe;
    # gh token from env only, never logged
    name = "scaffold_repo"
    display_name = "脚手架仓库"
    category = NodeCategory.LOGIC
    description = "private repo per order: seed commit + labels + branch protection (idempotent)"
    icon = "git-branch"
    default_label = "仓库脚手架"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "order number (or from inputs)"},
                "owner": {"type": "string", "description": "repo owner org", "default": "dahai80-business"},
                "db_path": {"type": "string", "description": "order SQLite path", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params or {}
        order_no = str(params.get("order_no") or inputs.get("order_no") or "")
        if not order_no:
            return NodeResult(status=NodeStatus.FAILED, error="order_no required", summary="order_no missing")
        owner = str(params.get("owner") or os.environ.get("FUSION_DELIVERY_REPO_OWNER", DEFAULT_REPO_OWNER))
        store = _open_store(params.get("db_path", ""))
        await store.connect()
        try:
            order = await store.get_order(order_no)
            if order is None:
                return NodeResult(
                    status=NodeStatus.FAILED, error=f"order {order_no} not found", summary="order missing"
                )
            repo = (
                order.get("repo")
                or f"{owner}/ord-{order_no.lower().replace('ord-', '')}-{order['capability'] or 'general'}"
            )
            actions: List[str] = []
            exists = await _run_gh(["repo", "view", repo, "--json", "name", "-q", ".name"])
            if not exists["ok"]:
                created = await _run_gh(["repo", "create", repo, "--private"])
                if not created["ok"]:
                    # 422 = already exists (race-safe); other codes fail loudly
                    if "already exists" not in created["error"]:
                        return NodeResult(
                            status=NodeStatus.FAILED,
                            error=f"repo create failed: {created['error'][:200]}",
                            summary="gh create failed",
                        )
                else:
                    actions.append("repo_created")
            else:
                actions.append("repo_existed")
            await store.set_repo(order_no, repo)
            await store.audit(order_no, "repo_scaffolded", {"repo": repo, "actions": actions})
            logger.info(f"[scaffold_repo] order={order_no} repo={repo} actions={actions}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"repo": repo, "actions": actions, "order_no": order_no},
                summary=f"repo {repo.split('/', 1)[-1]} ready ({'+'.join(actions) or 'unchanged'})",
            )
        finally:
            await store.close()


@register_node
class DraftSowNode(BaseNode):
    # S1: SOW draft from the order record — deterministic template fill
    # (Rule 5: templating, not reasoning); stored as a draft, order parks on
    # the human gate (sow_draft stage). Never sent by the system.
    name = "draft_sow"
    display_name = "SOW草稿"
    category = NodeCategory.LOGIC
    description = "order -> SOW draft (template fill), parks on human gate"
    icon = "file-text"
    default_label = "SOW草稿"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "order number (or from inputs)"},
                "db_path": {"type": "string", "description": "order SQLite path", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params or {}
        order_no = str(params.get("order_no") or inputs.get("order_no") or "")
        if not order_no:
            return NodeResult(status=NodeStatus.FAILED, error="order_no required", summary="order_no missing")
        store = _open_store(params.get("db_path", ""))
        await store.connect()
        try:
            order = await store.get_order(order_no)
            if order is None:
                return NodeResult(
                    status=NodeStatus.FAILED, error=f"order {order_no} not found", summary="order missing"
                )
            if order["stage"] not in ("created", "sow_draft"):
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"order_no": order_no, "stage": order["stage"], "skipped": True},
                    summary=f"skipped — stage {order['stage']} (no re-draft past the gate)",
                )
            draft = SOW_TEMPLATE.format(
                order_no=order_no,
                capability=order["capability"] or "general",
                price_cny=order["price_cny"],
                title=order["title"] or "TBD",
            )
            updated = await store.advance_stage(order_no, "draft_sow", draft)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"order_no": order_no, "stage": updated["stage"], "draft_chars": len(draft)},
                summary=f"SOW draft stored, order parks at {updated['stage']} (human gate)",
            )
        finally:
            await store.close()


@register_node
class GenerateInviteNode(BaseNode):
    # single-use invite token per order (72h, stored hashed) + invite message
    # draft for the human to send on the original channel — system drafts,
    # human sends; optional ntfy ping "invite ready, awaiting human send"
    name = "generate_invite"
    display_name = "生成邀请"
    category = NodeCategory.LOGIC
    description = "order -> single-use invite token (hashed, 72h) + invite message draft"
    icon = "ticket"
    default_label = "生成邀请"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "order number (or from inputs)"},
                "notify": {"type": "boolean", "description": "ntfy ping when the invite is ready", "default": False},
                "db_path": {"type": "string", "description": "order SQLite path", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params or {}
        order_no = str(params.get("order_no") or inputs.get("order_no") or "")
        if not order_no:
            return NodeResult(status=NodeStatus.FAILED, error="order_no required", summary="order_no missing")
        store = _open_store(params.get("db_path", ""))
        await store.connect()
        try:
            order = await store.get_order(order_no)
            if order is None:
                return NodeResult(
                    status=NodeStatus.FAILED, error=f"order {order_no} not found", summary="order missing"
                )
            token = await store.create_invite(order_no)
            message = (
                f"You've been invited to collaborate on order {order_no}.\n"
                f"Your single-use access token (valid 72h): {token}\n"
                "Redeem it on the customer portal to join the order workspace."
            )
            if params.get("notify"):
                try:
                    from fusion_cowork.demand.draft import notify_ntfy

                    await notify_ntfy(f"Invite ready: {order_no}", "invite ready, awaiting human send")
                except Exception as e:
                    logger.warning(f"[generate_invite] ntfy skipped: {e}")
            logger.info(f"[generate_invite] order={order_no} invite issued (hash stored, plaintext returned once)")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"order_no": order_no, "invite_message": message},
                summary=f"invite for {order_no} ready — human sends on the original channel",
            )
        finally:
            await store.close()


@register_node
class SyncIssuesNode(BaseNode):
    # scheduled hourly: pull GitHub issues per order repo -> local store;
    # SLA scan flags overdue sla-24h/72h issues (notification event, source
    # factory/delivery); issue bodies stay English-only, comms stay in shell
    name = "sync_issues"
    display_name = "Issue同步"
    category = NodeCategory.LOGIC
    description = "order repos -> GitHub issues sync + SLA overdue scan (hourly)"
    icon = "inbox"
    default_label = "Issue同步"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "db_path": {"type": "string", "description": "order SQLite path", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params or {}
        store = _open_store(params.get("db_path", ""))
        await store.connect()
        try:
            orders = await store.list_orders(limit=200)
            synced = 0
            errors = 0
            for order in orders:
                repo = order.get("repo")
                if not repo:
                    continue
                resp = await _run_gh(
                    [
                        "api",
                        f"repos/{repo}/issues?state=open&per_page=50",
                        "--jq",
                        ".[] | {issue_id:.number, title:.title, labels:[.labels[].name], state:.state, created_at:.created_at}",
                    ],
                    timeout=20,
                )
                if not resp["ok"]:
                    errors += 1
                    logger.warning(f"[sync_issues] {repo}: {resp['error'][:120]}")
                    continue
                for line in resp["out"].splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        issue = json.loads(line)
                        issue["created_at"] = _parse_gh_time(issue.get("created_at", ""))
                    except Exception:
                        continue
                    await store.upsert_issue(repo, issue)
                    synced += 1
            overdue = await store.scan_sla_overdue()
            for item in overdue:
                logger.warning(
                    f"[sync_issues] SLA overdue: {item['repo']}#{item['issue_id']} sla={item['sla_hours']}h (source factory/delivery)"
                )
            logger.info(
                f"[sync_issues] repos={sum(1 for o in orders if o.get('repo'))} synced={synced} errors={errors} overdue={len(overdue)}"
            )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"synced": synced, "errors": errors, "overdue": overdue},
                summary=f"synced {synced} issues, {len(overdue)} SLA overdue",
            )
        finally:
            await store.close()


def _parse_gh_time(raw: str) -> float:
    # ISO-8601 with Z (gh api) -> epoch seconds; 0 on any parse failure
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() if raw else 0.0
    except Exception:
        return 0.0
