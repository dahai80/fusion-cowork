"""Deep research - P2-6.

Multi-agent orchestration:
  1. Plan: LLM decomposes the question into N sub-questions
  2. Search: for each sub-question, parallel web search (DuckDuckGo Lite) + fetch summaries
  3. Synthesize: LLM aggregates into a cited Markdown report

Degraded when LLM unavailable: skip planning (search the original question directly)
+ skip synthesis (return raw findings).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 10.0
_FETCH_TIMEOUT = 15.0
_MAX_FETCH_CHARS = 4000
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0"
_LINK_RE = re.compile(r'<a[^>]+class="result-link"[^>]+href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_SNIPPET_RE = re.compile(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)


@dataclass
class ResearchSubQuestion:
    question: str
    rationale: str = ""


@dataclass
class ResearchFinding:
    sub_question: str
    title: str
    url: str
    snippet: str = ""


@dataclass
class ResearchReport:
    question: str
    sub_questions: List[ResearchSubQuestion] = field(default_factory=list)
    findings: List[ResearchFinding] = field(default_factory=list)
    synthesis: str = ""
    sources_used: int = 0
    degraded: bool = False
    error: str = ""


async def run_deep_research(
    question: str,
    *,
    depth: int = 3,
    max_sources: int = 3,
    model: str = "",
    mlx_client: Any = None,
) -> ResearchReport:
    """Run deep research, returns a cited report."""
    if not question:
        # i18n: user-facing
        return ResearchReport(question="", error="研究问题不能为空")

    report = ResearchReport(question=question)
    client = mlx_client
    if client is None:
        from ..ai.mlx_client import FusionMLXClient

        client = FusionMLXClient()

    resolved_model = await _resolve_model(client, model)
    logger.info(f"deep research start: question={question!r} model={resolved_model} depth={depth}")

    # 1. plan: decompose sub-questions
    sub_qs = await _plan(client, resolved_model, question, depth)
    if not sub_qs:
        report.degraded = True
        logger.warning("planning failed / no LLM, degrading to single-question search")
        # i18n: user-facing
        sub_qs = [ResearchSubQuestion(question=question, rationale="降级: 原问题直接搜索")]
    report.sub_questions = sub_qs

    # 2. search: search every sub-question in parallel
    findings = await _search_all(sub_qs, max_sources)
    report.findings = findings
    report.sources_used = len(findings)
    logger.info(f"search done: {len(findings)} findings")

    if not findings:
        # i18n: user-facing
        report.error = "未找到任何搜索结果"
        return report

    # 3. synthesize: LLM aggregates the report
    synthesis = await _synthesize(client, resolved_model, question, findings)
    if not synthesis:
        report.degraded = True
        report.synthesis = _fallback_synthesis(question, findings)
        logger.warning("synthesis failed / no LLM, using degraded summary")
    else:
        report.synthesis = synthesis

    return report


async def _resolve_model(client: Any, model: str) -> str:
    if model:
        return model
    try:
        models = await client.list_models()
        if models:
            mid = models[0].get("id", models[0].get("model", ""))
            if mid:
                return mid
    except Exception as e:
        logger.debug(f"list_models failed: {e}")
    return "qwen3.5-9b"


async def _plan(client: Any, model: str, question: str, depth: int) -> List[ResearchSubQuestion]:
    """LLM decomposes the question into a sub-question list."""
    # i18n: user-facing
    prompt = (
        f"将以下研究问题分解为 {depth} 个可独立搜索的子问题, 输出 JSON 数组, "  # i18n: user-facing
        f'每个元素为 {{"question": "...", "rationale": "..."}}。只输出 JSON。\n\n问题: {question}'  # i18n: user-facing
    )
    try:
        resp = await client.chat(
            model,
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        raw = resp.content.strip()
        subs = _parse_subquestions(raw)
        if subs:
            logger.info(f"planned {len(subs)} sub-questions")
        return subs
    except Exception as e:
        logger.warning(f"planning LLM call failed: {e}")
        return []


def _parse_subquestions(raw: str) -> List[ResearchSubQuestion]:
    """Parse sub-question JSON from LLM output, tolerantly stripping markdown wrapping."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return []
    out = []
    if not isinstance(data, list):
        return []
    for item in data[:10]:
        if isinstance(item, dict) and item.get("question"):
            out.append(ResearchSubQuestion(question=str(item["question"]), rationale=str(item.get("rationale", ""))))
        elif isinstance(item, str):
            out.append(ResearchSubQuestion(question=item))
    return out


async def _search_all(sub_qs: List[ResearchSubQuestion], max_sources: int) -> List[ResearchFinding]:
    """Search all sub-questions in parallel."""
    tasks = [_search_one(sq, max_sources) for sq in sub_qs]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    findings: List[ResearchFinding] = []
    for r in results:
        if isinstance(r, list):
            findings.extend(r)
    return findings


async def _search_one(sq: ResearchSubQuestion, max_sources: int) -> List[ResearchFinding]:
    """Single sub-question: search + fetch top result summaries."""
    results = await _ddg_search(sq.question, max_sources)
    findings = []
    for r in results:
        snippet = r.get("snippet", "")
        if r.get("url"):
            content = await _fetch_text(r["url"])
            if content:
                snippet = content[:_MAX_FETCH_CHARS]
        findings.append(
            ResearchFinding(sub_question=sq.question, title=r.get("title", ""), url=r.get("url", ""), snippet=snippet)
        )
    return findings


async def _ddg_search(query: str, max_results: int) -> List[Dict[str, str]]:
    """DuckDuckGo Lite search, returns [{title,url,snippet}]."""
    try:
        async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/", params={"q": query}, headers={"User-Agent": _UA}
            )
            resp.raise_for_status()
            html = resp.text
            links = _LINK_RE.findall(html)
            snippets = _SNIPPET_RE.findall(html)
            import html as html_mod

            out = []
            for i, (url, title) in enumerate(links[:max_results]):
                snippet = ""
                if i < len(snippets):
                    snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippets[i]).strip())
                out.append(
                    {
                        "title": html_mod.unescape(re.sub(r"<[^>]+>", "", title).strip()),
                        "url": url,
                        "snippet": snippet,
                    }
                )
            return out
    except Exception as e:
        logger.warning(f"search failed query={query!r}: {e}")
        return []


async def _fetch_text(url: str) -> str:
    """Fetch a URL and extract plain text."""
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _UA})
            resp.raise_for_status()
            if "text/html" not in resp.headers.get("content-type", ""):
                return ""
            text = re.sub(r"<script[^>]*>.*?</script>", "", resp.text, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text
    except Exception as e:
        logger.debug(f"fetch failed url={url}: {e}")
        return ""


async def _synthesize(client: Any, model: str, question: str, findings: List[ResearchFinding]) -> str:
    """LLM synthesizes the cited research report."""
    context_parts = []
    for i, f in enumerate(findings, 1):
        context_parts.append(f"[{i}] ({f.sub_question}) {f.title}\nURL: {f.url}\n{f.snippet[:800]}")
    context = "\n\n".join(context_parts)
    # i18n: user-facing
    prompt = (
        f"基于以下搜索发现, 撰写关于「{question}」的研究报告。\n"  # i18n: user-facing
        f"要求: 结构化 (要点 + 结论), 关键论断用 [n] 标注引用编号。\n\n{context}"  # i18n: user-facing
    )
    try:
        resp = await client.chat(
            model,
            [{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=2048,
        )
        return resp.content.strip()
    except Exception as e:
        logger.warning(f"synthesis LLM call failed: {e}")
        return ""


def _fallback_synthesis(question: str, findings: List[ResearchFinding]) -> str:
    """Degraded summary without an LLM: list findings by sub-question."""
    # i18n: user-facing
    lines = [f"# 研究报告: {question}", "", ">(降级模式: LLM 不可用, 仅汇总原始搜索发现)", ""]
    by_sq: Dict[str, List[ResearchFinding]] = {}
    for f in findings:
        by_sq.setdefault(f.sub_question, []).append(f)
    for sq, items in by_sq.items():
        lines.append(f"## {sq}")
        for i, f in enumerate(items, 1):
            lines.append(f"{i}. [{f.title}]({f.url})")
            if f.snippet:
                lines.append(f"   > {f.snippet[:200]}")
        lines.append("")
    return "\n".join(lines)
