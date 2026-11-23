# issue #586: normalize_lead — golden fixtures per platform, model path +
# rule fallback, spam excluded from matching, node wiring.

import json

import pytest

from fusion_cowork.demand.normalize import _validate_lead, classify_rules, normalize_lead
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
        if self.content == "__raise__":
            raise RuntimeError("model down")
        return FakeResp(self.content)


class NoLiveModel:
    # blocks the node from reaching a live fusion-mlx in unit tests — a running
    # local server must not change rule-fallback test outcomes
    def __init__(self):
        raise RuntimeError("no live model in unit tests")


@pytest.fixture
def no_live_model(monkeypatch):
    import fusion_cowork.ai.mlx_client as mlx_mod

    monkeypatch.setattr(mlx_mod, "FusionMLXClient", NoLiveModel)


UPWORK_GOLDEN = {
    "message_id": "<u1@upwork>",
    "subject": "Need RAG chatbot over company docs",
    "from_addr": "hiring@client.upwork.com",
    "body_text": "We need a RAG knowledge base assistant over our documents. Job #: ~AB12CD34. "
    "Budget: $500-$1000. Skills: python, llm, embeddings. Payment verified.",
    "arrived_at": 1764000000.0,
}
FIVERR_GOLDEN = {
    "message_id": "<f1@fiverr>",
    "subject": "Order: build n8n automation workflow",
    "from_addr": "orders@fiverr.com",
    "body_text": "Please build a workflow automation pipeline connecting our forms to the CRM.",
    "arrived_at": 1764000100.0,
}
SPAM_GOLDEN = {
    "message_id": "<sp1@spam>",
    "subject": "Congratulations",
    "from_addr": "promo@unknown.biz",
    "body_text": "You won the lottery! Click to claim. To unsubscribe click here.",
    "arrived_at": 1764000200.0,
}


def test_classify_rules_upwork_golden():
    lead = classify_rules(UPWORK_GOLDEN["subject"], UPWORK_GOLDEN["from_addr"], UPWORK_GOLDEN["body_text"])
    assert lead["source"] == "upwork"
    assert lead["external_id"] == "AB12CD34"
    assert "$500-$1000" in lead["budget_hint"]
    assert "rag" in lead["skills"]
    assert lead["title"] == UPWORK_GOLDEN["subject"]  # subject fallback title
    assert lead["client_meta"] == {}


def test_classify_rules_fiverr_golden():
    lead = classify_rules(FIVERR_GOLDEN["subject"], FIVERR_GOLDEN["from_addr"], FIVERR_GOLDEN["body_text"])
    assert lead["source"] == "fiverr"
    assert "workflow" in lead["skills"] or "workflow automation" in lead["skills"]


def test_classify_rules_spam_detected():
    lead = classify_rules(SPAM_GOLDEN["subject"], SPAM_GOLDEN["from_addr"], SPAM_GOLDEN["body_text"])
    assert lead["source"] == "spam"


def test_classify_rules_direct_default():
    lead = classify_rules("custom project", "boss@acme.cn", "please build an internal tool")
    assert lead["source"] == "direct"


def test_validate_lead_rejects_bad_source():
    assert _validate_lead({"source": "linkedin"}, UPWORK_GOLDEN) is None
    ok = _validate_lead({"source": "Upwork", "skills": "notalist"}, UPWORK_GOLDEN)
    assert ok["source"] == "upwork"
    assert ok["skills"] == []  # non-list coerced empty
    assert ok["status"] == "normalized"


async def test_normalize_model_path():
    payload = {
        "source": "upwork",
        "external_id": "ZZ99",
        "title": "RAG bot",
        "description": "build rag",
        "budget_hint": "$800",
        "skills": ["rag", "python"],
        "client_meta": {"payment_verified": "true"},
    }
    model = FakeModel(json.dumps(payload) + "```")  # model wraps JSON in prose/fence
    lead = await normalize_lead(UPWORK_GOLDEN, client=model, model="chat-x")
    assert lead["source"] == "upwork"
    assert lead["external_id"] == "ZZ99"
    assert lead["skills"] == ["rag", "python"]
    assert model.calls == 1


async def test_normalize_model_garbage_falls_back():
    model = FakeModel("not json at all")
    lead = await normalize_lead(UPWORK_GOLDEN, client=model, model="chat-x")
    assert lead["source"] == "upwork"  # rule fallback still classifies
    assert lead["external_id"] == "AB12CD34"


async def test_normalize_model_down_falls_back():
    model = FakeModel("__raise__")
    lead = await normalize_lead(UPWORK_GOLDEN, client=model, model="chat-x")
    assert lead["source"] == "upwork"
    assert lead["status"] == "normalized"


async def test_normalize_no_client_rules_only():
    lead = await normalize_lead(SPAM_GOLDEN, client=None, model="")
    assert lead["source"] == "spam"
    assert lead["status"] == "spam"


async def _seed(store, records):
    await store.connect()
    for r in records:
        await store.upsert_raw(r)
    await store.close()


async def test_node_normalizes_and_marks_spam(tmp_path, no_live_model):
    db = str(tmp_path / "demand.db")
    await _seed(LeadStore(db), [UPWORK_GOLDEN, FIVERR_GOLDEN, SPAM_GOLDEN])
    node = NormalizeLeadNode("n1", NodeConfig(params={"db_path": db}))
    r = await node.execute({})
    assert r.status == NodeStatus.SUCCESS
    assert r.data["normalized"] == 3
    assert r.data["spam"] == 1
    assert r.data["model_used"] is False  # no model in test env -> rule fallback

    store = LeadStore(db)
    await store.connect()
    matched_pool = await store.list_for_match()
    await store.close()
    assert len(matched_pool) == 2  # spam excluded
    assert all(ld["message_id"] != SPAM_GOLDEN["message_id"] for ld in matched_pool)

    # already normalized -> second run no-ops
    r2 = await node.execute({})
    assert r2.data["normalized"] == 0


async def test_node_uses_model_client(tmp_path, monkeypatch):
    db = str(tmp_path / "demand.db")
    await _seed(LeadStore(db), [UPWORK_GOLDEN])
    payload = {
        "source": "upwork",
        "external_id": "MM11",
        "title": "t",
        "description": "d",
        "budget_hint": "",
        "skills": [],
        "client_meta": {},
    }
    model = FakeModel(json.dumps(payload))

    import fusion_cowork.ai.mlx_client as mlx_mod
    import fusion_cowork.nodes.demand.demand_nodes as dn_mod

    monkeypatch.setattr(dn_mod, "_pick_chat_model", lambda client: "chat-x")
    monkeypatch.setattr(mlx_mod, "FusionMLXClient", lambda: model)

    node = NormalizeLeadNode("n1", NodeConfig(params={"db_path": db}))
    r = await node.execute({})
    assert r.data["model_used"] is True
    assert model.calls == 1
    store = LeadStore(db)
    await store.connect()
    leads = await store.list_for_match()
    await store.close()
    assert leads[0]["external_id"] == "MM11"


async def test_node_record_error_counted(tmp_path, monkeypatch, no_live_model):
    db = str(tmp_path / "demand.db")
    await _seed(LeadStore(db), [UPWORK_GOLDEN])
    import fusion_cowork.demand.normalize as nm

    async def boom(record, client=None, model=""):
        raise ValueError("bad record")

    monkeypatch.setattr(nm, "normalize_lead", boom)
    node = NormalizeLeadNode("n1", NodeConfig(params={"db_path": db}))
    r = await node.execute({})
    assert r.status == NodeStatus.SUCCESS
    assert r.data["errors"] == 1  # per-record tolerance, node does not fail
