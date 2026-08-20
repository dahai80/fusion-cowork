from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fusion_cowork.research.deep_research import (
    ResearchFinding,
    ResearchReport,
    _fallback_synthesis,
    _parse_subquestions,
    run_deep_research,
)


def _llm_resp(content):
    r = MagicMock()
    r.content = content
    return r


def _mock_search_html():
    return (
        '<a class="result-link" href="https://example.com/a">结果 A</a>'
        '<td class="result-snippet">摘要 A</td>'
        '<a class="result-link" href="https://example.com/b">结果 B</a>'
        '<td class="result-snippet">摘要 B</td>'
    )


def _mock_fetch_text(url):
    return f"<html><body>正文内容 {url}</body></html>"


def test_parse_subquestions_json():
    raw = '[{"question": "什么是 X", "rationale": "基础"}, {"question": "X 如何工作"}]'
    subs = _parse_subquestions(raw)
    assert len(subs) == 2
    assert subs[0].question == "什么是 X"
    assert subs[0].rationale == "基础"
    assert subs[1].question == "X 如何工作"


def test_parse_subquestions_markdown_wrapped():
    raw = '```json\n[{"question": "q1"}]\n```'
    subs = _parse_subquestions(raw)
    assert len(subs) == 1
    assert subs[0].question == "q1"


def test_parse_subquestions_invalid():
    assert _parse_subquestions("not json at all") == []
    assert _parse_subquestions("[") == []


def test_fallback_synthesis():
    findings = [
        ResearchFinding(sub_question="q1", title="T1", url="https://a.com", snippet="S1"),
        ResearchFinding(sub_question="q1", title="T2", url="https://b.com", snippet=""),
    ]
    md = _fallback_synthesis("主问题", findings)
    assert "主问题" in md
    assert "降级模式" in md
    assert "T1" in md
    assert "https://a.com" in md
    assert "q1" in md


def test_run_deep_research_full_flow():
    client = MagicMock()
    client.chat = AsyncMock(
        side_effect=[
            _llm_resp('[{"question": "子问题1", "rationale": "why"}]'),
            _llm_resp("这是合成报告 [1] 结论。"),
        ]
    )
    client.list_models = AsyncMock(return_value=[{"id": "test-model"}])

    with patch("fusion_cowork.research.deep_research.httpx.AsyncClient") as mock_cls:
        client_http = MagicMock()
        search_resp = MagicMock()
        search_resp.text = _mock_search_html()
        search_resp.raise_for_status = MagicMock()
        fetch_resp = MagicMock()
        fetch_resp.text = _mock_fetch_text("u")
        fetch_resp.headers = {"content-type": "text/html"}
        fetch_resp.raise_for_status = MagicMock()
        client_http.get = AsyncMock(side_effect=[search_resp, fetch_resp, fetch_resp])
        client_http.__aenter__ = AsyncMock(return_value=client_http)
        client_http.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = client_http

        report = asyncio.run(run_deep_research("研究主题", depth=1, max_sources=2, mlx_client=client))

    assert isinstance(report, ResearchReport)
    assert report.question == "研究主题"
    assert len(report.sub_questions) == 1
    assert report.sub_questions[0].question == "子问题1"
    assert report.sources_used == 2
    assert report.degraded is False
    assert "合成报告" in report.synthesis
    assert report.error == ""


def test_run_deep_research_llm_fail_degrades():
    client = MagicMock()
    client.chat = AsyncMock(side_effect=ConnectionError("no llm"))
    client.list_models = AsyncMock(return_value=[])

    with patch("fusion_cowork.research.deep_research.httpx.AsyncClient") as mock_cls:
        client_http = MagicMock()
        search_resp = MagicMock()
        search_resp.text = _mock_search_html()
        search_resp.raise_for_status = MagicMock()
        client_http.get = AsyncMock(return_value=search_resp)
        client_http.__aenter__ = AsyncMock(return_value=client_http)
        client_http.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = client_http

        report = asyncio.run(run_deep_research("主题", depth=2, max_sources=1, mlx_client=client))

    assert report.degraded is True
    assert len(report.sub_questions) == 1
    assert report.sub_questions[0].question == "主题"
    assert "降级模式" in report.synthesis


def test_run_deep_research_empty_question():
    report = asyncio.run(run_deep_research(""))
    assert report.error != ""
    assert "不能为空" in report.error
