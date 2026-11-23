#!/usr/bin/env python3
# Local end-to-end driver for the demand-engine M1 mail rail.
# Synthetic mailbox (in-process fake IMAP) -> poll -> normalize -> match ->
# dual_draft -> SQLite. Drafts are stored proposals only, never sent.
#
# Usage:
#   python scripts/demand_e2e_local.py [--db PATH] [--keep]
# Env:
#   FUSION_MLX_HOST/FUSION_MLX_PORT  local model endpoint (default 127.0.0.1:11434)
#   FUSION_GLM_API_KEY               optional, enables cloud draft_b (GLM-5)
#
# Exit codes: 0 = pipeline green, 1 = a node failed.

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("demand_e2e")

# All LLM traffic routes through fusion-gateway :11432 (ruling 2026-11-23):
# the FusionMLXClient default already points there and the demo key allowlist
# covers qwen* (local arm) + glm* (cloud arm). Set FUSION_MLX_URL to override.
_endpoint = os.environ.get("FUSION_MLX_URL", "").strip() or "http://127.0.0.1:11432/v1"
logger.info("model endpoint (both draft arms): %s", _endpoint)
if not os.environ.get("FUSION_CLIENT_KEY", ""):
    logger.warning("FUSION_CLIENT_KEY not set: model calls will 401")
# Fake mailbox creds: poll node refuses to run without them; values unused offline
os.environ.setdefault("FUSION_QQ_IMAP_USER", "e2e@local.test")
os.environ.setdefault("FUSION_QQ_IMAP_PASSWORD", "e2e-fake-authcode")

SYNTHETIC_MAILS = [
    # hit: RAG inquiry with budget + skills
    (
        101,
        "Message-ID: <e2e-rag-1@local.test>\r\n"
        "Subject: Need RAG chatbot over company docs\r\n"
        "From: hiring@client.upwork.com\r\n"
        "Date: Sun, 23 Nov 2026 08:00:00 +0800\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        "We need a RAG knowledge base assistant over our internal documents. "
        "Job #: ~AB12CD34. Budget: $500-$1000. Skills: python, llm, embeddings. Payment verified.",
    ),
    # spam: excluded from matching, never dropped
    (
        102,
        "Message-ID: <e2e-spam-1@local.test>\r\n"
        "Subject: Congratulations you won\r\n"
        "From: promo@unknown.biz\r\n"
        "Date: Sun, 23 Nov 2026 08:05:00 +0800\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        "You won the lottery! Click to claim. To unsubscribe click here.",
    ),
    # non-lead: direct mail that scores below match threshold -> parked
    (
        103,
        "Message-ID: <e2e-chat-1@local.test>\r\n"
        "Subject: Quick question about your blog post\r\n"
        "From: reader@example.org\r\n"
        "Date: Sun, 23 Nov 2026 08:10:00 +0800\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        "Hi, enjoyed your article on vector databases. Do you have reading recommendations?",
    ),
]


class FakeIMAP4SSL:
    # minimal IMAP4_SSL stand-in: mailbox served from SYNTHETIC_MAILS
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.closed = False

    def login(self, user: str, password: str):
        return ("OK", [b"Logged in"])

    def select(self, folder: str, readonly: bool = False):
        assert readonly is True, "poller must be read-only"
        return ("OK", [b"1"])

    def uid(self, command: str, *args):
        if command == "search":
            rng = args[-1]
            last = int(rng.split(":")[0].split()[-1])
            uids = [u for u, _ in SYNTHETIC_MAILS if u > last]
            return ("OK", [b" ".join(str(u).encode() for u in uids)])
        if command == "fetch":
            uid = int(args[0])
            raw = next((raw for u, raw in SYNTHETIC_MAILS if u == uid), None)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if raw is None:
                return ("OK", [None])
            return ("OK", [(b"1 (UID %d BODY[] {%d}" % (uid, len(raw)), raw), b")"])
        return ("BAD", [b"unknown"])

    def logout(self):
        self.closed = True
        return ("BYE", [b""])


def patch_fake_imap() -> None:
    import fusion_cowork.demand.imap_poll as mod

    mod.IMAP4_SSL = FakeIMAP4SSL
    logger.info("imap backend patched: in-process fake mailbox (%d mails)", len(SYNTHETIC_MAILS))


async def run_node(name: str, node, inputs: dict) -> dict:
    result = await node.execute(inputs)
    data = dict(result.data or {})
    if result.status.name != "SUCCESS":
        logger.error("%s FAILED: %s", name, result.error)
        sys.exit(1)
    logger.info("%s ok: %s | %s", name, result.summary, json.dumps(data, ensure_ascii=False))
    return data


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="demand SQLite path (default: temp dir)")
    ap.add_argument("--keep", action="store_true", help="keep temp dir after run")
    args = ap.parse_args()

    tmp_dir = None
    if args.db:
        db_path = args.db
    else:
        tmp_dir = tempfile.mkdtemp(prefix="demand-e2e-")
        db_path = os.path.join(tmp_dir, "demand.db")
    logger.info("db: %s", db_path)

    patch_fake_imap()

    from fusion_cowork.engine.node import NodeConfig
    from fusion_cowork.nodes.demand.demand_nodes import (
        DualDraftNode,
        MatchLeadNode,
        NormalizeLeadNode,
        PollQqImapNode,
    )

    poll = await run_node("poll", PollQqImapNode("n1", NodeConfig(params={"db_path": db_path})), {})
    assert poll["new"] == 3, f"expected 3 new mails, got {poll['new']}"

    norm = await run_node("normalize", NormalizeLeadNode("n2", NodeConfig(params={"db_path": db_path})), {})
    logger.info(
        "normalize: normalized=%s spam=%s errors=%s model_used=%s",
        norm.get("normalized"),
        norm.get("spam"),
        norm.get("errors"),
        norm.get("model_used"),
    )

    await run_node("match", MatchLeadNode("n3", NodeConfig(params={"db_path": db_path})), {})

    draft = await run_node(
        "draft",
        DualDraftNode(
            "n4",
            NodeConfig(
                params={
                    "db_path": db_path,
                    "catalog_path": "",  # seed catalog
                    "local_model": os.environ.get("FUSION_E2E_LOCAL_MODEL", "Qwen3.8-27B-4bit-mtp"),
                    "portfolio_links": ["https://folio.example/p1"],
                }
            ),
        ),
        {},
    )
    cloud_on = bool(os.environ.get("FUSION_GLM_API_KEY")) or draft.get("cloud_enabled", False)
    logger.info(
        "draft: drafted=%s (cloud arm %s)",
        draft.get("drafted"),
        "GLM-5 via gateway" if cloud_on else "OFF: no FUSION_CLIENT_KEY/FUSION_GLM_API_KEY, draft_b stays empty",
    )

    from fusion_cowork.demand.store import LeadStore

    store = LeadStore(db_path)
    await store.connect()
    try:
        leads = await store.list_leads("")
    finally:
        await store.close()

    print("\n=== LEADS ===")
    for lead in leads:
        print(f"\n[{lead['id']}] {lead['source']} / {lead['status']} / score={lead.get('match_score')}")
        print(f"  title: {lead['title']}")
        print(f"  match: {lead.get('matched_capabilities')} ({lead.get('catalog_version')}) {lead.get('match_reason')}")
        if lead.get("draft_a") or lead.get("draft_b"):
            print("  --- draft_a ---")
            print(lead.get("draft_a") or "(empty)")
            print("  --- draft_b ---")
            print(lead.get("draft_b") or "(empty: cloud arm disabled — no gateway/GLM key)")
            meta = lead.get("draft_meta") or {}
            print(f"  meta: model_a={meta.get('model_a')} model_b={meta.get('model_b')} errors={meta.get('errors')}")

    ok = sum(1 for lead in leads if lead["status"] in ("drafted", "parked", "spam"))
    print(f"\n=== RESULT: {ok}/{len(leads)} leads terminal, db={db_path} ===")
    if tmp_dir:
        if args.keep:
            logger.info("temp dir kept for inspection: %s (remove with rm -rf)", tmp_dir)
        else:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("temp dir cleaned: %s", tmp_dir)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
