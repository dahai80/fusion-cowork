# issue #585-#588: demand engine M1 nodes — poll_qq_imap / normalize_lead /
# match / dual_draft. One poll/normalize/match/draft cycle per invocation;
# state lives in LeadStore (SQLite) so the scheduler can drive them on cron.
#
# Safety: mail content is untrusted data (never node instructions), links not
# fetched, attachments never opened, passwords/API keys env-only never logged.
# draft nodes only store proposals — they never send anything.

from __future__ import annotations

import logging
from typing import Any, Dict

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

logger = logging.getLogger(__name__)


def _pick_chat_model(models: Any) -> str:
    # shared picker filters embedding/draft engine types; empty on failure
    # (models = list from client.list_models(), not the client itself)
    try:
        from fusion_cowork.ai.nl_parser import _pick_chat_model

        return _pick_chat_model(models) or ""
    except Exception:
        return ""


def _open_store(db_path: str = ""):
    from fusion_cowork.demand.store import LeadStore

    return LeadStore(db_path)


@register_node
class PollQqImapNode(BaseNode):
    # issue #585: QQ mail IMAP poll — read-only, UID cursor for restart-safe
    # resume, Message-ID dedupe. One cycle per invocation (cron-driven).
    name = "poll_qq_imap"
    display_name = "QQ邮箱IMAP轮询"  # i18n: user-facing
    category = NodeCategory.IO
    description = "IMAP read-only poll: new mail -> raw_messages (UID cursor + Message-ID dedupe)"
    icon = "mail"
    default_label = "IMAP轮询"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "batch_limit": {"type": "integer", "description": "max mails per cycle", "default": 50},
                "db_path": {
                    "type": "string",
                    "description": "demand SQLite path (empty = FUSION_COWORK_DEMAND_DB)",
                    "default": "",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from fusion_cowork.demand.imap_poll import ImapConfig, poll_once, raw_mail_to_record
        from fusion_cowork.demand.store import default_db_path

        params = self.config.params or {}
        cfg = ImapConfig.from_env()
        if not cfg.user or not cfg.password:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="FUSION_QQ_IMAP_USER/FUSION_QQ_IMAP_PASSWORD not set",
                summary="credentials missing",
            )
        store = _open_store(params.get("db_path") or default_db_path())
        await store.connect()
        try:
            cursor_key = f"uid:{cfg.folder}"
            last_uid = await store.get_cursor(cursor_key)
            result = await poll_once(cfg, last_uid, batch_limit=int(params.get("batch_limit", 50)))
            if result["error"] and not result["mails"]:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=result["error"],
                    data={"last_uid": last_uid},
                    summary="poll failed",
                )
            new_count = 0
            dup_count = 0
            for mail in result["mails"]:
                if await store.upsert_raw(raw_mail_to_record(mail)):
                    new_count += 1
                else:
                    dup_count += 1
            if result["max_uid"] > last_uid:
                await store.set_cursor(cursor_key, result["max_uid"])
            logger.info(f"[poll_qq_imap] new={new_count} dup={dup_count} cursor={result['max_uid']}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "new": new_count,
                    "duplicates": dup_count,
                    "last_uid": last_uid,
                    "cursor": result["max_uid"],
                    "error": result["error"],
                },
                summary=f"polled {new_count} new mails ({dup_count} duplicates)",
            )
        finally:
            await store.close()


@register_node
class NormalizeLeadNode(BaseNode):
    # issue #586: raw mail -> unified Lead. Classification/extraction via the
    # local model (fixed prompt + JSON validation), deterministic rule
    # fallback when the model is unreachable — node never fails on model-down.
    # Spam leads are marked, never deleted. issue #592: empty external_id gets
    # a deterministic per-message fallback; (source, external_id) collisions
    # merge into the existing lead and file a duplicate marker instead of
    # wedging the raw mail in a permanent error loop.
    name = "normalize_lead"
    display_name = "线索归一化"  # i18n: user-facing
    category = NodeCategory.IO
    description = "raw_messages -> unified Lead schema (model classify+extract, rule fallback)"
    icon = "inbox"
    default_label = "线索归一化"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "max records per cycle", "default": 20},
                "model": {
                    "type": "string",
                    "description": "model id override (empty = auto-pick chat model)",
                    "default": "",
                },
                "db_path": {"type": "string", "description": "demand SQLite path", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from fusion_cowork.demand.normalize import normalize_lead

        params = self.config.params or {}
        client = None
        model = str(params.get("model", "")) or str(inputs.get("model", ""))
        if not model:
            try:
                from fusion_cowork.ai.mlx_client import FusionMLXClient

                client = FusionMLXClient()
                model = _pick_chat_model(await client.list_models())
            except Exception as e:
                logger.warning(f"[normalize_lead] model client unavailable, rule fallback: {e}")
                client = None
        store = _open_store(params.get("db_path", ""))
        await store.connect()
        try:
            records = await store.list_unnormalized(limit=int(params.get("limit", 20)))
            normalized = 0
            spam = 0
            merged = 0
            errors = 0
            by_extraction = {"model": 0, "rules": 0}
            for record in records:
                try:
                    lead = await normalize_lead(record, client=client, model=model)
                    # issue #592: collision merges into the existing lead and
                    # files a duplicate marker — raw never re-processes
                    result = await store.insert_or_merge_lead(lead)
                    path = lead.get("extraction", "rules")
                    by_extraction[path] = by_extraction.get(path, 0) + 1
                    if result.get("merged"):
                        merged += 1
                    else:
                        normalized += 1
                        if lead["status"] == "spam":
                            spam += 1
                except Exception as e:
                    errors += 1
                    logger.error(f"[normalize_lead] record failed: {e}")
            logger.info(
                f"[normalize_lead] normalized={normalized} spam={spam} merged={merged} errors={errors} "
                f"by_extraction={by_extraction}"
            )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "normalized": normalized,
                    "spam": spam,
                    "merged": merged,
                    "errors": errors,
                    "model_used": bool(model),
                    "by_extraction": by_extraction,
                },
                summary=f"normalized {normalized} leads (spam {spam}, merged {merged}, errors {errors})",
            )
        finally:
            await store.close()


@register_node
class MatchLeadNode(BaseNode):
    # issue #587: Lead x capability catalog — pure-code scoring (keyword/skill
    # tables + budget range), no LLM (Rule 5). Below-threshold leads parked,
    # never dropped; catalog version recorded per row. Re-runs skip leads
    # already scored against the current catalog version.
    name = "match"
    display_name = "能力目录匹配"  # i18n: user-facing
    category = NodeCategory.LOGIC
    description = "Lead x capability catalog deterministic scoring (no LLM, re-runnable)"
    icon = "target"
    default_label = "目录匹配"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "number",
                    "description": "match score threshold (below = parked)",
                    "default": 3.0,
                },
                "catalog_path": {"type": "string", "description": "catalog JSON path (empty = seed)", "default": ""},
                "db_path": {"type": "string", "description": "demand SQLite path", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from fusion_cowork.demand.catalog import load_catalog, match_lead

        params = self.config.params or {}
        threshold = float(params.get("threshold", 3.0))
        catalog = load_catalog(params.get("catalog_path", ""))
        catalog_version = str(catalog.get("version", ""))
        store = _open_store(params.get("db_path", ""))
        await store.connect()
        try:
            leads = await store.list_for_match()
            matched = 0
            parked = 0
            skipped = 0
            for lead in leads:
                if lead.get("catalog_version") == catalog_version and lead.get("status") in ("matched", "parked"):
                    skipped += 1
                    continue
                result = match_lead(lead, catalog, threshold=threshold)
                status = "parked" if result["parked"] else "matched"
                top = result["match_details"][0] if result["match_details"] else None
                fields = {
                    "matched_capabilities": result["matched_capabilities"],
                    "match_score": result["match_score"],
                    "match_reason": result["match_reason"],
                    "catalog_version": result["catalog_version"],
                    "status": status,
                }
                if not lead.get("budget_hint") and top:
                    fields["budget_hint"] = f"{top['price_cny']} CNY"
                await store.update_lead(lead["id"], fields)
                if status == "matched":
                    matched += 1
                else:
                    parked += 1
                logger.debug(f"[match] lead={lead['id']} status={status} score={result['match_score']}")
            logger.info(f"[match] matched={matched} parked={parked} skipped={skipped} catalog=v{catalog_version}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"matched": matched, "parked": parked, "skipped": skipped, "catalog_version": catalog_version},
                summary=f"matched {matched} / parked {parked} (catalog v{catalog_version})",
            )
        finally:
            await store.close()


@register_node
class DualDraftNode(BaseNode):
    # issue #588: dual draft — local model (gateway :11432) + cloud GLM-5.
    # Fixed template; lead content wrapped as untrusted JSON data (injection
    # cannot alter the brief); price comes from the catalog quote; output is
    # stored drafts + optional ntfy ping. Never sends anything.
    name = "dual_draft"
    display_name = "双模型草稿"  # i18n: user-facing
    category = NodeCategory.AI_PROCESSING
    description = "matched Lead -> draft_a (local) + draft_b (cloud GLM-5), stored drafts only, never sends"
    icon = "pen"
    default_label = "双草稿"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "local_model": {
                    "type": "string",
                    "description": "local model id (empty = auto-pick chat model)",
                    "default": "",
                },
                "catalog_path": {"type": "string", "description": "catalog JSON path for price lookup", "default": ""},
                "portfolio_links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "sanitized public portfolio links",
                    "default": [],
                },
                "notify": {
                    "type": "boolean",
                    "description": "ntfy ping when drafts ready (FUSION_NTFY_URL)",
                    "default": False,
                },
                "db_path": {"type": "string", "description": "demand SQLite path", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from fusion_cowork.demand.catalog import load_catalog
        from fusion_cowork.demand.draft import CloudGLMClient, dual_draft, notify_ntfy

        params = self.config.params or {}
        portfolio = list(params.get("portfolio_links") or [])
        catalog = load_catalog(params.get("catalog_path", ""))
        price_by_cap = {c["id"]: int(c.get("price_cny", 0)) for c in catalog.get("capabilities", [])}
        local_client = None
        local_model = str(params.get("local_model", ""))
        try:
            from fusion_cowork.ai.mlx_client import FusionMLXClient

            local_client = FusionMLXClient()
            if not local_model:
                local_model = _pick_chat_model(await local_client.list_models())
        except Exception as e:
            logger.warning(f"[dual_draft] local client unavailable: {e}")
            local_client = None
        cloud = CloudGLMClient()
        store = _open_store(params.get("db_path", ""))
        await store.connect()
        try:
            leads = [
                ld
                for status in ("matched", "drafted")  # drafted re-listed for restart-safe skip
                for ld in await store.list_leads(status=status, limit=50)
                if ld.get("catalog_version")
            ]
            drafted = 0
            errors = 0
            skipped = 0
            for lead in leads:
                if lead.get("draft_a") or lead.get("draft_b"):
                    skipped += 1  # already drafted — restart-safe
                    continue
                caps = lead.get("matched_capabilities") or []
                price = next((price_by_cap[c] for c in caps if c in price_by_cap), 0)
                try:
                    result = await dual_draft(
                        lead,
                        local_client if local_client is not None else cloud,
                        local_model,
                        cloud,
                        price_cny=price,
                        portfolio=portfolio,
                    )
                    if not result["draft_a"] and not result["draft_b"]:
                        errors += 1
                        continue
                    await store.update_lead(
                        lead["id"],
                        {
                            "draft_a": result["draft_a"],
                            "draft_b": result["draft_b"],
                            "draft_meta": result["meta"],
                            "status": "drafted",
                        },
                    )
                    drafted += 1
                    if params.get("notify"):
                        await notify_ntfy(
                            f"Drafts ready: {lead.get('title', '')[:60]}",
                            f"lead #{lead['id']} score={lead.get('match_score')} - review in demand inbox",
                        )
                except Exception as e:
                    errors += 1
                    logger.error(f"[dual_draft] lead={lead['id']} failed: {e}")
            logger.info(f"[dual_draft] drafted={drafted} errors={errors} skipped={skipped} cloud={cloud.enabled}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"drafted": drafted, "errors": errors, "skipped": skipped, "cloud_enabled": cloud.enabled},
                summary=f"drafted {drafted} leads (errors {errors}, cloud {'on' if cloud.enabled else 'no key'})",
            )
        finally:
            await store.close()
            try:
                await cloud.close()
            except Exception:
                pass
