# issue #592: normalize_lead model path — duplicate (source, external_id)
# silently dropped raw records. Fixes: deterministic fallback external_id from
# message_id, UNIQUE-collision merge + duplicate marker (raw never re-processes),
# per-lead extraction path recorded (model vs rules).

import json

import pytest

from fusion_cowork.demand.normalize import _auto_external_id, normalize_lead
from fusion_cowork.demand.store import LeadStore
from fusion_cowork.engine.node import NodeConfig, NodeStatus
from fusion_cowork.nodes.demand.demand_nodes import NormalizeLeadNode


class FakeResp:
    def __init__(self, content: str):
        self.content = content


class FakeModel:
    def __init__(self, content: str = ""):
        self.content = content
        self.calls = 0

    async def list_models(self):
        return [{"id": "chat-x"}]

    async def chat(self, model, messages, **kw):
        self.calls += 1
        return FakeResp(self.content)


class NoLiveModel:
    def __init__(self):
        raise RuntimeError("no live model in unit tests")


@pytest.fixture
def no_live_model(monkeypatch):
    import fusion_cowork.ai.mlx_client as mlx_mod

    monkeypatch.setattr(mlx_mod, "FusionMLXClient", NoLiveModel)


MAIL_A = {
    "message_id": "<a1@x>",
    "subject": "Build me a RAG chatbot",
    "from_addr": "client1@upwork.com",
    "body_text": "Need a RAG assistant. Budget $500.",
    "arrived_at": 1764000000.0,
}
MAIL_B = {
    "message_id": "<b2@x>",
    "subject": "Build me a voice agent",
    "from_addr": "client2@upwork.com",
    "body_text": "Need a voice agent. Budget $900.",
    "arrived_at": 1764000100.0,
}

MODEL_PAYLOAD_EMPTY_ID = {
    "source": "upwork",
    "external_id": "",
    "title": "t",
    "description": "d",
    "budget_hint": "",
    "skills": [],
    "client_meta": {},
}


async def test_auto_external_id_deterministic_and_distinct():
    id_a = _auto_external_id(MAIL_A)
    id_b = _auto_external_id(MAIL_B)
    assert id_a != id_b
    assert id_a == _auto_external_id(dict(MAIL_A))  # deterministic per message
    assert id_a.startswith("auto-") and len(id_a) == len("auto-") + 12
    no_msgid = dict(MAIL_A, message_id="")
    assert _auto_external_id(no_msgid).startswith("auto-")


async def test_model_empty_external_id_gets_message_fallback():
    model = FakeModel(json.dumps(MODEL_PAYLOAD_EMPTY_ID))
    lead_a = await normalize_lead(MAIL_A, client=model, model="chat-x")
    lead_b = await normalize_lead(MAIL_B, client=model, model="chat-x")
    assert lead_a["external_id"].startswith("auto-")
    assert lead_a["external_id"] != lead_b["external_id"]
    assert lead_a["extraction"] == "model"


async def test_model_collision_distinct_ids_both_insert(tmp_path):
    # exact repro of issue #592: model returns identical (source, external_id)
    # for two different mails — both must land, no silent per-record error
    db = str(tmp_path / "demand.db")
    payload = {**MODEL_PAYLOAD_EMPTY_ID, "external_id": "SAMEID"}
    model_a = FakeModel(json.dumps({**payload, "description": "need a rag assistant"}))
    model_b = FakeModel(json.dumps({**payload, "description": "need a voice agent"}))
    store = LeadStore(db)
    await store.connect()
    try:
        lead_a = await normalize_lead(MAIL_A, client=model_a, model="chat-x")
        lead_b = await normalize_lead(MAIL_B, client=model_b, model="chat-x")
        r1 = await store.insert_or_merge_lead(lead_a)
        r2 = await store.insert_or_merge_lead(lead_b)
        assert r1["merged"] is False
        assert r2["merged"] is True
        primary = await store.get_lead(r2["id"])
        marker = await store.get_lead(r2["marker_id"])
        assert primary["external_id"] == "SAMEID"
        assert "need a voice agent" in primary["description"]
        assert marker["status"] == "duplicate"
        assert marker["message_id"] == MAIL_B["message_id"]
        assert marker["external_id"] == ""
    finally:
        await store.close()


async def test_merge_fills_budget_and_unions_skills(tmp_path):
    db = str(tmp_path / "demand.db")
    store = LeadStore(db)
    await store.connect()
    try:
        first = {
            "message_id": "<m1@x>",
            "source": "upwork",
            "external_id": "J1",
            "title": "RAG bot",
            "description": "build rag bot",
            "budget_hint": "",
            "skills": ["rag", "python"],
            "client_meta": {},
            "arrived_at": 1.0,
            "status": "normalized",
            "extraction": "model",
        }
        second = {
            "message_id": "<m2@x>",
            "source": "upwork",
            "external_id": "J1",
            "title": "RAG bot again",
            "description": "build rag bot, add voice",
            "budget_hint": "$800",
            "skills": ["rag", "voice agent"],
            "client_meta": {},
            "arrived_at": 2.0,
            "status": "normalized",
            "extraction": "model",
        }
        r1 = await store.insert_or_merge_lead(first)
        r2 = await store.insert_or_merge_lead(second)
        assert r1["merged"] is False and r2["merged"] is True
        merged = await store.get_lead(r2["id"])
        assert merged["budget_hint"] == "$800"  # filled from duplicate
        assert merged["skills"] == ["rag", "python", "voice agent"]
        assert merged["description"] == "build rag bot\n---\nbuild rag bot, add voice"
        # merging identical description again does not duplicate text
        r3 = await store.insert_or_merge_lead({**second, "message_id": "<m3@x>"})
        merged2 = await store.get_lead(r3["id"])
        assert merged2["description"].count("build rag bot, add voice") == 1
    finally:
        await store.close()


async def test_duplicate_marker_excluded_from_match_and_raw_not_wedged(tmp_path):
    db = str(tmp_path / "demand.db")
    store = LeadStore(db)
    await store.connect()
    try:
        await store.upsert_raw(MAIL_A)
        await store.upsert_raw(MAIL_B)
    finally:
        await store.close()
    node = NormalizeLeadNode("n1", NodeConfig(params={"db_path": db}))
    r = await node.execute({})
    assert r.status == NodeStatus.SUCCESS
    assert r.data["errors"] == 0
    assert r.data["normalized"] + r.data["merged"] == 2

    store = LeadStore(db)
    await store.connect()
    try:
        pool = await store.list_for_match()
        assert len(pool) == 2  # duplicate markers excluded from match pool
        pending = await store.list_unnormalized()
        assert pending == []  # raw messages never re-process
    finally:
        await store.close()

    # second cycle: nothing left to do — the wedge from #592 is gone
    r2 = await node.execute({})
    assert r2.data["normalized"] == 0 and r2.data["merged"] == 0 and r2.data["errors"] == 0


async def test_node_model_collision_counts_merged(tmp_path, monkeypatch):
    db = str(tmp_path / "demand.db")
    store = LeadStore(db)
    await store.connect()
    try:
        await store.upsert_raw(MAIL_A)
        await store.upsert_raw(MAIL_B)
    finally:
        await store.close()
    payload = {**MODEL_PAYLOAD_EMPTY_ID, "external_id": "COLL", "description": "same desc"}
    model = FakeModel(json.dumps(payload))

    import fusion_cowork.ai.mlx_client as mlx_mod

    monkeypatch.setattr(mlx_mod, "FusionMLXClient", lambda: model)

    node = NormalizeLeadNode("n1", NodeConfig(params={"db_path": db}))
    r = await node.execute({})
    assert r.status == NodeStatus.SUCCESS
    assert r.data["model_used"] is True
    assert r.data["errors"] == 0
    assert r.data["normalized"] == 1
    assert r.data["merged"] == 1
    assert r.data["by_extraction"]["model"] == 2

    store = LeadStore(db)
    await store.connect()
    try:
        leads = await store.list_leads()
        statuses = sorted(ld["status"] for ld in leads)
        assert statuses == ["duplicate", "normalized"]
    finally:
        await store.close()


async def test_rule_fallback_records_extraction_path(no_live_model):
    lead = await normalize_lead(MAIL_A, client=None, model="")
    assert lead["extraction"] == "rules"
    assert lead["external_id"].startswith("auto-")  # rules found no platform id


async def test_legacy_db_migration_adds_extraction_column(tmp_path):
    # pre-0.5.142 DB: leads table without extraction column must migrate
    db = str(tmp_path / "legacy.db")
    import aiosqlite

    legacy_schema = """
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'unknown',
        external_id TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        budget_hint TEXT NOT NULL DEFAULT '',
        skills TEXT NOT NULL DEFAULT '[]',
        client_meta TEXT NOT NULL DEFAULT '{}',
        arrived_at REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'normalized'
    );
    """
    async with aiosqlite.connect(db) as db_conn:
        await db_conn.executescript(legacy_schema)
        await db_conn.commit()

    store = LeadStore(db)
    await store.connect()
    try:
        lead_id = await store.insert_lead({**json.loads(json.dumps(MODEL_PAYLOAD_EMPTY_ID)), "extraction": "model"})
        lead = await store.get_lead(lead_id)
        assert lead["extraction"] == "model"
    finally:
        await store.close()
