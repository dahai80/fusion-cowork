# issue #587: capability catalog match — pure-code scoring matrix (hit /
# borderline / miss / multi-capability), catalog version recorded, backlog
# re-score skips already-scored leads, no network anywhere.

import json

import pytest

from fusion_cowork.demand.catalog import load_catalog, match_lead, score_lead
from fusion_cowork.demand.store import LeadStore
from fusion_cowork.engine.node import NodeConfig, NodeStatus
from fusion_cowork.nodes.demand.demand_nodes import MatchLeadNode


@pytest.fixture
def cat(tmp_path):
    cat_data = {
        "version": "7",
        "capabilities": [
            {
                "id": "rag-qa",
                "enabled": True,
                "name_en": "RAG Q&A",
                "price_cny": 2000,
                "budget_min_cny": 1000,
                "budget_max_cny": 8000,
                "keywords": ["rag", "knowledge base", "retrieval"],
            },
            {
                "id": "dag-workflow",
                "enabled": True,
                "name_en": "DAG workflow",
                "price_cny": 2500,
                "budget_min_cny": 1000,
                "budget_max_cny": 10000,
                "keywords": ["workflow", "n8n", "automation"],
            },
            {
                "id": "video-pipeline",
                "enabled": False,  # disabled must never score
                "name_en": "Video",
                "price_cny": 4000,
                "budget_min_cny": 100,
                "budget_max_cny": 100,
                "keywords": ["video"],
            },
        ],
    }
    p = tmp_path / "cat.json"
    p.write_text(json.dumps(cat_data), encoding="utf-8")
    return cat_data


def test_score_hit_single_keyword(cat):
    lead = {"title": "Build a RAG chatbot", "description": "", "skills": [], "budget_hint": ""}
    scored = score_lead(lead, cat)
    ids = [s["id"] for s in scored]
    assert "rag-qa" in ids
    assert "video-pipeline" not in ids  # disabled excluded
    top = scored[0]
    assert top["score"] >= 2.0
    assert top["price_cny"] == 2000
    assert "rag" in top["reason"]


def test_score_skill_overlap_and_multi_capability(cat):
    lead = {
        "title": "Automation agent",
        "description": "n8n workflow plus a RAG knowledge base",
        "skills": ["rag", "n8n"],
        "budget_hint": "",
    }
    scored = score_lead(lead, cat)
    ids = [s["id"] for s in scored]
    assert "rag-qa" in ids and "dag-workflow" in ids
    rag = next(s for s in scored if s["id"] == "rag-qa")
    assert rag["score"] > 4.0  # 2 keywords + 1 skill overlap
    assert scored[0]["score"] >= scored[-1]["score"]  # sorted desc


def test_score_budget_in_range_bonus_and_out_of_range_penalty(cat):
    in_range = score_lead(
        {"title": "RAG retrieval helper", "description": "", "skills": [], "budget_hint": "$2000"},
        cat,
    )
    out_range = score_lead(
        {"title": "RAG retrieval helper", "description": "", "skills": [], "budget_hint": "$100000"},
        cat,
    )
    rag_in = next(s for s in in_range if s["id"] == "rag-qa")
    rag_out = next(s for s in out_range if s["id"] == "rag-qa")
    assert rag_in["score"] > rag_out["score"]
    assert rag_in["score"] >= 4.0  # keyword 2.0 + budget 2.0
    assert "in range" in rag_in["reason"]


def test_score_miss_empty(cat):
    scored = score_lead({"title": "logo design", "description": "draw a logo", "skills": [], "budget_hint": ""}, cat)
    assert scored == []


def test_match_lead_hit(cat):
    result = match_lead(
        {"title": "RAG system", "description": "", "skills": ["rag"], "budget_hint": "$3000"},
        cat,
        threshold=3.0,
    )
    assert result["parked"] is False
    assert result["matched_capabilities"] == ["rag-qa"]
    assert result["catalog_version"] == "7"
    assert result["match_score"] >= 3.0
    assert result["price_cny"] == 2000


def test_match_lead_borderline_parked_not_dropped(cat):
    # single weak keyword (2.0) below threshold 3.0 -> parked with score kept
    result = match_lead(
        {"title": "mention retrieval once", "description": "", "skills": [], "budget_hint": ""},
        cat,
        threshold=3.0,
    )
    assert result["parked"] is True
    assert result["matched_capabilities"] == []
    assert result["match_score"] == 2.0  # best candidate kept for review
    assert result["catalog_version"] == "7"


def test_match_lead_total_miss(cat):
    result = match_lead({"title": "x", "description": "", "skills": [], "budget_hint": ""}, cat)
    assert result["parked"] is True
    assert result["match_score"] == 0.0
    assert result["match_reason"] == "no keyword/skill overlap"


def test_load_catalog_requires_version(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"capabilities": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(str(p))


async def _insert_leads(db, leads):
    store = LeadStore(db)
    await store.connect()
    for ld in leads:
        await store.insert_lead(ld)
    await store.close()


async def test_node_match_updates_statuses_and_records_version(tmp_path, cat):
    db = str(tmp_path / "demand.db")
    cat_path = str(tmp_path / "cat.json")
    await _insert_leads(
        db,
        [
            {
                "message_id": "m1",
                "source": "upwork",
                "title": "RAG chatbot",
                "description": "knowledge base over docs",
                "status": "normalized",
            },
            {
                "message_id": "m2",
                "source": "direct",
                "title": "logo design",
                "description": "draw a logo",
                "status": "normalized",
            },
        ],
    )
    node = MatchLeadNode("n1", NodeConfig(params={"db_path": db, "catalog_path": cat_path, "threshold": 3.0}))
    r = await node.execute({})
    assert r.status == NodeStatus.SUCCESS
    assert r.data["matched"] == 1
    assert r.data["parked"] == 1
    assert r.data["catalog_version"] == "7"

    store = LeadStore(db)
    await store.connect()
    leads = {ld["message_id"]: ld for ld in await store.list_leads()}
    await store.close()
    assert leads["m1"]["status"] == "matched"
    assert leads["m1"]["catalog_version"] == "7"
    assert leads["m1"]["matched_capabilities"] == ["rag-qa"]
    assert leads["m2"]["status"] == "parked"  # parked, not deleted
    assert leads["m1"]["budget_hint"] == "2000 CNY"  # catalog price filled

    # backlog re-score: nothing new -> all skipped (fast, no rescoring)
    r2 = await node.execute({})
    assert r2.data["skipped"] == 2
    assert r2.data["matched"] == 0


async def test_node_rescores_when_catalog_version_changes(tmp_path):
    db = str(tmp_path / "demand.db")
    cat_path = tmp_path / "cat.json"
    cat_path.write_text(json.dumps({"version": "1", "capabilities": []}), encoding="utf-8")
    await _insert_leads(
        db, [{"message_id": "m1", "source": "direct", "title": "RAG bot", "description": "rag", "status": "normalized"}]
    )
    node = MatchLeadNode("n1", NodeConfig(params={"db_path": db, "catalog_path": str(cat_path)}))
    await node.execute({})
    # operator edits catalog + bumps version -> backlog re-scored
    cat_path.write_text(json.dumps({"version": "2", "capabilities": []}), encoding="utf-8")
    r2 = await node.execute({})
    assert r2.data["skipped"] == 0
    assert r2.data["parked"] == 1
    store = LeadStore(db)
    await store.connect()
    lead = (await store.list_leads())[0]
    await store.close()
    assert lead["catalog_version"] == "2"


async def test_node_spam_never_scored(tmp_path, cat):
    db = str(tmp_path / "demand.db")
    await _insert_leads(
        db, [{"message_id": "m1", "source": "spam", "title": "rag", "description": "rag rag", "status": "spam"}]
    )
    node = MatchLeadNode("n1", NodeConfig(params={"db_path": db, "catalog_path": str(tmp_path / "cat.json")}))
    r = await node.execute({})
    assert r.data["matched"] == 0 and r.data["parked"] == 0
