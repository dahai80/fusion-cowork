# issue #597: delivery rail — order store state machine (no-skip ladder,
# idempotent create/advance), repo scaffold idempotency via a fake gh,
# SOW draft parks on the human gate, invite tokens (hashed, 72h, single-use),
# issue sync + SLA overdue scan, tamper-evident audit chain.

import asyncio

import pytest


@pytest.fixture
def stores(tmp_path):
    from fusion_cowork.delivery.store import OrderStore
    from fusion_cowork.demand.store import LeadStore

    async def seed():
        demand = LeadStore(str(tmp_path / "demand.db"))
        await demand.connect()
        lead_id = await demand.insert_lead(
            {
                "message_id": "m-d597",
                "source": "qq_imap",
                "title": "RAG delivery",
                "status": "approved",
                "matched_capabilities": ["rag-qa"],
            }
        )
        await demand.update_lead(lead_id, {"draft_pick": "a", "status": "approved"})
        await demand.close()
        orders = OrderStore(str(tmp_path / "orders.db"))
        await orders.connect()
        return lead_id, orders

    lead_id, store = asyncio.run(seed())
    try:
        yield lead_id, store, tmp_path
    finally:
        asyncio.run(store.close())


class TestOrderStateMachine:
    def test_create_order_idempotent_and_sequence(self, stores):
        lead_id, store, _ = stores
        o1 = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG delivery", 2000))
        o2 = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG delivery", 2000))
        assert o1["order_no"] == o2["order_no"]
        assert o1["order_no"].startswith("ORD-")
        # same-year sequence increments for a different lead
        other = asyncio.run(store.create_order(lead_id + 100, "dag-workflow", "other", 0))
        assert other["order_no"] != o1["order_no"]

    def test_stage_ladder_no_skip(self, stores):
        lead_id, store, _ = stores
        o = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG delivery", 2000))
        assert o["stage"] == "created"
        o = asyncio.run(store.advance_stage(o["order_no"], "draft_sow", "SOW text"))
        assert o["stage"] == "sow_draft"
        o = asyncio.run(store.advance_stage(o["order_no"]))
        assert o["stage"] == "sow_gate"
        # ladder is strictly linear: each advance moves exactly one stage
        for expected in ("tad_draft", "tad_gate", "test_report", "acceptance", "closed"):
            o = asyncio.run(store.advance_stage(o["order_no"]))
            assert o["stage"] == expected
        # closed is terminal — advance is a no-op, never wraps
        o = asyncio.run(store.advance_stage(o["order_no"]))
        assert o["stage"] == "closed"

    def test_advance_unknown_order_raises(self, stores):
        _, store, _ = stores
        with pytest.raises(ValueError):
            asyncio.run(store.advance_stage("ORD-9999-001"))


class TestRepoScaffold:
    def test_scaffold_idempotent_via_fake_gh(self, stores, tmp_path, monkeypatch):
        lead_id, store, tmp = stores
        order = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG delivery", 2000))
        import fusion_cowork.delivery.nodes as dn

        calls = []

        async def fake_run_gh(args, timeout=30):
            calls.append(args)
            if args[:2] == ["repo", "view"]:
                return {"ok": len(calls) > 2, "code": 0 if len(calls) > 2 else 1, "out": "", "error": "not found"}
            return {"ok": True, "code": 0, "out": "", "error": ""}

        monkeypatch.setattr(dn, "_run_gh", fake_run_gh)
        from fusion_cowork.engine.node import NodeConfig

        node = dn.ScaffoldRepoNode(
            "n1", NodeConfig(params={"order_no": order["order_no"], "db_path": str(tmp / "orders.db")})
        )
        r1 = asyncio.run(node.execute({}))
        assert r1.status.name == "SUCCESS"
        r2 = asyncio.run(node.execute({}))
        assert r2.status.name == "SUCCESS"
        assert "repo_created" in r1.data["actions"]
        assert r2.data["actions"] == ["repo_existed"]  # second run skips create
        detail = asyncio.run(store.get_order(order["order_no"]))
        assert detail["repo"].startswith("dahai80-business/ord-")

    def test_scaffold_create_failure_422_race_is_soft(self, stores, monkeypatch):
        lead_id, store, tmp = stores
        order = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG", 0))
        import fusion_cowork.delivery.nodes as dn
        from fusion_cowork.engine.node import NodeConfig

        async def fake_run_gh(args, timeout=30):
            if args[:2] == ["repo", "view"]:
                return {"ok": False, "code": 1, "out": "", "error": "not found"}
            return {"ok": False, "code": 422, "out": "", "error": "HTTP 422: name already exists on this account"}

        monkeypatch.setattr(dn, "_run_gh", fake_run_gh)
        node = dn.ScaffoldRepoNode(
            "n1", NodeConfig(params={"order_no": order["order_no"], "db_path": str(tmp / "orders.db")})
        )
        r = asyncio.run(node.execute({}))
        assert r.status.name == "SUCCESS"  # exists-race tolerated, not a failure

    def test_gh_token_never_in_payload(self, stores, monkeypatch):
        # node results and audit detail never carry credentials
        lead_id, store, tmp = stores
        order = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG", 0))
        import fusion_cowork.delivery.nodes as dn
        from fusion_cowork.engine.node import NodeConfig

        async def fake_run_gh(args, timeout=30):
            return {"ok": True, "code": 0, "out": "", "error": ""}

        monkeypatch.setattr(dn, "_run_gh", fake_run_gh)
        monkeypatch.setenv("GH_TOKEN", "ghp_supersecret")
        node = dn.ScaffoldRepoNode(
            "n1", NodeConfig(params={"order_no": order["order_no"], "db_path": str(tmp / "orders.db")})
        )
        r = asyncio.run(node.execute({}))
        assert "ghp_supersecret" not in str(r.data)
        detail = asyncio.run(store.get_order(order["order_no"]))
        assert "ghp_supersecret" not in str(detail)


class TestSowDraftGate:
    def test_draft_sow_parks_on_gate(self, stores):
        lead_id, store, tmp = stores
        order = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG delivery", 2000))
        import fusion_cowork.delivery.nodes as dn
        from fusion_cowork.engine.node import NodeConfig

        node = dn.DraftSowNode(
            "n1", NodeConfig(params={"order_no": order["order_no"], "db_path": str(tmp / "orders.db")})
        )
        r = asyncio.run(node.execute({}))
        assert r.status.name == "SUCCESS"
        assert r.data["stage"] == "sow_draft"
        detail = asyncio.run(store.get_order(order["order_no"]))
        assert "DRAFT" in detail["draft_sow"]
        assert detail["stage"] == "sow_draft"
        # re-run past the draft stage is a no-op (no re-draft past the gate)
        asyncio.run(store.advance_stage(order["order_no"]))
        r2 = asyncio.run(node.execute({}))
        assert r2.data.get("skipped") is True


class TestInviteTokens:
    def test_invite_hashed_single_use_expiry(self, stores):
        lead_id, store, _ = stores
        order = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG", 0))
        token = asyncio.run(store.create_invite(order["order_no"]))
        assert token
        # storage holds the hash only
        import hashlib

        async def _stored_hash():
            async with store.db().execute("SELECT token_hash FROM invite_tokens") as cur:
                return (await cur.fetchone())[0]

        stored = asyncio.run(_stored_hash())
        assert stored == hashlib.sha256(token.encode()).hexdigest()
        assert token not in stored
        # consume once works, twice fails
        assert asyncio.run(store.consume_invite(token)) == order["order_no"]
        assert asyncio.run(store.consume_invite(token)) is None

    def test_invite_expired_rejected(self, stores):
        lead_id, store, _ = stores
        order = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG", 0))
        token = asyncio.run(store.create_invite(order["order_no"], ttl_hours=72))
        # backdate expiry
        import hashlib

        async def _backdate():
            await store.db().execute(
                "UPDATE invite_tokens SET expires_at=? WHERE token_hash=?",
                (0, hashlib.sha256(token.encode()).hexdigest()),
            )
            await store.db().commit()

        asyncio.run(_backdate())
        assert asyncio.run(store.consume_invite(token)) is None


class TestIssueSyncSla:
    def test_sla_overdue_flagged_within_one_cycle(self, stores):
        lead_id, store, _ = stores
        import time as _t

        # sla-24h issue created 25h ago -> overdue on the first scan
        asyncio.run(
            store.upsert_issue(
                "dahai80-business/ord-x-rag-qa",
                {
                    "issue_id": 1,
                    "title": "bug",
                    "labels": ["bug", "sla-24h"],
                    "state": "open",
                    "created_at": _t.time() - 25 * 3600,
                },
            )
        )
        # fresh sla-24h issue -> not overdue
        asyncio.run(
            store.upsert_issue(
                "dahai80-business/ord-x-rag-qa",
                {
                    "issue_id": 2,
                    "title": "q",
                    "labels": ["question", "sla-24h"],
                    "state": "open",
                    "created_at": _t.time(),
                },
            )
        )
        overdue = asyncio.run(store.scan_sla_overdue())
        assert [o["issue_id"] for o in overdue] == [1]
        # flagged once — second scan does not re-notify
        assert asyncio.run(store.scan_sla_overdue()) == []


class TestAuditChain:
    def test_audit_chain_tamper_evident(self, stores):
        lead_id, store, _ = stores
        o = asyncio.run(store.create_order(lead_id, "rag-qa", "RAG", 0))
        asyncio.run(store.advance_stage(o["order_no"]))
        asyncio.run(store.audit(o["order_no"], "manual_event", {"x": 1}))
        assert asyncio.run(store.audit_verify()) is True

        # tamper with a detail -> chain breaks
        async def _tamper():
            await store.db().execute("UPDATE delivery_audit SET detail='{\"x\":2}' WHERE id=2")
            await store.db().commit()

        asyncio.run(_tamper())
        assert asyncio.run(store.audit_verify()) is False
